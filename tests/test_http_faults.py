"""Real loopback wire faults and synthetic-only fixture publication checks."""
from contextlib import redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

from contractdock import core
from contractdock.__main__ import main
from contractdock.scenario import scenario_server


def packet():
    value = {"version": 1, "request": core.request_key("GET", "/service"),
             "response": {"status": 200, "body": {"ready": True},
                          "schema": core.schema({"ready": True})}}
    value["sha256"] = hashlib.sha256(core.canonical(value)).hexdigest()
    return value


class HttpFaultTests(unittest.TestCase):
    def upstream(self, headers, body):
        response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" + headers + b"Connection: close\r\n\r\n" + body

        class Wire(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2)
                request = b""
                while b"\r\n\r\n" not in request:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    request += chunk
                self.request.sendall(response)

        server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Wire)
        self.running(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def running(self, server):
        worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        worker.start()

        def stop():
            server.shutdown()
            server.server_close()
            worker.join(2)
            self.assertFalse(worker.is_alive())

        self.addCleanup(stop)

    def test_short_content_length_valid_json_is_not_complete_recording(self):
        body = b'{"ready":true}'
        origin = self.upstream(b"Content-Length: 100\r\n", body)
        with self.assertRaises(core.ContractError):
            core.record(origin + "/service", allowed_origins=[origin])

    def test_invalid_response_framing_is_rejected(self):
        cases = [b"Content-Length: -1\r\n", b"Content-Length: invalid\r\n",
                 b"Content-Length: 14\r\nContent-Length: 14\r\n",
                 b"Content-Length: 999999999999999999999\r\n",
                 b"Transfer-Encoding: gzip\r\n", b"Transfer-Encoding: \r\n",
                 b"Transfer-Encoding: chunked\r\nContent-Length: 14\r\n"]
        for headers in cases:
            with self.subTest(headers=headers):
                origin = self.upstream(headers, b'{"ready":true}')
                with self.assertRaises(core.ContractError):
                    core.record(origin + "/service", allowed_origins=[origin])

    def test_chunked_truncation_is_sanitized_cli_failure_without_fixture(self):
        origin = self.upstream(b"Transfer-Encoding: chunked\r\n", b"20\r\nsynthetic-private-partial")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "output.json"
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["record", origin + "/service?token=synthetic-query-private",
                             str(target), "--allow-origin", origin])
            self.assertEqual(code, 2)
            result = json.loads(output.getvalue())
            self.assertFalse(result["complete"])
            self.assertNotIn("recorded", result)
            for private in (origin, "synthetic-query-private", "synthetic-private-partial"):
                self.assertNotIn(private, output.getvalue())
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(".fixture-*")), [])

    def test_complete_chunked_response_still_records(self):
        body = b'{"ready":true}'
        wire = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
        origin = self.upstream(b"Transfer-Encoding: chunked\r\n", wire)
        value = core.record(origin + "/service", allowed_origins=[origin])
        self.assertEqual(value["response"]["body"], {"ready": True})
        self.assertEqual(core.validate(value), value)

    def test_eof_delimited_and_exact_length_json_still_record(self):
        body = b'{"ready":true}'
        for headers in (b"", f"Content-Length: {len(body)}\r\n".encode()):
            with self.subTest(headers=headers):
                origin = self.upstream(headers, body)
                self.assertEqual(core.record(origin + "/service", allowed_origins=[origin])["response"]["body"],
                                 {"ready": True})

    def raw(self, port, request):
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            content = b""
            while chunk := client.recv(4096):
                content += chunk
        return int(content.split(b"\r\n", 1)[0].split()[1])

    def test_malformed_request_framing_never_consumes_scenario(self):
        server = scenario_server({"version": 1, "steps": [{"fixture": packet()}]})
        self.running(server)
        cases = [b"Transfer-Encoding: \r\n", b"Transfer-Encoding: chunked\r\n",
                 b"Content-Length: 0\r\nContent-Length: 0\r\n",
                 b"Content-Length: 4\r\n\r\nnull"]
        for headers in cases:
            with self.subTest(headers=headers):
                request = b"GET /service HTTP/1.1\r\nHost: localhost\r\n" + headers
                request += b"\r\n"
                self.assertEqual(self.raw(server.server_port, request), 400)
        valid = b"GET /service HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assertEqual(self.raw(server.server_port, valid), 200)
        self.assertEqual(self.raw(server.server_port, valid), 410)

    def test_absolute_target_is_not_an_offline_relative_request(self):
        server = scenario_server({"version": 1, "steps": [{"fixture": packet()}]})
        self.running(server)
        request = b"GET http://synthetic.invalid/service HTTP/1.1\r\nHost: localhost\r\n\r\n"
        self.assertEqual(self.raw(server.server_port, request), 400)
        self.assertEqual(self.raw(server.server_port, b"GET /service HTTP/1.1\r\nHost: localhost\r\n\r\n"), 200)

    def test_fixture_fsync_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "output.json"
            with patch.object(core.os, "fsync", side_effect=OSError(errno.EIO, "injected sync failure")):
                with self.assertRaises(core.ContractError):
                    core.write_fixture(target, packet())
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.glob(".fixture-*")), [])

    def test_publication_race_preserves_competing_complete_fixture(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "output.json"
            competitor = core.canonical(packet()) + b"\n"
            original = os.link
            hits = []

            def race(source, destination, *args, **kwargs):
                hits.append(Path(destination))
                Path(destination).write_bytes(competitor)
                return original(source, destination, *args, **kwargs)

            with patch.object(core.os, "link", side_effect=race):
                with self.assertRaises(core.ContractError):
                    core.write_fixture(target, packet())
            self.assertEqual(hits, [target])
            self.assertEqual(target.read_bytes(), competitor)
            self.assertEqual(core.load_fixture(target), packet())
            self.assertEqual(list(target.parent.glob(".fixture-*")), [])


if __name__ == "__main__":
    unittest.main()

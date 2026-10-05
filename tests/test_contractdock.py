import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from contractdock.core import (ContractError, canonical, compare, load_fixture, parse_json,
                               record, replay_server, request_key, schema, validate, write_fixture)


def fixture(body, status=200, target="/players"):
    packet = {"version": 1, "request": request_key("GET", target),
              "response": {"status": status, "body": body, "schema": schema(body)}}
    packet["sha256"] = hashlib.sha256(canonical(packet)).hexdigest()
    return packet


class ContractTests(unittest.TestCase):
    def test_sensitive_query_and_body_redaction(self):
        private_value = "secret-plaintext"
        request = request_key("POST", "/players?token=super-sensitive-value&name=alice",
                              {"password": private_value, "nested": [{"api_key": "testing-secret", "level": 5}]})
        content = canonical(request).decode()
        for value in ("super-sensitive-value", "secret-plaintext", "testing-secret"):
            self.assertNotIn(value, content)
        self.assertIn("alice", content)
        self.assertEqual(request["body"]["nested"][0]["level"], 5)

    def test_request_normalization_ignores_query_order(self):
        self.assertEqual(request_key("GET", "/x?b=2&a=1"), request_key("GET", "/x?a=1&b=2"))
        self.assertNotEqual(request_key("GET", "/x?a=1"), request_key("GET", "/x?a=2"))

    def test_invalid_request_targets_and_methods(self):
        for target in ("//evil", "/x#fragment", "/x\nheader", "relative", "/x?" + "&".join("a=1" for _ in range(101))):
            with self.subTest(target=target[:30]), self.assertRaises(ContractError):
                request_key("GET", target)
        for method in (None, "PUT", "DELETE"):
            with self.assertRaises(ContractError):
                request_key(method, "/x")

    def test_duplicate_json_and_nonfinite_numbers_rejected(self):
        for content in (b'{"id":1,"id":2}', b'{"x":NaN}', b'{"x":1e999}', b'not json', b'x' * (1024 * 1024 + 1)):
            with self.assertRaises(ContractError):
                parse_json(content)

    def test_checksum_and_schema_tampering(self):
        original = fixture({"level": 1})
        for mutate in (lambda p: p.update(version=True),
                       lambda p: p["response"].update(body={"level": "changed"}),
                       lambda p: p["response"].update(status=True),
                       lambda p: p["request"].update(key="fake"),
                       lambda p: p["response"]["schema"].update(type="string")):
            packet = copy.deepcopy(original)
            mutate(packet)
            with self.assertRaises(ContractError):
                validate(packet)

    def test_missing_fields_types_status_and_request_drift(self):
        before = fixture({"level": 1, "hero": {"name": "mage"}})
        cases = [fixture({"level": 1}), fixture({"level": "one", "hero": {"name": "mage"}}),
                 fixture({"level": 1, "hero": {"name": "mage"}}, 500),
                 fixture({"level": 1, "hero": {"name": "mage"}}, target="/other")]
        for after in cases:
            with self.subTest(after=after):
                self.assertFalse(compare(before, after)["passed"])

    def test_additive_fields_and_value_changes_pass_observed_schema(self):
        self.assertTrue(compare(fixture({"id": 1}), fixture({"id": 2, "name": "mage"}))["passed"])

    def test_arrays_nested_types_and_empty_array_uncertainty(self):
        self.assertFalse(compare(fixture({"items": [{"id": 1}]}), fixture({"items": [{"name": "mage"}]}))["passed"])
        self.assertFalse(compare(fixture([1, 2]), fixture(["name"]))["passed"])
        self.assertTrue(compare(fixture([]), fixture(["name"]))["passed"])

    def test_multiple_observed_object_shapes_cannot_hide_breaking_drift(self):
        before = fixture([{ "id": 1 }, { "name": "mage" }])
        after = fixture([{ "removed_every_old_field": True }])
        self.assertFalse(compare(before, after)["passed"])
        self.assertTrue(compare(before, fixture([{ "id": 2, "extra": "allowed" }]))["passed"])

    def test_empty_array_reports_uncertainty_instead_of_proof(self):
        report = compare(fixture([]), fixture([{"id": 1}]))
        self.assertTrue(report["passed"])
        self.assertIsNone(report["compatible"])
        self.assertTrue(report["uncertainties"])

    def test_fixture_roundtrip_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.json"
            packet = fixture({"id": 1})
            write_fixture(path, packet)
            self.assertEqual(load_fixture(path), packet)
            with self.assertRaises(ContractError):
                write_fixture(path, fixture({"id": 2}))
            self.assertEqual(load_fixture(path), packet)
            self.assertEqual(list(path.parent.glob(".fixture-*")), [])

    def test_request_and_body_limits_fail_closed(self):
        with self.assertRaises(ContractError):
            request_key("POST", "/x", {"x": float("inf")})
        with self.assertRaises(ContractError):
            record("http://127.0.0.1:1/x", allowed_origins=["http://127.0.0.1:1"], timeout=0)
        packet = fixture({"x": "s" * (1024 * 1024)})
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "large.json"
            with self.assertRaises(ContractError):
                write_fixture(path, packet)
            self.assertFalse(path.exists())

    def test_origin_allowlist_before_any_network(self):
        for url in ("http://example.invalid/", "http://user:password@example.invalid/", "file:///etc/passwd", "http://example.invalid/#fragment"):
            with self.subTest(url=url), self.assertRaises(ContractError):
                record(url, allowed_origins=["http://127.0.0.1:8099"])

    def test_duplicate_replay_requests_rejected(self):
        packet = fixture({"id": 1})
        with self.assertRaises(ContractError):
            replay_server([packet, packet])

    def test_cli_compare_pass_fail_and_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old, new = root / "old.json", root / "new.json"
            write_fixture(old, fixture({"id": 1}))
            write_fixture(new, fixture({"name": "mage"}))
            for first, second, expected in ((old, old, 0), (old, new, 1), (old, root / "missing", 2)):
                result = subprocess.run([sys.executable, "-m", "contractdock", "compare", str(first), str(second)], capture_output=True, text=True)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                json.loads(result.stdout)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", "/players")
                    self.end_headers()
                    return
                if self.path.startswith("/html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"not json")
                    return
                payload = json.dumps({"id": 1, "token": "server-private-value", "nested": {"password": "not-for-fixture"}}).encode()
                self.send_response(404 if self.path.startswith("/missing") else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "private-cookie")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                value = json.loads(body)
                payload = json.dumps({"name": value["name"], "password": value["password"]}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.thread = threading.Thread(target=self.upstream.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.upstream.server_port}"
        self.addCleanup(self.stop_upstream)

    def stop_upstream(self):
        if self.upstream is not None:
            self.upstream.shutdown()
            self.upstream.server_close()
            self.thread.join(timeout=2)
            self.upstream = None

    def start_replay(self, packets):
        server = replay_server(packets)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.addCleanup(stop)
        return f"http://127.0.0.1:{server.server_port}"

    def test_record_redacts_body_query_and_all_headers(self):
        packet = record(self.origin + "/players?token=query-private-value", allowed_origins=[self.origin])
        content = canonical(packet).decode()
        for secret in ("query-private-value", "server-private-value", "not-for-fixture", "private-cookie"):
            self.assertNotIn(secret, content)
        self.assertEqual(validate(packet), packet)

    def test_original_server_stopped_offline_replay_still_works(self):
        packet = record(self.origin + "/players", allowed_origins=[self.origin])
        self.stop_upstream()
        replay = self.start_replay([packet])
        with urlopen(replay + "/players", timeout=2) as response:
            value = json.load(response)
        self.assertEqual(value["id"], 1)
        self.assertEqual(value["token"], "[REDACTED]")
        with self.assertRaises(HTTPError) as caught:
            urlopen(replay + "/unrecorded", timeout=2)
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_post_record_replay_matches_redacted_credentials(self):
        private_value = "synthetic-private"
        other_value = "different-private"
        body = {"name": "mage", "password": private_value}
        packet = record(self.origin + "/players", allowed_origins=[self.origin], method="POST", body=body)
        replay = self.start_replay([packet])
        outgoing = Request(replay + "/players", data=canonical({"name": "mage", "password": other_value}),
                           headers={"Content-Type": "application/json"})
        with urlopen(outgoing, timeout=2) as response:
            self.assertEqual(response.status, 201)
            self.assertEqual(json.load(response)["password"], "[REDACTED]")

    def test_redirect_and_html_refused(self):
        for path in ("/redirect", "/html"):
            with self.subTest(path=path), self.assertRaises(ContractError):
                record(self.origin + path, allowed_origins=[self.origin])

    def test_error_json_status_is_recorded(self):
        packet = record(self.origin + "/missing", allowed_origins=[self.origin])
        self.assertEqual(packet["response"]["status"], 404)

    def test_invalid_replay_body_rejected(self):
        replay = self.start_replay([fixture({"id": 1})])
        with self.assertRaises(HTTPError) as caught:
            urlopen(Request(replay + "/players", data=b"invalid-json"), timeout=2)
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()

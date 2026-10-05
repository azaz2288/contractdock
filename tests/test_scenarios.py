from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from contractdock.core import ContractError, canonical, replay_server, request_key, schema
from contractdock.scenario import load_scenario, scenario_server


def fixture(value, status=200, target='/service', method='GET', body=None):
    result = {'version': 1, 'request': request_key(method, target, body),
              'response': {'status': status, 'body': value, 'schema': schema(value)}}
    result['sha256'] = hashlib.sha256(canonical(result)).hexdigest()
    return result


def scene(*packets):
    return {'version': 1, 'steps': [{'fixture': packet} for packet in packets]}


class ScenarioTests(unittest.TestCase):
    def start(self, description):
        server = scenario_server(description)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def stop():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(stop)
        return f'http://127.0.0.1:{server.server_port}'

    def request(self, origin, path='/service', data=None):
        try:
            response = urlopen(Request(origin + path, data=data), timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_outage_then_recovery_then_explicit_exhaustion(self):
        origin = self.start(scene(fixture({'error': 'offline'}, 503), fixture({'ready': True})))
        with patch('contractdock.core.build_opener', side_effect=AssertionError('no upstream')):
            self.assertEqual(self.request(origin), (503, {'error': 'offline'}))
            self.assertEqual(self.request(origin), (200, {'ready': True}))
            self.assertEqual(self.request(origin)[0], 410)
            self.assertEqual(self.request(origin)[0], 410)

    def test_wrong_order_and_malformed_request_do_not_advance(self):
        origin = self.start(scene(fixture({'first': 1}, target='/first'), fixture({'second': 2}, target='/second')))
        self.assertEqual(self.request(origin, '/second')[0], 409)
        self.assertEqual(self.request(origin, '/unknown')[0], 409)
        self.assertEqual(self.request(origin, '/first', b'not-json')[0], 400)
        self.assertEqual(self.request(origin, '/first'), (200, {'first': 1}))
        self.assertEqual(self.request(origin, '/second'), (200, {'second': 2}))

    def test_repeat_count_and_delay(self):
        description = scene(fixture({'retry': True}, 503), fixture({'ready': True}))
        description['steps'][0].update(repeat=2, delay_ms=40)
        origin = self.start(description)
        started = time.perf_counter()
        self.assertEqual(self.request(origin)[0], 503)
        self.assertGreaterEqual(time.perf_counter() - started, 0.035)
        self.assertEqual(self.request(origin)[0], 503)
        self.assertEqual(self.request(origin)[0], 200)
        self.assertEqual(self.request(origin)[0], 410)

    def test_concurrent_requests_consume_each_response_exactly_once(self):
        origin = self.start(scene(*(fixture({'sequence': index}) for index in range(16))))
        with ThreadPoolExecutor(max_workers=4) as workers:
            responses = list(workers.map(lambda _: self.request(origin), range(16)))
        self.assertTrue(all(status == 200 for status, _ in responses))
        self.assertEqual(sorted(body['sequence'] for _, body in responses), list(range(16)))
        self.assertEqual(self.request(origin)[0], 410)

    def test_caller_mutation_cannot_change_running_scenario(self):
        description = scene(fixture({'id': 1}))
        origin = self.start(description)
        description['steps'][0]['fixture']['response']['body']['id'] = 999
        description['steps'].clear()
        self.assertEqual(self.request(origin), (200, {'id': 1}))

    def test_post_matches_normalized_query_and_redacted_body(self):
        packet = fixture({'created': True}, 201, '/create?a=1&b=2', 'POST', {'token': '[REDACTED]', 'name': 'mage'})
        origin = self.start(scene(packet))
        self.assertEqual(self.request(origin, '/create?b=2&a=1', canonical({'token': 'different', 'name': 'mage'})),
                         (201, {'created': True}))

    def test_invalid_description_rejected_before_binding(self):
        valid = scene(fixture({'id': 1}))
        bad = [None, [], {'version': True, 'steps': valid['steps']}, {'version': 1, 'steps': []},
               dict(valid, extra=True), {'version': 1, 'steps': [None]},
               {'version': 1, 'steps': valid['steps'] * 101}]
        for key, values in (('repeat', [0, -1, True, 1.5, 10001]), ('delay_ms', [-1, True, 1.5, 5001]),
                            ('unexpected', [1])):
            for value in values:
                candidate = copy.deepcopy(valid)
                candidate['steps'][0][key] = value
                bad.append(candidate)
        for description in bad:
            with self.subTest(description=str(description)[:100]), patch('contractdock.core.ThreadingHTTPServer') as bind:
                with self.assertRaises(ContractError):
                    scenario_server(description)
                bind.assert_not_called()

    def test_all_fixtures_verified_before_binding_and_total_limits(self):
        bad = fixture({'id': 1})
        bad['response']['body']['id'] = 2
        candidates = [scene(fixture({'ok': True}), bad),
                      scene(fixture({'large': 'a' * 600000}), fixture({'large': 'b' * 600000}))]
        too_many = scene(fixture({'id': 1}), fixture({'id': 2}))
        for step in too_many['steps']:
            step['repeat'] = 6000
        candidates.append(too_many)
        for description in candidates:
            with self.subTest(description=str(description)[:80]), self.assertRaises(ContractError):
                scenario_server(description)

    def test_loader_duplicate_json_oversize_and_cli_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'scenario.json'
            for value in (b'{"version":1,"version":2,"steps":[]}', b'x' * (1024 * 1024 + 1), b'{"version":1,"steps":[]}'):
                path.write_bytes(value)
                with self.assertRaises(ContractError):
                    load_scenario(path)
            result = subprocess.run([sys.executable, '-m', 'contractdock', 'scenario', str(path), '--port', '0'],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertFalse(json.loads(result.stdout)['complete'])

    def test_cli_serves_valid_scenario_over_real_http(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'scenario.json'
            path.write_bytes(canonical(scene(fixture({'ready': True}))))
            process = subprocess.Popen([sys.executable, '-m', 'contractdock', 'scenario', str(path), '--port', '0'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            lines = []
            reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
            reader.start()
            try:
                reader.join(5)
                self.assertFalse(reader.is_alive(), 'CLI did not announce ready server')
                announcement = json.loads(lines[0])
                self.assertTrue(announcement['offline'])
                self.assertEqual(announcement['mode'], 'scenario')
                self.assertEqual(self.request(announcement['replay_url']), (200, {'ready': True}))
                self.assertEqual(self.request(announcement['replay_url'])[0], 410)
            finally:
                process.terminate()
                process.wait(5)
                reader.join(2)
                process.stdout.close()
                process.stderr.close()

    def test_fixed_replay_also_detaches_from_caller_mutation(self):
        packet = fixture({'id': 1})
        with replay_server([packet]) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                packet['response']['body']['id'] = 999
                self.assertEqual(self.request(f'http://127.0.0.1:{server.server_port}'), (200, {'id': 1}))
            finally:
                server.shutdown()
                worker.join(2)

    def test_invalid_length_does_not_crash_handler_or_consume_step(self):
        origin = self.start(scene(fixture({'id': 1})))
        port = int(origin.rsplit(':', 1)[1])
        for header in (b'9' * 5000, b'-1', b'1\r\nContent-Length: 2', b'10000000000'):
            with socket.create_connection(('127.0.0.1', port), timeout=2) as client:
                client.sendall(b'GET /service HTTP/1.1\r\nHost: localhost\r\nContent-Length: ' + header + b'\r\n\r\n')
                self.assertIn(b'400', client.recv(1024).split(b'\r\n')[0])
        self.assertEqual(self.request(origin), (200, {'id': 1}))

    def test_delay_is_applied_after_atomic_reservation(self):
        description = scene(fixture({'sequence': 1}), fixture({'sequence': 2}))
        description['steps'][0]['delay_ms'] = 100
        origin = self.start(description)
        sleeping = threading.Event()
        release = threading.Event()
        def delay(seconds):
            self.assertEqual(seconds, 0.1)
            sleeping.set()
            self.assertTrue(release.wait(2))
        with patch('contractdock.core.time.sleep', side_effect=delay), ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(self.request, origin)
            try:
                self.assertTrue(sleeping.wait(2))
                self.assertEqual(self.request(origin), (200, {'sequence': 2}))
            finally:
                release.set()
            self.assertEqual(first.result(2), (200, {'sequence': 1}))


if __name__ == '__main__':
    unittest.main()

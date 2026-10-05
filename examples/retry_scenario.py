"""Actual local HTTP retry/recovery with synthetic, checksum-verified fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contractdock.core import canonical, request_key, schema
from contractdock.scenario import scenario_server


def packet(status, body):
    value = {'version': 1, 'request': request_key('GET', '/service'),
             'response': {'status': status, 'body': body, 'schema': schema(body)}}
    value['sha256'] = hashlib.sha256(canonical(value)).hexdigest()
    return value


description = {'version': 1, 'steps': [
    {'fixture': packet(503, {'error': 'temporarily unavailable'}), 'repeat': 2, 'delay_ms': 10},
    {'fixture': packet(200, {'ready': True})}]}
with scenario_server(description) as server:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    def request(path):
        try:
            response = urlopen(origin + path, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)
    try:
        wrong = request('/unrecorded')[0]
        statuses = []
        for _ in range(3):
            status, body = request('/service')
            statuses.append(status)
            if status == 200:
                assert body == {'ready': True}
                break
        exhausted = request('/service')[0]
        assert wrong == 409 and statuses == [503, 503, 200] and exhausted == 410
        print(json.dumps({'offline_retry_statuses': statuses, 'wrong_order': wrong, 'exhausted': exhausted,
                          'synthetic_fixture_demo': True}))
    finally:
        server.shutdown()
        thread.join(2)

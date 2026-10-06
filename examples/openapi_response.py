"""Synthetic offline response acceptance; exits nonzero if expectations fail."""
import hashlib
import json
from pathlib import Path
import sys

if not sys.flags.isolated:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contractdock.core import canonical, check_openapi_response, redact, request_key, schema


def packet(body):
    body = redact(body)
    value = {'version': 1, 'request': request_key('GET', '/players'),
             'response': {'status': 200, 'body': body, 'schema': schema(body)}}
    value['sha256'] = hashlib.sha256(canonical(value)).hexdigest()
    return value


document = {'openapi': '3.0.3', 'paths': {'/players': {'get': {'responses': {
    '200': {'description': 'synthetic response', 'content': {'application/json': {'schema': {
        'type': 'object', 'required': ['id'], 'additionalProperties': False,
        'properties': {'id': {'type': 'integer'}, 'nickname': {'type': 'string', 'nullable': True}}}}}}
}}}}}
reports = [check_openapi_response(document, packet(body), method='GET', path='/players')
           for body in ({'id': 1}, {'id': 1, 'nickname': None}, {'id': 'invalid'})]
assert [report['passed'] for report in reports] == [True, True, False]
assert all(report['complete'] and not report['request_validated'] for report in reports)
print(json.dumps({'synthetic': True, 'offline': True, 'expected_results': [True, True, False],
                  'reports': reports}))

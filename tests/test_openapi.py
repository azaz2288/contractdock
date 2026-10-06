"""Synthetic local OpenAPI response contracts, never live APIs or real fixtures."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from contractdock import core


def fixture(body, status=200, target='/players', method='GET'):
    body = core.redact(body)
    packet = {'version': 1, 'request': core.request_key(method, target),
              'response': {'status': status, 'body': body, 'schema': core.schema(body)}}
    packet['sha256'] = hashlib.sha256(core.canonical(packet)).hexdigest()
    return packet


def document(schema):
    return {'openapi': '3.0.3', 'info': {'title': 'synthetic', 'version': '1'},
            'paths': {'/players': {'get': {'responses': {
                '200': {'description': 'synthetic', 'content': {'application/json': {'schema': schema}}}}}}}}


class OpenAPITests(unittest.TestCase):
    def check(self, spec, packet, **options):
        return core.check_openapi_response(spec, packet, method='GET', path='/players', **options)

    def test_required_optional_and_additional_fields(self):
        spec = document({'type': 'object', 'required': ['id'],
                         'properties': {'id': {'type': 'integer'}, 'name': {'type': 'string'}}, 'additionalProperties': False})
        self.assertTrue(self.check(spec, fixture({'id': 1}))['passed'])
        self.assertTrue(self.check(spec, fixture({'id': 1, 'name': 'mage'}))['passed'])
        for body in ({}, {'id': True}, {'id': 1, 'name': None}, {'id': 1, 'extra': 2}):
            with self.subTest(body=body):
                self.assertFalse(self.check(spec, fixture(body))['passed'])

    def test_nullable_and_type_aware_enum(self):
        spec = document({'type': 'integer', 'nullable': True, 'enum': [None, 1]})
        for body, passed in ((None, True), (1, True), (True, False), (2, False), ('1', False), (1.0, True)):
            self.assertEqual(self.check(spec, fixture(body))['passed'], passed)
        self.assertFalse(self.check(document({'type': 'integer'}), fixture(1.5))['passed'])

    def test_anyof_and_exactly_one_oneof(self):
        integer, number, string = {'type': 'integer'}, {'type': 'number'}, {'type': 'string'}
        self.assertTrue(self.check(document({'anyOf': [integer, string]}), fixture('mage'))['passed'])
        self.assertFalse(self.check(document({'anyOf': [integer, string]}), fixture(False))['passed'])
        self.assertFalse(self.check(document({'oneOf': [integer, number]}), fixture(1))['passed'])
        self.assertTrue(self.check(document({'oneOf': [integer, number]}), fixture(1.5))['passed'])

    def test_local_refs_nested_arrays_and_unselected_operation_not_imported(self):
        spec = document({'type': 'array', 'items': {'$ref': '#/components/schemas/Player'}})
        spec['components'] = {'schemas': {'Player': {'type': 'object', 'required': ['id'], 'properties': {'id': {'type': 'integer'}}}}}
        spec['paths']['/ignored'] = {'get': {'responses': {'200': {'content': {'application/json': {'schema': {'pattern': 'ignored'}}}}}}}
        self.assertTrue(self.check(spec, fixture([{'id': 1}, {'id': 2}]))['passed'])
        self.assertTrue(self.check(spec, fixture([]))['passed'])
        self.assertFalse(self.check(spec, fixture([{'id': 1}, {'id': 'bad'}]))['passed'])

    def test_explicit_operation_method_path_and_status_default(self):
        spec = document({'type': 'object'})
        spec['paths']['/players']['get']['responses']['default'] = {
            'description': 'error', 'content': {'application/json': {'schema': {'type': 'string'}}}}
        self.assertTrue(self.check(spec, fixture('error', 503))['passed'])
        self.assertFalse(self.check(spec, fixture({}, 503))['passed'])
        self.assertFalse(self.check(spec, fixture({}, target='/other'))['passed'])
        self.assertFalse(self.check(spec, fixture({}, method='POST'))['passed'])
        self.assertTrue(self.check(spec, fixture({}, target='/players?offset=2'))['passed'])
        report = self.check(document({'type': 'object'}), fixture({}, 404))
        self.assertFalse(report['passed'])
        self.assertEqual(report['violations'], ['undocumented_response_status'])

    def test_additional_properties_schema_and_unconstrained_schema(self):
        spec = document({'type': 'object', 'additionalProperties': {'type': 'string'}})
        self.assertTrue(self.check(spec, fixture({'key': 'value'}))['passed'])
        self.assertFalse(self.check(spec, fixture({'key': 1}))['passed'])
        self.assertTrue(self.check(document({}), fixture([None, {'id': 1}]))['passed'])

    def test_unsupported_constraints_fail_closed_even_unselected_response(self):
        unsupported = ({'type': 'string', 'pattern': 'x'}, {'type': 'integer', 'minimum': 0},
                       {'type': 'string', 'format': 'date-time'}, {'allOf': [{'type': 'string'}]},
                       {'type': 'object', 'discriminator': {'propertyName': 'kind'}},
                       {'type': 'string', 'readOnly': True}, {'type': ['string', 'null']})
        for schema in unsupported:
            with self.subTest(schema=schema), self.assertRaises(core.ContractError):
                self.check(document(schema), fixture('x'))
        spec = document({'type': 'object'})
        spec['paths']['/players']['get']['responses']['500'] = {'content': {'application/json': {'schema': {'pattern': 'x'}}}}
        with self.assertRaises(core.ContractError):
            self.check(spec, fixture({}))

    def test_remote_refs_cycles_and_sibling_ref_are_refused_without_network(self):
        spec = document({'$ref': '#/components/schemas/A'})
        spec['components'] = {'schemas': {'A': {'$ref': '#/components/schemas/A'}}}
        with patch('socket.socket', side_effect=AssertionError('must remain offline')):
            for schema in ({'$ref': 'https://example.invalid/schema'}, {'$ref': 'file:///private'},
                           {'$ref': '#/components/schemas/missing'}, {'$ref': '#/components/schemas/A', 'type': 'string'}):
                with self.subTest(schema=schema), self.assertRaises(core.ContractError):
                    self.check(document(schema), fixture(None))
            with self.assertRaises(core.ContractError):
                self.check(spec, fixture(None))

    def test_strict_schema_shape_and_metadata(self):
        for schema in ({'type': 'array'}, {'type': 'object', 'required': ['id', 'id']},
                       {'type': 'object', 'required': 'id'}, {'type': 'object', 'properties': []},
                       {'type': 'object', 'additionalProperties': 1}, {'type': 'string', 'nullable': 1},
                       {'oneOf': []}, {'anyOf': [{'type': 'string'}], 'type': 'string'}, {'enum': []},
                       {'enum': [True, True]}, {'nullable': True}, {'type': 'null'}):
            with self.subTest(schema=schema), self.assertRaises(core.ContractError):
                self.check(document(schema), fixture(None))
        for version in ('3.1.0', '2.0', True, None):
            spec = document({})
            spec['openapi'] = version
            with self.assertRaises(core.ContractError):
                self.check(spec, fixture(None))

    def test_fixture_integrity_and_redacted_scope_not_original_network_claim(self):
        packet = fixture({'password': 'synthetic-only'})
        report = self.check(document({'type': 'object'}), packet)
        self.assertTrue(report['complete'])
        self.assertEqual(report['scope'], 'selected-openapi-response-redacted-fixture')
        self.assertFalse(report['request_validated'])
        self.assertFalse(report['full_openapi_validated'])
        self.assertNotIn('synthetic-only', json.dumps(report))
        self.assertNotIn('password', json.dumps(report))
        packet['response']['body'] = 'tampered'
        with self.assertRaises(core.ContractError):
            self.check(document({}), packet)

    def test_input_detachment_and_deterministic_document_digest(self):
        spec, packet = document({'type': 'string'}), fixture('ok')
        before = copy.deepcopy((spec, packet))
        report = self.check(spec, packet)
        self.assertEqual((spec, packet), before)
        self.assertEqual(report['document_sha256'], hashlib.sha256(core.canonical(spec)).hexdigest())
        self.assertEqual(report['fixture_sha256'], packet['sha256'])
        self.assertEqual(report, self.check(spec, packet))

    def test_json_cli_exit_codes_and_no_network_or_output_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, packet = root / 'openapi.json', root / 'fixture.json'
            path.write_text(json.dumps(document({'type': 'integer'})))
            core.write_fixture(packet, fixture(1))
            command = [sys.executable, '-m', 'contractdock', 'openapi-check', str(path), str(packet), '--method', 'GET', '--path', '/players']
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)['passed'])
            path.write_text(json.dumps(document({'type': 'string'})))
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)['passed'])
            path.write_text('{"openapi":"3.0.3","openapi":"3.1.0"}')
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stdout)['complete'])
            self.assertEqual({p.name for p in root.iterdir()}, {'openapi.json', 'fixture.json'})

    def test_compilation_nodes_and_repeated_ref_expansion_budget(self):
        schema = {'type': 'object', 'properties': {str(i): {} for i in range(1023)}}
        self.assertTrue(self.check(document(schema), fixture({}))['passed'])
        schema['properties']['overflow'] = {}
        with self.assertRaises(core.ContractError):
            self.check(document(schema), fixture({}))
        spec = document({'type': 'object', 'properties': {str(i): {'$ref': '#/components/schemas/A'} for i in range(513)}})
        spec['components'] = {'schemas': {'A': {}}}
        with self.assertRaises(core.ContractError):
            self.check(spec, fixture({}))

    def test_depth_and_branch_limits(self):
        schema = {}
        for _ in range(32):
            schema = {'type': 'array', 'items': schema}
        self.assertTrue(self.check(document(schema), fixture([]))['passed'])
        with self.assertRaises(core.ContractError):
            self.check(document({'type': 'array', 'items': schema}), fixture([]))
        self.assertTrue(self.check(document({'anyOf': [{}] * 16}), fixture(None))['passed'])
        with self.assertRaises(core.ContractError):
            self.check(document({'anyOf': [{}] * 17}), fixture(None))

    def test_runtime_budget_exhaustion_never_returns_partial_pass(self):
        spec = document({'type': 'array', 'items': {'oneOf': [{'type': 'integer'}, {'type': 'string'}]}})
        self.assertTrue(self.check(spec, fixture([1] * 33_000))['passed'])
        with self.assertRaises(core.ContractError):
            self.check(spec, fixture([1] * 34_000))
        # One shared budget, not reset per union branch/array element.
        from contractdock import openapi
        node = openapi._compile({'type': 'string', 'enum': [str(i) for i in range(40)]}, {}, openapi.Budget(1024))
        with self.assertRaises(core.ContractError):
            openapi._matches(node, '39', openapi.Budget(30))

    def test_python_non_json_and_nonfinite_inputs_fail_closed(self):
        for value in ({1: 'coerced'}, {'example': (1, 2)}, {'example': float('nan')},
                      {'example': float('inf')}, {'example': object()}):
            with self.subTest(value=value), self.assertRaises(core.ContractError):
                self.check(document(value), fixture(None))
        schema = {}
        schema['example'] = schema
        with self.assertRaises(core.ContractError):
            self.check(document(schema), fixture(None))

    def test_document_size_and_scalar_enum_limits(self):
        for schema in ({'example': 'x' * (core.MAX_BYTES + 1)}, {'enum': list(range(257))},
                       {'enum': [1, 1.0]}, {'enum': [[]]}, {'enum': [{}]}):
            with self.subTest(), self.assertRaises(core.ContractError):
                self.check(document(schema), fixture(None))
        self.assertTrue(self.check(document({'enum': [True, 1, None]}), fixture(1.0))['passed'])

    def test_post_literal_selection_and_annotations(self):
        spec = document({'type': 'string', 'title': 'label', 'description': 'metadata', 'example': 'example', 'deprecated': True})
        spec['paths']['/players']['post'] = spec['paths']['/players'].pop('get')
        report = core.check_openapi_response(spec, fixture('ok', method='POST'), method='POST', path='/players')
        self.assertTrue(report['passed'])
        for method, path in (('get', '/players'), ('DELETE', '/players'), ('GET', '//players'),
                             ('GET', '/players/{id}'), ('GET', '/players?secret=x'), ('GET', '/players#x')):
            with self.subTest(method=method, path=path), self.assertRaises(core.ContractError):
                core.check_openapi_response(spec, fixture('ok'), method=method, path=path)

    def test_json_pointer_escaping_and_invalid_reference_shapes(self):
        spec = document({'$ref': '#/components/schemas/A~1B~0C'})
        spec['components'] = {'schemas': {'A/B~C': {'type': 'integer'}}}
        self.assertTrue(self.check(spec, fixture(1))['passed'])
        for ref in ('#/components/schemas/', '#/components/schemas/A~2B', '#/components/schemas/A/B', '#/paths/x'):
            with self.subTest(ref=ref), self.assertRaises(core.ContractError):
                self.check(document({'$ref': ref}), fixture(None))

    def test_response_media_and_status_shapes(self):
        for responses in ([], {}, {'2XX': {}}, {'200': {'$ref': '#/components/responses/A'}},
                          {'200': {'content': {'text/plain': {'schema': {}}}}}, {'200': {'content': []}}):
            spec = document({})
            spec['paths']['/players']['get']['responses'] = responses
            with self.subTest(), self.assertRaises(core.ContractError):
                self.check(spec, fixture(None))
        spec = document({})
        spec['paths']['/players']['get']['responses']['200']['content']['text/plain'] = {'schema': {'pattern': 'ignored alternate media'}}
        self.assertTrue(self.check(spec, fixture(None))['passed'])

    def test_safe_cli_errors_and_bounded_local_document_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packet, path = root / 'fixture.json', root / 'document.json'
            core.write_fixture(packet, fixture(None))
            for content in (b'{"private-field":', b'x' * (core.MAX_BYTES + 1)):
                path.write_bytes(content)
                result = subprocess.run([sys.executable, '-m', 'contractdock', 'openapi-check', str(path), str(packet),
                                         '--method', 'GET', '--path', '/players'], capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse(json.loads(result.stdout)['complete'])
                self.assertNotIn('private-field', result.stdout.decode())
                self.assertNotIn(directory, result.stdout.decode())

    def test_version_metadata_alignment(self):
        import contractdock
        import tomllib
        metadata = tomllib.loads((Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text())
        self.assertEqual(contractdock.__version__, '0.4.0')
        self.assertEqual(metadata['project']['version'], contractdock.__version__)


if __name__ == '__main__':
    unittest.main()

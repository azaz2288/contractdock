"""Bounded, offline acceptance of one explicitly selected OpenAPI response.

This is intentionally not a general OpenAPI validator or compatibility engine.
Unsupported schema constraints fail closed; input content never enters errors.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from .core import ContractError, MAX_BYTES, canonical, parse_json, validate

MAX_SCHEMA_NODES = 1024
MAX_SCHEMA_DEPTH = 32
MAX_BRANCHES = 16
MAX_EVALUATIONS = 100_000
MAX_ENUM = 256
ANNOTATIONS = {'title', 'description', 'example', 'deprecated'}
TYPES = {'string', 'integer', 'number', 'boolean', 'object', 'array'}


class Budget:
    def __init__(self, limit):
        self.remaining = limit

    def charge(self):
        self.remaining -= 1
        if self.remaining < 0:
            raise ContractError('OpenAPI validation budget exceeded')


def _detach(value):
    # Reject Python-only types/key coercion before canonical JSON serialization.
    budget = Budget(MAX_EVALUATIONS)

    def visit(item, depth):
        budget.charge()
        if depth > 64:
            raise ContractError('OpenAPI JSON nesting budget exceeded')
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ContractError('OpenAPI input must be finite JSON')
                visit(child, depth + 1)
        elif type(item) is list:
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ContractError('OpenAPI input must be finite JSON')
    visit(value, 0)
    return parse_json(canonical(value))


def _enum_equal(left, right):
    if type(left) in (int, float) and type(right) in (int, float):
        return left == right
    return type(left) is type(right) and left == right


def _compile(spec, document, budget, depth=0, refs=frozenset()):
    budget.charge()
    if depth > MAX_SCHEMA_DEPTH or type(spec) is not dict:
        raise ContractError('Invalid or excessively nested OpenAPI schema')
    keys = set(spec) - ANNOTATIONS
    if '$ref' in keys:
        ref = spec['$ref']
        # Even annotation siblings are refused on Reference Objects in 3.0.
        if set(spec) != {'$ref'} or type(ref) is not str or not ref.startswith('#/components/schemas/'):
            raise ContractError('Only standalone local schema references are supported')
        name = ref[len('#/components/schemas/'):]
        if not name or '/' in name or re.search(r'~(?![01])', name) or ref in refs:
            raise ContractError('Invalid or recursive local schema reference')
        name = name.replace('~1', '/').replace('~0', '~')
        components = document.get('components', {})
        schemas = components.get('schemas', {}) if type(components) is dict else {}
        if type(schemas) is not dict or name not in schemas:
            raise ContractError('Missing local schema reference')
        return _compile(schemas[name], document, budget, depth + 1, refs | {ref})
    union = keys & {'oneOf', 'anyOf'}
    if union:
        if len(union) != 1 or keys != union:
            raise ContractError('Unsupported union schema combination')
        kind = next(iter(union))
        branches = spec[kind]
        if type(branches) is not list or not 1 <= len(branches) <= MAX_BRANCHES:
            raise ContractError('Invalid or excessive union branches')
        return (kind, tuple(_compile(child, document, budget, depth + 1, refs) for child in branches))
    if keys - {'type', 'nullable', 'enum', 'properties', 'required', 'additionalProperties', 'items'}:
        raise ContractError('Unsupported OpenAPI schema constraint')
    kind = spec.get('type')
    if 'type' in spec and (type(kind) is not str or kind not in TYPES):
        raise ContractError('Unsupported OpenAPI schema type')
    nullable = spec.get('nullable', False)
    if type(nullable) is not bool or ('nullable' in spec and kind is None):
        raise ContractError('Invalid nullable schema')
    enum = spec.get('enum')
    if 'enum' in spec:
        if type(enum) is not list or not 1 <= len(enum) <= MAX_ENUM:
            raise ContractError('Invalid or excessive scalar enum')
        seen = set()
        for value in enum:
            if value is not None and type(value) not in (str, int, float, bool):
                raise ContractError('Only scalar enums are supported')
            # JSON numeric equality: 1 and 1.0 duplicate; True and 1 distinct.
            key = ('number' if type(value) in (int, float) else type(value).__name__, value)
            if key in seen:
                raise ContractError('Duplicate scalar enum value')
            seen.add(key)
        enum = tuple(enum)
    if keys & {'properties', 'required', 'additionalProperties'} and kind != 'object':
        raise ContractError('Object keywords require object type')
    if 'items' in keys and kind != 'array':
        raise ContractError('Array items require array type')
    properties, required, additional, items = {}, (), True, None
    if kind == 'object':
        properties = spec.get('properties', {})
        required = spec.get('required', [])
        additional = spec.get('additionalProperties', True)
        if type(properties) is not dict or type(required) is not list or len(required) > MAX_SCHEMA_NODES:
            raise ContractError('Invalid object schema')
        if any(type(key) is not str for key in required) or len(set(required)) != len(required):
            raise ContractError('Invalid required property list')
        required = tuple(required)
        properties = {key: _compile(child, document, budget, depth + 1, refs) for key, child in properties.items()}
        if type(additional) is dict:
            additional = _compile(additional, document, budget, depth + 1, refs)
        elif type(additional) is not bool:
            raise ContractError('Invalid additional properties schema')
    if kind == 'array':
        if 'items' not in spec:
            raise ContractError('Array schema requires items')
        items = _compile(spec['items'], document, budget, depth + 1, refs)
    return ('value', kind, nullable, enum, properties, required, additional, items)


def _matches(node, value, budget):
    budget.charge()
    if node[0] in ('oneOf', 'anyOf'):
        matches = 0
        for branch in node[1]:
            matches += _matches(branch, value, budget)
            if node[0] == 'anyOf' and matches:
                return True
            if node[0] == 'oneOf' and matches > 1:
                return False
        return matches == 1
    _, kind, nullable, enum, properties, required, additional, items = node
    if value is None:
        valid_type = kind is None or nullable
    elif kind is None:
        valid_type = True
    elif kind == 'integer':
        valid_type = type(value) is int or (type(value) is float and math.isfinite(value) and value.is_integer())
    elif kind == 'number':
        valid_type = type(value) is int or (type(value) is float and math.isfinite(value))
    else:
        valid_type = type(value) is {'string': str, 'boolean': bool, 'object': dict, 'array': list}[kind]
    if not valid_type:
        return False
    if enum is not None:
        matched = False
        for option in enum:
            budget.charge()
            if _enum_equal(value, option):
                matched = True
                break
        if not matched:
            return False
    if value is None:
        return True
    if kind == 'object':
        for key in required:
            budget.charge()
            if key not in value:
                return False
        for key, child in value.items():
            budget.charge()
            if key in properties:
                if not _matches(properties[key], child, budget):
                    return False
            elif additional is False:
                return False
            elif additional is not True and not _matches(additional, child, budget):
                return False
    if kind == 'array':
        for child in value:
            if not _matches(items, child, budget):
                return False
    return True


def load_document(path):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise ContractError('OpenAPI document must be a regular local file')
        with path.open('rb') as stream:
            return parse_json(stream.read(MAX_BYTES + 1))
    except OSError as exc:
        raise ContractError('Cannot read local OpenAPI document') from exc


def check_response(document, packet, *, method, path):
    if type(method) is not str or method not in ('GET', 'POST'):
        raise ContractError('Select explicit GET or POST')
    if (type(path) is not str or not path.startswith('/') or path.startswith('//') or len(path) > 8192
            or any(ord(char) < 32 or char in '?#{}' for char in path)):
        raise ContractError('Select a literal local operation path')
    document, packet = _detach(document), _detach(packet)
    validate(packet)
    if (type(document) is not dict or type(document.get('openapi')) is not str
            or re.fullmatch(r'3\.0\.[0-9]+', document['openapi']) is None):
        raise ContractError('Only OpenAPI 3.0 JSON documents are supported')
    paths = document.get('paths')
    item = paths.get(path) if type(paths) is dict else None
    operation = item.get(method.lower()) if type(item) is dict and '$ref' not in item else None
    responses = operation.get('responses') if type(operation) is dict else None
    if type(responses) is not dict or not responses:
        raise ContractError('Selected operation must declare responses')
    budget = Budget(MAX_SCHEMA_NODES)
    compiled = {}
    for status, response in responses.items():
        if status != 'default' and re.fullmatch(r'[1-5][0-9]{2}', status) is None:
            raise ContractError('Unsupported response status selector')
        if type(response) is not dict or '$ref' in response:
            raise ContractError('Selected responses require inline JSON schemas')
        content = response.get('content')
        media = content.get('application/json') if type(content) is dict else None
        if type(media) is not dict or 'schema' not in media:
            raise ContractError('Selected responses require application/json schema')
        compiled[status] = _compile(media['schema'], document, budget)
    response_status = packet['response']['status']
    selected = str(response_status) if str(response_status) in compiled else 'default'
    violations = []
    if packet['request']['method'] != method or urlsplit(packet['request']['target']).path != path:
        violations.append('fixture_operation_mismatch')
    if selected not in compiled:
        violations.append('undocumented_response_status')
    elif not _matches(compiled[selected], packet['response']['body'], Budget(MAX_EVALUATIONS)):
        violations.append('response_schema_mismatch')
    return {'version': 1, 'complete': True, 'passed': not violations,
            'scope': 'selected-openapi-response-redacted-fixture',
            'request_validated': False, 'full_openapi_validated': False,
            'document_sha256': hashlib.sha256(canonical(document)).hexdigest(),
            'fixture_sha256': packet['sha256'],
            'operation_sha256': hashlib.sha256(canonical([method, path])).hexdigest(),
            'response_status': response_status, 'violations': violations}

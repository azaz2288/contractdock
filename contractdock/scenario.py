"""Explicit offline response sequences for retry and workflow testing."""
import copy
from pathlib import Path
import threading

from .core import ContractError, MAX_BYTES, _replay_server, canonical, parse_json, validate


def validate_scenario(description):
    if not isinstance(description, dict) or set(description) != {'version', 'steps'}:
        raise ContractError('Scenario needs exactly version and steps')
    if type(description['version']) is not int or description['version'] != 1:
        raise ContractError('Unsupported scenario version')
    steps = description['steps']
    if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
        raise ContractError('Scenario needs 1 to 100 ordered steps')
    total = 0
    for step in steps:
        if not isinstance(step, dict) or 'fixture' not in step or set(step) - {'fixture', 'repeat', 'delay_ms'}:
            raise ContractError('Each scenario step needs fixture and optional repeat/delay_ms')
        repeat, delay = step.get('repeat', 1), step.get('delay_ms', 0)
        if type(repeat) is not int or not 1 <= repeat <= 10000:
            raise ContractError('Step repeat must be an integer in [1, 10000]')
        if type(delay) is not int or not 0 <= delay <= 5000:
            raise ContractError('Step delay_ms must be an integer in [0, 5000]')
        total += repeat
        if total > 10000:
            raise ContractError('Scenario exceeds 10000 total responses')
        validate(step['fixture'])
    if len(canonical(description)) > MAX_BYTES:
        raise ContractError('Scenario exceeds 1 MiB')
    return description


def load_scenario(path: Path):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file():
            raise ContractError('Scenario must be a regular file')
        with path.open('rb') as stream:
            return validate_scenario(parse_json(stream.read(MAX_BYTES + 1)))
    except OSError as exc:
        raise ContractError('Cannot read scenario') from exc


def scenario_server(description, *, port=0):
    # Freeze before validation and bind only after validating every step. Caller
    # mutations after construction cannot silently bypass checksum verification.
    description = copy.deepcopy(description)
    validate_scenario(description)
    steps = description['steps']
    position, remaining = 0, steps[0].get('repeat', 1)
    lock = threading.Lock()

    def select(key):
        nonlocal position, remaining
        with lock:
            if position == len(steps):
                return 410, {'error': 'scenario exhausted'}, 0
            step = steps[position]
            packet = step['fixture']
            if key != packet['request']['key']:
                return 409, {'error': 'unexpected scenario request'}, 0
            # Reservation commits at match, not after response delivery. A
            # disconnected client still consumes its step; never rewind retries.
            remaining -= 1
            if remaining == 0:
                position += 1
                if position < len(steps):
                    remaining = steps[position].get('repeat', 1)
            return packet['response']['status'], packet['response']['body'], step.get('delay_ms', 0)

    return _replay_server(select, port=port)

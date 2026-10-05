"""Synthetic local HTTP throughput; validates every response, not just timing."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import platform
import threading
import time
import tracemalloc
from urllib.request import urlopen

from contractdock.core import ContractError, canonical, replay_server, request_key, schema
from contractdock.scenario import scenario_server


def _packet(index):
    body = {'sequence': index}
    packet = {'version': 1, 'request': request_key('GET', '/benchmark'),
              'response': {'status': 200, 'body': body, 'schema': schema(body)}}
    packet['sha256'] = hashlib.sha256(canonical(packet)).hexdigest()
    return packet


def measure(requests=256, workers=4, mode='fixed'):
    if type(requests) is not int or not 1 <= requests <= 10000:
        raise ContractError('Benchmark requests must be in [1, 10000]')
    if type(workers) is not int or not 1 <= workers <= 16:
        raise ContractError('Benchmark workers must be in [1, 16]')
    if mode not in ('fixed', 'scenario'):
        raise ContractError('Benchmark mode must be fixed or scenario')
    if tracemalloc.is_tracing():
        raise ContractError('Benchmark needs its own tracemalloc session')
    if mode == 'scenario':
        steps = min(requests, 64)
        counts = [requests // steps + int(index < requests % steps) for index in range(steps)]
        description = {'version': 1, 'steps': [{'fixture': _packet(index), 'repeat': count}
                                              for index, count in enumerate(counts)]}
        expected = Counter({index: count for index, count in enumerate(counts)})
    else:
        packet = _packet(-1)
        expected = Counter({-1: requests})
    tracemalloc.start()
    try:
        with (scenario_server(description) if mode == 'scenario' else replay_server([packet])) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f'http://127.0.0.1:{server.server_port}/benchmark'
            def request(_):
                started = time.perf_counter()
                with urlopen(endpoint, timeout=10) as response:
                    if response.status != 200:
                        raise ContractError('Unexpected benchmark HTTP status')
                    body = json.load(response)
                if set(body) != {'sequence'} or type(body['sequence']) is not int:
                    raise ContractError('Unexpected benchmark response')
                return time.perf_counter() - started, body['sequence']
            try:
                started = time.perf_counter()
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    responses = list(pool.map(request, range(requests)))
                elapsed = time.perf_counter() - started
                if Counter(value for _, value in responses) != expected:
                    raise ContractError('Benchmark response multiplicities changed')
                _, peak = tracemalloc.get_traced_memory()
            finally:
                server.shutdown()
                thread.join(5)
            latencies = sorted(duration * 1000 for duration, _ in responses)
            return {'mode': mode, 'requests': requests, 'workers': workers, 'verified_responses': len(responses),
                    'elapsed_seconds': elapsed, 'requests_per_second': requests / elapsed,
                    'p50_ms': latencies[math.ceil(len(latencies) * 0.50) - 1],
                    'p95_ms': latencies[math.ceil(len(latencies) * 0.95) - 1],
                    'python_peak_bytes': peak, 'native_rss_measured': False}
    finally:
        tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests', type=int, default=256)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    print(json.dumps({'synthetic_loopback_only': True, 'python': platform.python_version(),
                      'platform': platform.platform(),
                      'runs': [measure(args.requests, args.workers, mode) for mode in ('fixed', 'scenario')]}, indent=2))


if __name__ == '__main__':
    main()

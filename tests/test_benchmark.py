import unittest
import tracemalloc

from benchmarks.replay import measure
from contractdock.core import ContractError


class BenchmarkTests(unittest.TestCase):
    def test_fixed_and_scenario_verify_real_responses(self):
        for mode in ('fixed', 'scenario'):
            with self.subTest(mode=mode):
                report = measure(requests=8, workers=2, mode=mode)
                self.assertEqual(report['verified_responses'], 8)
                self.assertGreater(report['elapsed_seconds'], 0)
                self.assertGreaterEqual(report['p95_ms'], report['p50_ms'])
                self.assertGreater(report['python_peak_bytes'], 0)
                self.assertFalse(tracemalloc.is_tracing())

    def test_limits_before_server_or_tracing(self):
        for kwargs in ({'requests': 0}, {'requests': True}, {'requests': 10001},
                       {'workers': 0}, {'workers': 17}, {'workers': True}, {'mode': 'unknown'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ContractError):
                measure(**kwargs)
        self.assertFalse(tracemalloc.is_tracing())

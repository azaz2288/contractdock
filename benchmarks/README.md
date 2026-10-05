# Verified synthetic loopback measurement

Run from the repository root (not `python benchmarks/replay.py`):

```sh
python -m benchmarks.replay --requests 256 --workers 4
```

The harness starts a real loopback HTTP service, issues bounded concurrent requests,
reads and validates every JSON response, and checks exact response multiplicities.
Fixed replay returns one immutable fixture. Scenario mode uses up to 64 distinct
responses with distributed repeat counts, verifying none are skipped or duplicated.
Any network/status/content error fails the run rather than counting as throughput.
Only synthetic in-memory fixtures are used; no upstream, credentials or user data.

Local single run, 2026-10-06, Windows 11 build 22631 / Python 3.12.10, 4 workers:

| Mode | Verified requests | Elapsed | req/s | p50 | p95 | Python allocation peak |
|---|---:|---:|---:|---:|---:|---:|
| fixed | 256 | 0.556149 s | 460.308 | 6.664400 ms | 9.563200 ms | 1,250,901 bytes |
| scenario | 256 | 0.397272 s | 644.395 | 5.847500 ms | 8.176100 ms | 788,934 bytes |

These are **not** proof that scenarios are faster. Fixed always ran first, there is
no separate warm-up, order/cold-start/runtime load confound the comparison, and
tracemalloc itself adds overhead. No repeated-run median or confidence interval is
claimed. Percentiles are nearest-rank across individual full-response round trips.
Elapsed time includes thread-pool setup and request dispatch, but excludes fixture
synthesis, server construction and shutdown. Traced peak starts before server
construction and ends after requests; it includes Python server/client allocations
but not native/socket/OS RSS, the preconstructed description or another machine.
Tiny JSON and local sockets are not Internet latency, production capacity or a
resource isolation guarantee. Handler thread concurrency remains OS-managed; the
benchmark bounds its clients, not all possible clients of the service.

Smoke tests exercise both real HTTP modes and reject invalid measurement settings.

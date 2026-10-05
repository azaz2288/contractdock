# Verified progress

## 2026-10-06 v0.1
19 tests, real upstream-shutdown/offline-replay demo, wheel installation and installed CLI passed. Published 9989170cfbec800f9ef5fc6ba63e5a3087145d9d, matching Windows/Linux CI success. Explicit origin, no redirect/proxy, redacted JSON fixtures, fixed offline matching, immutable output publication. Default redaction is not comprehensive privacy assurance.

## 2026-10-06 v0.2 observed-schema hardening
Added tests that first exposed multiple object shapes bypassing item field checks and missing uncertainty reporting for empty array samples. Reworked directional candidate matching; report uncertainties explicitly with compatible=null instead of an unfounded proof. Does not claim full OpenAPI or business semantics compatibility.

## 2026-10-06 v0.3 offline response scenarios
Added explicit globally ordered response sequences, repeat counts, bounded delays, atomic match/advance and 409 mismatch / 410 exhaustion. Malformed or wrong-order requests do not advance; matches consume even on disconnect, delay outside lock permits response arrival reordering. Validates every fixture before binding and detaches caller data; fixed replay also freezes validated fixtures. No network fallback, auto-loop or per-user session claim. Combined 1MiB/100 steps/10,000 responses limits; HTTP length bounds reject extreme integers before conversion. Tests cover retry recovery, concurrent exactly-once responses, lock-free delay, malformed header/body, policy limits, mutation, POST/redaction and CLI.

First test import failed because new scenario API did not exist yet; after implementation, one delay test measured 31ms with Windows coarse monotonic clock for a requested 40ms sleep. Changed measurement to perf_counter without changing delay semantics. Final suite/example/wheel/CI evidence recorded separately after verification.

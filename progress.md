# Verified progress

## 2026-10-06 v0.1
19 tests, real upstream-shutdown/offline-replay demo, wheel installation and installed CLI passed. Published 9989170cfbec800f9ef5fc6ba63e5a3087145d9d, matching Windows/Linux CI success. Explicit origin, no redirect/proxy, redacted JSON fixtures, fixed offline matching, immutable output publication. Default redaction is not comprehensive privacy assurance.

## 2026-10-06 v0.2 observed-schema hardening
Added tests that first exposed multiple object shapes bypassing item field checks and missing uncertainty reporting for empty array samples. Reworked directional candidate matching; report uncertainties explicitly with compatible=null instead of an unfounded proof. Does not claim full OpenAPI or business semantics compatibility.

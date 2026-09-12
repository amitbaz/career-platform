## Task 1 Report

- Scope: added configuration-driven profession gating for prefiltering, including new `SearchPolicy` fields, config loading/defaults, production YAML, and focused tests.
- Behavior: `prefilter_job()` now applies gates in this order: non-remote, blocked title, off-target profession, relevance. It also emits stable `reason_code` values: `not_remote`, `blocked_title`, `off_target_profession`, `no_relevance`, and `passed`.
- Verification: `PYTHONPATH=src pytest tests/test_config.py tests/test_prefilter.py -q`
- Result: `25 passed in 0.05s`
- Commit: pending at report write time
- Concerns: the repo-local shell did not have the package installed for bare `pytest`, so verification used `PYTHONPATH=src` to exercise the repo directly.

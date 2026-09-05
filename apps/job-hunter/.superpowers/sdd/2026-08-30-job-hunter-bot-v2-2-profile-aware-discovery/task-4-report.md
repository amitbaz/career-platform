# Task 4 Report

## Status

Completed on 2026-08-30.

## What Changed

- Integrated profile-aware ranking into the pipeline by passing extracted `CandidatePreferences` into `rank_jobs(...)` before shortlist selection.
- Replaced the pipeline's naive shortlist slice with `select_diverse_candidates(...)` using `max_jobs_per_run`, `source_minimum_per_run`, and `source_max_share`.
- Preserved evaluation caching, delivery retry behavior, and the existing `run_pipeline(...) -> RunSummary` interface.
- Added safe shortlist fallback behavior: if diverse selection fails, the pipeline logs the failure and falls back to the stable global ranked slice.
- Added diagnostics for `profile extraction: source=...`, `eligible sources: ...`, `selected sources: ...`, and `deferred_by_budget=...` without logging profile or job-description content.
- Updated shortlist selection so diversity caps shape the first pass, then any unused budget is backfilled from remaining ranked candidates in order.

## Tests

- Focused: `.venv/bin/pytest tests/test_ranking.py tests/test_pipeline.py -q`
- Regression slice: `.venv/bin/pytest tests/test_store.py tests/test_ranking.py tests/test_pipeline.py -q`
- Full suite: `.venv/bin/pytest -q`

All passed at the end of the task.

## Concerns

- The source share cap is treated as a best-effort diversity constraint, not an absolute hard stop. When strict enforcement would leave evaluation budget unused, the pipeline backfills remaining slots by score order. This matches the `100 eligible -> evaluate exactly 35` requirement, but it means a dominant source can exceed the configured share by the minimum amount needed to fill the shortlist.

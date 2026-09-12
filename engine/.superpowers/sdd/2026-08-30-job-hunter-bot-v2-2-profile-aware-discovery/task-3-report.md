## Task 3 Report

Date: 2026-08-30

Status: completed

Summary:
- Added `source_minimum_per_run` and `source_max_share` to `SearchPolicy`, with loader defaults and YAML defaults aligned to the v2.2 design.
- Reduced the default shortlist budget from 75 to 35 in both the model/config defaults and `config/search.yml`.
- Added `profile_priority_score()` with deterministic role/seniority, signal coverage, location fit, avoid-signal penalty, and source-quality scoring.
- Added `select_diverse_candidates()` with a two-pass source-diverse shortlist that preserves ranked order while enforcing per-source minimums and a max-share cap.
- Extended ranking/config coverage with focused tests for profile-aware scoring, unique-signal caps, source-minimum selection, max-share enforcement, stable order, and new defaults.

Verification:
- `pytest tests/test_ranking.py tests/test_config.py -q` -> `16 passed`
- `git diff --check` -> clean

Commit:
- Pending at report write time; created immediately after this file is added.

Concerns:
- The location-fit portion intentionally stays simple and deterministic. It matches explicit preferred-location text and otherwise falls back to coarse remote-friendly heuristics, which is enough for this task but may need refinement once Task 4 starts driving real shortlist outcomes.

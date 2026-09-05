# First-Run Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the first market-driven production run trustworthy by hardening targeted search, exposing candidate-context fallback causes, pacing Gemini under the existing RPM ceiling, correcting market telemetry, and keeping Brave Search safely inside the user's free monthly allowance.

**Architecture:** Add a targeted-search backend chain with Brave used only for a budgeted subset of market queries and DuckDuckGo as the zero-cost fallback. Persist Brave attempts in a small SQLite search-usage ledger and distribute the remaining allowance across remaining calendar days. Keep Gemini quotas unchanged but distinguish temporary rolling-window capacity from daily exhaustion so the client can wait once and retry. Keep candidate-context extraction fail-open while adding explicit source/error metadata. Move market metrics to the correct points in the pipeline and count actual Telegram delivery.

**Tech Stack:** Python 3.12+, requests, BeautifulSoup, dataclasses, SQLite, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-02-first-run-hardening-design.md`

## Global Constraints

- Do not change market shares, salary floors, or role strategy.
- Do not change `gemini-3.5-flash-lite`, `GEMINI_FREE_RPM=15`, `GEMINI_FREE_TPM=250000`, `GEMINI_FREE_RPD=500`, or the existing 80% internal safety ceiling.
- Brave Search is optional through `BRAVE_SEARCH_API_KEY` and must respect `BRAVE_MONTHLY_QUERY_LIMIT` (default `250`).
- Canonical lookup must never consume Brave credits.
- One search backend/query failure must not abort discovery.
- Never log candidate profile contents, Gemini response bodies from candidate extraction, API keys, or Telegram secrets.
- Fallback candidate context remains fail-open and must retry on a later run rather than being persisted as a successful extraction.
- `delivered` means successful Telegram job-message/card persistence during the current run, not digest candidates or PDFs.
- Existing canonicalization, dedupe, pending-work priority, company watch, Gmail, cover-letter, and Telegram navigation behavior must remain intact.

---

### Task 1: Add resilient targeted-search backends

**Files:**
- Create: `src/job_hunter/search_backend.py`
- Create: `src/job_hunter/sources/targeted_search.py`
- Modify: `src/job_hunter/sources/duckduckgo.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `.env.example`
- Test: `tests/test_search_backend.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- `SearchHit(title: str, url: str)`
- `SearchResponse(hits: list[SearchHit], backend: str)`
- `BraveSearchBackend.search(query: str) -> SearchResponse`
- `DuckDuckGoSearchBackend.search(query: str) -> SearchResponse`
- `FallbackSearchBackend.search(query: str) -> SearchResponse`
- `TargetedSearchSource.stats` exposes planned/attempted/succeeded/results by market.

- [ ] Write tests proving Brave result normalization, Brave failure -> DuckDuckGo fallback, no-key DuckDuckGo behavior, market-hint preservation, and successful zero-result accounting.
- [ ] Push the tests alone and verify CI fails because the new interfaces do not exist.
- [ ] Implement the backends/source with minimal code and wire `build_sources()` to use `TargetedSearchSource`.
- [ ] Keep `DuckDuckGoSource` as a compatibility wrapper around `TargetedSearchSource(DuckDuckGoSearchBackend)` so existing imports/tests do not break.
- [ ] Add optional `BRAVE_SEARCH_API_KEY` loading and `.env.example` documentation.
- [ ] Run CI and require the targeted tests plus full suite to pass.

### Task 2: Make candidate-context fallback observable

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/candidate_context.py`
- Modify: `src/job_hunter/pipeline.py`
- Test: `tests/test_candidate_context.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Extend `CandidateContext` with defaulted metadata fields `source: str = "unknown"` and `load_error: str = ""`.
- Successful fresh extraction returns `source="gemini"`.
- Cached extraction returns `source="cache"`.
- Blank profile fallback returns `source="fallback_empty_profile"`.
- Extraction/parse failure returns `source="fallback_error"` and `load_error=<exception class name>`.

- [ ] Write tests that force invalid Gemini JSON and assert fallback metadata plus a warning containing only exception class/reason, not profile or raw response.
- [ ] Write a pipeline test asserting candidate-context loading happens once and the structured log contains its source/error metadata.
- [ ] Push tests and verify the expected failures.
- [ ] Implement metadata/logging while preserving the existing successful-context cache format and fail-open behavior.
- [ ] Run candidate-context/pipeline tests and full CI.

### Task 3: Pace Gemini instead of deferring on temporary RPM pressure

**Files:**
- Modify: `src/job_hunter/gemini_usage.py`
- Modify: `src/job_hunter/gemini.py`
- Test: `tests/test_gemini.py`
- Test: `tests/test_gemini_single_attempt_accounting.py`
- Test: `tests/test_gemini_pacing.py`

**Interfaces:**
- Add `GeminiTemporaryCapacity(GeminiBudgetExceeded)` with `retry_after_seconds: float`.
- `GeminiUsageTracker.preflight()` raises `GeminiBudgetExceeded` for daily/RPD exhaustion, but `GeminiTemporaryCapacity` for rolling RPM/TPM-only pressure.
- Temporary-capacity checks do not write a `blocked_budget` row before the client waits.
- `GeminiClient(..., sleep_fn=time.sleep)` waits once for `retry_after_seconds`, refreshes `now`, runs preflight again, and only then issues HTTP.

- [ ] Write tests for RPM-only retry delay, TPM-only retry delay, unchanged RPD hard block, and no provider request before the injected sleep completes.
- [ ] Push tests and verify CI fails on missing temporary-capacity behavior.
- [ ] Implement the exception/delay calculation and one-retry client pacing.
- [ ] Verify provider 429 accounting and existing guardrail tests remain unchanged.
- [ ] Run full CI.

### Task 4: Correct market metrics and delivery telemetry

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Modify: `src/job_hunter/pipeline.py`
- Test: `tests/test_discovery.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_first_run_telemetry.py`

**Interfaces:**
- Search stats are aggregated only after source discovery.
- Add per-market `search_results` and `reattributed` metrics.
- Maintain a `delivered_by_market` counter incremented only after successful `store.mark_delivered(..., "telegram_message", ...)` for a digest/card in this run.
- `_log_market_metrics(...)` executes after delivery attempts (or at end of dry run with zero deliveries).
- Preserve existing structured-log field order/labels where possible for compatibility.

- [ ] Write tests reproducing the old `queries_attempted=0` snapshot bug and the old `selected < delivered` mislabeling.
- [ ] Write a discovery test where enrichment changes the market and assert `reattributed` increments.
- [ ] Push tests and verify CI fails for the old behavior.
- [ ] Move stats collection after `collect_candidates`, add result/reattribution counters, and count actual Telegram delivery.
- [ ] Run `pytest -q` through CI and verify no regressions.

### Task 5: Enforce Brave free-tier monthly budget

**Files:**
- Create: `src/job_hunter/search_budget.py`
- Modify: `src/job_hunter/search_backend.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `.github/workflows/daily.yml`
- Modify: `.env.example`
- Test: `tests/test_brave_budget.py`

**Interfaces:**
- `SearchUsageLedger(db_path)` persists metered provider attempts in the existing SQLite state file.
- `brave_queries_available_today(..., monthly_limit, now)` returns the safe remaining allowance for the current UTC day.
- `split_queries_for_brave(queries, limit)` round-robins selected Brave queries across markets and returns all other queries for zero-cost fallback.
- `BraveSearchBackend(..., on_attempt=...)` records a conservative usage attempt before the provider request.
- `build_search_backend(..., enable_brave=False)` makes Brave explicit opt-in so canonical lookup cannot spend credits accidentally.

- [ ] Write tests for 250/month persistence, same-day manual rerun protection, market round-robin selection, and canonical lookup never using Brave.
- [ ] Verify the tests fail before implementation.
- [ ] Implement the SQLite usage ledger and daily allowance calculation.
- [ ] Split market queries into budgeted Brave and DuckDuckGo lanes; keep per-query Brave -> DuckDuckGo fallback.
- [ ] Default `BRAVE_MONTHLY_QUERY_LIMIT` to `250` and allow a GitHub Actions variable to override it later.
- [ ] Verify the full suite.

## Completion Verification

- [ ] Compare branch to `main` and inspect every changed file.
- [ ] Confirm CI is green on the final branch head.
- [ ] Confirm no market/salary/model/Gemini quota configuration changed unintentionally.
- [ ] Confirm Brave usage cannot exceed the configured monthly allowance even across manual reruns.
- [ ] Confirm logs can distinguish: planned vs attempted vs succeeded vs results, candidate-context source/error, temporary Gemini pacing, reattribution, and actual delivered jobs.

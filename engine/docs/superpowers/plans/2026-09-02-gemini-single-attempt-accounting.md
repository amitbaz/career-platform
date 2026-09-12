# Gemini Single-Attempt Accounting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure each logical Gemini call makes at most one provider HTTP attempt and that HTTP/network failures are counted once in Gemini usage accounting.

**Architecture:** Keep the generic `HttpClient` retry defaults for the rest of the bot, but add one per-request `retry` switch. `GeminiClient` passes `retry=False`, keeps the existing retryable-status metadata for compatibility, and records request exceptions as failed Gemini attempts before re-raising them.

**Tech Stack:** Python 3.12, requests, pytest

**Spec:** `docs/superpowers/specs/2026-09-02-gemini-single-attempt-accounting-design.md`

## Global Constraints

- Gemini free-tier production limits are configured externally; this fix must not hard-code provider quota values.
- Existing non-Gemini HTTP retry behavior must remain unchanged.
- Existing Gemini 429 circuit-breaker behavior must remain unchanged.
- Use TDD: failing regression tests first, then the minimal implementation.

---

### Task 1: Disable hidden Gemini retries and account for request failures

**Files:**
- Modify: `src/job_hunter/http.py`
- Modify: `src/job_hunter/gemini.py`
- Test: `tests/test_gemini_single_attempt_accounting.py`
- Test: `tests/test_http.py`

**Interfaces:**
- `HttpClient.post(..., retry: bool = True, retry_status_codes: set[int] | None = None)` preserves current behavior by default.
- `GeminiClient.generate_text(...)` calls `HttpClient.post` with the existing Gemini retryable-status set plus `retry=False`.
- `GeminiUsageTracker.record_error(...)` remains the usage-ledger entry point for non-429 failed attempts.

- [x] **Step 1: Write failing Gemini regression tests**

Tests prove a Gemini request opts out of automatic retries and that a request exception is recorded once before being re-raised.

- [x] **Step 2: Run tests and verify RED**

CI run `33622638585`: `3 failed, 611 passed`; failures were exactly the missing retry opt-out and missing network-error accounting.

- [x] **Step 3: Add an HttpClient regression test**

The regression test proves `retry=False` performs one request attempt while the default path still retries request exceptions.

- [x] **Step 4: Implement the minimal retry opt-out**

`HttpClient.get/post/_request` accepts `retry: bool = True`. When false, status and exception retries are skipped. `GeminiClient` passes `retry=False`, records request exceptions through `record_error`, and re-raises them.

- [ ] **Step 5: Run full suite and verify GREEN**

Run: `pytest -q`

Expected: all tests pass.

- [ ] **Step 6: Finish branch**

Verify the branch diff is limited to the spec, plan, regression tests, and the two production files, then prepare it for merge.

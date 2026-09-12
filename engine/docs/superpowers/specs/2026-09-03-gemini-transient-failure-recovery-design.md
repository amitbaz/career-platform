# Gemini Transient Failure Recovery Design

## Goal

Stop losing otherwise-good job evaluations and cover letters to one-off transient Gemini failures (HTTP 5xx, read timeouts, `MAX_TOKENS` truncation), without introducing a generic retry framework or weakening free-tier quota protections.

## Context

Production run #30 showed three failure classes that currently drop work for the run instead of recovering:
- one job evaluation failed on Gemini HTTP `500 INTERNAL`,
- one job evaluation failed on a 25-second read timeout,
- three cover-letter generations failed with `finish_reason=MAX_TOKENS`.

`GeminiClient.generate_text` (see [[2026-09-02-gemini-single-attempt-accounting-design]]) deliberately calls the transport with `retry=False`: a bare HTTP-layer retry would not re-run `GeminiUsageTracker.preflight()` per attempt, so RPM/TPM pacing and per-attempt accounting would be silently skipped for retried calls. Any retry therefore has to live inside `GeminiClient` itself, where pacing and accounting are already wired per attempt.

## Design

**Transient HTTP/network retry (`gemini.py`)**
- `GeminiClient.generate_text` gains `max_attempts: int = 1`. The existing preflight -> POST -> parse body is wrapped in a loop bounded by `max_attempts`.
- Retryable only: HTTP `5xx` (`_RETRYABLE_STATUS_CODES`, already defined) and `requests.Timeout` (covers connect/read timeout — the issue's "network read timeouts"). Plain `ConnectionError`, other `RequestException` subtypes, 4xx, and 429 are never retried by this loop — 429 keeps its existing quota-pause behavior untouched.
- Every attempt (including retries) re-runs `_preflight_with_pacing` and calls `tracker.record_error`/`record_success` exactly as before, so retries are fully reflected in RPM/TPM/RPD accounting and pacing.
- A small fixed delay (`_TRANSIENT_RETRY_DELAY_SECONDS = 2.0`, via the existing injectable `_sleep_fn`) separates attempts.
- Default `max_attempts=1` preserves current behavior for `gmail_semantic` and `candidate_context` (candidate-context reliability is tracked separately in #27) and for any caller that doesn't opt in.

**Bounded evaluation retry (`evaluation.py`)**
- `evaluate_job` passes `max_attempts=2` (one retry) to `generate_text`. Local JSON/schema validation failures (`EvaluationError`) happen after `generate_text` returns and are never retried — deterministic validation failures are not retried blindly.

**Bounded cover-letter MAX_TOKENS recovery (`cover_letter.py`)**
- `generate_cover_letter` tries `max_output_tokens=800`, and on `GeminiIncompleteResponse` retries once more at `max_output_tokens=1600` before giving up. Each attempt is a full `generate_text` call, so it goes through the same preflight/accounting path as any other call. Both the truncation and the final outcome are logged.
- If the second attempt also truncates, `GeminiIncompleteResponse` propagates; `pipeline.py`'s existing per-job `except Exception` around cover-letter generation catches it exactly like any other cover-letter failure today.

**Pipeline (`pipeline.py`)**
- No changes. Per-job failure isolation (`_evaluate_and_deliver_job`'s `try/except Exception` around `evaluate_job` and `generate_cover_letter`) already gives fail-open behavior; an exhausted retry surfaces as the same `GeminiError`/`GeminiIncompleteResponse` these blocks already handle.

## Out of scope

- Candidate-context structured-output reliability (#27).
- Discovery/canonical-resolution performance (#29).
- Switching Gemini models or paid tiers.
- A generic/pluggable retry framework — retries are explicit, purpose-scoped call sites (`evaluate_job`, `generate_cover_letter`), not a cross-cutting policy.

## Success Criteria

1. A job evaluation that hits HTTP 500 or a read timeout once, then succeeds, produces a normal evaluation and exactly two tracked attempts (one error, one success).
2. A job evaluation that fails transiently on every attempt raises cleanly after exactly `max_attempts` tries and does not abort the run (existing per-job isolation).
3. A cover letter that truncates once (`MAX_TOKENS`) at 800 tokens succeeds on a second attempt at 1600 tokens.
4. A cover letter that truncates twice raises `GeminiIncompleteResponse` after exactly two attempts, handled by the pipeline's existing per-job error handling.
5. `gmail_semantic` and `candidate_context` calls are unaffected (`max_attempts` still defaults to `1`).
6. Deterministic local validation errors (`EvaluationError`, cover-letter placeholder/empty checks) are never retried.
7. Full test suite passes.

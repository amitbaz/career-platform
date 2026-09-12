# Gemini Single-Attempt Accounting Design

## Goal

Keep Gemini usage predictable on the free tier by ensuring one logical Gemini call produces at most one HTTP attempt, and every attempt that reaches the Gemini request boundary is reflected in usage accounting.

## Context

The bot's generic `HttpClient` retries retryable HTTP statuses and network exceptions. `GeminiClient` currently opts into retries for 500/502/503/504, so a single logical Gemini operation can create up to three provider requests while the Gemini usage tracker records only the final logical outcome. With a free-tier RPD limit of 20, hidden retries can consume a meaningful portion of the daily quota.

## Design

- Add a per-request `retry: bool = True` switch to `HttpClient`; defaults preserve all existing callers.
- Gemini requests pass `retry=False`, so neither retryable HTTP statuses nor `requests.RequestException` failures are attempted again automatically.
- Preserve Gemini's retryable-status metadata (`500/502/503/504`) for compatibility and observability even though retries are disabled for Gemini calls.
- Other bot HTTP traffic keeps the existing retry behavior unchanged.
- `GeminiClient` records a non-429 provider error exactly once for HTTP failures such as 503.
- `GeminiClient` also records a failed attempt when the HTTP request raises a `requests.RequestException` (including read timeouts), then re-raises the original network exception so existing pipeline error handling remains unchanged.
- 429 behavior remains unchanged: one request, one quota row, then the existing circuit breaker pauses further Gemini work.
- The usage status continues to represent actual Gemini attempts rather than application-level retry loops.

## Success Criteria

1. A Gemini 503 results in exactly one HTTP attempt and one tracked error row.
2. A Gemini network timeout results in exactly one HTTP attempt and one tracked error row.
3. No Gemini request automatically retries 500/502/503/504 or request exceptions.
4. Existing retry behavior for non-Gemini `HttpClient` callers is unchanged.
5. Existing success and 429 behavior remains unchanged.

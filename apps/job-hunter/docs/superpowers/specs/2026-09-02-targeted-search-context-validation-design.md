# Targeted Search and Candidate Context Validation Fix

Date: 2026-09-02
Status: Approved for implementation
Branch: `fix/targeted-search-context-validation`

## Problem

The first post-hardening production run exposed two remaining failures:

1. All 9 metered Brave Search requests returned HTTP 422. The request already uses valid `q`, `count=20`, and `safesearch=moderate` parameters. Brave's current API examples include `Accept-Encoding: gzip`, and a documented 2026 integration failure shows Brave can reject requests unless `Cache-Control: no-cache` is explicit. Our client sends neither header.
2. Candidate-context extraction reaches Gemini successfully but `_parse_context()` raises `ValueError`. The parser currently rejects any structural deviation from its exact shape and logs only the exception class, which hides the actionable validation reason. The provider schema also omits the parser's array-size limits, allowing schema-valid output that can still exceed local validation.

## Design

### Brave request compatibility

`BraveSearchBackend` will send the documented/required request headers explicitly:

- `Accept: application/json`
- `Accept-Encoding: gzip`
- `Cache-Control: no-cache`
- `X-Subscription-Token: <secret>`

On an HTTP error, the backend will extract only safe structured Brave error metadata (`code`, `detail`, and validation locations/messages) for logging. It must never log the subscription token, request headers, or arbitrary response bodies. The normal DuckDuckGo fallback remains unchanged, and every Brave attempt still counts against the persisted monthly budget before the request.

### Candidate-context validation

Keep structured output and the existing schema, but make local parsing tolerant where strictness adds no safety and align provider-side constraints with local validation:

- Require every known top-level field, but ignore unknown extra fields instead of rejecting the entire context.
- Require every known preference field, but ignore unknown extra preference fields.
- Empty evidence arrays remain valid.
- Encode `maxItems=8` for preference arrays and `maxItems=20` for evidence arrays in the Gemini response schema, matching the existing parser limits.
- Preserve existing type, item-count, item-length, and summary-length validation locally.
- If parsing still fails, log a sanitized local validation reason derived from our own `ValueError` message, plus the exception class. Never log the candidate profile or raw Gemini response.

This should convert harmless provider-added fields into successful context extraction and prevent Gemini from generating arrays the parser is guaranteed to reject, while retaining safeguards against malformed or oversized data.

## Verification

- Unit tests prove Brave sends the compatibility headers.
- Unit tests prove structured 422 metadata is logged without secret/body leakage.
- Unit tests prove candidate context accepts harmless extra fields.
- Unit tests prove actual invalid values still fail and produce a safe validation reason.
- Unit tests prove the Gemini response schema carries the parser's 8/20 array limits.
- Full test suite passes.
- Production success criterion: a subsequent workflow run shows at least one Brave query completing without HTTP 422; candidate profile extraction reports `source=gemini` or `source=cache` rather than `fallback_error`.

## Out of scope

- Market allocations, salary thresholds, role targeting, Gemini quotas/model, Brave monthly budget, direct job-board adapters, or DuckDuckGo replacement.
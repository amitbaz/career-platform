# Gmail Structured Semantic Output Fix Design

## Problem

Production Gmail backfill is repeatedly failing with `invalid_semantic_response`, including real LinkedIn job-alert emails that contain valid job metadata and job URLs. The Gmail classifier currently asks Gemini for `application/json`, but does not provide a response schema. The parser then applies a stricter contract than the provider was asked to follow: supported `kind`, numeric confidence, rationale length, exact job keys, non-null strings for several job fields, and typed arrays/booleans.

This mismatch causes harmless model-output variation to abort processing of the entire email. In the latest production run, released LinkedIn alerts repeatedly hit this boundary until the Gmail workflow step timed out, so no `gmail:linkedin` jobs reached evaluation.

## Root Cause

`GeminiClient.generate_text(..., json_mode=True)` only sets `generationConfig.responseMimeType = "application/json"`. That guarantees JSON formatting, not conformance to the Gmail classifier's expected schema.

The Gmail parser also rejects `null` for optional extracted job text such as company, title, location, and description even though unknown values are legitimate during extraction.

Finally, `SemanticClassificationError` collapses every parser failure into `invalid_semantic_response`, so production logs do not reveal which validation rule was violated.

## Design

### 1. Constrain Gemini output with `responseSchema`

Extend `GeminiClient.generate_text` with an optional `json_schema` argument. When provided, it must enable JSON mode and send the schema as `generationConfig.responseSchema` alongside `responseMimeType = "application/json"`.

The Gmail classifier will own a concrete schema describing the existing `GmailClassification` contract. It will constrain:

- `kind` to the existing supported enum values.
- `confidence` to a number in `[0, 1]`.
- `rationale` to a string.
- optional top-level extraction metadata to nullable strings.
- `job_urls` to an array of strings.
- `jobs` to an array of objects with the current supported fields.
- job `remote` to nullable boolean.

No API migration or Gemini SDK dependency is introduced; the existing REST `generateContent` integration remains in place.

### 2. Tolerate unknown optional job text

Keep `source_platform` strict and non-empty because it is required for candidate identity. Keep URL validation strict when a URL is present.

Allow `company`, `title`, `location`, and `description` inside semantic jobs to be either strings or `null`, normalizing `null` to `""`. This matches the meaning of unknown extracted metadata and prevents a whole email from failing over a harmless absent value.

### 3. Preserve strict validation as a safety net

The local parser remains authoritative after Gemini returns. Unsupported kinds, invalid URLs, invalid confidence values, malformed JSON, invalid boolean values, and structurally invalid job objects must still raise `SemanticClassificationError` rather than silently becoming review events.

### 4. Improve safe diagnostics

Extend `SemanticClassificationError` with an optional safe `detail` string containing only the local validation reason, never the raw Gemini response or email content.

`GmailSyncService` will log both `reason` and `detail` for semantic failures. Example:

`gmail_semantic_classification_failed message_id=... reason=invalid_semantic_response detail=remote must be a boolean or null`

This gives future production debugging enough information without leaking mailbox content.

## Files

- `src/job_hunter/gemini.py` — support `json_schema` in REST generation config.
- `src/job_hunter/gmail_classifier.py` — define schema, pass it to Gemini, tolerate nullable optional job text, preserve parser failure detail.
- `src/job_hunter/gmail_sync.py` — log safe parser failure detail.
- `tests/test_gemini.py` — verify structured-output request payload.
- `tests/test_gmail_classifier.py` — verify schema is requested and nullable optional job text is accepted while malformed data remains rejected.
- Gmail sync tests — verify safe failure detail is logged without raw response content.

## Success Criteria

1. Gmail semantic classification requests a schema-constrained JSON response from Gemini.
2. `null` optional job text no longer causes `invalid_semantic_response`.
3. Malformed or unsafe semantic responses are still rejected.
4. Production logs identify the local validation reason without logging email/model contents.
5. Full test suite passes.
6. No changes are made to discovery ranking, Telegram behavior, or non-Gmail Gemini calls.
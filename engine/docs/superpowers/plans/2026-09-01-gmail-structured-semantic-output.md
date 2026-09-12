# Gmail Structured Semantic Output Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop valid Gmail/LinkedIn emails from failing at the Gemini-output parser boundary by constraining Gemini to the classifier schema, tolerating unknown optional job text, and exposing safe validation diagnostics.

**Architecture:** Keep the existing `generateContent` REST client and Gmail classifier pipeline. Add an optional schema parameter at the Gemini client boundary, define the Gmail response schema in the classifier, retain local validation, and surface only local validation reasons through `SemanticClassificationError.detail`.

**Tech Stack:** Python 3.12, pytest, Gemini REST `generateContent`, SQLite-backed Gmail sync.

**Spec:** `docs/superpowers/specs/2026-09-01-gmail-structured-semantic-output-design.md`

## Global Constraints

- Do not migrate to a new Gemini SDK or Interactions API in this fix.
- Do not change discovery ranking, Telegram delivery, or non-Gmail Gemini behavior.
- Never log raw Gemini responses or Gmail message content.
- Keep malformed JSON, unsupported kinds, invalid confidence, invalid URLs, and invalid non-null types as technical failures.

---

### Task 1: Add schema-constrained Gemini requests

**Files:**
- Modify: `tests/test_gemini.py`
- Modify: `src/job_hunter/gemini.py`

**Interfaces:**
- Consumes: existing `GeminiClient.generate_text(prompt, json_mode=False)`.
- Produces: `GeminiClient.generate_text(prompt, *, json_mode=False, json_schema: dict | None = None) -> str`.

- [ ] **Step 1: Write the failing request-payload test**

Add a test that passes a small object schema and asserts the request contains both:

```python
assert generation_config["responseMimeType"] == "application/json"
assert generation_config["responseSchema"] == schema
```

Also assert that passing `json_schema` without `json_mode=True` still creates JSON mode so callers cannot accidentally request a schema with a non-JSON response.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest -q tests/test_gemini.py`

Expected: new schema test fails because `generate_text` has no `json_schema` argument.

- [ ] **Step 3: Implement the minimal Gemini client change**

Update `generate_text` so:

```python
generation_config = None
if json_mode or json_schema is not None:
    generation_config = {"responseMimeType": "application/json"}
    if json_schema is not None:
        generation_config["responseSchema"] = json_schema
```

Only add `generationConfig` to the payload when needed.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `pytest -q tests/test_gemini.py`

Expected: all Gemini client tests pass.

- [ ] **Step 5: Commit**

Commit message: `feat: support Gemini response schemas`

---

### Task 2: Constrain Gmail semantic output and tolerate nullable optional job text

**Files:**
- Modify: `tests/test_gmail_classifier.py`
- Modify: `src/job_hunter/gmail_classifier.py`

**Interfaces:**
- Consumes: `GeminiClient.generate_text(..., json_schema=...)` from Task 1.
- Produces: Gmail semantic requests with a response schema; parser normalization of nullable optional job text.

- [ ] **Step 1: Write failing classifier regression tests**

Add tests that verify:

1. The fake Gemini records the schema passed by `classify_email` for a semantic Gmail message.
2. A semantic `JOB_ALERT` job containing `null` for `company`, `title`, `location`, and `description` parses successfully and normalizes each to `""`.
3. Invalid `remote="yes"` still raises `SemanticClassificationError`.

Update `FakeGemini.generate_text` to accept and record `json_schema`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `pytest -q tests/test_gmail_classifier.py`

Expected: schema-recording and nullable-job tests fail on current behavior.

- [ ] **Step 3: Define the Gmail response schema**

Add one module-level schema dictionary matching the existing classifier contract. Use Gemini `responseSchema` types and nullable fields without changing supported kinds.

Require `kind`, `confidence`, and `rationale` at the top level. Keep extraction metadata structurally defined but optional. Define each job with the existing eight supported fields so provider output matches `_JOB_FIELDS`.

- [ ] **Step 4: Pass the schema to Gemini**

Change the semantic call to:

```python
raw = gemini.generate_text(
    _build_semantic_prompt(message, extract_job_alert=extract_job_alert),
    json_mode=True,
    json_schema=_GMAIL_CLASSIFICATION_SCHEMA,
)
```

- [ ] **Step 5: Normalize optional job text**

For semantic job fields `company`, `title`, `location`, and `description`, replace strict `_validate_string` calls with the existing nullable-aware text helper so `None` becomes `""`.

Do not relax `source_platform`, URL validation, or `remote` type validation.

- [ ] **Step 6: Run the focused tests and verify GREEN**

Run: `pytest -q tests/test_gmail_classifier.py`

Expected: classifier tests pass, including existing malformed/unsupported-response tests.

- [ ] **Step 7: Commit**

Commit message: `fix: constrain Gmail semantic output schema`

---

### Task 3: Surface safe semantic validation diagnostics

**Files:**
- Modify: `src/job_hunter/gmail_classifier.py`
- Modify: `src/job_hunter/gmail_sync.py`
- Modify: the existing Gmail sync test module covering semantic classification failures.

**Interfaces:**
- Produces: `SemanticClassificationError(reason: str, detail: str | None = None)` with `reason` stable for program behavior and `detail` safe for logs.

- [ ] **Step 1: Write a failing log regression test**

Trigger a semantic response with a locally invalid field (for example `remote="yes"`) through `GmailSyncService`, capture logs with `caplog`, and assert:

```python
assert "reason=invalid_semantic_response" in caplog.text
assert "detail=remote must be a boolean or null" in caplog.text
```

Also assert a sentinel string placed only in the raw model response is not present in the logs.

- [ ] **Step 2: Run the focused sync test and verify RED**

Run the exact new pytest node for the log test.

Expected: fails because only the generic reason is logged.

- [ ] **Step 3: Add safe detail to the exception boundary**

Update `SemanticClassificationError` to store optional `detail`. When `_parse_semantic_classification` or reconciliation raises `ValueError`, raise:

```python
SemanticClassificationError(
    "invalid_semantic_response",
    detail=str(exc),
)
```

Do not include `raw` or message content.

- [ ] **Step 4: Log the detail**

Update the Gmail sync warning to include a sanitized local detail field, defaulting to `unknown` when absent.

- [ ] **Step 5: Run the focused test and verify GREEN**

Run the exact new pytest node.

Expected: pass with safe detail visible and raw response absent.

- [ ] **Step 6: Commit**

Commit message: `fix: expose Gmail semantic validation reason`

---

### Task 4: Full verification

**Files:** none expected.

- [ ] **Step 1: Run the complete suite**

Run: `pytest -q`

Expected: 0 failures.

- [ ] **Step 2: Review branch scope**

Compare `main...fix/gmail-structured-semantic-output` and confirm changes are limited to:

- required spec/plan docs,
- Gemini structured-output support,
- Gmail classifier/parser diagnostics,
- focused tests.

- [ ] **Step 3: Stop for integration choice**

Do not merge automatically. Present the standard branch-completion options to the user.
# Gmail Review Classification Spam Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop technical Gmail semantic-classification failures from becoming Telegram `REVIEW_NEEDED` spam, improve semantic robustness, narrow historical Gmail intake, and safely reprocess already-polluted legacy rows.

**Architecture:** Keep the existing Gmail sync and SQLite architecture. Correct the boundary between genuine ambiguity and technical failure in `gmail_classifier.py`, let technical failures propagate into the existing per-message sync error/retry path, reduce false-positive backfill candidates, and add an idempotent exact-match cleanup for previously persisted technical review artifacts.

**Tech Stack:** Python 3.12, SQLite, Gmail API, existing Gemini REST client, pytest, GitHub Actions, Telegram Bot API.

**Spec:** `docs/superpowers/specs/2026-09-01-gmail-review-classification-spam-design.md`

## Global Constraints

- `REVIEW_NEEDED` is only for genuine classification or job-matching ambiguity.
- Gemini/API/parser failures must increment Gmail sync errors and must not create application events or Telegram review items.
- Gmail access remains read-only.
- Full email bodies and raw Gemini responses must not be persisted or logged.
- Public-source job hunting remains fail-open when Gmail fails.
- Existing bounded backfill behavior and workflow timeout strategy remain in place.
- This fix stays on SQLite; Supabase migration is out of scope.
- Legacy cleanup must match only the exact old technical-failure rationale `semantic classification unavailable or invalid`.

---

## File Structure

**Modify**

- `src/job_hunter/gmail_models.py` — shared legacy-failure constant used by classifier/store repair tests.
- `src/job_hunter/gmail_classifier.py` — semantic normalization, genuine-review contract, dedicated technical classification exception.
- `src/job_hunter/gmail_sync.py` — semantic failure logging/counting, narrower historical query, legacy repair orchestration.
- `src/job_hunter/store.py` — idempotent release of legacy synthetic review rows.
- `README.md` — operator-facing Gmail failure/retry behavior.
- `tests/test_gmail_classifier.py` — classifier contract and semantic normalization coverage.
- `tests/test_gmail_sync.py` — retry/error metrics, query precision, cleanup orchestration.
- `tests/test_store.py` — exact-match legacy repair behavior.
- `tests/test_pipeline.py` — regression that technical semantic failure cannot create Telegram review delivery.

**No new runtime dependencies.**

---

### Task 1: Separate Genuine Review From Semantic Technical Failure

**Files:**
- Modify: `src/job_hunter/gmail_classifier.py`
- Test: `tests/test_gmail_classifier.py`

**Interfaces:**
- Produces: `SemanticClassificationError(reason: str)` with public attribute `reason`.
- Produces: `classify_email(message, gemini) -> GmailClassification` for valid classifications only; technical semantic failures raise.
- Preserves: deterministic classification behavior and `AUTO_CONFIDENCE_THRESHOLD` review behavior.

- [ ] **Step 1: Replace tests that currently expect malformed semantic output to become review**

In `tests/test_gmail_classifier.py`, replace the current malformed/unsupported semantic tests with explicit technical-failure assertions:

```python
from job_hunter.gmail_classifier import SemanticClassificationError


def test_malformed_semantic_json_raises_classification_error():
    with pytest.raises(SemanticClassificationError) as exc_info:
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FakeGemini("not json"),
        )

    assert exc_info.value.reason == "invalid_semantic_response"


def test_unsupported_semantic_kind_raises_classification_error():
    with pytest.raises(SemanticClassificationError) as exc_info:
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FakeGemini(semantic_response(kind="FOLLOW_UP")),
        )

    assert exc_info.value.reason == "invalid_semantic_response"
```

Add a fake Gemini that raises to cover provider/transport failure:

```python
class FailingGemini:
    def generate_text(self, prompt: str, *, json_mode: bool = False) -> str:
        raise RuntimeError("provider unavailable")


def test_gemini_failure_raises_classification_error_without_review_result():
    with pytest.raises(SemanticClassificationError) as exc_info:
        classify_email(
            message("Hiring conversation", "Can we discuss this role?"),
            FailingGemini(),
        )

    assert exc_info.value.reason == "gemini_error"
```

- [ ] **Step 2: Run the focused tests and verify they fail under current behavior**

Run:

```bash
pytest tests/test_gmail_classifier.py -k "malformed_semantic_json or unsupported_semantic_kind or gemini_failure" -v
```

Expected: FAIL because current code returns `REVIEW_NEEDED` instead of raising `SemanticClassificationError`.

- [ ] **Step 3: Add the dedicated exception and narrow exception mapping**

In `src/job_hunter/gmail_classifier.py`, add:

```python
class SemanticClassificationError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
```

Refactor the semantic block in `classify_email()` so provider failures and expected validation failures become technical exceptions rather than review classifications:

```python
try:
    raw = gemini.generate_text(
        _build_semantic_prompt(
            message,
            extract_job_alert=extract_job_alert,
        ),
        json_mode=True,
    )
except Exception as exc:
    raise SemanticClassificationError("gemini_error") from exc

try:
    classification = _parse_semantic_classification(raw)
    classification = _reconcile_semantic_urls(message, classification)
except ValueError as exc:
    raise SemanticClassificationError("invalid_semantic_response") from exc
```

Do not add a broad catch around the remainder of `classify_email()`. Unexpected programming errors should propagate to the existing per-message sync error path rather than be mislabeled as user review.

Keep only the genuine ambiguity conversion:

```python
if (
    classification.kind == "REVIEW_NEEDED"
    or classification.confidence < AUTO_CONFIDENCE_THRESHOLD
):
    return _review_needed("semantic classification requires review")
return classification
```

- [ ] **Step 4: Run the focused classifier tests**

Run:

```bash
pytest tests/test_gmail_classifier.py -k "malformed_semantic_json or unsupported_semantic_kind or gemini_failure or low_confidence" -v
```

Expected: PASS.

- [ ] **Step 5: Commit the semantic failure boundary**

```bash
git add src/job_hunter/gmail_classifier.py tests/test_gmail_classifier.py
git commit -m "fix: separate Gmail semantic failures from review"
```

---

### Task 2: Normalize Harmless Semantic Output Variations

**Files:**
- Modify: `src/job_hunter/gmail_classifier.py`
- Test: `tests/test_gmail_classifier.py`

**Interfaces:**
- Consumes: `SemanticClassificationError` from Task 1.
- Produces: tolerant `_parse_semantic_classification(raw: str) -> GmailClassification` that still validates the fields needed for safe decisions.
- Produces: `_reconcile_semantic_urls(message, classification)` that removes untrusted URLs instead of invalidating a valid lifecycle classification.

- [ ] **Step 1: Add regression tests for irrelevant and nullable semantic output**

Add:

```python
def test_semantic_irrelevant_discards_harmless_extracted_metadata():
    result = classify_email(
        message("Special offer", "A consumer promotion unrelated to jobs."),
        FakeGemini(
            semantic_response(
                kind="IRRELEVANT",
                confidence=0.98,
                company="Example Brand",
                role_title="",
                source_job_id=None,
                job_urls=[],
                jobs=[],
                rationale="Consumer marketing email.",
            )
        ),
    )

    assert result.kind == "IRRELEVANT"
    assert result.company == ""
    assert result.role_title == ""
    assert result.job_urls == []
    assert result.jobs == []


def test_semantic_optional_extraction_fields_may_be_null_or_missing():
    import json

    response = json.dumps(
        {
            "kind": "INTERVIEW",
            "confidence": 0.96,
            "company": None,
            "role_title": None,
            "rationale": "Scheduling an interview.",
        }
    )

    result = classify_email(
        message("Interview scheduling", "Can we find a time for an interview?"),
        FakeGemini(response),
    )

    assert result.kind == "INTERVIEW"
    assert result.company == ""
    assert result.role_title == ""
    assert result.source_job_id is None
    assert result.job_urls == []
    assert result.jobs == []
```

- [ ] **Step 2: Add a regression test for an invented semantic URL on a valid lifecycle classification**

```python
def test_semantic_lifecycle_drops_url_not_present_in_email():
    result = classify_email(
        message("Interview scheduling", "Can we find a time for an interview?"),
        FakeGemini(
            semantic_response(
                kind="INTERVIEW",
                confidence=0.97,
                company="Acme",
                role_title="Frontend Engineer",
                job_urls=["https://jobs.acme.example/invented"],
                jobs=[
                    {
                        "source_platform": "acme",
                        "source_job_id": None,
                        "url": "https://jobs.acme.example/invented",
                        "company": "Acme",
                        "title": "Frontend Engineer",
                        "location": "",
                        "remote": None,
                        "description": "",
                    }
                ],
                rationale="Interview scheduling message.",
            )
        ),
    )

    assert result.kind == "INTERVIEW"
    assert result.job_urls == []
    assert result.jobs[0].url == ""
```

- [ ] **Step 3: Add a regression test for generic job alerts with no safe candidate URL**

```python
def test_generic_job_alert_with_no_safe_candidate_remains_job_alert():
    result = classify_email(
        message(
            "Job alert",
            "A new role matches your saved search.",
            sender="alerts@talentboard.example",
        ),
        FakeGemini(
            semantic_response(
                kind="JOB_ALERT",
                company="",
                role_title="",
                source_job_id=None,
                job_urls=[],
                jobs=[],
                rationale="Saved-search job alert.",
            )
        ),
    )

    assert result.kind == "JOB_ALERT"
    assert result.jobs == []
```

- [ ] **Step 4: Run these tests and verify current strict validation fails**

Run:

```bash
pytest tests/test_gmail_classifier.py -k "irrelevant_discards or optional_extraction or drops_url or no_safe_candidate" -v
```

Expected: FAIL under the current exact-key/null/conflict rules.

- [ ] **Step 5: Relax only optional extraction parsing**

Replace the exact top-level-key requirement with required decision fields:

```python
_REQUIRED_CLASSIFICATION_FIELDS = frozenset({"kind", "confidence", "rationale"})
```

In `_parse_semantic_classification()`:

```python
if not isinstance(data, dict):
    raise ValueError("response must be an object")
if not _REQUIRED_CLASSIFICATION_FIELDS.issubset(data):
    raise ValueError("response missing required classification fields")
```

Add helpers that normalize missing/null optional fields:

```python
def _optional_text(data: dict, key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value.strip()


def _optional_nullable_text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value.strip() or None
```

Allow `job_urls`/`jobs` to default to empty lists when missing or null.

- [ ] **Step 6: Make `IRRELEVANT` terminal before extraction validation**

After parsing valid `kind`, `confidence`, and `rationale`, return immediately for irrelevant mail:

```python
if kind == "IRRELEVANT":
    return GmailClassification(
        kind="IRRELEVANT",
        confidence=float(confidence),
        rationale=rationale,
    )
```

Remove the later rule that rejects irrelevant classifications containing extraction metadata.

- [ ] **Step 7: Filter untrusted semantic URLs instead of rejecting the full classification**

Refactor `_reconcile_semantic_urls()` so it uses only URLs present in `_message_urls(message)`:

```python
message_urls = set(_message_urls(message))

job_urls = [
    url for url in classification.job_urls
    if url in message_urls
]

jobs = [
    replace(job, url="")
    if job.url and job.url not in message_urls
    else job
    for job in classification.jobs
]
```

Then merge `_known_jobs(message)` as today. Remove the hard errors for semantic URL presence/conflict that can be resolved safely by filtering.

For `IRRELEVANT`, return before calling `_reconcile_semantic_urls()`.

Remove the `generic job alert extraction returned no usable jobs` exception. A `JOB_ALERT` with zero staged candidates is valid and silent.

- [ ] **Step 8: Run all classifier tests**

Run:

```bash
pytest tests/test_gmail_classifier.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit semantic normalization**

```bash
git add src/job_hunter/gmail_classifier.py tests/test_gmail_classifier.py
git commit -m "fix: normalize Gmail semantic classification output"
```

---

### Task 3: Count Semantic Failures as Sync Errors and Keep Them Retryable

**Files:**
- Modify: `src/job_hunter/gmail_sync.py`
- Test: `tests/test_gmail_sync.py`

**Interfaces:**
- Consumes: `SemanticClassificationError` from `job_hunter.gmail_classifier`.
- Preserves: `_process_message_ids(...) -> tuple[bool, int]`.
- Produces: privacy-safe semantic-failure log and `summary.errors += 1` without persisting the message.

- [ ] **Step 1: Add a failing sync test for technical failure semantics**

In `tests/test_gmail_sync.py`, add a fake Gemini that raises during semantic classification and a message that bypasses deterministic classification.

Test the persisted state:

```python
def test_semantic_classification_failure_is_error_not_review_and_remains_retryable(store):
    gmail = FakeGmail(
        messages={
            "m1": message(
                "Hiring conversation",
                "Can we discuss an engineering role?",
                message_id="m1",
            )
        }
    )
    service = GmailSyncService(
        gmail=gmail,
        gemini=FailingGemini(),
        store=store,
        backfill_batch_size=10,
    )

    summary = service.sync(NOW)

    assert summary.errors == 1
    assert summary.review_needed == 0
    assert store.has_processed_gmail_message("m1") is False
    assert store.pending_review_events() == []
```

Use the existing test fixtures/fake constructors in this file rather than introducing a second incompatible fake API.

- [ ] **Step 2: Run the test and verify failure**

Run:

```bash
pytest tests/test_gmail_sync.py -k "semantic_classification_failure_is_error" -v
```

Expected: FAIL until Task 1 behavior is wired through sync expectations.

- [ ] **Step 3: Add explicit semantic-failure handling in `_process_message_ids()`**

Import the exception:

```python
from job_hunter.gmail_classifier import (
    SemanticClassificationError,
    classify_email,
    source_candidate_key,
)
```

Before the existing generic `except Exception`, add:

```python
except SemanticClassificationError as exc:
    _LOGGER.warning(
        "gmail_semantic_classification_failed message_id=%s reason=%s",
        message_id,
        exc.reason,
    )
    summary.errors += 1
    had_hard_errors = True
    continue
```

Keep the existing generic exception branch for unexpected failures.

Do not call `record_gmail_message()` on this path. The missing processed marker is what preserves retry behavior.

- [ ] **Step 4: Add a retry assertion**

Extend the test so a second run with a valid semantic response processes the same message:

```python
service.gemini = FakeGemini(
    semantic_response(
        kind="IRRELEVANT",
        confidence=0.99,
        company="",
        role_title="",
        source_job_id=None,
        job_urls=[],
        jobs=[],
        rationale="Not job related.",
    )
)

second = service.sync(NOW)

assert second.errors == 0
assert store.has_processed_gmail_message("m1") is True
assert store.pending_review_events() == []
```

- [ ] **Step 5: Run sync tests**

Run:

```bash
pytest tests/test_gmail_sync.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit sync error semantics**

```bash
git add src/job_hunter/gmail_sync.py tests/test_gmail_sync.py
git commit -m "fix: retry Gmail semantic processing failures"
```

---

### Task 4: Narrow Historical Gmail Intake

**Files:**
- Modify: `src/job_hunter/gmail_sync.py`
- Modify: `src/job_hunter/gmail_classifier.py`
- Test: `tests/test_gmail_sync.py`
- Test: `tests/test_gmail_classifier.py`

**Interfaces:**
- Produces: higher-precision `build_backfill_query(now)`.
- Produces: `is_probably_job_related(message)` that no longer treats bare `offer` or `position` as sufficient job signals.

- [ ] **Step 1: Add query precision tests**

In `tests/test_gmail_sync.py`:

```python
def test_backfill_query_uses_job_specific_offer_phrases():
    query = build_backfill_query(NOW)

    assert '"job offer"' in query
    assert '"offer letter"' in query
    assert '"thanks for applying"' in query
    assert '"received your application"' in query
    assert " position " not in f" {query} "
    assert " offer " not in f" {query} "
```

- [ ] **Step 2: Add local relevance-gate tests**

In `tests/test_gmail_classifier.py`:

```python
@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Special offer", "Save 20% on your next purchase."),
        ("Your position in the queue", "Your order is being processed."),
    ],
)
def test_bare_marketing_offer_or_position_is_not_probably_job_related(subject, body):
    assert is_probably_job_related(message(subject, body)) is False
```

Keep a positive test:

```python
def test_job_offer_phrase_is_probably_job_related():
    assert is_probably_job_related(
        message("Job offer", "We would like to discuss your job offer.")
    ) is True
```

- [ ] **Step 3: Run the new precision tests and verify current failure**

Run:

```bash
pytest tests/test_gmail_sync.py -k "backfill_query_uses_job_specific" -v
pytest tests/test_gmail_classifier.py -k "marketing_offer_or_position or job_offer_phrase" -v
```

Expected: FAIL because current rules contain bare `offer`/`position`.

- [ ] **Step 4: Replace `_QUERY_TERMS` with high-signal phrases**

Use:

```python
_QUERY_TERMS = (
    '{application interview recruiter hiring "job alert" '
    '"technical assessment" "coding challenge" "job offer" '
    '"offer letter" "thanks for applying" "received your application"}'
)
```

- [ ] **Step 5: Align `is_probably_job_related()`**

Use strong terms that carry job context:

```python
strong_terms = (
    "application",
    "interview",
    "recruiter",
    "hiring",
    "job alert",
    "job offer",
    "offer letter",
    "technical assessment",
    "coding challenge",
    "thanks for applying",
    "received your application",
)
```

Do not include bare `offer` or `position`.

- [ ] **Step 6: Run classifier and sync tests**

Run:

```bash
pytest tests/test_gmail_classifier.py tests/test_gmail_sync.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit intake precision changes**

```bash
git add src/job_hunter/gmail_sync.py src/job_hunter/gmail_classifier.py tests/test_gmail_sync.py tests/test_gmail_classifier.py
git commit -m "fix: narrow Gmail historical job signal intake"
```

---

### Task 5: Release Legacy Synthetic Review Failures for Reprocessing

**Files:**
- Modify: `src/job_hunter/gmail_models.py`
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/gmail_sync.py`
- Test: `tests/test_store.py`
- Test: `tests/test_gmail_sync.py`

**Interfaces:**
- Produces: `LEGACY_SEMANTIC_FAILURE_RATIONALE` shared constant.
- Produces: `JobStore.release_legacy_gmail_semantic_failures() -> int`.
- Preserves: all meaningful application/review rows.

- [ ] **Step 1: Add the exact legacy rationale constant**

In `src/job_hunter/gmail_models.py`:

```python
LEGACY_SEMANTIC_FAILURE_RATIONALE = (
    "semantic classification unavailable or invalid"
)
```

Use this exact string only for cleanup matching; do not reuse it for new classification behavior.

- [ ] **Step 2: Add store tests for exact-match cleanup**

In `tests/test_store.py`, create one legacy synthetic review and one meaningful review, mark both delivered, then assert only the legacy row is removed:

```python
def test_release_legacy_gmail_semantic_failures_removes_only_technical_artifacts(store):
    legacy_event_id = _record_review_event(
        store,
        message_id="legacy",
        rationale=LEGACY_SEMANTIC_FAILURE_RATIONALE,
    )
    real_event_id = _record_review_event(
        store,
        message_id="real-review",
        rationale="ambiguous scheduling language",
    )
    store.mark_review_delivered([legacy_event_id, real_event_id], "msg-1")

    released = store.release_legacy_gmail_semantic_failures()

    assert released == 1
    assert store.has_processed_gmail_message("legacy") is False
    assert store.has_processed_gmail_message("real-review") is True
    assert [row["id"] for row in store.pending_review_events()] == []
    remaining = store._conn.execute(
        "SELECT id, rationale FROM application_events ORDER BY id"
    ).fetchall()
    assert [(row["id"], row["rationale"]) for row in remaining] == [
        (real_event_id, "ambiguous scheduling language")
    ]
```

Add idempotency:

```python
assert store.release_legacy_gmail_semantic_failures() == 0
```

Adapt helper names to the existing `tests/test_store.py` fixtures, keeping the exact assertions.

- [ ] **Step 3: Run the store test and verify failure**

Run:

```bash
pytest tests/test_store.py -k "release_legacy_gmail_semantic_failures" -v
```

Expected: FAIL because the store method does not exist.

- [ ] **Step 4: Implement narrow transactional cleanup**

In `src/job_hunter/store.py`:

```python
def release_legacy_gmail_semantic_failures(self) -> int:
    rationale = LEGACY_SEMANTIC_FAILURE_RATIONALE
    with self._conn:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM gmail_messages
            WHERE classification = 'REVIEW_NEEDED'
              AND rationale = ?
            """,
            (rationale,),
        ).fetchone()
        released = int(row["count"])
        if released == 0:
            return 0

        self._conn.execute(
            """
            DELETE FROM review_deliveries
            WHERE event_id IN (
                SELECT id FROM application_events
                WHERE event_type = 'REVIEW_NEEDED'
                  AND rationale = ?
            )
            """,
            (rationale,),
        )
        self._conn.execute(
            """
            DELETE FROM application_events
            WHERE event_type = 'REVIEW_NEEDED'
              AND rationale = ?
            """,
            (rationale,),
        )
        self._conn.execute(
            """
            DELETE FROM gmail_messages
            WHERE classification = 'REVIEW_NEEDED'
              AND rationale = ?
            """,
            (rationale,),
        )
    return released
```

Import the shared constant from `gmail_models`.

Do not delete rows based only on `event_type = REVIEW_NEEDED`.

- [ ] **Step 5: Run the store tests**

Run:

```bash
pytest tests/test_store.py -v
```

Expected: PASS.

- [ ] **Step 6: Add sync orchestration test for reopening a completed backfill**

In `tests/test_gmail_sync.py`, set up:

1. completed `gmail_sync_state`;
2. one exact legacy semantic-failure row;
3. Gmail search returning that message ID;
4. a valid classifier result on reprocessing.

Assert:

```python
summary = service.sync(NOW)

assert summary.processed == 1
assert store.has_processed_gmail_message("legacy") is True
assert store.get_gmail_sync_state("account-1")["backfill_completed_at"] is not None
```

Also assert a dry run performs no legacy cleanup.

- [ ] **Step 7: Invoke cleanup at the beginning of writable sync**

In `GmailSyncService.sync()` after loading account/state and only when `dry_run is False`:

```python
released_legacy_failures = self.store.release_legacy_gmail_semantic_failures()
if released_legacy_failures:
    _LOGGER.info(
        "gmail_legacy_semantic_failures_released count=%s",
        released_legacy_failures,
    )
    if state is not None and state["backfill_completed_at"] is not None:
        self.store.save_gmail_sync_state(
            account_id=account_id,
            history_id=state["history_id"],
            last_successful_sync_at=state["last_successful_sync_at"],
            backfill_completed_at=None,
        )
        state = self.store.get_gmail_sync_state(account_id)
```

Then calculate `backfill_pending` from the refreshed `state`.

The cleanup must execute before the `backfill_pending` decision.

- [ ] **Step 8: Run store and sync tests**

Run:

```bash
pytest tests/test_store.py tests/test_gmail_sync.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit legacy repair**

```bash
git add src/job_hunter/gmail_models.py src/job_hunter/store.py src/job_hunter/gmail_sync.py tests/test_store.py tests/test_gmail_sync.py
git commit -m "fix: requeue legacy Gmail semantic failures"
```

---

### Task 6: Prove Technical Failures Cannot Reach Telegram Review Delivery

**Files:**
- Modify: `tests/test_pipeline.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: corrected sync persistence contract from Tasks 1-5.
- Produces: end-to-end regression around shared SQLite state and Telegram review delivery.

- [ ] **Step 1: Add an integration-style regression test**

Use the same temporary SQLite store for a failed Gmail sync and the normal pipeline.

The test should assert this sequence:

```python
sync_summary = failing_sync_service.sync(NOW)
assert sync_summary.errors == 1
assert sync_summary.review_needed == 0

telegram = FakeTelegram()
run_pipeline(
    settings,
    sources=[],
    store=store,
    gemini=FakeGemini(),
    telegram=telegram,
)

assert not any("Gmail review needed" in text for text in telegram.messages)
```

Keep the existing pipeline test proving a real review event is still delivered.

- [ ] **Step 2: Run the focused pipeline Gmail review tests**

Run:

```bash
pytest tests/test_pipeline.py -k "gmail_review or semantic_failure" -v
```

Expected: PASS after prior tasks.

- [ ] **Step 3: Update README operator behavior**

In the Gmail intelligence section, document these exact semantics:

```text
Semantic API/parser failures are treated as retryable Gmail sync errors, not manual-review events. They increase the Gmail error count and leave the message unprocessed so a later sync can retry it. Telegram review items are reserved for genuinely ambiguous classifications or job matches.
```

Also state that historical Gmail search uses job-specific phrases to reduce unrelated marketing mail.

- [ ] **Step 4: Commit the regression and docs**

```bash
git add tests/test_pipeline.py README.md
git commit -m "test: prevent Gmail technical failures from review delivery"
```

---

### Task 7: Full Verification

**Files:**
- Verify only; no new implementation unless a failing test reveals a defect in this plan's scope.

**Interfaces:**
- Validates all tasks against the design spec.

- [ ] **Step 1: Run the Gmail-focused test suite**

Run:

```bash
pytest \
  tests/test_gmail_classifier.py \
  tests/test_gmail_sync.py \
  tests/test_store.py \
  tests/test_pipeline.py \
  tests/test_telegram.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Run whitespace/diff validation**

Run:

```bash
git diff --check main...HEAD
```

Expected: no output and exit code 0.

- [ ] **Step 4: Verify no implementation accidentally changes safety boundaries**

Run:

```bash
git diff --name-only main...HEAD
```

Expected implementation files are limited to the files listed in this plan plus the spec/plan docs. Confirm there are no Gmail write APIs, LinkedIn browser automation changes, Supabase migration changes, or application-submission changes.

- [ ] **Step 5: Verify the legacy string is cleanup-only**

Run:

```bash
grep -R "semantic classification unavailable or invalid" -n src tests
```

Expected: runtime usage is limited to the shared legacy cleanup constant and cleanup-focused tests. The classifier must not emit this rationale for new messages.

- [ ] **Step 6: Final commit if verification required only test/doc corrections**

If verification exposed a plan-scoped test/doc correction, commit only that correction with a descriptive `fix:` or `test:` message. Otherwise do not create an empty commit.

---

## Expected Post-Fix Production Signals

On the next normal workflow run after merge:

- Existing exact-match legacy technical reviews are released once.
- The bounded backfill reprocesses those message IDs over normal batches.
- `gmail_review_needed` drops sharply and represents actual ambiguity.
- `gmail_irrelevant` increases for non-job marketing mail that still reaches semantic classification.
- `gmail_errors` reflects real provider/parser failures instead of hiding them as reviews.
- Telegram stops receiving repeated `Unknown company — Unknown role | semantic classification unavailable or invalid` lines.
- Genuine ambiguous interview/recruiter/application events continue to appear once in Telegram review delivery.

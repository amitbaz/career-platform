# Gmail Activity Review Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent non-job Gmail promotions from becoming lifecycle events and replace the confusing `Gmail review needed` Telegram output with a clear, informational Gmail activity summary that preserves the original event type and links back to Gmail.

**Architecture:** Keep the existing Gmail sync, SQLite event store, and Telegram delivery pipeline. Tighten Gmail candidate search and classification at the source, preserve original lifecycle event types in `application_events`, derive review eligibility from matching/confidence instead of overwriting the event type, and render user-facing Gmail activity copy from that preserved event type.

**Tech Stack:** Python 3.12, pytest, SQLite, Gmail API search semantics, Telegram Bot API.

**Spec:** `docs/superpowers/specs/2026-09-01-gmail-activity-review-design.md`

## Global Constraints

- Do not build `Ignore`, `Track job`, or `Match to job` Telegram actions in this change.
- Do not migrate Gmail state to Supabase.
- Do not change job ranking, evaluation, or the existing job navigator behavior.
- Do not put Gmail activity items into the job carousel.
- Do not expose deterministic/semantic classifier rationale in user-facing Telegram messages.
- Preserve legacy `REVIEW_NEEDED` application-event rows.
- Keep the job navigator as the final Telegram message in a run.

---

### Task 1: Tighten Gmail search and classification boundaries

**Files:**
- Modify: `tests/test_gmail_classifier.py`
- Modify: `tests/test_gmail_sync.py`
- Modify: `src/job_hunter/gmail_classifier.py`
- Modify: `src/job_hunter/gmail_sync.py`

**Interfaces:**
- Consumes: `classify_email(message: GmailMessage, gemini: GeminiClient) -> GmailClassification`, `is_probably_job_related(message: GmailMessage) -> bool`, `build_backfill_query(now: datetime) -> str`.
- Produces: non-job promotions classified as `IRRELEVANT`; employment-specific offer phrases remain `OFFER`; Gmail search no longer contains bare `offer`.

- [ ] **Step 1: Add the false-positive classifier regression test**

Add to `tests/test_gmail_classifier.py`:

```python
def test_non_job_pleased_to_offer_promotion_is_irrelevant():
    gemini = FakeGemini(semantic_response(kind="IRRELEVANT", rationale="promotion"))

    result = classify_email(
        message(
            "Travel together, Save up to 15%!",
            "Vietnam Airlines is pleased to offer exclusive fares for friends and family.",
            sender="no-reply@e-news.vietnamairlines.com",
        ),
        gemini,
    )

    assert result.kind == "IRRELEVANT"
    assert gemini.calls == []
```

This proves the message is rejected before semantic classification and cannot become an `OFFER` review artifact.

- [ ] **Step 2: Add positive regressions for real employment offers and recruiter activity**

Add:

```python
def test_employment_offer_phrase_remains_deterministic_offer():
    result = classify_email(
        message(
            "Your offer",
            "We are pleased to offer you the position of Senior Frontend Engineer.",
        ),
        FakeGemini(semantic_response()),
    )

    assert result.kind == "OFFER"


def test_linkedin_recruiter_message_remains_recruiter_activity():
    result = classify_email(
        message(
            "Ofer just messaged you",
            "1 new message awaits your response. Senior Talent Acquisition Manager | IT recruiter",
            sender="messaging-digest-noreply@linkedin.com",
        ),
        FakeGemini(semantic_response()),
    )

    assert result.kind == "RECRUITER_CONTACT"
```

- [ ] **Step 3: Update Gmail query expectations to reject bare `offer`**

In `tests/test_gmail_sync.py`, replace expected query fragments that currently end with:

```text
"technical assessment" "coding challenge" offer}
```

with:

```text
"technical assessment" "coding challenge" "job offer" "offer letter" "offer of employment" "pleased to offer you"}
```

Cover both the initial 12-month backfill assertion and the expired-history overlap assertion.

- [ ] **Step 4: Run focused tests and verify RED**

Run:

```bash
pytest -q tests/test_gmail_classifier.py \
  tests/test_gmail_sync.py::test_first_sync_scans_12_months_and_marks_backfill_complete \
  tests/test_gmail_sync.py::test_expired_history_uses_one_day_overlap_search
```

Expected: the Vietnam Airlines regression fails because deterministic classification currently runs before the job-related guard, and query assertions fail because `_QUERY_TERMS` still contains bare `offer`.

- [ ] **Step 5: Tighten the job-related signal set**

In `src/job_hunter/gmail_classifier.py`, keep the existing strong terms and add employment-specific offer signals:

```python
strong_terms = (
    "application",
    "interview",
    "recruiter",
    "hiring",
    "job alert",
    "job offer",
    "offer letter",
    "offer of employment",
    "offer you the position",
    "pleased to offer you the position",
    "technical assessment",
    "coding challenge",
    "thanks for applying",
    "received your application",
)
```

Do not add bare `offer` or bare `pleased to offer`.

- [ ] **Step 6: Tighten deterministic offer matching**

Replace the broad offer condition:

```python
if "offer you the position" in text or "pleased to offer" in text:
```

with employment-specific signals:

```python
offer_signals = (
    "offer you the position",
    "pleased to offer you the position",
    "offer of employment",
    "offer letter",
)
if any(signal in text for signal in offer_signals):
    lifecycle_matches.append(("OFFER", "deterministic offer template"))
```

The internal rationale may remain for logs/storage; later tasks ensure it is not rendered to Telegram.

- [ ] **Step 7: Gate deterministic classification behind job-related detection**

At the start of `classify_email`, make the non-job guard run before deterministic lifecycle classification:

```python
def classify_email(message: GmailMessage, gemini: GeminiClient) -> GmailClassification:
    if not is_probably_job_related(message):
        return GmailClassification(
            kind="IRRELEVANT",
            confidence=1.0,
            rationale="no deterministic job signal",
        )

    deterministic = classify_deterministically(message)
    # existing extract_job_alert / semantic fallback logic follows
```

Remove the later duplicate `is_probably_job_related` guard so there is only one boundary.

- [ ] **Step 8: Tighten the Gmail search query**

In `src/job_hunter/gmail_sync.py`, change `_QUERY_TERMS` to:

```python
_QUERY_TERMS = (
    '{application interview recruiter hiring "job alert" position '
    '"technical assessment" "coding challenge" "job offer" '
    '"offer letter" "offer of employment" "pleased to offer you"}'
)
```

- [ ] **Step 9: Run focused tests and verify GREEN**

Run the command from Step 4.

Expected: all focused tests pass; the promotion is `IRRELEVANT`, real employment offer/recruiter cases remain classified, and Gmail search contains no bare `offer` term.

- [ ] **Step 10: Commit**

```bash
git add tests/test_gmail_classifier.py tests/test_gmail_sync.py \
  src/job_hunter/gmail_classifier.py src/job_hunter/gmail_sync.py
git commit -m "fix: tighten Gmail lifecycle classification"
```

---

### Task 2: Preserve lifecycle event type and derive review eligibility

**Files:**
- Modify: `tests/test_gmail_sync.py`
- Modify: `tests/test_store.py`
- Modify: `src/job_hunter/gmail_sync.py`
- Modify: `src/job_hunter/store.py`

**Interfaces:**
- Consumes: `_match_classification(...) -> tuple[GmailClassification, int | None]`, `save_application_event(...)`, `pending_review_events() -> list[sqlite3.Row]`, `AUTO_CONFIDENCE_THRESHOLD`.
- Produces: original lifecycle `event_type` persisted even when effective classification becomes `REVIEW_NEEDED`; pending review rows derived from unresolved/low-confidence lifecycle events plus legacy `REVIEW_NEEDED` rows.

- [ ] **Step 1: Update unresolved and low-confidence persistence tests**

In `tests/test_gmail_sync.py`, change the existing assertions so lifecycle type is preserved:

```python
assert (event["job_id"], event["event_type"]) == (None, "INTERVIEW")
```

for unresolved/ambiguous interview cases, and:

```python
assert (event["job_id"], event["event_type"]) == (job_id, "INTERVIEW")
```

for the low-confidence interview case.

Keep `summary.review_needed == 1` in all three cases; the effective sync classification still represents that the item requires attention.

- [ ] **Step 2: Add store-level pending-review selection coverage**

In `tests/test_store.py`, import `AUTO_CONFIDENCE_THRESHOLD` and add one test that creates four Gmail/application rows:

```python
def test_pending_review_events_are_derived_from_linkage_and_confidence(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(
        Job(source="test", title="Frontend Engineer", company="Acme")
    )

    cases = [
        ("unresolved", None, "RECRUITER_CONTACT", 1.0, True),
        ("low-confidence", job_id, "INTERVIEW", AUTO_CONFIDENCE_THRESHOLD - 0.01, True),
        ("resolved", job_id, "INTERVIEW", AUTO_CONFIDENCE_THRESHOLD, False),
        ("legacy-review", None, "REVIEW_NEEDED", 1.0, True),
    ]

    for message_id, linked_job_id, event_type, confidence, _expected in cases:
        store.record_gmail_message(
            message_id=message_id,
            thread_id=f"thread-{message_id}",
            sender="jobs@example.com",
            subject=f"subject-{message_id}",
            occurred_at="2026-09-01T10:00:00+00:00",
            classification="REVIEW_NEEDED",
            confidence=confidence,
            rationale="fixture",
        )
        store.save_application_event(
            job_id=linked_job_id,
            event_type=event_type,
            occurred_at="2026-09-01T10:00:00+00:00",
            source_message_id=message_id,
            source_thread_id=f"thread-{message_id}",
            confidence=confidence,
            company="Acme",
            role_title="Frontend Engineer",
            rationale="fixture",
        )

    pending_ids = {row["source_message_id"] for row in store.pending_review_events()}
    assert pending_ids == {"unresolved", "low-confidence", "legacy-review"}
```

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
pytest -q \
  tests/test_gmail_sync.py::test_unresolved_lifecycle_event_is_review_needed_without_job_association \
  tests/test_gmail_sync.py::test_ambiguous_lifecycle_event_is_review_needed_without_job_association \
  tests/test_gmail_sync.py::test_low_confidence_lifecycle_event_is_review_needed \
  tests/test_store.py::test_pending_review_events_are_derived_from_linkage_and_confidence
```

Expected: persistence tests fail because `_save_event` receives the effective `REVIEW_NEEDED` classification, and the store test fails because `pending_review_events` only selects `event_type='REVIEW_NEEDED'`.

- [ ] **Step 4: Persist the original lifecycle classification**

In `GmailSyncService.process_message`, retain effective matching behavior but save the original lifecycle classification:

```python
if classification.kind in _LIFECYCLE_KINDS:
    effective, job_id = self._match_classification(message, classification)
    self._save_event(message, classification, job_id)
elif classification.kind == "REVIEW_NEEDED":
    self._save_event(message, classification, None)
```

Do not change the later `record_gmail_message(... classification=effective.kind ...)` call; the sync/message record can still say `REVIEW_NEEDED` while the application event preserves what was actually detected.

- [ ] **Step 5: Derive pending review eligibility in the store**

In `src/job_hunter/store.py`, import `AUTO_CONFIDENCE_THRESHOLD` and replace the current `WHERE e.event_type = 'REVIEW_NEEDED'` predicate with:

```sql
WHERE d.event_id IS NULL
  AND (
      e.event_type = 'REVIEW_NEEDED'
      OR (
          e.event_type IN (
              'RECRUITER_CONTACT', 'APPLIED', 'INTERVIEW',
              'TECHNICAL', 'OFFER', 'REJECTED'
          )
          AND (
              e.job_id IS NULL
              OR e.confidence < ?
          )
      )
  )
ORDER BY e.occurred_at, e.id
```

Pass `(AUTO_CONFIDENCE_THRESHOLD,)` to `.execute(...)`.

Keep `SELECT e.*, m.subject` so `event_type` and `source_message_id` naturally remain available to the pipeline.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the command from Step 3.

Expected: all four tests pass.

- [ ] **Step 7: Verify application-state safety**

Run the existing Gmail matching/application-state tests:

```bash
pytest -q tests/test_gmail_matching.py tests/test_gmail_sync.py -k "lifecycle or application_state or confidence"
```

Expected: low-confidence or unresolved events do not change tracked application state because state derivation still requires a non-null `job_id` and confidence at or above `AUTO_CONFIDENCE_THRESHOLD`.

- [ ] **Step 8: Commit**

```bash
git add tests/test_gmail_sync.py tests/test_store.py \
  src/job_hunter/gmail_sync.py src/job_hunter/store.py
git commit -m "fix: preserve Gmail lifecycle review context"
```

---

### Task 3: Replace debug review output with user-facing Gmail activity

**Files:**
- Modify: `tests/test_telegram.py`
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/telegram.py`
- Modify: `src/job_hunter/pipeline.py`

**Interfaces:**
- Consumes: `ReviewItem`, `build_gmail_review_digest(items)`, `build_gmail_review_digest_chunks(items)`, rows from `pending_review_events()`.
- Produces: `ReviewItem.event_type`, `ReviewItem.source_message_id`, user-facing activity copy, and a Gmail deep link for each item.

- [ ] **Step 1: Extend the review fixture contract in tests**

In `tests/test_telegram.py`, update `_review_item` defaults to include:

```python
event_type="RECRUITER_CONTACT",
source_message_id="gmail-message-1",
```

Keep `rationale` in the fixture so the test can prove it is not exposed.

- [ ] **Step 2: Replace old review-digest expectations with user-facing activity expectations**

Replace the current `Gmail review needed` assertions with tests equivalent to:

```python
def test_build_gmail_review_digest_uses_user_facing_activity_copy():
    digest = build_gmail_review_digest(
        [
            _review_item(
                company="Montash",
                role_title="Senior Frontend Engineer",
                rationale="deterministic recruiter template",
            )
        ]
    )

    assert digest == (
        "Gmail activity I couldn't link\n\n"
        "Montash — Senior Frontend Engineer\n"
        "A recruiter contacted you, but I couldn't link this email to a tracked job.\n"
        "Open email: https://mail.google.com/mail/#all/gmail-message-1"
    )
    assert "deterministic recruiter template" not in digest
```

Add event-copy coverage for `APPLIED`, `INTERVIEW`, `TECHNICAL`, `OFFER`, `REJECTED`, and `REVIEW_NEEDED`.

- [ ] **Step 3: Add identity-fallback coverage**

Add:

```python
def test_gmail_activity_identity_falls_back_to_company_event_label_then_subject():
    digest = build_gmail_review_digest(
        [
            _review_item(
                event_id=1,
                company="Supabase",
                role_title="",
                event_type="REVIEW_NEEDED",
                subject="Supabase product update",
                source_message_id="m-company",
            ),
            _review_item(
                event_id=2,
                company="",
                role_title="",
                event_type="INTERVIEW",
                subject="Interview details inside",
                source_message_id="m-subject",
                occurred_at="2026-08-31T11:00:00+00:00",
            ),
        ]
    )

    assert "Supabase — Job-related activity" in digest
    assert "Interview details inside" in digest
```

- [ ] **Step 4: Run Telegram tests and verify RED**

Run:

```bash
pytest -q tests/test_telegram.py -k "gmail"
```

Expected: failures because `ReviewItem` does not yet have the new fields and Telegram still renders rationale/debug copy.

- [ ] **Step 5: Extend `ReviewItem` without removing persisted diagnostic data**

In `src/job_hunter/models.py`, update the dataclass to:

```python
@dataclass(slots=True)
class ReviewItem:
    """Compact, privacy-minimized representation of unresolved Gmail activity."""

    event_id: int
    company: str
    role_title: str
    occurred_at: str
    subject: str
    rationale: str
    event_type: str
    source_message_id: str
```

Retain `rationale` for internal compatibility, but Telegram must no longer render it.

- [ ] **Step 6: Add user-facing event copy helpers**

In `src/job_hunter/telegram.py`, replace `_REVIEW_HEADER` with:

```python
_REVIEW_HEADER = "Gmail activity I couldn't link"
```

Add mappings:

```python
_GMAIL_ACTIVITY_COPY = {
    "RECRUITER_CONTACT": "A recruiter contacted you, but I couldn't link this email to a tracked job.",
    "APPLIED": "This looks like an application confirmation, but I couldn't link it to a tracked job.",
    "INTERVIEW": "This looks like an interview email, but I couldn't link it to a tracked job.",
    "TECHNICAL": "This looks like a technical assessment, but I couldn't link it to a tracked job.",
    "OFFER": "This looks like a job offer, but I couldn't link it to a tracked job.",
    "REJECTED": "This looks like a rejection email, but I couldn't link it to a tracked job.",
    "REVIEW_NEEDED": "This looks job-related, but I couldn't classify or link it confidently.",
}

_GMAIL_ACTIVITY_LABEL = {
    "RECRUITER_CONTACT": "Recruiter contact",
    "APPLIED": "Application update",
    "INTERVIEW": "Interview",
    "TECHNICAL": "Technical assessment",
    "OFFER": "Job offer",
    "REJECTED": "Rejection",
    "REVIEW_NEEDED": "Job-related activity",
}
```

Use the `REVIEW_NEEDED` value as the fallback for any unexpected/legacy event type.

- [ ] **Step 7: Render one activity block per event**

Replace `_gmail_review_line` with an item-block helper:

```python
def _gmail_review_block(item: ReviewItem) -> str:
    event_type = item.event_type if item.event_type in _GMAIL_ACTIVITY_COPY else "REVIEW_NEEDED"
    if item.company and item.role_title:
        identity = f"{item.company} — {item.role_title}"
    elif item.company:
        identity = f"{item.company} — {_GMAIL_ACTIVITY_LABEL[event_type]}"
    else:
        identity = item.subject or "Gmail message"

    gmail_url = f"https://mail.google.com/mail/#all/{item.source_message_id}"
    return "\n".join(
        [
            identity,
            _GMAIL_ACTIVITY_COPY[event_type],
            f"Open email: {gmail_url}",
        ]
    )
```

Update `build_gmail_review_digest` to join blocks with blank lines:

```python
ordered = sorted(items, key=lambda item: (item.occurred_at, item.event_id))
return "\n\n".join([_REVIEW_HEADER, *(_gmail_review_block(item) for item in ordered)])
```

- [ ] **Step 8: Keep chunking event-atomic**

Update `build_gmail_review_digest_chunks` so it treats each `_gmail_review_block(item)` as one unit, accounts for `\n\n` separators, and never splits a single event across delivery acknowledgements. Preserve the existing `limit=3900` behavior and return shape `list[tuple[str, list[int]]]`.

Use this structure:

```python
ordered = sorted(items, key=lambda item: (item.occurred_at, item.event_id))
chunks = []
current_blocks = []
current_ids = []
current_length = len(_REVIEW_HEADER)

for item in ordered:
    block = _gmail_review_block(item)
    max_block_length = max(1, limit - len(_REVIEW_HEADER) - 2)
    if len(block) > max_block_length:
        block = block[: max_block_length - 1].rstrip() + "…"

    added_length = 2 + len(block)
    if current_blocks and current_length + added_length > limit:
        chunks.append(("\n\n".join([_REVIEW_HEADER, *current_blocks]), current_ids))
        current_blocks = []
        current_ids = []
        current_length = len(_REVIEW_HEADER)

    current_blocks.append(block)
    current_ids.append(item.event_id)
    current_length += 2 + len(block)

if current_blocks:
    chunks.append(("\n\n".join([_REVIEW_HEADER, *current_blocks]), current_ids))
return chunks
```

- [ ] **Step 9: Populate the new fields in the pipeline**

In `src/job_hunter/pipeline.py`, extend `ReviewItem(...)` construction:

```python
ReviewItem(
    event_id=row["id"],
    company=row["company"],
    role_title=row["role_title"],
    occurred_at=row["occurred_at"],
    subject=row["subject"],
    rationale=row["rationale"],
    event_type=row["event_type"],
    source_message_id=row["source_message_id"],
)
```

- [ ] **Step 10: Run focused tests and verify GREEN**

Run:

```bash
pytest -q tests/test_telegram.py -k "gmail"
```

Expected: Telegram Gmail activity tests pass, debug rationale is absent, and every item contains a Gmail deep link.

- [ ] **Step 11: Commit**

```bash
git add tests/test_telegram.py src/job_hunter/models.py \
  src/job_hunter/telegram.py src/job_hunter/pipeline.py
git commit -m "feat: clarify unresolved Gmail activity"
```

---

### Task 4: Verify pipeline delivery order and end-to-end regression behavior

**Files:**
- Modify: `tests/test_pipeline_navigator.py`
- No production files expected unless the regression exposes a missed integration edge.

**Interfaces:**
- Consumes: `run_pipeline(...)`, `pending_review_events()`, Telegram navigator delivery.
- Produces: proof that Gmail activity is delivered before the interactive job navigator and remains separate from carousel cards.

- [ ] **Step 1: Add call-order recording to the navigator fake**

Extend `NavigatorTelegram` in `tests/test_pipeline_navigator.py`:

```python
class NavigatorTelegram:
    def __init__(self, *, card_result="nav-msg-1"):
        self.cards = []
        self.messages = []
        self.documents = []
        self.events = []
        self.card_result = card_result

    def send_job_card(self, text, keyboard):
        self.events.append(("card", text))
        self.cards.append((text, keyboard))
        return self.card_result

    def send_message(self, text):
        self.events.append(("message", text))
        self.messages.append(text)
        return "msg-1"
```

- [ ] **Step 2: Add a pipeline integration test with one pending Gmail event and one job card**

Create a tracked source job as existing tests do, then seed an unresolved recruiter event:

```python
store.record_gmail_message(
    message_id="gmail-recruiter-1",
    thread_id="thread-gmail-recruiter-1",
    sender="recruiter@example.com",
    subject="Senior Frontend Engineer opportunity",
    occurred_at="2026-09-01T10:00:00+00:00",
    classification="REVIEW_NEEDED",
    confidence=1.0,
    rationale="deterministic recruiter template",
)
store.save_application_event(
    job_id=None,
    event_type="RECRUITER_CONTACT",
    occurred_at="2026-09-01T10:00:00+00:00",
    source_message_id="gmail-recruiter-1",
    source_thread_id="thread-gmail-recruiter-1",
    confidence=1.0,
    company="Montash",
    role_title="Senior Frontend Engineer",
    rationale="deterministic recruiter template",
)
```

After `run_pipeline(...)`, assert:

```python
assert telegram.events[0][0] == "message"
assert "Gmail activity I couldn't link" in telegram.events[0][1]
assert "deterministic recruiter template" not in telegram.events[0][1]
assert telegram.events[-1][0] == "card"
assert len(telegram.cards) == 1
```

Also assert the review delivery was marked so it does not resend on the next pipeline run.

- [ ] **Step 3: Run the integration test and verify GREEN**

Run:

```bash
pytest -q tests/test_pipeline_navigator.py
```

Expected: all navigator tests pass, including the new Gmail-activity ordering regression.

- [ ] **Step 4: Run the complete focused Gmail/Telegram suite**

Run:

```bash
pytest -q \
  tests/test_gmail_classifier.py \
  tests/test_gmail_sync.py \
  tests/test_gmail_matching.py \
  tests/test_store.py \
  tests/test_telegram.py \
  tests/test_pipeline_navigator.py
```

Expected: 0 failures.

- [ ] **Step 5: Run the full suite**

Run:

```bash
pytest -q
```

Expected: 0 failures.

- [ ] **Step 6: Review branch scope**

Run:

```bash
git diff --stat main...fix/gmail-review-ux-classifier
git diff main...fix/gmail-review-ux-classifier -- \
  src/job_hunter/gmail_classifier.py \
  src/job_hunter/gmail_sync.py \
  src/job_hunter/store.py \
  src/job_hunter/models.py \
  src/job_hunter/telegram.py \
  src/job_hunter/pipeline.py \
  tests/test_gmail_classifier.py \
  tests/test_gmail_sync.py \
  tests/test_store.py \
  tests/test_telegram.py \
  tests/test_pipeline_navigator.py
```

Confirm the branch contains only the approved Gmail classifier/review UX work plus its spec and plan.

- [ ] **Step 7: Commit any integration-test-only changes**

```bash
git add tests/test_pipeline_navigator.py
git commit -m "test: verify Gmail activity delivery order"
```

- [ ] **Step 8: Stop for integration choice**

Do not merge automatically. Present the completed branch, test evidence, and standard merge/PR options to the user.

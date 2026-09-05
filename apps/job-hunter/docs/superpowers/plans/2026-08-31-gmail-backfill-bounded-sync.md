# Bounded Gmail Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the initial 12-month Gmail backfill resumable and bounded so it cannot consume the entire GitHub Actions job and prevent the normal Job Hunter pipeline from running.

**Architecture:** Keep the existing SQLite-backed Gmail sync and repeated 12-month search. Limit each pending/forced backfill invocation to 100 previously unprocessed message attempts, rely on persisted processed-message IDs for cross-run progress, and mark the backfill complete only when there are no hard errors and no deferred unprocessed messages. Add a 10-minute GitHub Actions timeout specifically to the Gmail step as a final fail-open safety net.

**Tech Stack:** Python 3.12, SQLite, pytest, PyYAML, GitHub Actions, Gmail API, Gemini

**Spec:** `docs/superpowers/specs/2026-08-31-gmail-backfill-bounded-sync-design.md`

## Global Constraints

- Preserve the current SQLite backend; do not add Supabase or any database migration.
- Preserve the existing 12-month Gmail backfill query.
- Default backfill batch size is exactly `100` previously unprocessed message attempts per sync invocation.
- The Gmail GitHub Actions step timeout is exactly `10` minutes.
- Keep the overall workflow job timeout at `30` minutes.
- Keep `continue-on-error: true` on the Gmail step.
- Incremental Gmail History sync must remain uncapped by the backfill limit.
- Existing processed Gmail message IDs remain the resumability mechanism.
- Do not add Gmail write scopes or persist email bodies.

---

## File Structure

- `src/job_hunter/gmail_sync.py`
  - Owns the backfill batch limit, deferred-work detection, completion decision, and progress logging.
- `tests/test_gmail_sync.py`
  - Proves batching, resumability, completion, error behavior, forced-backfill behavior, and uncapped incremental sync.
- `.github/workflows/daily.yml`
  - Adds the dedicated 10-minute timeout to `Sync Gmail intelligence`.
- `tests/test_workflow.py`
  - Guards the Gmail step's timeout/fail-open workflow contract.
- `README.md`
  - Documents the resumable multi-run first backfill.

---

### Task 1: Bound and Resume the Historical Gmail Backfill

**Files:**
- Modify: `src/job_hunter/gmail_sync.py`
- Modify: `tests/test_gmail_sync.py`

**Interfaces:**
- Consumes: existing `GmailSyncService(gmail=..., gemini=..., store=...)`, `JobStore.has_processed_gmail_message(message_id)`, and existing Gmail state persistence.
- Produces: `GmailSyncService(..., backfill_batch_size: int = 100)` and `_process_message_ids(..., max_unprocessed: int | None = None) -> tuple[bool, int]`, where the tuple is `(had_hard_errors, deferred_unprocessed_count)`.

- [ ] **Step 1: Add a failing test proving a backfill processes only the configured number of unprocessed messages**

Add to `tests/test_gmail_sync.py`:

```python
def test_backfill_limits_unprocessed_messages_and_defers_remaining(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["m1", "m2"]
    assert summary.fetched == 3
    assert summary.processed == 2
    assert store.has_processed_gmail_message("m1") is True
    assert store.has_processed_gmail_message("m2") is True
    assert store.has_processed_gmail_message("m3") is False
    assert store.get_gmail_sync_state("candidate@example.com") is None
```

- [ ] **Step 2: Run the focused test and verify it fails because the constructor has no batch-size parameter yet**

Run:

```bash
pytest tests/test_gmail_sync.py::test_backfill_limits_unprocessed_messages_and_defers_remaining -v
```

Expected: FAIL with `TypeError` for unexpected keyword argument `backfill_batch_size`.

- [ ] **Step 3: Add the default batch size and constructor validation**

In `src/job_hunter/gmail_sync.py`, add near the module constants:

```python
DEFAULT_BACKFILL_BATCH_SIZE = 100
```

Change the service constructor to:

```python
class GmailSyncService:
    def __init__(
        self,
        *,
        gmail,
        gemini,
        store,
        backfill_batch_size: int = DEFAULT_BACKFILL_BATCH_SIZE,
    ) -> None:
        if backfill_batch_size <= 0:
            raise ValueError("backfill_batch_size must be positive")
        self.gmail = gmail
        self.gemini = gemini
        self.store = store
        self.backfill_batch_size = backfill_batch_size
        self._backfill_now: datetime | None = None
```

- [ ] **Step 4: Extend message processing to cap unprocessed attempts and report deferred work**

Change `_process_message_ids` to this contract:

```python
def _process_message_ids(
    self,
    message_ids: list[str],
    *,
    summary: GmailSyncSummary,
    dry_run: bool,
    max_unprocessed: int | None = None,
) -> tuple[bool, int]:
    had_hard_errors = False
    attempted_unprocessed = 0
    deferred_unprocessed = 0

    for message_id in message_ids:
        summary.fetched += 1
        if not dry_run and self.store.has_processed_gmail_message(message_id):
            continue

        if (
            max_unprocessed is not None
            and attempted_unprocessed >= max_unprocessed
        ):
            deferred_unprocessed += 1
            continue

        attempted_unprocessed += 1
        try:
            classification = self.process_message(
                self.gmail.get_message(message_id),
                dry_run=dry_run,
            )
        except Exception:
            _LOGGER.exception(
                "gmail_message_processing_failed message_id=%s",
                message_id,
            )
            summary.errors += 1
            had_hard_errors = True
            continue

        summary.processed += 1
        if classification.kind == "JOB_ALERT":
            summary.job_alerts += 1
        elif classification.kind == "REVIEW_NEEDED":
            summary.review_needed += 1
        elif classification.kind == "IRRELEVANT":
            summary.irrelevant += 1
        elif classification.kind in _LIFECYCLE_KINDS:
            summary.application_events += 1

    return had_hard_errors, deferred_unprocessed
```

The key rule is that a failed attempt still consumes one slot in the batch because it consumed Gmail/Gemini runtime.

- [ ] **Step 5: Apply the limit only to pending/forced backfills and gate completion on deferred work**

In the backfill branch of `sync`, replace the single boolean result with:

```python
had_hard_errors, deferred_unprocessed = self._process_message_ids(
    message_ids,
    summary=summary,
    dry_run=dry_run,
    max_unprocessed=self.backfill_batch_size,
)
_LOGGER.info(
    "gmail_backfill_batch candidates=%s batch_size=%s deferred=%s",
    len(message_ids),
    self.backfill_batch_size,
    deferred_unprocessed,
)
```

Change the completion condition to:

```python
if not dry_run and not had_hard_errors and deferred_unprocessed == 0:
    self.store.save_gmail_sync_state(
        account_id=account_id,
        history_id=checkpoint_history_id,
        last_successful_sync_at=now.isoformat(),
        backfill_completed_at=now.isoformat(),
    )
```

In `_sync_incremental`, unpack the new return type without imposing a limit:

```python
had_hard_errors, deferred_unprocessed = self._process_message_ids(
    message_ids,
    summary=summary,
    dry_run=dry_run,
)
assert deferred_unprocessed == 0
```

Keep the existing `if dry_run or had_hard_errors: return` behavior after that assertion.

- [ ] **Step 6: Run the first focused test and verify it passes**

Run:

```bash
pytest tests/test_gmail_sync.py::test_backfill_limits_unprocessed_messages_and_defers_remaining -v
```

Expected: PASS.

- [ ] **Step 7: Add a failing continuation test proving the second run skips prior work and completes the final batch**

Add:

```python
def test_backfill_resumes_and_marks_complete_after_final_batch(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    first = service.sync(NOW)
    second = service.sync(NOW + timedelta(minutes=1))

    state = store.get_gmail_sync_state("candidate@example.com")
    assert first.processed == 2
    assert second.processed == 1
    assert gmail.message_calls == ["m1", "m2", "m3"]
    assert state is not None
    assert state["history_id"] == "100"
    assert state["backfill_completed_at"] == (
        NOW + timedelta(minutes=1)
    ).isoformat()
```

- [ ] **Step 8: Add a test proving already-processed IDs do not consume the batch allowance**

Add:

```python
def test_processed_ids_do_not_consume_backfill_batch_allowance(tmp_path):
    gmail = FakeGmail(
        message_ids=["old", "new-1", "new-2"],
        messages={
            "new-1": _message("new-1"),
            "new-2": _message("new-2"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    store.record_gmail_message(
        message_id="old",
        thread_id="thread-old",
        sender="newsletter@example.com",
        subject="Old",
        occurred_at=NOW.isoformat(),
        classification="IRRELEVANT",
        confidence=1.0,
        rationale="already processed",
    )
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["new-1", "new-2"]
    assert summary.fetched == 3
    assert summary.processed == 2
    assert store.get_gmail_sync_state("candidate@example.com") is not None
```

- [ ] **Step 9: Add a test proving incremental history sync ignores the historical backfill limit**

Add:

```python
def test_incremental_sync_is_not_limited_by_backfill_batch_size(tmp_path):
    gmail = FakeGmail(
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        }
    )
    gmail.history_pages = {
        None: GmailHistoryPage(["m1", "m2", "m3"], "103", None)
    }
    store = JobStore(tmp_path / "state.sqlite3")
    _save_completed_state(store, history_id="100")
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=1,
    )

    summary = service.sync(NOW)

    assert gmail.message_calls == ["m1", "m2", "m3"]
    assert summary.processed == 3
    assert store.get_gmail_sync_state("candidate@example.com")["history_id"] == "103"
```

- [ ] **Step 10: Extend the existing forced-backfill test to prove force mode also obeys the limit**

Add a focused test using a completed state and three unprocessed historical messages:

```python
def test_forced_backfill_uses_same_batch_limit(tmp_path):
    gmail = FakeGmail(
        message_ids=["m1", "m2", "m3"],
        messages={
            "m1": _message("m1"),
            "m2": _message("m2"),
            "m3": _message("m3"),
        },
    )
    store = JobStore(tmp_path / "state.sqlite3")
    _save_completed_state(store)
    service = GmailSyncService(
        gmail=gmail,
        gemini=FakeGemini(),
        store=store,
        backfill_batch_size=2,
    )

    summary = service.sync(NOW, force_backfill=True)

    state = store.get_gmail_sync_state("candidate@example.com")
    assert summary.processed == 2
    assert gmail.message_calls == ["m1", "m2"]
    assert state is not None
    assert state["backfill_completed_at"] is None
```

- [ ] **Step 11: Run the complete Gmail sync test module**

Run:

```bash
pytest tests/test_gmail_sync.py -v
```

Expected: all tests PASS, including the existing hard-error tests that ensure errors prevent the completion marker.

- [ ] **Step 12: Commit the bounded/resumable Gmail service change**

```bash
git add src/job_hunter/gmail_sync.py tests/test_gmail_sync.py
git commit -m "fix: bound Gmail historical backfill"
```

---

### Task 2: Protect the Main Pipeline with a Dedicated Gmail Step Timeout

**Files:**
- Modify: `.github/workflows/daily.yml`
- Create: `tests/test_workflow.py`

**Interfaces:**
- Consumes: existing `Daily Job Hunter` workflow and Gmail step named exactly `Sync Gmail intelligence`.
- Produces: workflow contract where Gmail has `timeout-minutes: 10`, remains `continue-on-error: true`, and the job retains `timeout-minutes: 30`.

- [ ] **Step 1: Write a failing workflow contract test**

Create `tests/test_workflow.py`:

```python
from pathlib import Path

import yaml


def test_gmail_sync_step_is_bounded_and_fail_open():
    workflow = yaml.safe_load(Path(".github/workflows/daily.yml").read_text())
    job = workflow["jobs"]["run"]
    gmail_step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Sync Gmail intelligence"
    )

    assert job["timeout-minutes"] == 30
    assert gmail_step["timeout-minutes"] == 10
    assert gmail_step["continue-on-error"] is True
```

- [ ] **Step 2: Run the test and verify it fails because the Gmail step has no timeout yet**

Run:

```bash
pytest tests/test_workflow.py -v
```

Expected: FAIL with `KeyError: 'timeout-minutes'` on `gmail_step`.

- [ ] **Step 3: Add the dedicated timeout to the Gmail step**

In `.github/workflows/daily.yml`, make the Gmail step read:

```yaml
      - name: Sync Gmail intelligence
        continue-on-error: true
        timeout-minutes: 10
        env:
          GMAIL_CLIENT_ID: ${{ secrets.GMAIL_CLIENT_ID }}
          GMAIL_CLIENT_SECRET: ${{ secrets.GMAIL_CLIENT_SECRET }}
          GMAIL_REFRESH_TOKEN: ${{ secrets.GMAIL_REFRESH_TOKEN }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python -m job_hunter sync-gmail
```

Do not change the job-level `timeout-minutes: 30`.

- [ ] **Step 4: Run the workflow contract test**

Run:

```bash
pytest tests/test_workflow.py -v
```

Expected: PASS.

- [ ] **Step 5: Run the CLI tests to ensure the workflow change does not mask a CLI regression**

Run:

```bash
pytest tests/test_cli.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit the workflow protection**

```bash
git add .github/workflows/daily.yml tests/test_workflow.py
git commit -m "fix: cap Gmail workflow step runtime"
```

---

### Task 3: Document Multi-run Backfill Behavior and Verify the Repository

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the implemented 100-message backfill cap and 10-minute Gmail step timeout.
- Produces: operator documentation explaining that first-time Gmail history can span multiple runs and that the normal pipeline remains fail-open.

- [ ] **Step 1: Update the Gmail intelligence documentation**

In the `## Gmail intelligence setup` section of `README.md`, add the following paragraph after the description of `--dry-run` / `--force-backfill`:

```markdown
The first successful Gmail setup performs a 12-month historical backfill. Historical processing is resumable and intentionally bounded to 100 previously unprocessed messages per sync invocation, so a large mailbox may need multiple workflow runs to finish. Successfully processed message IDs are stored in the SQLite state artifact and skipped on later runs. In GitHub Actions the Gmail step also has a 10-minute fail-open timeout; if it reaches that safety limit, the normal Job Hunter pipeline continues and the next run resumes the remaining Gmail backlog.
```

- [ ] **Step 2: Run the entire test suite**

Run:

```bash
pytest -q
```

Expected: all tests PASS.

- [ ] **Step 3: Check formatting/whitespace**

Run:

```bash
git diff --check
```

Expected: no output and exit status 0.

- [ ] **Step 4: Review the final diff for scope**

Run:

```bash
git diff main...HEAD -- \
  src/job_hunter/gmail_sync.py \
  tests/test_gmail_sync.py \
  .github/workflows/daily.yml \
  tests/test_workflow.py \
  README.md
```

Expected: only the bounded-backfill logic, tests, workflow timeout, and operator documentation described by the spec. No Supabase migration, Gmail scope change, classifier change, or unrelated refactor.

- [ ] **Step 5: Commit the documentation**

```bash
git add README.md
git commit -m "docs: explain resumable Gmail backfill"
```

---

## Final Acceptance Check

Before opening or merging a PR, verify all of the following from test output and the diff:

- [ ] Pending backfills attempt at most 100 previously unprocessed messages per invocation by default.
- [ ] Already processed IDs do not consume that allowance.
- [ ] Deferred historical messages keep `backfill_completed_at` unset.
- [ ] The final clean batch writes the completion marker and profile history checkpoint.
- [ ] Hard per-message errors still prevent completion.
- [ ] Forced backfills use the same bounded behavior.
- [ ] Incremental Gmail History sync remains uncapped.
- [ ] `Sync Gmail intelligence` has a 10-minute step timeout and remains fail-open.
- [ ] The overall job timeout remains 30 minutes.
- [ ] `pytest -q` passes.
- [ ] `git diff --check` passes.

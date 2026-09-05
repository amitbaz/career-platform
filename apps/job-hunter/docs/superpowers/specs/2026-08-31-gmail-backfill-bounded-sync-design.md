# Bounded Gmail Backfill Design

## Problem

The first Gmail intelligence sync scans the previous 12 months and processes every matching message sequentially. On the first real GitHub Actions run, `Sync Gmail intelligence` started at 20:47:38 UTC and was still running when the job-level 30-minute timeout cancelled the workflow at 21:17:38 UTC.

This is harmful even though the Gmail step is configured with `continue-on-error: true`: the timeout applies to the entire job, so when Gmail consumes the full job budget the normal `Run job hunter` step is skipped.

The existing implementation already provides the most important resumability primitive: successfully processed Gmail message IDs are persisted in SQLite and skipped on later backfill attempts. The fix should build on that behavior rather than introduce a new persistence model.

## Goals

- Prevent Gmail backfill from starving the normal Job Hunter pipeline.
- Allow the initial 12-month Gmail backfill to finish across multiple workflow runs.
- Preserve the existing 12-month search window and Gmail classification behavior.
- Preserve idempotency: already processed Gmail messages must not be processed again.
- Keep Gmail failures fail-open so public-source job discovery still runs.
- Keep the current SQLite backend; do not introduce Supabase or a database migration for this fix.
- Make backfill progress visible in logs.

## Non-goals

- Moving the bot to Supabase.
- Running Gmail sync as a separate workflow or separate job.
- Parallelizing Gmail message processing.
- Changing Gmail OAuth scopes or credentials.
- Changing the semantic/deterministic classification rules.
- Changing the 12-month historical window.

## Considered approaches

### 1. Increase the workflow timeout

Rejected. It would hide the symptom but still allow Gmail to monopolize the workflow. A larger mailbox could simply hit the larger timeout later, and the normal job pipeline would remain coupled to Gmail backfill duration.

### 2. Move Gmail into a separate GitHub Actions job/workflow

Not selected for this fix. It would provide strong isolation, but the bot currently restores and uploads one SQLite state artifact. Splitting Gmail and the normal pipeline would introduce artifact coordination and ordering concerns that are unnecessary for a small reliability fix.

### 3. Bound Gmail work inside the existing workflow

Selected. Process only a fixed number of previously unprocessed backfill messages per invocation and add a step-level Gmail timeout as a final safety net. Existing persisted message IDs provide resumability, and the existing state artifact continues to carry progress between workflow runs.

## Design

### Backfill batch limit

`GmailSyncService` will accept a `backfill_batch_size` constructor argument with a production default of **100**.

During a pending or forced backfill:

1. Search Gmail using the existing 12-month query and collect matching message IDs.
2. Iterate the IDs in their current order.
3. Count all matching IDs in `summary.fetched`, preserving current summary semantics.
4. Skip IDs already present in the processed-message store.
5. Attempt at most `backfill_batch_size` previously unprocessed IDs during this invocation.
6. Count additional unprocessed IDs as deferred rather than processing them.
7. Persist each successfully processed message exactly as today.

The existing `_process_message_ids` helper should be extended with an optional `max_unprocessed` limit and return both:

- whether any selected message had a hard processing error; and
- how many unprocessed messages were deferred because the limit was reached.

Incremental sync passes no limit and therefore preserves its current behavior.

### Backfill completion

A backfill is marked complete only when both conditions are true:

- there were no hard per-message errors in the selected batch; and
- there are zero deferred unprocessed messages.

If either condition is false, `backfill_completed_at` remains unset and the next ordinary sync performs the 12-month backfill query again. Already processed IDs are skipped, so each invocation advances through the backlog without requiring a new cursor table or schema migration.

When the final batch completes, the sync stores the profile history checkpoint captured at the start of that invocation. Messages arriving during earlier partial runs remain discoverable through the repeated date-based backfill search. Messages arriving after the final invocation's checkpoint are then handled by the normal Gmail History incremental sync.

Forced backfills use the same batch limit and completion rules.

### Step-level timeout

The `Sync Gmail intelligence` workflow step will get:

```yaml
timeout-minutes: 10
```

The existing `continue-on-error: true` remains.

The overall job timeout remains 30 minutes. This reserves the majority of the job budget for `Run job hunter` and artifact upload. The 100-message batch limit is the normal control mechanism; the 10-minute step timeout is a hard safety net for slow or hung Gmail/Gemini requests.

If GitHub terminates the Gmail step at 10 minutes, successfully committed SQLite progress from earlier messages is still uploaded by the existing `if: always()` artifact step. The next run repeats the backfill query and skips those completed message IDs.

### Logging

Pending backfills should emit one concise progress log containing:

- total candidate message IDs returned by the backfill search;
- configured batch size;
- number of unprocessed messages deferred to a future run.

The existing final Gmail summary log remains unchanged.

This provides enough evidence to distinguish a large but progressing mailbox from an actual hang.

## Files expected to change during implementation

- `src/job_hunter/gmail_sync.py`
  - Add the default backfill batch size and constructor argument.
  - Bound unprocessed backfill attempts.
  - Return deferred-count information from message processing.
  - Gate `backfill_completed_at` on both errors and deferred work.
  - Add backfill progress logging.

- `tests/test_gmail_sync.py`
  - Cover capped first-run processing, continuation across runs, final completion, existing-message skipping, hard-error behavior, and forced backfills.

- `.github/workflows/daily.yml`
  - Add the 10-minute timeout to the Gmail step while keeping `continue-on-error: true`.

- `tests/test_workflow.py`
  - Assert the Gmail step remains fail-open and has the 10-minute timeout.

- `README.md`
  - Explain that the initial 12-month backfill is resumable and may span multiple runs.

## Error handling and safety

- A per-message processing exception behaves as it does today: log the exception, increment the error count, leave that message unrecorded, and retry it on a later run.
- Hard errors prevent the backfill completion marker from being written.
- Deferred work is not an error and does not increment `summary.errors`.
- A Gmail step timeout must not prevent `Run job hunter` or state artifact upload.
- No email bodies or new private data are persisted as part of this fix.
- No Gmail write scope is introduced.

## Testing requirements

The implementation must prove:

1. A first backfill with more than the configured batch size processes only that many unprocessed messages.
2. Deferred messages leave the backfill incomplete.
3. The next run skips already processed IDs and processes the next batch.
4. The final batch writes `backfill_completed_at` and the current profile history checkpoint.
5. Already processed IDs do not consume the unprocessed batch allowance.
6. A hard error keeps the backfill incomplete even when there is no deferred work.
7. Forced backfill obeys the same limit.
8. Incremental history sync is not capped by the backfill limit.
9. The workflow Gmail step has `timeout-minutes: 10` and `continue-on-error: true`.
10. Existing Gmail and pipeline tests continue to pass.

Final verification:

```bash
pytest -q
git diff --check
```

## Acceptance criteria

- A large first-time Gmail mailbox can no longer consume the entire 30-minute workflow budget under normal operation.
- Backfill progress survives through the existing SQLite artifact and resumes on later workflow runs.
- The normal Job Hunter pipeline still runs when Gmail exceeds its 10-minute step budget.
- Once the historical backlog is exhausted, Gmail returns to the existing incremental history-sync path automatically.
- No schema migration, Supabase dependency, or Gmail permission change is required.

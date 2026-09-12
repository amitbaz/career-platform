# Gmail Review Classification Spam Fix Design

Date: 2026-09-01
Status: Ready for implementation
Repository: `amitbaz/job-hunter-bot`
Branch: `fix/gmail-review-classification-spam`

## 1. Problem

The Gmail integration is creating large Telegram review digests containing repeated entries like:

```text
Unknown company — Unknown role | semantic classification unavailable or invalid
```

These are not meaningful manual-review items. They are technical classification failures that are being persisted as `REVIEW_NEEDED` application events and then rendered by Telegram with fallback labels.

The result is noisy Telegram output, polluted application-event history, wasted semantic-classification work, and a risk that genuinely useful Gmail lifecycle signals are lost among technical failures.

## 2. Verified evidence

The issue is reproducible from the current `main` branch and the latest production state.

### Workflow evidence

The `Daily Job Hunter` run `33484862495` on 2026-09-01 processed one bounded Gmail backfill batch with:

```text
gmail_fetched=1853
gmail_processed=100
gmail_job_alerts=48
gmail_application_events=0
gmail_review_needed=49
gmail_irrelevant=3
gmail_errors=0
```

Almost half of the processed messages became review items while the sync reported zero errors.

### Persisted-state evidence

Aggregate inspection of the restored SQLite artifact, without reading email bodies, showed:

- 361 Gmail messages persisted as `REVIEW_NEEDED` with rationale `semantic classification unavailable or invalid`.
- 316 deterministic job alerts.
- 41 irrelevant messages classified before semantic processing.
- Only a very small number of meaningful review events with a specific rationale.

Many of the generic failure rows came from sender domains unrelated to job applications, including retail, travel, consumer-services, and marketing senders. This confirms that broad historical intake plus semantic failure handling is turning ordinary non-job email into user-facing review work.

## 3. Root cause

There are three connected defects.

### 3.1 Technical semantic failures are incorrectly converted into user review events

`classify_email()` currently wraps the Gemini call, JSON parsing, semantic validation, and URL reconciliation in one broad `except Exception` block:

```python
except Exception:
    return _review_needed("semantic classification unavailable or invalid")
```

That means all of these distinct failures become the same successful-looking classification:

- Gemini transport/API errors.
- Invalid JSON.
- Schema/type mismatches.
- Unexpected null or omitted optional fields.
- URL reconciliation failures.
- Any future programming error inside the semantic-classification block.

`GmailSyncService.process_message()` then persists that synthetic result as an application event. The sync summary counts it as `review_needed`, not `errors`.

This violates the original R1 Gmail design, which says individual parse/service failures must be logged as sync errors and must avoid repeated noisy notifications. `REVIEW_NEEDED` is intended for genuine ambiguity, not classifier infrastructure failure.

### 3.2 The semantic parser is stricter than the prompt contract

The current parser requires exact top-level keys, exact job-object keys, strict string values for fields that models may reasonably return as `null`, and exact consistency between `job_urls` and `jobs`.

It also rejects `IRRELEVANT` if the model returns any extracted company/role information, even though the prompt does not explicitly require all extraction fields to be blank for irrelevant mail. A model can reasonably identify the sender company of a marketing email while still correctly classifying the message as irrelevant.

The parser therefore turns semantically correct-but-imperfect model output into technical failure.

### 3.3 Historical Gmail intake is too broad

The 12-month backfill query includes bare terms such as `offer` and `position`. These are common in ordinary marketing and transactional mail.

That creates a large false-positive candidate set. `is_probably_job_related()` also treats some broad terms as strong signals, increasing unnecessary Gemini calls.

The broad intake does not by itself explain the Telegram spam, but it amplifies the classifier defect and increases runtime/cost.

## 4. Design goals

The fix must:

1. Reserve `REVIEW_NEEDED` for genuinely ambiguous job/application information.
2. Treat Gemini/API/parser failures as sync errors, not application events.
3. Allow harmless semantic-output variation without rejecting an otherwise useful classification.
4. Make `IRRELEVANT` a safe terminal classification even if the model identified sender/company metadata.
5. Reduce obvious non-job traffic entering historical semantic classification.
6. Recover previously polluted `semantic classification unavailable or invalid` rows so potentially useful old emails can be reclassified.
7. Preserve Gmail read-only behavior and the existing fail-open daily workflow.
8. Keep the bot on SQLite for this fix; Supabase migration is explicitly out of scope.
9. Never log or persist full email bodies as part of diagnostics.

## 5. Non-goals

This fix does not:

- Add Gmail mutation.
- Add Telegram reply/correction controls.
- Change public job-source ranking or scoring.
- Change application-status precedence.
- Migrate Gmail state to Supabase.
- Introduce a new LLM provider.
- Redesign the Telegram job-navigation feature.

## 6. Approaches considered

### Option A — Suppress the Telegram lines only

Filter out review events whose rationale is `semantic classification unavailable or invalid` before formatting Telegram.

**Rejected.** This hides the symptom while leaving polluted state, incorrect metrics, lost retry behavior, and unnecessary semantic failures.

### Option B — Convert semantic failures to `IRRELEVANT`

If semantic classification fails, silently treat the message as irrelevant.

**Rejected.** This creates false negatives and can discard real interview, recruiter, offer, or rejection emails during transient API failures.

### Option C — Separate ambiguity from failure, normalize model output, narrow intake, and repair legacy bad rows

This is the recommended approach.

It fixes the semantic boundary rather than the presentation layer, preserves retry safety, reduces false positives, and repairs already-polluted historical state without changing real application events.

## 7. Classification contract

### 7.1 Genuine review outcomes

`classify_email()` may return `GmailClassification(kind="REVIEW_NEEDED", ...)` only when the email itself is ambiguous, for example:

- Gemini explicitly classifies it as `REVIEW_NEEDED`.
- Semantic confidence is below `AUTO_CONFIDENCE_THRESHOLD`.
- A lifecycle classification is valid but matching to an existing job is ambiguous or unresolved.

These remain user-facing review items.

### 7.2 Technical failures

Gemini/API/parser/reconciliation failures must not return `GmailClassification`.

Introduce a dedicated exception such as:

```python
class SemanticClassificationError(RuntimeError):
    def __init__(self, reason: str): ...
```

The exception carries a short safe reason code/category only. It must not contain email body text or raw model output.

Suggested categories:

```text
gemini_error
invalid_json
invalid_semantic_shape
invalid_semantic_value
```

`GmailSyncService._process_message_ids()` catches the exception as a per-message processing failure, increments `summary.errors`, logs the message ID plus safe reason, and leaves the message unrecorded so the existing retry behavior can process it again later.

The normal public-source job run remains fail-open.

## 8. Semantic output normalization

The parser should remain safety-conscious but stop requiring irrelevant exactness.

### Required semantic fields

Require valid values for:

- `kind`
- `confidence`
- `rationale`

### Optional extraction fields

Treat these as optional/nullable and normalize them:

- `company` -> `""`
- `role_title` -> `""`
- `source_job_id` -> `None`
- `job_urls` -> `[]`
- `jobs` -> `[]`

Unknown extra top-level fields should not invalidate the classification; only supported fields are consumed.

### `IRRELEVANT`

When `kind == "IRRELEVANT"`:

- accept the classification if `kind`, `confidence`, and `rationale` are valid;
- discard extracted job/company/role payload before returning;
- do not run job-URL reconciliation;
- never create an application event or staged job.

This makes a semantically correct irrelevant decision robust even if the model included harmless sender metadata.

### Job URLs

For non-irrelevant classifications:

- never trust or persist a semantic URL that is not present in the email;
- filter unsupported/invented URLs out rather than failing the whole lifecycle classification;
- merge known safe job URLs discovered directly from the email;
- keep staging rules unchanged for actual job candidates.

A generic job-alert email that yields no safe extractable job candidates remains `JOB_ALERT` with zero staged candidates. It should not become a Telegram review item merely because extraction found no usable job URL.

## 9. Backfill intake tightening

Replace broad historical query terms with job-specific phrases.

Remove bare:

```text
offer
position
```

Keep or add higher-signal terms such as:

```text
application
interview
recruiter
hiring
"job alert"
"technical assessment"
"coding challenge"
"job offer"
"offer letter"
"thanks for applying"
"received your application"
```

Also align `is_probably_job_related()` so bare marketing uses of `offer`/`position` are not sufficient semantic-classification triggers.

This is a precision improvement for Gmail intake, not a change to public job discovery.

## 10. Legacy data repair

Already-persisted technical failures are marked as processed, so simply fixing the classifier would leave them permanently skipped.

Add an exact-match repair for legacy rows with all of:

```text
gmail_messages.classification = REVIEW_NEEDED
gmail_messages.rationale = semantic classification unavailable or invalid
application_events.event_type = REVIEW_NEEDED
application_events.rationale = semantic classification unavailable or invalid
```

The repair must be narrow and idempotent:

1. Find application events with the exact legacy technical-failure rationale.
2. Delete their corresponding `review_deliveries` rows.
3. Delete only those synthetic `application_events` rows.
4. Delete only the corresponding `gmail_messages` processed markers.
5. Leave every meaningful `REVIEW_NEEDED` event untouched.
6. If historical backfill had already been marked complete, clear only `backfill_completed_at` so the normal bounded backfill can re-read released message IDs.
7. Log only the number of released legacy failures.

This is corrective cleanup of invalid technical artifacts, not deletion of real application history.

No schema migration is required.

## 11. Telegram behavior

Telegram formatting does not need to become the primary fix.

After the classifier/sync boundary is corrected:

- technical failures never enter `pending_review_events()`;
- genuine review events continue to use the existing compact review digest and chunked delivery tracking;
- `Unknown company` / `Unknown role` remains an acceptable fallback for the rare genuine review that lacks those fields.

Add a regression test proving an operational semantic failure cannot reach Telegram review delivery.

## 12. Observability

For a semantic technical failure, log a privacy-safe line such as:

```text
gmail_semantic_classification_failed message_id=<id> reason=invalid_json
```

Do not log:

- email body;
- raw Gemini response;
- OAuth credentials;
- full prompt;
- message subject unless already required by an existing operator workflow.

`gmail_errors` must increase for these failures. `gmail_review_needed` must not.

The existing overall summary remains sufficient; a new persistent error table is not required in this fix.

## 13. Data flow after the fix

```text
Gmail message
   |
   +--> deterministic classifier
   |      |
   |      +--> confident result --------------------------+
   |                                                       |
   +--> local relevance gate                               |
          |                                                |
          +--> irrelevant --> IRRELEVANT ------------------+
          |                                                |
          +--> semantic classifier                         |
                 |                                         |
                 +--> valid high-confidence result --------+
                 |
                 +--> valid ambiguous/low-confidence --> REVIEW_NEEDED
                 |
                 +--> API/parser failure --> sync error + retry later

Only genuine REVIEW_NEEDED
   |
   +--> application_events
   +--> Telegram review digest
```

## 14. Testing strategy

### Classifier tests

Add/adjust tests for:

- Gemini transport failure raises `SemanticClassificationError` rather than returning `REVIEW_NEEDED`.
- Malformed JSON raises `SemanticClassificationError`.
- Semantic `IRRELEVANT` with company/role populated is accepted and normalized to empty extraction fields.
- Optional/null extraction fields are accepted.
- Unsupported/invented semantic URLs are dropped instead of invalidating a lifecycle classification.
- Explicit semantic `REVIEW_NEEDED` remains a genuine review.
- Low-confidence semantic results remain genuine review.
- Generic job alert with zero safe candidates remains `JOB_ALERT` and does not become review.

### Sync tests

Add tests for:

- semantic technical failure increments `summary.errors`;
- technical failure does not persist `gmail_messages` or `application_events`;
- technical failure remains retryable on the next sync;
- `summary.review_needed` counts only genuine ambiguity.

### Backfill-query tests

Assert:

- bare `offer` and `position` are absent;
- job-specific offer/application phrases are present.

### Legacy-repair tests

Assert:

- exact legacy generic failures are released;
- matching review deliveries are removed;
- meaningful review events remain untouched;
- repair is idempotent;
- completed backfill is reopened only when legacy failures were actually released.

### Pipeline/Telegram regression

Verify that a semantic technical failure cannot produce a `Gmail review needed` Telegram line, while a genuine low-confidence classification still can.

## 15. Rollout

1. Merge the fix.
2. Let the next normal workflow run restore the existing SQLite artifact.
3. The sync startup performs the narrow legacy repair once.
4. The bounded backfill reprocesses released failures in normal batch sizes.
5. Monitor workflow summaries for:
   - sharp reduction in `gmail_review_needed`;
   - non-zero `gmail_errors` only for true technical failures;
   - increased `gmail_irrelevant` for non-job mail;
   - continued job-alert and lifecycle extraction.
6. Do not manually delete the state artifact unless a separate recovery issue requires it.

## 16. Success criteria

The fix is successful when:

1. `semantic classification unavailable or invalid` is never persisted as a user-facing `REVIEW_NEEDED` event.
2. Gemini/API/parser failures increment `gmail_errors` and remain retryable.
3. Ordinary marketing/consumer mail is classified as irrelevant or filtered before Gemini rather than shown in Telegram.
4. Genuine ambiguous lifecycle mail still appears exactly once in Telegram review delivery.
5. Existing legacy technical-review rows are reprocessed without deleting meaningful review history.
6. The bounded Gmail backfill still respects the current workflow timeout strategy.
7. The public-source job-hunter run remains fail-open when Gmail has per-message errors.
8. No full email body or raw semantic response is persisted or logged.
9. No Supabase migration work is introduced by this fix.

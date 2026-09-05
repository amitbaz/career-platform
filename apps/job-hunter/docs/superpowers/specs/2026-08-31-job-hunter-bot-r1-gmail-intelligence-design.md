# Job Hunter Bot R1 — Gmail Intelligence Design

Date: 2026-08-31
Status: Proposed for review
Repository: `amitbaz/job-hunter-bot`

## 1. Purpose

Release 1 adds Gmail as a first-class intelligence input to the standalone Job Hunter Bot without coupling Gmail logic into the existing job-discovery pipeline.

The release has two goals:

1. Discover job opportunities from Gmail, especially LinkedIn and other job-board alert emails plus recruiter outreach.
2. Build an auditable application lifecycle history from application confirmations, recruiter responses, interview invitations, technical assessments, offers, and rejections.

This release does not automate LinkedIn login, browser scraping, job applications, or email mutation. Gmail access is read-only.

## 2. Scope

### In scope

- Gmail OAuth with read-only scope.
- One-time local OAuth bootstrap that produces credentials suitable for GitHub Actions.
- A dedicated `sync-gmail` command, separate from the existing `run` command.
- A one-time 12-month historical backfill on first successful connection.
- Incremental Gmail sync after the backfill.
- Job-alert discovery from LinkedIn and other job-board emails.
- Recruiter inbound opportunity extraction.
- Application lifecycle event detection.
- Append-only application event history.
- High-confidence automatic status derivation.
- Low-confidence `REVIEW_NEEDED` events surfaced for manual review.
- Idempotent processing keyed by Gmail message ID.
- Shared SQLite persistence with the existing Job Hunter Bot.
- Fail-open workflow behavior so Gmail problems do not stop the normal daily job search.
- Logs and metrics for Gmail processing.
- A Gmail dry-run inspection mode with no persistent writes.

### Out of scope

- Logged-in LinkedIn browser automation.
- LinkedIn credential or cookie storage.
- Gmail send, reply, archive, delete, label, or other mutation.
- Company watchlists.
- New ATS discovery adapters.
- Telegram inbound job-URL ingestion.
- Automatic job application submission.
- Learning-to-rank from application outcomes.
- Real-time Gmail Pub/Sub infrastructure.
- Relay integration.

These are candidates for later release specs.

## 3. Architectural decision

Use a separate Gmail synchronization stage that shares the existing SQLite database.

The existing daily job-search pipeline remains responsible for public-source discovery, normalization, ranking, Gemini evaluation, cover-letter generation, and Telegram delivery. Gmail synchronization is isolated in a dedicated command and module boundary.

```text
GitHub Actions
      |
      +--> restore SQLite state
      |
      +--> sync-gmail
      |       |
      |       +--> Gmail OAuth (read-only)
      |       +--> 12-month backfill once
      |       +--> incremental sync thereafter
      |       +--> deterministic classification first
      |       +--> Gemini classification when needed
      |       |
      |       +--> job alert ----------> inbound_job_candidates
      |       +--> recruiter inbound --> inbound_job_candidates and/or application_event
      |       +--> application email --> application_event
      |       +--> rejection ---------> application_event
      |       +--> interview ---------> application_event
      |       +--> ambiguous ---------> REVIEW_NEEDED
      |
      +--> existing job_hunter run
      |       |
      |       +--> public sources
      |       +--> Gmail staged source
      |       +--> normal dedupe / ranking / Gemini
      |       +--> Telegram digest + review-needed section
      |
      +--> persist SQLite artifact
```

This architecture is preferred because it keeps Gmail failures isolated, preserves the current pipeline boundaries, and allows Gmail ingestion to be tested independently.

## 4. Command model

Add a new CLI command:

```text
python -m job_hunter sync-gmail
```

The command performs only Gmail synchronization and persistence. It does not run public job discovery or the normal Telegram job digest.

The existing command remains:

```text
python -m job_hunter run
```

The scheduled workflow runs `sync-gmail` before `run`.

A Gmail-specific dry-run option must be supported. In dry-run mode the command may read Gmail and classify/extract messages, but it must not persist jobs, inbound candidates, application events, review-delivery state, or Gmail sync cursors.

## 5. Authentication and secrets

### OAuth model

Use Google OAuth for a personal Gmail account with the minimum required read-only Gmail scope.

A one-time local bootstrap flow performs interactive Google authorization and produces the refresh token needed by GitHub Actions. The scheduled runner must never require interactive login.

### Secret storage

OAuth credentials are stored only as GitHub Actions secrets.

Expected secret inputs:

- Gmail OAuth client ID.
- Gmail OAuth client secret.
- Gmail refresh token.

No OAuth credential may be committed to the repository or written to the SQLite state artifact.

### Gmail permission boundary

The bot must have read-only Gmail access. It must not send, reply, archive, delete, label, mark read/unread, or otherwise mutate mailbox state.

## 6. Gmail sync state

Add a small sync-state table so the bot can distinguish the first historical backfill from normal incremental operation.

Conceptual schema:

```text
gmail_sync_state
- account_id                 primary key
- last_successful_sync_at
- last_processed_message_at
- backfill_completed_at
- created_at
- updated_at
```

The implementation may use Gmail history IDs or message timestamps as the concrete cursor, but these guarantees are required:

- The 12-month backfill runs once after the first successful authorization.
- Subsequent runs process only messages not successfully processed before.
- The sync cursor advances only after the corresponding batch is durably persisted.
- A crash during a batch must not silently skip messages on the next run.
- Reprocessing is safe because Gmail message IDs are idempotency keys.

## 7. Historical backfill

On the first successful Gmail connection, scan the previous 12 months.

The backfill must:

- Fetch in bounded batches.
- Persist successful work incrementally.
- Avoid loading the full 12-month mailbox window into memory.
- Use the same classification and matching logic as incremental sync.
- Tolerate partial failures and resume safely.
- Mark `backfill_completed_at` only after the backfill has been fully processed.

The backfill is intended to reconstruct as much recent application history as possible before ongoing incremental tracking begins.

## 8. Email classification strategy

Use a two-stage classifier.

### Stage 1 — deterministic classification

Apply deterministic rules to known high-signal email patterns before spending Gemini calls.

Examples include:

- LinkedIn job alert senders and known job-alert structures.
- Standard application confirmation language.
- Common rejection templates.
- Obvious interview scheduling messages.
- Obvious coding challenge or technical-assessment invitations.

Deterministic classification should be preferred when confidence is high.

### Stage 2 — semantic classification

Use Gemini only when deterministic rules cannot confidently classify the message.

Gemini receives only the minimum message content required for classification and extraction.

Supported classifications:

```text
JOB_ALERT
RECRUITER_CONTACT
APPLIED
INTERVIEW
TECHNICAL
OFFER
REJECTED
REVIEW_NEEDED
IRRELEVANT
```

### Confidence policy

High-confidence outcomes may create application events automatically.

Low-confidence or conflicting outcomes must become `REVIEW_NEEDED` and must not alter the derived application status automatically.

The implementation plan must define concrete numeric or categorical confidence thresholds, but it may not weaken this high-confidence-only mutation rule.

## 9. Staging Gmail-discovered jobs into the existing pipeline

Gmail discovery must not bypass the existing `JobSource` contract or evaluation policy.

Add a small persistent staging table for Gmail-origin candidates:

```text
inbound_job_candidates
- id                       primary key
- origin                   default 'gmail'
- source_message_id
- source_candidate_key
- source_platform
- source_job_id            nullable
- url                      nullable
- company
- title
- location                 nullable
- remote                   nullable
- description              nullable
- created_at
- last_seen_at
- unique(origin, source_message_id, source_candidate_key)
```

`source_candidate_key` is a stable per-message candidate key derived from the best available identifier, preferring normalized job URL, then source job ID, then normalized company/title/index fallback.

The normal `run` command creates the SQLite store before constructing its final source list, then adds a DB-backed Gmail staging source that implements the existing `discover() -> list[Job]` contract. That source reads staged Gmail candidates and returns normal `Job` objects. From that point onward, Gmail-origin jobs follow the same enrichment, deduplication, profession gate, ranking, Gemini evaluation, and delivery rules as every other source.

A staged candidate is not considered an application event merely because it was discovered.

## 10. Job-alert and recruiter-opportunity ingestion

### Job alerts

A job-alert email may contain one or more job postings.

The Gmail sync stage extracts candidate fields such as:

- company
- role title
- location when present
- job URL
- source platform
- source job ID when recoverable

Extracted jobs are inserted/upserted into `inbound_job_candidates`. LinkedIn alert emails are therefore a discovery channel, not a separate scoring system.

### Recruiter inbound

Recruiter outreach that describes a concrete role or includes a job URL becomes an inbound opportunity candidate.

If enough information is available, the role is staged into `inbound_job_candidates`. If the outreach itself represents a meaningful application-lifecycle signal, it may also create a `RECRUITER_CONTACT` event. If information is insufficient or ambiguous, the system must not invent missing job details and may create `REVIEW_NEEDED` instead.

## 11. Application event model

Do not make the existing `jobs.status` column the primary history store.

Add an append-only `application_events` table.

Conceptual schema:

```text
application_events
- id                     primary key
- job_id                  nullable foreign key to jobs
- event_type
- occurred_at
- source                  default 'gmail'
- source_message_id       unique
- source_thread_id        nullable
- confidence
- company                 extracted snapshot
- role_title              extracted snapshot
- rationale               short classifier rationale
- created_at
```

Supported event types:

```text
APPLIED
RECRUITER_CONTACT
INTERVIEW
TECHNICAL
OFFER
REJECTED
REVIEW_NEEDED
```

The sync pipeline does not rewrite existing event facts. Corrections must be represented explicitly rather than silently replacing history.

The current application state is derived from event history, not treated as the only durable record.

## 12. Job matching strategy

When an application-related email is classified, match it back to an existing job using the following priority order:

1. Exact canonical or known job URL match.
2. Source job ID match.
3. Company plus normalized role-title match.
4. Company-only plus a recent application window, but only when the result is unambiguous.
5. Otherwise leave `job_id` unresolved and create `REVIEW_NEEDED`.

Matching must prefer false negatives over false positives. An unresolved event is acceptable; attaching an event to the wrong job is not.

No ambiguous email may mutate a derived job/application status automatically.

## 13. Derived application state

Only high-confidence lifecycle events with a resolved `job_id` participate in derived application state.

The current state is the lifecycle-bearing event with the latest `occurred_at` timestamp. If two eligible events have the same timestamp, break ties with this precedence:

```text
OFFER
REJECTED
TECHNICAL
INTERVIEW
RECRUITER_CONTACT
APPLIED
```

`REVIEW_NEEDED` never becomes a derived lifecycle state.

This timestamp-first rule intentionally favors the most recent known real-world signal. Because the underlying event history is preserved, a later correction mechanism can repair a misclassified or delayed message without losing auditability.

## 14. Idempotency

Gmail message ID is the primary idempotency key for message-level processing.

Processing the same Gmail message more than once must not create duplicate application events.

For multi-job alert emails, `inbound_job_candidates` uses `(origin, source_message_id, source_candidate_key)` uniqueness so the same alert can safely be reprocessed without duplicating individual jobs.

The sync stage must be safe to rerun after crashes, workflow retries, or restored database artifacts.

## 15. Privacy and data minimization

The SQLite artifact should contain only the data needed for job intelligence and auditability.

Persisted Gmail-derived metadata may include:

- Gmail message ID
- Gmail thread ID
- sender
- subject
- relevant timestamps
- extracted company
- extracted role title
- extracted job URLs / IDs
- classification
- confidence
- short classifier rationale

Do not persist the full email body by default.

The message body may be processed in memory for extraction/classification and then discarded.

OAuth credentials must never be stored in SQLite.

## 16. Workflow integration

The scheduled workflow becomes:

```text
restore SQLite artifact
        |
        v
sync-gmail
        |
        v
existing job hunter run
        |
        v
upload SQLite artifact
```

### Fail-open requirement

Gmail synchronization must not become a hard dependency for the existing daily job search.

If Gmail authorization fails, Google is temporarily unavailable, or an individual message cannot be parsed:

- log the error;
- record sync error metrics;
- preserve the last safe sync cursor where appropriate;
- continue the normal public-source job-hunter run;
- avoid repeated noisy notifications for the same persistent Gmail failure.

The workflow step for `sync-gmail` therefore records failure state for observability but must not prevent the later `run` step from executing.

## 17. Review-needed delivery

Ambiguous email classifications or ambiguous job matches create `REVIEW_NEEDED` events.

The existing `run` command appends a compact Gmail review section to Telegram when undelivered review events exist. Review events may have no `job_id`, so their delivery tracking is separate from the existing job-delivery table.

Conceptual schema:

```text
application_event_deliveries
- id
- application_event_id
- delivery_type            default 'telegram_review'
- status
- delivered_at
- telegram_message_id      nullable
- unique(application_event_id, delivery_type)
```

After a review item is successfully surfaced, it is not repeated on every daily run unless a later explicit workflow chooses to re-open it.

Release 1 does not require a conversational correction UI. The important requirement is visibility without automatic status corruption.

## 18. Observability

Log a compact Gmail sync summary on every run.

Example metrics:

```text
gmail_fetched=42
gmail_processed=39
gmail_job_alerts=12
gmail_application_events=7
gmail_review_needed=2
gmail_errors=1
```

Logs must not include OAuth secrets, full email bodies, or other unnecessary private content.

## 19. Testing strategy

### Unit tests

- Deterministic email classifier rules.
- Event-type mapping.
- Confidence threshold behavior.
- Company/title normalization.
- Job matching priority order.
- Derived application-state timestamp/tie precedence.
- Gmail message-ID idempotency.
- Multi-job alert candidate idempotency.

### Gmail client tests

Use mocked Gmail API responses to test:

- pagination/batching;
- message retrieval;
- transient API failures;
- expired/invalid credentials;
- incremental cursor behavior.

### Integration tests

Use a temporary SQLite database and fixture emails to verify:

- 12-month backfill writes expected events and candidates;
- rerunning a backfill does not duplicate events or candidates;
- incremental sync processes only new messages;
- a crash before cursor advancement is safely recoverable;
- ambiguous matching creates `REVIEW_NEEDED` rather than a false association;
- Gmail failure does not block the normal public-source pipeline;
- the DB-backed Gmail source feeds staged jobs through normal deduplication/ranking/evaluation;
- review-needed Telegram delivery is not repeated after successful delivery;
- dry-run performs no persistent writes.

### Privacy tests

Assert that full fixture email bodies and OAuth credentials are not persisted in SQLite.

## 20. Operational safety

Release 1 must preserve the existing Job Hunter safety boundary:

- no application submission;
- no legal or work-authorization attestations;
- no CAPTCHA or 2FA automation;
- no logged-in LinkedIn browser automation;
- no Gmail mutation.

The bot remains an assistant for discovery, analysis, material preparation, and application-state intelligence.

## 21. Success criteria

Release 1 is complete when:

1. Gmail can be authorized once locally with read-only access and used non-interactively from GitHub Actions.
2. A 12-month backfill can complete incrementally and resume safely after interruption.
3. LinkedIn/job-board alerts create staged normalized candidates without duplicates.
4. The DB-backed Gmail source feeds those candidates into the unchanged scoring/delivery policy of the normal run.
5. Recruiter outreach can produce useful inbound opportunities or recruiter-contact events without inventing missing details.
6. Clear application confirmations, rejections, interviews, technical assessments, and offers create idempotent application events.
7. Ambiguous messages never change application status automatically and are surfaced exactly once as review-needed items after successful Telegram delivery.
8. Existing public-source job hunting continues when Gmail fails.
9. The SQLite artifact does not persist full email bodies or OAuth credentials.
10. Gmail dry-run performs no persistent writes.
11. Tests cover classifier behavior, matching, staging, idempotency, backfill, incremental sync, derived state, review delivery, privacy, and fail-open execution.

## 22. Later releases

The following work is intentionally deferred:

- Release 2: broader discovery expansion, direct company/ATS watchlists, canonical URL resolution improvements, and additional specialist sources.
- Release 3: Telegram inbound job-URL ingestion and immediate scoring.
- Release 4: application-outcome analytics and learning feedback for ranking and interview preparation.

Each later release requires its own design spec and implementation plan before code changes begin.

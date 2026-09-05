# Gmail Activity Review Cleanup Design

## Problem

The Gmail integration currently has two related problems.

First, deterministic classification is too broad. The backfill query searches for the generic word `offer`, and the classifier treats any message containing `pleased to offer` as a job offer. This caused a Vietnam Airlines marketing email (`Travel together, Save up to 15%!`) to be classified as an `OFFER` because its body said that the airline was "pleased to offer exclusive fares". The event then failed to match a tracked job and surfaced in Telegram as `Gmail review needed`.

Second, the Telegram review message is an internal diagnostic presented as if it were a user workflow. It exposes classifier rationale such as `deterministic offer template` and `deterministic recruiter template`, uses placeholders such as `Unknown role`, and says `review needed` without giving the user a meaningful action or explaining what happened.

## Goals

1. Prevent obvious non-job promotions from becoming Gmail lifecycle events.
2. Keep legitimate recruiter/application/interview/offer/rejection emails detectable.
3. Replace the confusing `Gmail review needed` digest with an informational Gmail activity summary.
4. Explain why each item is shown in user language, not classifier/debug language.
5. Give the user a direct way to open the source email.
6. Do not build a full interactive resolution workflow yet.
7. Do not move Gmail review items into the job carousel; the carousel remains for evaluated job opportunities only.

## Non-Goals

- No `Ignore`, `Track job`, or `Match to job` Telegram actions in this change.
- No Supabase migration.
- No changes to job ranking or evaluation.
- No redesign of the existing job navigator.
- No attempt to automatically infer a missing job from every unresolved Gmail event.

## Design

### 1. Tighten Gmail candidate search

The Gmail backfill query should stop searching for the generic word `offer` by itself.

Replace it with employment-specific phrases such as:

- `"job offer"`
- `"offer letter"`
- `"offer of employment"`
- `"pleased to offer you"`

The existing application, interview, recruiter, hiring, job-alert, technical-assessment, and coding-challenge terms remain.

This reduces irrelevant Gmail messages before classification and avoids wasting semantic-classification work on ordinary promotions.

### 2. Require job context before deterministic lifecycle classification

`classify_email` currently runs deterministic lifecycle rules before the broader `is_probably_job_related` guard. Reverse that responsibility: obvious non-job messages must be rejected as `IRRELEVANT` before lifecycle templates are allowed to create events.

The deterministic offer phrases must also be tightened. `pleased to offer` alone is not sufficient. Offer rules should require employment-specific language such as:

- `offer you the position`
- `pleased to offer you the position`
- `offer of employment`
- `offer letter`

The existing deterministic rules for application receipts, interviews, assessments, rejections, job alerts, and recruiter contacts remain, but they operate only after the message has passed the job-related guard.

A LinkedIn message digest from a recruiter can therefore still be treated as recruiter activity, while a Vietnam Airlines fare promotion becomes `IRRELEVANT`.

### 3. Preserve the lifecycle event type when matching fails

`REVIEW_NEEDED` is currently used as both a classification state and an application-event type. That loses useful information: a recruiter contact that cannot be matched becomes merely `REVIEW_NEEDED`, so Telegram cannot explain what kind of event was detected.

For lifecycle classifications (`RECRUITER_CONTACT`, `APPLIED`, `INTERVIEW`, `TECHNICAL`, `OFFER`, `REJECTED`):

- persist the original lifecycle `event_type` in `application_events`;
- persist the matched `job_id` when one exists;
- leave `job_id` null when no unique tracked job can be linked;
- keep the original confidence;
- continue returning an effective `REVIEW_NEEDED` classification to the sync summary when human attention would otherwise have been required.

Review eligibility becomes a derived delivery concern rather than an application state:

- unresolved lifecycle event: `job_id IS NULL`;
- low-confidence lifecycle event: `confidence < AUTO_CONFIDENCE_THRESHOLD`;
- explicit semantic `REVIEW_NEEDED` events remain review-eligible;
- legacy rows whose `event_type` is already `REVIEW_NEEDED` remain supported.

This works with the existing application-state derivation because only events with a non-null `job_id` and sufficient confidence can affect tracked application state.

### 4. Replace the Telegram review digest with an informational activity digest

Rename the message header from:

`Gmail review needed`

to:

`Gmail activity I couldn't link`

Each event should show the best available human-readable identity:

- `Company — Role` when both are available;
- company plus event label when only company is available;
- otherwise the email subject.

Do not display internal classifier rationale in Telegram.

Instead, render a short explanation based on the original lifecycle event type:

- `RECRUITER_CONTACT` → `A recruiter contacted you, but I couldn't link this email to a tracked job.`
- `APPLIED` → `This looks like an application confirmation, but I couldn't link it to a tracked job.`
- `INTERVIEW` → `This looks like an interview email, but I couldn't link it to a tracked job.`
- `TECHNICAL` → `This looks like a technical assessment, but I couldn't link it to a tracked job.`
- `OFFER` → `This looks like a job offer, but I couldn't link it to a tracked job.`
- `REJECTED` → `This looks like a rejection email, but I couldn't link it to a tracked job.`
- `REVIEW_NEEDED` / legacy fallback → `This looks job-related, but I couldn't classify or link it confidently.`

The copy describes what the bot knows and why the item is being surfaced. It does not imply that a separate review workflow exists.

### 5. Add a direct Gmail link

Carry `source_message_id` into `ReviewItem` and build a Gmail deep link for each item:

`https://mail.google.com/mail/#all/<message_id>`

Render it as a plain Telegram-clickable URL beneath the item, for example:

```text
Montash — Senior Frontend Engineer
A recruiter contacted you, but I couldn't link this email to a tracked job.
Open email: https://mail.google.com/mail/#all/abc123
```

This keeps the implementation simple and does not add a second interactive Telegram navigation subsystem.

### 6. Keep Gmail activity separate from the job carousel

The interactive job carousel represents evaluated job opportunities with a score, location, job URL, and apply action. Gmail activity represents application/recruiter lifecycle events that could not be linked confidently.

They remain separate messages because combining them would mix two different concepts and would require fake job-card fields for events that may not have a real job posting.

The pipeline should continue sending Gmail activity before the job navigator so the interactive job card remains the latest Telegram message.

## Data Flow

### Classification

Gmail search → job-related guard → deterministic/semantic classification → lifecycle classification or irrelevant

### Matching and persistence

Lifecycle classification → match against tracked jobs → persist original lifecycle event type + optional `job_id` + confidence

### Telegram delivery

Pending unresolved/low-confidence Gmail events → `ReviewItem` with source message id → user-facing event copy → `Gmail activity I couldn't link` message → mark review delivery sent

## Files Expected to Change

- `src/job_hunter/gmail_sync.py`
  - tighten Gmail query terms;
  - preserve original lifecycle event type when matching yields review status.
- `src/job_hunter/gmail_classifier.py`
  - gate deterministic classification behind job-related detection;
  - tighten deterministic offer phrases.
- `src/job_hunter/store.py`
  - make pending review selection derive from unresolved/low-confidence lifecycle events while retaining legacy `REVIEW_NEEDED` rows;
  - include `source_message_id` and `event_type` in pending review rows.
- `src/job_hunter/models.py`
  - extend `ReviewItem` with `event_type` and `source_message_id`.
- `src/job_hunter/telegram.py`
  - replace debug-oriented review formatting with user-facing activity copy and Gmail links.
- `src/job_hunter/pipeline.py`
  - populate the extended `ReviewItem` fields.
- focused Gmail classifier/sync/store/Telegram tests.

## Testing

Add regression coverage for at least these cases:

1. Vietnam Airlines `pleased to offer exclusive fares` is classified as `IRRELEVANT` and does not create a review event.
2. A real employment phrase such as `pleased to offer you the position` still classifies as `OFFER`.
3. A legitimate LinkedIn recruiter message still classifies as recruiter activity.
4. An unmatched recruiter contact is persisted with `event_type=RECRUITER_CONTACT`, `job_id=NULL`, and appears in pending Gmail activity.
5. A matched but low-confidence lifecycle event remains review-eligible but cannot affect application state.
6. Legacy `REVIEW_NEEDED` events can still be delivered.
7. Telegram output says `Gmail activity I couldn't link` and contains user-facing explanations, not strings such as `deterministic recruiter template`.
8. Telegram activity includes a Gmail deep link built from the source message id.
9. Existing job-carousel delivery behavior remains unchanged.
10. Full test suite passes.

## Success Criteria

- The Vietnam Airlines promotion no longer appears in Telegram Gmail activity.
- `deterministic ... template` text never appears in user-facing Gmail notifications.
- Unresolved Gmail lifecycle events explain what happened in plain language.
- Each surfaced Gmail activity item can be opened directly in Gmail.
- Legitimate lifecycle emails continue to be processed.
- The job carousel remains unchanged and separate.

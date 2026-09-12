# Telegram Job Navigator — Design Spec

Date: 2026-08-31
Status: Approved design, corrected for current bot infrastructure

## Summary

Replace the current long Telegram digest with a single interactive job card that lets the user browse deliverable jobs using inline buttons. The same Telegram message is edited in place when the user moves to the previous or next job.

The first version introduces navigation only. A visible `Apply` button is included as a placeholder for a future application workflow, but it must not submit an application or perform any application-side effects.

This feature must stay on the bot project's current persistence model: SQLite restored/uploaded through the existing `job-hunter-state` GitHub Actions artifact. It must not introduce Supabase.

## Goals

- Present one job at a time instead of a long digest.
- Browse deliverable jobs using `Previous` and `Next` inline buttons.
- Keep deterministic score-first ordering.
- `View job` opens the original posting URL.
- Show an `Apply` placeholder now without application side effects.
- Preserve current score/decision eligibility and PDF delivery behavior.
- Make navigation work after the scheduled GitHub Actions process exits.
- Reuse the existing SQLite + GitHub Actions artifact persistence model.

## Non-goals

- Automated application submission or form filling.
- CAPTCHA/2FA handling.
- Changes to discovery, ranking, Gemini evaluation, or cover-letter generation.
- A general-purpose Telegram command framework.
- Wraparound navigation.
- Supabase or another database migration.

## Current State

The pipeline currently builds one grouped text digest and sends it through an outbound-only `TelegramClient`. Strong matches receive cover-letter PDFs as separate Telegram documents.

`DigestItem` currently contains `job_id`, `company`, `title`, `score`, `decision`, `url`, and `hard_blockers`. Job location already exists in `Job` and SQLite, but is not propagated to `DigestItem`.

The project already persists `var/job_hunter.sqlite3` by restoring the newest `job-hunter-state` GitHub Actions artifact before a run and uploading the updated database after the run.

GitHub Actions runners are ephemeral, so Telegram button callbacks cannot depend on process memory after the scheduled workflow finishes.

## User Experience

The bot sends one card:

```text
Senior Frontend Developer

Company: Example GmbH
Location: Berlin
Match: 87%
```

Inline keyboard:

```text
[ View job ]  [ Apply ]
[ ◀ Previous ]  [ 3 / 12 ]  [ Next ▶ ]
```

### View job

A Telegram URL button that opens the original posting. If the URL is missing or unusable, omit the button.

### Apply

A callback button reserved for the future application flow. In this feature it only answers the callback with `Apply functionality coming soon.` It must not generate material, submit anything, or mark a job as applied.

### Previous / Next

Callback buttons use Telegram `editMessageText` so the existing job-card message changes in place. First/last positions do not wrap.

### Position

Render `current / total` as a no-op callback button because Telegram inline keyboards do not provide a truly disabled button. Edge navigation buttons also use no-op callbacks when navigation in that direction is unavailable.

## Ordering

Use one deterministic order for each navigation session:

1. score descending
2. company ascending, case-insensitive
3. title ascending, case-insensitive
4. job ID ascending

## Navigation Session

A navigation session represents the ordered batch attached to one Telegram message.

Persist it in the existing SQLite database with:

- opaque session ID
- ordered card snapshot JSON
- Telegram message ID after send succeeds
- created timestamp
- expiry timestamp

The card snapshot contains only presentation data needed by Telegram navigation:

- job ID
- title
- company
- location
- score
- URL

This intentionally avoids re-running discovery, Gemini, or cover-letter logic during button clicks.

Callback data must stay compact and below Telegram's 64-byte limit. Use a short encoding such as:

- `n|<session>|<index>` — navigate
- `a|<session>|<index>` — Apply placeholder
- `x|<session>|<index>` — no-op

Session IDs are opaque URL-safe random values and contain no secrets.

## Production Interaction Architecture

Interactive Telegram callbacks require a public inbound HTTPS endpoint. The scheduled GitHub Actions process cannot receive button presses after it exits.

The feature therefore has two runtimes but one persistence model:

1. **Scheduled delivery runtime** — current GitHub Actions pipeline. It evaluates jobs, writes the navigation session into SQLite, sends the first card, updates the session with the Telegram message ID, then the normal workflow uploads the SQLite database as `job-hunter-state`.
2. **Telegram webhook runtime** — a small public service. On callback, it loads the latest `job-hunter-state` artifact through the GitHub Actions REST API, opens the extracted SQLite database read-only, loads the requested session, edits the Telegram message, and acknowledges the callback.

The webhook service must not use Supabase and must not maintain a second database.

### Artifact-backed state access

Refactor the existing artifact-download logic from `scripts/restore_state.py` into a reusable module so both the restore script and webhook runtime can use the same safe ZIP extraction behavior.

The webhook uses a fine-grained GitHub token limited to this repository with `Actions: read`. GitHub's artifact API supports listing and downloading private-repository artifacts with that permission.

The webhook should cache the newest artifact ID and extracted SQLite file in local temporary storage for the lifetime of the process. Before each navigation request it checks whether the newest artifact ID changed; it downloads/extracts only when needed.

If a user taps a button during the small interval after the first card is sent but before the workflow's updated artifact is available, return `Job list is still syncing. Try again shortly.` rather than navigating against stale data.

## Components

### `DigestItem` enrichment

Add `location` and populate it in both normal evaluation and retry paths.

### Job-card formatter

A deterministic formatter owns card text and inline-keyboard JSON, including location fallback, one-based position display, URL omission, edge-button behavior, and callback payload construction.

### Telegram client extensions

Add narrowly scoped methods for:

- sending a card with `reply_markup`
- editing a card
- answering callback queries

### SQLite navigation-session storage

Extend `JobStore` with a `telegram_navigation_sessions` table and methods to:

- create a session
- attach a Telegram message ID
- retrieve a session by ID
- prune expired sessions

No external database adapter is required.

### GitHub artifact state loader

A focused module lists the latest non-expired `job-hunter-state` artifact, downloads it, safely extracts `job_hunter.sqlite3`, and returns the local path plus artifact ID.

### Callback handler

Responsibilities:

- parse callback payloads
- load the current artifact-backed SQLite state
- retrieve/validate the navigation session and target index
- rebuild the target card
- edit the Telegram message
- acknowledge callbacks
- return harmless responses for `apply` and `noop`
- handle stale or malformed callbacks without crashing

The callback handler must not invoke discovery, Gemini evaluation, or cover-letter generation.

### Pipeline integration

When deliverable jobs exist:

1. select and deterministically sort them
2. create the SQLite navigation session
3. send the first card
4. attach the Telegram message ID to the session
5. only after successful send, mark all represented jobs message-delivered
6. deliver ready-to-apply PDFs exactly as today

Prefer sending the navigator card after PDF delivery so the workflow reaches the artifact-upload step immediately afterward, minimizing the artifact-sync race window.

If no deliverable jobs exist, keep `No matching jobs today.` and do not create a session.

## Failure Handling

- Initial card send failure: mark none of the session jobs as message-delivered; keep retry eligibility.
- Card edit failure: log safely and answer the callback with a short failure notification where possible.
- Session absent from newest artifact: answer `Job list is still syncing. Try again shortly.` if the callback refers to a newly sent message; otherwise treat it as expired/stale.
- Unknown/expired session: answer `This job list has expired.` and do not crash.
- Invalid index/malformed callback: reject safely; do not edit the message.
- Artifact API/download failure: answer `Could not load this job list right now.` without exposing GitHub details or tokens.

## Security

- Never put Telegram or GitHub tokens into callback data or logs.
- Treat callback payloads as untrusted input.
- Validate session IDs and indices.
- Validate Telegram's webhook secret header before processing updates.
- Use a fine-grained GitHub token restricted to this repository with `Actions: read` for the webhook runtime.
- Open the downloaded SQLite database read-only in the webhook runtime.
- Keep `Apply` strictly side-effect free in this feature.

## Testing

Unit coverage must include:

- card text formatting and location fallback
- missing URL behavior
- first/middle/last keyboards
- callback serialization/parsing and 64-byte limit
- SQLite session creation/retrieval/expiry
- Telegram send/edit/answer payloads
- artifact listing/download/cache behavior
- placeholder Apply/no-op behavior

Pipeline coverage must verify:

- one navigator card replaces the long digest
- deterministic ordering
- location propagation
- delivery marking only after successful send
- retry behavior after failed send
- unchanged PDF delivery

Webhook coverage must verify Next, Previous, edges, no-op, Apply placeholder, expired session, sync-race response, artifact failure, missing data, invalid indices, and invalid webhook secret.

## Acceptance Criteria

1. Deliverable jobs are represented by one Telegram job card instead of one long multi-job digest.
2. Card shows title, company, location, and match percentage.
3. `View job` opens the posting URL.
4. `Apply` is visible and side-effect free.
5. `Previous` and `Next` edit the same Telegram message.
6. Position shows `<current> / <total>`.
7. Navigation does not wrap.
8. Navigation still works after the scheduled pipeline process exits.
9. Navigation state stays in the existing SQLite database persisted by the `job-hunter-state` GitHub Actions artifact.
10. No Supabase dependency or migration is introduced.
11. Existing >60 deliverability/decision filtering remains unchanged.
12. Cover-letter PDF delivery remains functional.
13. Stale/invalid callbacks and artifact failures fail safely.
14. Tests cover formatter, callbacks, SQLite persistence, GitHub artifact access, Telegram HTTP calls, pipeline integration, and webhook handling.

## Expected Implementation Surface

Existing files likely modified:

- `src/job_hunter/models.py`
- `src/job_hunter/telegram.py`
- `src/job_hunter/pipeline.py`
- `src/job_hunter/store.py`
- `src/job_hunter/config.py`
- `src/job_hunter/http.py`
- `scripts/restore_state.py`
- `tests/test_telegram.py`
- `tests/test_pipeline.py`
- `tests/test_store.py`
- `.github/workflows/daily.yml`
- `README.md`

Likely new files:

- `src/job_hunter/telegram_navigation.py`
- `src/job_hunter/github_state.py`
- `src/job_hunter/telegram_webhook.py`
- `tests/test_telegram_navigation.py`
- `tests/test_github_state.py`
- `tests/test_telegram_webhook.py`
- `scripts/set_telegram_webhook.py`
- `Dockerfile.telegram-webhook`

The future Apply workflow should be able to replace only the placeholder callback behavior without redesigning navigation.
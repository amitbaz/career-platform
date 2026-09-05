# Telegram Vercel Webhook Architecture Design

**Date:** 2026-09-01

## Status

Approved and implemented on `feature/telegram-vercel-webhook`.

## Context

The job hunter runs as a scheduled GitHub Actions workflow. It discovers and evaluates jobs, persists state in SQLite, sends Telegram messages/PDFs, uploads `job-hunter-state`, and exits.

The Telegram job navigator introduces interactive `Previous`, `Next`, and placeholder `Apply` buttons. Telegram callback queries can arrive long after the GitHub Actions run has finished, so they require a stable public HTTPS endpoint.

The longer-term direction is to gradually move Job Hunter Bot state from SQLite to the same Supabase ecosystem used by the Interviewer App without blocking current feature development.

## Decision

Deploy the Telegram callback receiver as a **Vercel-hosted Python/Flask Function in the Job Hunter Bot repository** and keep Telegram interaction independent of persistence through a `NavigationSessionRepository` interface.

Current implementation:

```text
GitHubArtifactNavigationRepository
    -> download latest job-hunter-state artifact
    -> open SQLite snapshot read-only
    -> load Telegram navigation session
```

Future implementation:

```text
SupabaseNavigationRepository
    -> query Supabase
    -> load Telegram navigation session
```

The Telegram callback handler does not know which storage implementation is active.

## Goals

1. Give Telegram a stable HTTPS callback endpoint without operating an always-on custom server.
2. Keep the existing GitHub Actions cron and SQLite persistence unchanged.
3. Deploy the webhook as a separate Vercel project from the Interviewer App.
4. Preserve existing Telegram navigator behavior and security.
5. Introduce a storage boundary that makes the later SQLite -> Supabase migration small and explicit.
6. Document the future migration so the decision does not need to be rediscovered later.

## Non-goals

- No Supabase migration in this change.
- No Supabase SDK dependency or schema migration.
- No scheduler move away from GitHub Actions.
- No Interviewer App deployment changes.
- No `Apply` implementation.
- No queue/Redis/new database.
- No Supabase Edge Function yet.

## Current production architecture

```text
GitHub Actions cron
        |
        | discovery / evaluation / PDFs
        v
SQLite: var/job_hunter.sqlite3
        |
        | uploaded after run
        v
GitHub Actions artifact: job-hunter-state

Telegram job card
        |
        | callback_query
        v
Vercel Python Function
        |
        v
NavigationSessionRepository
        |
        v
GitHubArtifactNavigationRepository
        |
        v
latest SQLite artifact snapshot
        |
        v
Telegram editMessageText
```

## Future architecture

```text
Job Hunter pipeline -------------------+
                                        |
                                        v
                                    Supabase
                                        ^
                                        |
Telegram -> Vercel webhook -> SupabaseNavigationRepository
                                        ^
                                        |
                               Interviewer App
```

Whether the webhook later stays on Vercel or moves to a Supabase Edge Function is intentionally deferred.

## Component boundaries

### Telegram callback domain logic

`telegram_navigation.py` remains responsible for callback payloads, index validation, card rendering, placeholder Apply acknowledgement, and Telegram edit operations. It remains unaware of GitHub, SQLite, Vercel, and Supabase.

### Navigation session repository

```python
class NavigationSessionRepository(Protocol):
    def get_session(self, session_id: str) -> NavigationSession | None: ...
```

`GitHubArtifactNavigationRepository` owns current storage mechanics: load the newest artifact, open SQLite read-only, load the session, and close the connection. Network/API exceptions propagate to the HTTP adapter for safe user-facing translation.

### Flask webhook application

`telegram_webhook.py` remains an application factory and owns:

- `GET /health`;
- `POST /telegram/webhook`;
- constant-time Telegram secret verification;
- JSON/callback validation;
- Apply/no-op short-circuiting without storage access;
- session loading through `NavigationSessionRepository` only;
- safe error acknowledgements without leaking credentials or exception text.

### Vercel deployment adapter

Root `main.py` exports `app = create_app()` and makes the repository `src/` layout importable.

`pyproject.toml` declares:

```toml
[tool.vercel]
entrypoint = "main:app"
```

Flask remains in the `webhook` optional dependency so the scheduled bot does not install web-runtime packages unnecessarily. Current Vercel documentation expects runtime dependencies to be explicitly installed, therefore `vercel.json` defines:

```json
{
  "installCommand": "pip install -e '.[webhook]'"
}
```

This replaces the previous Docker/Gunicorn deployment path. The Vercel project is dedicated to Job Hunter Bot infrastructure and is separate from Interviewer App.

## Environment variables

Required by the Vercel webhook now:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
GITHUB_REPOSITORY=amitbaz/job-hunter-bot
GITHUB_STATE_TOKEN
GITHUB_STATE_ARTIFACT_NAME=job-hunter-state
GITHUB_STATE_CACHE_DIR=/tmp/job-hunter-state
```

The webhook does not need Gemini, candidate profile, cover letter, Telegram chat ID, or Gmail credentials.

`GITHUB_STATE_TOKEN` must be server-side and repository-scoped with only the permissions needed to read Actions artifacts.

Future Supabase credentials are deliberately not added now. When the repository switches to Supabase, only minimum server-side credentials for the chosen access model should be introduced; secret/service-role credentials must never be exposed to clients or callback payloads.

## Vercel runtime model

Vercel is request-driven compute, not an always-running process. Telegram POSTs a callback, Vercel invokes Flask, the handler returns, and process-local state may disappear.

Therefore every navigation callback loads state from external persistence. `/tmp` artifact caching is only an optimization; correctness never depends on cache persistence.

## Data flow

1. Telegram POSTs `callback_query` to `/telegram/webhook`.
2. Flask verifies `X-Telegram-Bot-Api-Secret-Token`.
3. Callback data is parsed.
4. Apply/no-op actions return without repository access.
5. Previous/Next calls `NavigationSessionRepository.get_session(session_id)`.
6. Current repository reads the latest SQLite artifact snapshot.
7. Domain logic renders the requested card.
8. Telegram `editMessageText` edits the existing message.
9. The callback is acknowledged.

### Synchronization window

The GitHub Action writes navigation state before sending the Telegram card, while the artifact is uploaded later in the workflow. A very fast button press can therefore arrive before the latest session is downloadable.

The existing response remains:

```text
Job list is still syncing. Try again shortly.
```

No stale session is substituted.

## Error handling

- Invalid webhook secret -> HTTP 403 before storage access.
- Invalid JSON -> HTTP 400.
- Non-callback update -> HTTP 200, ignored.
- Artifact/API failure -> `Could not load this job list right now.` and HTTP 200.
- Missing session -> `Job list is still syncing. Try again shortly.`.
- Expired session -> `This job list has expired.`.
- Telegram edit failure -> `Could not update this job right now.`.

Valid Telegram webhook requests return HTTP 200 after safe application-level acknowledgement so Telegram does not unnecessarily retry them.

## Security

1. Verify Telegram's secret header before callback processing.
2. Use `hmac.compare_digest`.
3. Never put credentials in callback payloads.
4. Keep GitHub token and future Supabase secrets server-side.
5. Scope `GITHUB_STATE_TOKEN` to this repository and read-only Actions access where possible.
6. Do not log secrets/full environment dumps.
7. Keep SQLite artifact access read-only.
8. Do not treat the Vercel URL itself as authentication.

## Supabase migration path

### Phase A — current

- GitHub Actions is scheduler/worker.
- SQLite is source of truth.
- Vercel receives Telegram callbacks.
- `GitHubArtifactNavigationRepository` reads sessions.

### Phase B — gradual Supabase writes

Migrate bot persistence incrementally. When navigation sessions are reliably written to Supabase, implement `SupabaseNavigationRepository` with the same `get_session(session_id)` contract.

Temporary dual-write is acceptable if needed, but the webhook should use one explicit read source at a time. Avoid a permanent fallback chain because it can hide failed migrations and stale state.

### Phase C — Supabase navigation source of truth

- Switch Vercel to `SupabaseNavigationRepository`.
- Remove GitHub artifact access from the webhook.
- Remove artifact-specific Vercel credentials.
- Keep Telegram callback payloads and rendering unchanged.

### Phase D — optional Supabase Edge Function

Once Python/SQLite artifact access is gone, evaluate moving only the thin HTTP adapter to a Supabase Edge Function. This is optional; staying on Vercel remains valid. Decide based on ownership, observability, latency, deployment workflow, and cost.

## Testing

The implementation is covered by:

- repository contract tests, including read-only SQLite access;
- webhook dependency-injection tests;
- Apply/no-op tests ensuring no storage read;
- Vercel entrypoint import test;
- full regression suite including Gmail, discovery, PDF, Telegram, artifact, and pipeline behavior.

Final implementation verification on the feature branch:

```text
326 passed, 0 failed
```

## Acceptance criteria

1. Webhook is Vercel-compatible Flask/Python.
2. No Docker host is required.
3. Handler depends on `NavigationSessionRepository` only.
4. Current sessions still come from the SQLite Actions artifact.
5. No Supabase runtime/schema change is introduced.
6. GitHub Actions remains scheduler.
7. Interviewer App is untouched.
8. Full tests pass.
9. Operational docs record current deployment and future migration.

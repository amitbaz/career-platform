# Telegram Job Navigator

> **Legacy.** The Telegram bot is not part of the product and is being retired
> ([docs/product-vision.md](../../docs/product-vision.md)). This describes what exists until it
> is removed; do not extend it.

The bot delivers matching jobs as one interactive Telegram card instead of one long digest. `Previous` and `Next` edit the same message in place. `View job` opens the source posting. `Apply` is intentionally a placeholder and only shows `Apply functionality coming soon.`

## Current architecture

The scheduled job-search pipeline runs in GitHub Actions and Postgres (the shared Supabase
project) is the source of truth. The webhook reads live Postgres through
`PostgresNavigationRepository`, whose store is wrapped in `DryRunStore` so the internet-facing
webhook process cannot write — reads pass through, writes are silently discarded. That guards
against a future webhook edit corrupting state, not against cross-user exposure: row-level
security, scoped by a per-user token, already prevents that.

```text
GitHub Actions cron
        |
        | discover / evaluate
        v
Postgres: shared Supabase project (job_hunter_* tables)
        ^
        | PostgresJobStore, per-user ES256 token, RLS-scoped
        |
Telegram "Gen CL" tap
        |
        | callback_query
        v
Vercel Python/Flask Function
        |
        | repository_dispatch (generate_cover_letter)
        v
GitHub Actions: generate-cover-letter.yml
        |
        v
generate PDF + mark_delivered

Telegram card (Previous/Next/View job/Apply)
        |
        | callback_query
        v
Vercel Python/Flask Function
        |
        v
NavigationSessionRepository
        |
        v
PostgresNavigationRepository
        |
        v
DryRunStore(PostgresJobStore)  -- reads live, writes discarded
        |
        v
Telegram editMessageText
```

The webhook is request-driven serverless compute. There is no always-on custom server and no Docker host to operate.

The Vercel project should be a **separate project for `job-hunter-bot`**, not an API route inside the Interviewer App. Both projects may live in the same Vercel account/team, but they remain independently deployable.

## Storage boundary

The HTTP webhook does not know how navigation state is stored. It depends on:

```python
class NavigationSessionRepository(Protocol):
    def get_session(self, session_id: str) -> NavigationSession | None: ...
```

Today the concrete repository is `PostgresNavigationRepository`, which reads live Postgres
through a `PostgresJobStore` wrapped in `DryRunStore` (built lazily, at most once, on first real
use — so a missing Supabase env var surfaces at `/health` rather than at import time).

## Vercel deployment

The webhook is deployed as a Vercel Flask/Python Function from the repository root. The Flask application remains in `main.py`, and `pyproject.toml` declares:

```toml
[tool.vercel]
entrypoint = "main:app"
```

The repository must also explicitly tell Vercel to use its Flask backend pipeline. This is important because the project was initially imported with the dashboard framework preset **Other**. The repository config is the durable source of truth:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": "flask",
  "installCommand": "pip install -e '.[webhook]'",
  "functions": {
    "main.py": {
      "maxDuration": 30,
      "excludeFiles": "{tests/**,.superpowers/**,docs/**,var/**}"
    }
  }
}
```

`framework: "flask"` is not cosmetic. Without Flask framework detection, Vercel can create a deployment that reports READY but contains no Python Lambda. `pip install -e '.[webhook]'` keeps Flask out of the normal scheduled-bot dependency set while guaranteeing that Flask and the webhook package are installed during the Vercel build.

### 1. Create the Vercel project

Create a separate Vercel project connected to:

```text
amitbaz/job-hunter-bot
```

Recommended project name:

```text
job-hunter-bot
```

Use the repository root as the project root. It is fine if the initial dashboard framework preset is **Other**, because `vercel.json` explicitly overrides the deployment framework to Flask. Do not point this deployment at the Interviewer App.

### 2. Configure production environment variables

Set these as **server-side Vercel environment variables**:

```text
TELEGRAM_BOT_TOKEN=<same bot token used by the daily runner>
TELEGRAM_WEBHOOK_SECRET=<random URL-safe secret>
GITHUB_REPOSITORY=amitbaz/career-platform
GITHUB_DISPATCH_TOKEN=<repository-scoped token with permission to trigger repository_dispatch>
JOB_HUNTER_USER_ID=<UUID of the platform user the webhook acts for>
SUPABASE_URL=<base URL of the Supabase project>
SUPABASE_PUBLISHABLE_KEY=<Supabase project's publishable API key>
SUPABASE_SIGNING_KEY_B64=<base64-encoded private ES256 JWK>
```

All eight are required. `create_app()` builds its Supabase client lazily so a missing variable
does not fail the whole app at import time — `/health` reports HTTP 503 with the exact names of
whichever variables are missing, never their values. `/telegram/webhook` itself still fails fast
on a missing variable, since accepting a callback it cannot serve is worse than refusing it.

The webhook does **not** need:

```text
GEMINI_API_KEY
CANDIDATE_PROFILE_B64
COVER_LETTER_TEMPLATE_B64
TELEGRAM_CHAT_ID
GMAIL_CLIENT_ID
GMAIL_CLIENT_SECRET
GMAIL_REFRESH_TOKEN
```

`GITHUB_DISPATCH_TOKEN` and `SUPABASE_SIGNING_KEY_B64` must remain server-side. Do not put either
in Telegram callback data, URLs, logs, or browser-exposed environment variables.
`SUPABASE_SIGNING_KEY_B64` in particular can mint a token for any user — it is the platform's
most sensitive secret.

Generate a webhook secret locally, for example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use the same value for the Vercel `TELEGRAM_WEBHOOK_SECRET` variable and webhook registration.

### 3. Deploy

Vercel's Git integration creates preview deployments for branch pushes and a production deployment from `main`. Preview deployments should be used to verify build/runtime behavior before merging deployment changes. Telegram should ultimately be registered against the stable production domain.

### 4. Verify health

```bash
curl https://YOUR-VERCEL-DOMAIN/health
```

Expected:

```json
{"ok":true}
```

A deployment is not considered healthy merely because Vercel reports READY. `/health` must return HTTP 200 from the Flask application.

### 5. Register Telegram

Set the local values used by the registration helper:

```bash
export TELEGRAM_BOT_TOKEN='...'
export TELEGRAM_WEBHOOK_SECRET='...'
```

Register the stable production URL:

```bash
python scripts/set_telegram_webhook.py \
  --url https://YOUR-VERCEL-DOMAIN/telegram/webhook
```

The helper registers only `callback_query` updates and configures Telegram to send `X-Telegram-Bot-Api-Secret-Token`. The Flask route rejects requests whose secret header does not match.

Re-register whenever the production webhook URL or webhook secret changes.

## Expected Telegram behavior

A batch with 12 deliverable jobs appears as one message:

```text
Senior Frontend Developer

Company: Example GmbH
Location: Berlin
Match: 87%

[ View job ]  [ Apply ]  [ Gen CL ]
[ ◀ Previous ]  [ 3 / 12 ]  [ Next ▶ ]
```

Jobs are ordered by:

1. match score descending;
2. company ascending;
3. title ascending;
4. job ID ascending.

Navigation does not wrap at the first or last job.

## Session lookup

The webhook reads the navigation session directly from Postgres on every callback — there is no
artifact to wait for, so a session written by the daily pipeline is visible to the webhook
immediately. The "still syncing" response is now only a defensive path for a genuinely missing or
not-yet-visible session (a `None` result from `get_session`), not the routine race it used to be
against a slow artifact upload:

```text
Job list is still syncing. Try again shortly.
```

Navigation sessions remain in Postgres for 30 days and are pruned by later pipeline runs.

## Failure behavior

- Wrong Telegram secret: HTTP 403 before storage access.
- Invalid JSON: HTTP 400.
- Non-callback Telegram updates: ignored with HTTP 200.
- Postgres/lookup failure: callback says `Could not load this job list right now.`.
- Missing session: callback says `Job list is still syncing. Try again shortly.`.
- Expired session: callback says `This job list has expired.`.
- Telegram edit failure: callback says `Could not update this job right now.`.

Valid Telegram requests are acknowledged with HTTP 200 after application-level failures so Telegram does not unnecessarily retry them.

## Existing bot behavior preserved

- GitHub Actions remains the scheduler.
- Postgres remains the source of truth.
- Deliverability remains score `> 60` plus the current decision allowlist.
- Cover letters are no longer generated automatically; tapping Gen CL on a job's card
  triggers generation (or resends an already-generated letter) for that job only.
- Failed Telegram card delivery leaves jobs pending for the next run.
- A successful card marks all represented jobs as `telegram_message` delivered.
- The bot still sends nothing when a run has no new/pending deliverable jobs.
- `Apply` does not submit or mark an application.

## Supabase migration (completed)

The webhook originally read navigation state from the `job-hunter-state` GitHub Actions artifact
via `GitHubArtifactNavigationRepository`. That migration is done: the concrete repository is now
`PostgresNavigationRepository`, reading live Postgres (the shared Supabase project) through a
`DryRunStore`-wrapped `PostgresJobStore`, as described in "Current architecture" and "Storage
boundary" above. `GITHUB_STATE_TOKEN`, `GITHUB_STATE_ARTIFACT_NAME`, and
`GITHUB_STATE_CACHE_DIR` no longer exist as webhook configuration; see "Configure production
environment variables" for the current required set.

A future move of the thin Telegram HTTP adapter from Vercel to a Supabase Edge Function remains
possible but is not planned work — evaluate it on deployment ownership, observability, latency,
and cost if it comes up, not merely because the data already lives in Supabase.

## Troubleshooting

### Vercel reports `unmatched-function-pattern`

If the build says that `main.py` does not match a Serverless Function under `/api`, confirm the repository contains:

```json
"framework": "flask"
```

The project was initially imported with the **Other** preset. Root `main.py` function configuration is valid when Vercel is actually using its Flask backend pipeline.

### Vercel says READY but `/health` returns platform 404

Check the build logs. If the build completes almost instantly and never installs Python/Flask dependencies, Vercel produced an empty deployment rather than the webhook Lambda. Confirm `vercel.json` still declares `"framework": "flask"` and retains `functions.main.py`.

The successful reference behavior is a build that installs `.[webhook]`, reports a Python Lambda, and serves:

```json
{"ok":true}
```

from `/health`.

### Vercel build cannot import Flask

Confirm `vercel.json` still contains:

```text
pip install -e '.[webhook]'
```

and `pyproject.toml` still defines Flask in the `webhook` optional dependency.

### `/health` works but navigation fails

Check the Vercel runtime logs and confirm all four Supabase variables (`JOB_HUNTER_USER_ID`,
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`) are set correctly on the
Vercel project — `/health` reports 503 with the exact missing names if any are absent, but a
wrong (not merely missing) value will pass that check and still fail navigation.

### New card says it is still syncing

This should be rare now that the webhook reads Postgres directly rather than a periodically
uploaded artifact. Retrying the button should succeed within moments; if it persists, check
Postgres connectivity and Supabase project status rather than waiting on a GitHub Actions run.

### Telegram sends 403

The value registered with Telegram and the Vercel `TELEGRAM_WEBHOOK_SECRET` must match exactly. Re-run `scripts/set_telegram_webhook.py` after changing the secret.

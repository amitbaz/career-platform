# Monorepo migration runbook

## Status: complete

Every step in this runbook has been carried out. It is kept as a record of what was moved and
how, not as a list of work to do.

The two items that were outstanding when this was last written are both closed:

- **The missing Supabase migration is applied.** `202609040001_park_moves_off_target` was the
  one the database lacked, which made `record_conversation_turn` fail with `PGRST202`.
  `supabase db push` against `jpushgtdtkawrjxqjagk` now reports the remote database up to date.
- **The three Gmail secrets are migrated** (2026-09-07), so the daily Gmail sync step runs
  rather than being skipped by `continue-on-error`.

One thing has changed direction since: four of the secrets §2 describes are **no longer GitHub
secrets at all**. Issue #72 moved the Gemini and Brave API keys into Supabase Vault and the CV
and cover letter text into `source_documents`, both per user and read at run time through
`PostgresJobStore`. `GEMINI_API_KEY`, `BRAVE_SEARCH_API_KEY`, `CANDIDATE_PROFILE_B64` and
`COVER_LETTER_TEMPLATE_B64` were deleted from the repository's secrets on 2026-09-08, after
confirming no workflow passes them and no code reads them. Do not re-add them: the runtime
would not look at them, and a stale key in two places is worse than a key in one.

---

The code move (issue #1) landed in this repository. The work the sections below cover was the
configuration that lives outside Git: GitHub Actions secrets, Vercel project settings, the Job
Hunter state artifact, and the Telegram webhook.

The sections are in the order they were worked. Each ends with a check, which is still the way
to confirm that part of the setup is intact.

---

## 1. GitHub Actions variables — done

The five non-secret variables were copied from `amitbaz/job-hunter-bot`:

| Variable                     | Value                    |
| ---------------------------- | ------------------------ |
| `BRAVE_MONTHLY_QUERY_LIMIT`  | `1000`                   |
| `GEMINI_FREE_RPD`            | `500`                    |
| `GEMINI_FREE_RPM`            | `15`                     |
| `GEMINI_FREE_TPM`            | `250000`                 |
| `GEMINI_MODEL`               | `gemini-3.5-flash-lite`  |

Check: `gh variable list --repo amitbaz/career-platform`

---

## 2. GitHub Actions secrets — done

Secret values cannot be read back out of GitHub, so these had to be re-entered by hand.

| Secret                       | Used by                          | Still a GitHub secret?                   |
| ---------------------------- | -------------------------------- | ---------------------------------------- |
| `TELEGRAM_BOT_TOKEN`         | daily run, cover letter          | yes                                      |
| `TELEGRAM_CHAT_ID`           | daily run, cover letter          | yes                                      |
| `GMAIL_CLIENT_ID`            | daily Gmail sync                 | yes                                      |
| `GMAIL_CLIENT_SECRET`        | daily Gmail sync                 | yes                                      |
| `GMAIL_REFRESH_TOKEN`        | daily Gmail sync                 | yes                                      |
| `GEMINI_API_KEY`             | daily run, cover letter          | no — Supabase Vault, per user (#72)      |
| `BRAVE_SEARCH_API_KEY`       | daily run                        | no — Supabase Vault, per user (#72)      |
| `CANDIDATE_PROFILE_B64`      | daily run, cover letter          | no — `source_documents`, per user (#72)  |
| `COVER_LETTER_TEMPLATE_B64`  | daily run, cover letter          | no — `source_documents`, per user (#72)  |

The Postgres store added four more that the workflows do read from the environment:
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64` and
`JOB_HUNTER_USER_ID`. `SUPABASE_SIGNING_KEY_B64` is the private JWK of the project's ES256
signing key and can mint a token for any user — the most sensitive secret the platform has.

Add a secret at
<https://github.com/amitbaz/career-platform/settings/secrets/actions>, or from a shell:

```bash
gh secret set TELEGRAM_BOT_TOKEN --repo amitbaz/career-platform
# ...repeat per secret; each command prompts for the value
```

If you keep them in a local `.env`, this loop sets the current set at once:

```bash
cd ~/career-platform   # wherever your populated .env lives
for k in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID \
         GMAIL_CLIENT_ID GMAIL_CLIENT_SECRET GMAIL_REFRESH_TOKEN \
         SUPABASE_URL SUPABASE_PUBLISHABLE_KEY SUPABASE_SIGNING_KEY_B64 JOB_HUNTER_USER_ID; do
  v=$(grep "^$k=" .env | cut -d= -f2-)
  [ -n "$v" ] && printf '%s' "$v" | gh secret set "$k" --repo amitbaz/career-platform && echo "set $k"
done
```

The Gemini and Brave keys and the two documents are deliberately absent from that loop. They
are saved per user in Relay — **Profile → Replace source information** for the documents,
**Provider credentials** for the keys — and read from Postgres at run time.

Check: `gh secret list --repo amitbaz/career-platform` shows those nine names and none of the
four that moved.

---

## 3. Local development env

Both apps read their own env file; neither reads the repository root.

`apps/job-hunter/.env` — copy from `apps/job-hunter/.env.example`. Beyond the secrets above
it needs `GEMINI_MODEL`, `BRAVE_MONTHLY_QUERY_LIMIT`, `GEMINI_FREE_RPM`, `GEMINI_FREE_TPM`,
`GEMINI_FREE_RPD`, and — only if you run the Telegram webhook locally —
`TELEGRAM_WEBHOOK_SECRET`, `GITHUB_REPOSITORY`, `GITHUB_STATE_TOKEN`, `GITHUB_DISPATCH_TOKEN`.
The Gemini and Brave keys and the CV and cover letter are not env vars: a local run reads them
from Postgres for `JOB_HUNTER_USER_ID`, exactly as the scheduled run does.

`apps/relay/.env.local` — copy from `apps/relay/.env.example`:
`NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`, `GEMINI_API_KEY`,
`GEMINI_MODEL`.

---

## 4. Vercel — manual

Two projects currently build from the old repositories:

| Project           | Currently linked to        | Needs to become                                    |
| ----------------- | -------------------------- | -------------------------------------------------- |
| `interviewer-app` | `amitbaz/interviewer-app`  | `amitbaz/career-platform`, root `apps/relay`        |
| `job-hunter-bot`  | `amitbaz/job-hunter-bot`   | `amitbaz/career-platform`, root `apps/job-hunter`   |

For each project, in Settings:

1. **Git** → disconnect the old repository, connect `amitbaz/career-platform`.
   `career-platform` is private, so the Vercel GitHub app needs access granted to it.
2. **Build and Deployment → Root Directory** → set to `apps/relay` or `apps/job-hunter`.
   Leave *Include files outside the root directory* **enabled** — Relay needs the workspace
   `pnpm-lock.yaml` at the repository root.
3. **Environment Variables** → these carry over unchanged, except `GITHUB_REPOSITORY` on the
   Job Hunter project, which becomes `amitbaz/career-platform`.

Job Hunter project env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`,
`GITHUB_REPOSITORY`, `GITHUB_STATE_TOKEN`, `GITHUB_DISPATCH_TOKEN`, and optionally
`GITHUB_STATE_ARTIFACT_NAME` / `GITHUB_STATE_CACHE_DIR`.

Relay project env vars: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY`,
`GEMINI_API_KEY`, `GEMINI_MODEL`.

> `GITHUB_STATE_TOKEN` and `GITHUB_DISPATCH_TOKEN` are personal access tokens scoped to a
> repository. `career-platform` is private, so both must be reissued or re-scoped to grant
> `actions: read` (state) and `contents: write` / `repository_dispatch` (dispatch) on
> `amitbaz/career-platform`. Tokens scoped only to `job-hunter-bot` will fail silently at
> runtime.

Check: push to `main`, then confirm one successful deployment per project.

---

## 5. Job Hunter state artifact — one-shot (SUPERSEDED)

> **Superseded by issue #70.** Job Hunter's state is in Postgres now; `scripts/restore_state.py`
> and the `job-hunter-state` artifact are both deleted, so there is nothing to seed and this
> step cannot be performed. The one-time carry-over of existing history is the data migration
> in `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py` — see the cutover runbook in
> `apps/job-hunter/README.md`. The rest of this section is kept as a record of what was
> planned.

`scripts/restore_state.py` restores `var/job_hunter.sqlite3` from the *current* repository's
latest `job-hunter-state` artifact. That artifact history lives on `amitbaz/job-hunter-bot`,
so without seeding, the first scheduled run here starts from an empty database and re-notifies
jobs it has already seen.

**This is done.** `job-hunter-bootstrap-state.yml` copied the newest artifact across on
2026-09-05 (20,376,963 bytes, byte-identical to the source) and has since been deleted, since
re-running it would overwrite newer state with the old repository's snapshot.

The rest of this section is kept as a record of how it was done. To repeat it, restore the
workflow from history — `git show 7a27774:.github/workflows/job-hunter-bootstrap-state.yml` —
and run:

```bash
gh workflow run job-hunter-bootstrap-state.yml --repo amitbaz/career-platform
gh run watch --repo amitbaz/career-platform
```

Then confirm the artifact landed:

```bash
gh api repos/amitbaz/career-platform/actions/artifacts \
  --jq '.artifacts[] | select(.name=="job-hunter-state") | {id, size_in_bytes, created_at}'
```

It should be roughly 20 MB. Once it is there, delete the bootstrap workflow — it exists only
for this migration and re-running it would overwrite newer state with the old repository's
snapshot.

Artifacts expire after 90 days, so do this before the old repository's artifacts age out.

---

## 6. Telegram webhook — manual

The webhook only needs re-registering if the Job Hunter deployment URL changed. Repointing an
existing Vercel project keeps its domain, so usually there is nothing to do.

If the URL did change:

```bash
cd apps/job-hunter
TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=... \
  .venv/bin/python scripts/set_telegram_webhook.py --url https://<new-domain>/telegram/webhook
```

Check: `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"` reports the expected URL and
no `last_error_message`.

---

## 7. Scheduling — cron-job.org

The daily run is scheduled externally by cron-job.org, which calls the GitHub
`workflow_dispatch` API. `job-hunter-daily.yml` therefore has **no `schedule:` trigger** — that
is deliberate, so GitHub never starts a second run alongside the external one.

The cron-job.org job still points at the old repository. Update its request URL to:

```text
POST https://api.github.com/repos/amitbaz/career-platform/actions/workflows/job-hunter-daily.yml/dispatches
```

Note the workflow filename changed from `daily.yml` to `job-hunter-daily.yml`. The body stays
`{"ref":"main"}`, and the headers stay `Accept: application/vnd.github+json` plus
`Authorization: Bearer <PAT>`.

> `career-platform` is private. The PAT that cron-job.org sends must be re-scoped or reissued
> with `actions: write` on `amitbaz/career-platform`; a token scoped only to `job-hunter-bot`
> returns 404 rather than a clear permission error.

Check: trigger the cron-job.org job manually and confirm a `workflow_dispatch` run appears via
`gh run list --repo amitbaz/career-platform --workflow job-hunter-daily.yml`.

---

## 8. Old repositories

`amitbaz/job-hunter-bot` and `amitbaz/interviewer-app` stay in place, but the Job Hunter
workflows there are disabled so the two repositories cannot both run the hunt, send Telegram
messages, and write divergent state:

```bash
gh workflow disable daily.yml --repo amitbaz/job-hunter-bot
gh workflow disable generate-cover-letter.yml --repo amitbaz/job-hunter-bot
```

Cover-letter buttons in Telegram stay broken until the Vercel webhook is repointed at
`career-platform` (§4), since the webhook dispatches to whatever `GITHUB_REPOSITORY` names.

---

## Order of operations

1. ~~Merge PR #2.~~ Done.
2. ~~Add the Actions secrets (§2).~~ Done — the six the daily run needed at the time. The
   three Gmail secrets followed later (item 8), and four of the six have since moved out of
   GitHub entirely (item 10).
3. ~~Run the state bootstrap workflow once, then delete it (§5).~~ Done — 20,376,963 bytes,
   byte-identical to the old repository's latest artifact. The workflow has been deleted.
4. ~~Repoint the cron-job.org job at the new workflow URL and re-scope its PAT (§7).~~ Done.
5. ~~Repoint both Vercel projects and fix `GITHUB_REPOSITORY` (§4).~~ Done — both projects now
   build `amitbaz/career-platform` with root directories `apps/job-hunter` and `apps/relay`.
6. ~~Reissue `GITHUB_STATE_TOKEN` and `GITHUB_DISPATCH_TOKEN` against `career-platform`
   (§4).~~ Done — one fine-grained PAT scoped to `career-platform` with Actions: Read (the
   state token reads artifacts) and Contents: Read and write (the dispatch token posts to
   `/dispatches`), set as both variables, then redeployed so the running instance picks them
   up.
7. ~~Trigger `job-hunter-daily.yml` manually and confirm it runs.~~ Done — the scheduled run
   has been running daily since. "Restores state and uploads" no longer applies: state is in
   Postgres and neither workflow touches an artifact (§5).
8. ~~Migrate the three Gmail secrets.~~ Done 2026-09-07. They were deliberately skipped at
   first, since the Gmail sync step is `continue-on-error` and the daily run succeeded without
   them, simply skipping inbox intelligence.
9. ~~Apply `202609040001_park_moves_off_target` to the hosted database.~~ Done — `supabase db
   push` reports the remote database up to date.
10. ~~Move the per-user keys and documents out of repository secrets (#72) and delete the four
    GitHub secrets they replaced.~~ Done 2026-09-08.

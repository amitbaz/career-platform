# Job Hunter Bot

A daily, mostly hands-off job-hunting assistant that runs on GitHub Actions. It reads Gmail job signals, discovers public remote job postings, deduplicates them in SQLite, evaluates each one against your candidate profile with Gemini, and delivers a concise digest through Telegram. Tapping "Gen CL" on a job's card triggers on-demand cover letter drafting and PDF rendering for that job.

The bot **never submits applications**. It prepares material for you to review and send yourself — see [v1 safety boundary](#v1-safety-boundary) below.

## Project direction

Job Hunter Bot currently runs as a standalone Python application using SQLite for persistence.

The longer-term direction is to share the same Supabase/Postgres backend used by the [Interviewer App](https://github.com/amitbaz/interviewer-app). The migration will happen incrementally and must not block ongoing feature development in either project.

Until an individual persistence domain is explicitly migrated, SQLite remains the production source of truth for Job Hunter Bot. New features should avoid unnecessary coupling to SQLite so that persistence can later move behind shared repository/service boundaries without requiring unrelated feature rewrites.

Target ecosystem:

```text
Job Hunter Bot  ->  Supabase/Postgres  <-  Interviewer App
```

This is an evolutionary migration, not a rewrite. Documentation describing the target architecture should not be interpreted as meaning that Supabase is already available or in use by this repository.

## Roadmap

Current product direction includes:

- Improve Telegram job browsing and navigation.
- Add an explicit application workflow and application status tracking.
- Gradually migrate persistent job/application data from SQLite to Supabase/Postgres.
- Share candidate, job, application, and related data with the Interviewer App where appropriate.
- Move toward a unified job-search -> application -> interview-preparation workflow across both projects.

The exact shared schema and migration phases are intentionally not defined here. Each migration phase should have its own design and implementation plan before code changes begin.

## Architecture

```
all public sources (Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, DuckDuckGo, ATS boards)
  -> enrich + dedupe -> profession gate + prefilter -> deterministic ranking or profile-aware ranking
  -> diversity-constrained top-N shortlist (stable-ranking fallback on error) -> Gemini evaluation
  -> Telegram digest delivery
```

Cover letter generation + PDF rendering happens on demand, not as part of the daily run: tapping "Gen CL" on a job's Telegram card fires a `repository_dispatch` GitHub Actions workflow that generates (or resends) that job's cover letter and PDF.

- `src/job_hunter/sources/` — public job discovery adapters: Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, DuckDuckGo query expansion, plus optional Ashby/Lever/Greenhouse ATS boards. Each source fails open: if one adapter errors, the run continues with the rest.
- `config/search.yml` supports role families, query templates, ATS domains, and `max_search_queries_per_run`; DuckDuckGo queries expand each role/template pair across the configured ATS domains before deduping.
- Only software/product-engineering professions reach Gemini. The default evaluation budget is 35 jobs per run, with source-diverse selection (`source_minimum_per_run: 2`, `source_max_share: 0.5`) when profile extraction succeeds.
- `src/job_hunter/preferences.py` extracts a compact preference profile from `CANDIDATE_PROFILE_B64`; `src/job_hunter/ranking.py` then uses preferred roles, seniority, must-have signals, location fit, avoid signals, and source quality to rank eligible jobs before Gemini. If profile extraction or diversity selection fails, the pipeline falls back to the stable deterministic global ranking and logs the fallback without exposing private profile text.
- `skip` evaluations are persisted but never sent to Telegram. Telegram sections are ordered by effective match score descending, unknown decisions are omitted, and only scores strictly greater than 60 are eligible for digest or retry delivery.
- `src/job_hunter/prefilter.py` — cheap deterministic filtering before spending Gemini calls.
- `src/job_hunter/evaluation.py` / `gemini.py` — Gemini-based scoring and rationale.
- `src/job_hunter/cover_letter.py` / `pdf.py` — cover letter drafting and PDF rendering, triggered on demand per job via the "Gen CL" Telegram button.
- `src/job_hunter/store.py` — SQLite persistence (dedup, evaluation cache, delivery tracking) at `var/job_hunter.sqlite3` by default.
- `src/job_hunter/telegram.py` — outbound-only Telegram Bot API delivery (digest message + PDF documents).
- `src/job_hunter/gmail_sync.py` — read-only Gmail intake that classifies job signals and stages discovered jobs or review-needed events in the shared SQLite state.
- `src/job_hunter/pipeline.py` / `cli.py` — orchestration and the `python -m job_hunter run` and `python -m job_hunter sync-gmail` entrypoints.
- `scripts/restore_state.py` — restores the SQLite database from the most recent `job-hunter-state` GitHub Actions artifact before a run, since Actions runners are ephemeral.

SQLite state does not persist on the runner between workflow runs, so the daily workflow restores the previous run's database from an uploaded artifact at the start of each run and re-uploads it at the end (see [Schedule and state persistence](#schedule-and-state-persistence)).

### R2 automated discovery and company watch

R2 adds source-independent job identity, public canonical resolution, provenance, and a lightweight company-watch loop while keeping the existing filter, rank, evaluation, and delivery boundaries intact:

```text
Gmail + existing sources + YC + specialist-domain search + company watch
  -> canonical resolution + provenance/dedupe
  -> existing filter/rank/evaluate/deliver
  -> high_priority/package_match may promote company
```

Every discovered source copy is retained as provenance in SQLite before one logical job proceeds through deduplication. Canonical resolution uses public URLs and may recognize direct ATS listings, public redirects or embedded links, a known watch ATS target, or one targeted public search result. An unresolved lookup keeps the original candidate rather than blocking the run.

Gmail contributes only staged job signals from the read-only intake; its message bodies are not logged by the R2 discovery flow. YC uses public job pages. Wellfound, Welcome to the Jungle, and configured portfolio domains are reached through public targeted search queries. R2 does not perform authenticated scraping, sign into job platforms, or bypass access controls.

An evaluated job can promote its company to a watch only when its final decision is `high_priority` or `package_match`, it has no hard blockers, and it satisfies the configured package threshold. Promotion helps find future public postings; it never submits an application.

#### Manual company watch configuration

Add manual watch entries to `config/search.yml` when you know an employer's public ATS board or careers page:

```yaml
manual_company_watch:
  - company_name: Example GmbH
    ats_provider: greenhouse
    ats_identifier: example
  - company_name: Another Company
    careers_url: https://example.com/careers
```

Manual entries are synchronized idempotently and preserved: automatic promotion cannot replace a manual ownership marker or downgrade its stronger ATS endpoint. Company-watch checks use only the configured public ATS endpoint or public careers URL. Each check records either a success or a failure. After exactly three consecutive failures, the watch pauses for 24 hours; it is retried when that pause expires. A failed retry starts another 24-hour pause, while a successful retry clears the failure count and removes the pause. A failure for one watch does not stop the remaining discovery sources.

### Market-driven search

When `markets:` exists in `config/search.yml`, it is authoritative.
List order is priority order. `query_share` divides the bounded
`max_search_queries_per_run` budget, while every enabled market receives
at least one slot when the budget permits it.

Each market owns locations, required languages, gross base salary floor,
remote/relocation behavior, sponsorship policy, source domains, and query templates.
Unknown salary/sponsorship is not rejection; explicit incompatibility is.

A job is attributed to exactly one market: the highest-scoring enabled market wins, scored from strongest to weakest evidence (an explicit `job.location` match, then explicit remote country/region scope in the location/description, then sponsorship/relocation language tied to a market, then the query-time market hint), with ties broken by configured order and no-evidence jobs falling back to the first enabled market. A city listed under a market's `salary.location_floors` (for example San Francisco under `us_nyc_sf`) overrides that market's overall `gross_base_floor` for jobs attributed there.

The six configured markets, in priority (list) order, with their approved gross base salary floors:

| Market | `query_share` | Locations | Salary floor | Sponsorship |
| --- | --- | --- | --- | --- |
| `germany_eu` | 0.35 | Berlin, Germany, Europe | EUR 90,000 | not required |
| `israel_remote` | 0.25 | Israel, Tel Aviv | ILS 420,000 | not required |
| `london` | 0.17 | London, UK, United Kingdom | GBP 90,000 | required |
| `singapore` | 0.10 | Singapore | SGD 120,000 | required |
| `us_nyc_sf` | 0.10 | New York, NYC, San Francisco, Bay Area | USD 180,000 (San Francisco/Bay Area: 200,000) | required |
| `secondary_eu_relocation` | 0.03 | Amsterdam, Paris, Barcelona | EUR 70,000 (Amsterdam: 90,000; Paris: 80,000) | not required |

Normal tuning — shifting how much search volume a market gets, which job boards are preferred first within a market, or which query phrasing is tried first — should change `query_share`, the order of `source_domains`, or the order of `query_templates` for the relevant market in `config/search.yml`. It should not require code changes.

## Required GitHub secrets

Set these under **Settings -> Secrets and variables -> Actions** on your fork/repo:

| Secret | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Gemini API key used for evaluation and cover letter drafting |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat id to deliver the digest/PDFs to |
| `CANDIDATE_PROFILE_B64` | Base64-encoded plain-text candidate profile/CV |
| `COVER_LETTER_TEMPLATE_B64` | Base64-encoded plain-text cover letter template |
| `GMAIL_CLIENT_ID` | OAuth client ID used only by the Gmail intelligence sync |
| `GMAIL_CLIENT_SECRET` | OAuth client secret used only by the Gmail intelligence sync |
| `GMAIL_REFRESH_TOKEN` | Refresh token printed by the local Gmail OAuth bootstrap |

## Required GitHub Actions variables

Set these under **Settings -> Secrets and variables -> Actions -> Variables** tab. They are your free-tier rate limits, not credentials, so they belong in Variables, not Secrets:

| Variable | Purpose |
| --- | --- |
| `GEMINI_FREE_RPM` | Gemini free-tier requests-per-minute limit, copied from the AI Studio Rate Limits page |
| `GEMINI_FREE_TPM` | Gemini free-tier input-tokens-per-minute limit, copied from the AI Studio Rate Limits page |
| `GEMINI_FREE_RPD` | Gemini free-tier requests-per-day limit, copied from the AI Studio Rate Limits page |

The bot enforces its own ceiling at 80% of each of these three values, and both Gemini-using workflow steps (`sync-gmail` and `run`) fail closed at startup if any of the three is unset. See [Gemini API key and free-tier quota setup](#gemini-api-key-and-free-tier-quota-setup) below for exactly where to read these values and how often to refresh them.

`GEMINI_RUN_ID` is not something you configure: the workflow supplies it automatically as `${{ github.run_id }}` on both Gemini-using steps, so the Gmail sync process and the main pipeline process share one GitHub Actions run id and are accounted against one usage ledger for that run.

Never commit your CV or cover letter template text in plain form. Encode them locally and paste only the base64 output into the GitHub secret:

```bash
base64 -i candidate_profile.txt | tr -d '\n'
base64 -i cover_letter_template.txt | tr -d '\n'
```

(On Linux, `base64 -w0 candidate_profile.txt` produces the same single-line output.)

Paste the resulting string as the secret value. The bot decodes it in memory at runtime; it is never written back to the repo.

## Telegram bot setup

1. In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts to name your bot; BotFather returns a bot token — this is `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot (or add it to a group/channel you want the digest posted to).
3. Find your chat id without exposing the token in git: call `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser or with `curl` locally (substitute your real token only in that local command, never in a committed file), and read the `chat.id` field from the JSON response for your message.
4. Store the bot token and chat id as the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` GitHub secrets above. Do not put either value in `config/search.yml`, `.env`, or any committed file.

## Gemini API key and free-tier quota setup

This bot is designed to run entirely on the Gemini API free tier, at €0 cost. Follow this sequence exactly, in order, both on first setup and any time you change the Gemini project or model:

1. **Keep the Job Hunter Gemini Google Cloud project unlinked from Cloud Billing.** This is an operator-enforced deployment gate, not something the bot's code can verify or turn off. The bot's 80% usage ceilings and its 429 circuit breaker (see below) reduce how much of the free-tier quota gets used, but they cannot make overspending impossible: those guardrails run in application code and have no way to detect or block a linked billing account. If Cloud Billing is ever linked to this project, quota limits can stop being a hard wall and calls could be billed instead of rejected. Confirm "No billing account" in Google Cloud Console's **Billing** page for the project behind your API key, not just in AI Studio.
2. Create a free-tier API key for that unbilled project at [Google AI Studio](https://aistudio.google.com/).
3. Store the key as the `GEMINI_API_KEY` GitHub Actions **secret**.
4. In AI Studio, open **Rate Limits** for the same project and for the model configured via the `GEMINI_MODEL` environment variable (defaulting to `gemini-3.6-flash` if unset; override it as a repo secret or variable, or in your local `.env`, to point at a different model). Read off the RPM (requests/minute), input TPM (tokens/minute), and RPD (requests/day) values shown there.
5. Copy those three numbers into the GitHub Actions **variables** `GEMINI_FREE_RPM`, `GEMINI_FREE_TPM`, and `GEMINI_FREE_RPD` (see [Required GitHub Actions variables](#required-github-actions-variables)). Both Gemini-using workflow steps read these and enforce an 80%-of-quota ceiling before ever calling Gemini.
6. Whenever the Gemini project changes or `GEMINI_MODEL` changes, return to AI Studio's Rate Limits page first and refresh all three variables before the next run — free-tier limits differ per model and per project, and a stale, too-high value would let the app under-protect itself against the real provider limit.
7. Each normal bot run logs one structured `gemini_usage` line (RPD/RPM peak/TPM peak percentages, call count, and token totals) to the GitHub Actions run output. Those percentages are of your actual provider quota (the `GEMINI_FREE_RPD`/`RPM`/`TPM` values above), not of some smaller internal number — read them directly against 100%. Because the app stops itself at 80% of quota and the Gemini project stays unbilled, this line is diagnostic only; there is no matching Telegram message.
8. If Gemini returns HTTP 429 (quota exceeded), the bot does not retry that call automatically and does not fall back to any paid path. It records a pause, defers or skips the affected work for the rest of that run, and Telegram carries a warning; the deferred work is picked up again on a later run once the provider's quota window has reset. Free tier is the only mode this bot runs in — a 429 means "wait," never "switch to paid."

## Local dry run

Copy `.env.example` to `.env`, fill in `GEMINI_API_KEY`, `CANDIDATE_PROFILE_B64`, and `COVER_LETTER_TEMPLATE_B64`, and set `JOB_HUNTER_DRY_RUN=1` to skip Telegram delivery (Telegram credentials are not required in dry-run mode):

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
# edit .env with your values, then:
set -a; source .env; set +a
python -m job_hunter run
```

This runs discovery, profile extraction, source-diverse shortlisting, and evaluation against the local SQLite database at `var/job_hunter.sqlite3` (override with `JOB_HUNTER_DB_PATH`) without sending anything to Telegram. Cover letter/PDF generation is a separate on-demand step (`python -m job_hunter generate-cover-letter --job-id <id>`), not part of this run.

## Gmail intelligence setup

Gmail intelligence reads job-related messages into the shared SQLite state before normal job discovery. It uses the Gmail read-only OAuth scope: Gmail is never modified, and full email bodies are not stored. The sync stores only the privacy-minimized message metadata and extracted job/application signals needed by the bot.

Create an OAuth client for the Gmail API, then run the local bootstrap with the client credentials available only in your shell:

```bash
export GMAIL_CLIENT_ID='...'
export GMAIL_CLIENT_SECRET='...'
python scripts/gmail_oauth_bootstrap.py
```

The bootstrap opens the Google consent flow and prints a refresh token. Store that printed value as the GitHub Actions secret `GMAIL_REFRESH_TOKEN`; also add `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` as GitHub secrets. Never commit any of these values.

Use the following local commands after loading those Gmail variables and `GEMINI_API_KEY`:

```bash
python -m job_hunter sync-gmail --dry-run
python -m job_hunter sync-gmail --force-backfill
```

`--dry-run` classifies and extracts without advancing the Gmail cursor or persisting Gmail-derived state. `--force-backfill` repeats the 120-day backfill idempotently and is non-destructive. A completed sync with individual message errors keeps its cursor so those messages retry on the next sync; setup, authorization, profile, or listing failures return a nonzero status.

The first successful Gmail setup performs a 120-day historical backfill. Historical processing is resumable and intentionally bounded to 100 previously unprocessed messages per sync invocation, so a large mailbox may need multiple workflow runs to finish. Successfully processed message IDs are stored in the SQLite state artifact and skipped on later runs. In GitHub Actions the Gmail step also has a 10-minute fail-open timeout; if it reaches that safety limit, the normal Job Hunter pipeline continues and the next run resumes the remaining Gmail backlog.

## Manual GitHub Actions dispatch

Go to **Actions -> Daily Job Hunter -> Run workflow** to trigger an on-demand run using the current `main` branch and configured secrets. A manual dispatch always runs the full pipeline (`python -m job_hunter run`, no scheduled-hour gate).

## Schedule and state persistence

The repository workflow exposes `workflow_dispatch` only. Daily execution is expected to come from the configured external scheduler, which dispatches **Daily Job Hunter** at the desired local time; GitHub Actions itself has no cron trigger. Every external or manual dispatch runs the full pipeline directly, including the fail-open Gmail sync followed by `python -m job_hunter run`.

Because GitHub Actions runners are ephemeral, the workflow:
1. Restores `var/job_hunter.sqlite3` from the most recent non-expired `job-hunter-state` artifact (via `scripts/restore_state.py`, using the run's `GITHUB_TOKEN`) before running — silently starting with a fresh database if none exists.
2. Runs the read-only Gmail intelligence sync before the normal job pipeline. This step is fail-open, so Gmail setup or service failures do not prevent the public-source job run.
3. Uploads the resulting `var/job_hunter.sqlite3` as a `job-hunter-state` artifact (90-day retention) after the run, even if a prior step failed partway through (as long as the database file was created).

## Adding ATS board slugs

Edit `config/search.yml`'s `ats` section to add direct board adapters, keyed by ATS provider, with a list of board identifiers:

```yaml
ats:
  ashby: ["acme-inc"]
  lever: ["acme"]
  greenhouse: ["acmeinc"]
```

- `ashby`: the board slug from `https://jobs.ashbyhq.com/<slug>`.
- `lever`: the company slug from `https://jobs.lever.co/<slug>`.
- `greenhouse`: the board token from `https://boards.greenhouse.io/<token>`.

Each configured slug adds one additional source adapter queried on every run, alongside the built-in Remotive, Arbeitnow, and DuckDuckGo-search sources.

## v1 safety boundary

The bot prepares application-ready material — it does not submit anything on your behalf. Out of scope for v1, by design:

- Automated submission to employer application forms.
- Answering legal attestations, visa/work-authorization questions, salary commitments, notice period, or demographic questions.
- CAPTCHA/2FA handling or browser automation.

You remain responsible for reviewing and submitting every application yourself.

## Troubleshooting

### No prior state artifact found

On the very first run (or if the `job-hunter-state` artifact has expired past its 90-day retention, or was deleted), `scripts/restore_state.py` logs that no matching artifact was found and exits normally — the bot proceeds with a fresh, empty database. This is expected on first setup and simply means every discovered job is treated as new.

### Gemini quota / rate limits

The pipeline does not implement a Gemini-quota circuit breaker. Each daily run uses one compact profile-extraction call, then up to `max_jobs_per_run` (default 35) independent evaluation calls. A separate on-demand cover-letter call happens only when "Gen CL" is tapped for a given job. If quota or rate limits interrupt the run, each affected job fails independently and can be retried on the next run without blocking the rest. If you see repeated Gemini failures in the Actions log, check your API key's quota/rate limit in Google AI Studio.

### Telegram delivery errors

A failed Telegram send (bad token, bot not started, wrong chat id, message too large) is logged and does not crash the run or discard evaluation results. The job stays evaluated and marked undelivered in SQLite, and later runs retry only the missing Telegram deliveries without re-calling Gemini. Retry eligibility follows the same score floor as the digest: only jobs with final score `>60` are retried, and ready-to-apply jobs retry both the digest message and PDF until both succeed. Verify `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are correct and that you've sent at least one message to the bot (see [Telegram bot setup](#telegram-bot-setup)).

### Flaky web sources

Public source APIs and job boards occasionally time out or return errors. Each source adapter fails open — an exception during discovery for one source is logged and skipped, and the run continues with the remaining sources — so a single flaky source does not abort the whole run. Check the Actions log for `discovery failed` warnings to see which source had trouble on a given run.

# Job Hunter — the engine

The search-and-match engine, which is the product
([ADR-0001](../../docs/adr/0001-the-engine-is-the-product.md),
[product vision](../../docs/product-vision.md)). It ingests job postings from public sources
into one shared corpus, extracts objective facts from each posting once, and matches postings
against each user's profile on demand.

**It is mid-restructure.** Ingestion and enrichment run as queued stages (#181): four Render
cron services defined in [`render.yaml`](../../render.yaml) drain the crawl, facet-extraction,
freshness and posting-recovery queues. Matching is one operation, `match_jobs` (#187). The older
single-process daily run and its `.github/workflows/job-hunter-daily.yml` workflow, which crawled,
scored and delivered a Telegram digest in one pass, are retired (#189) along with that delivery --
Telegram is not part of the product, and nothing today replaces matching-and-delivery until
#260/#261 build the mobile app's own read of the corpus. **Much of the rest of this README still
describes the retired run and Telegram surfaces #287 has not deleted yet.** Where it disagrees
with the code or [`AGENTS.md`](AGENTS.md), trust those.

The engine **never submits applications**. It prepares material for the user to review and send
themselves — see [v1 safety boundary](#v1-safety-boundary) below.

## Project direction

Product direction lives in [docs/product-vision.md](../../docs/product-vision.md); engine
direction in epic #114 and #181.

Postgres (the shared Supabase project) is the production source of truth. Per-user reads and
writes go through `PostgresJobStore` (`src/job_hunter/postgres_store.py`), which reaches
PostgREST with a short-lived per-user ES256 token, so row-level security decides which rows are
visible. The ingestion stages connect to Postgres directly as a privileged role, because the
corpus they write is shared and has no per-user dimension.

## Architecture

```
all public sources (Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, DuckDuckGo, ATS boards)
  -> enrich + dedupe -> profession gate + prefilter -> deterministic ranking or profile-aware ranking
  -> diversity-constrained top-N shortlist (stable-ranking fallback on error) -> Gemini evaluation
  -> Telegram digest delivery
```

Cover letter generation + PDF rendering happens on demand, not as part of the daily run: tapping "Gen CL" on a job's Telegram card fires a `repository_dispatch` GitHub Actions workflow that generates (or resends) that job's cover letter and PDF.

- `src/job_hunter/sources/` — public job discovery adapters: Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, DuckDuckGo query expansion, plus optional Ashby/Lever/Greenhouse ATS boards. Each source fails open: if one adapter errors, the run continues with the rest.
- The user's search profile (stored in Postgres) supports role families, query templates, ATS domains, and `max_search_queries_per_run`; DuckDuckGo queries expand each role/template pair across the configured ATS domains before deduping.
- Only software/product-engineering professions reach Gemini. The default evaluation budget is 35 jobs per run, with source-diverse selection (`source_minimum_per_run: 2`, `source_max_share: 0.5`) when profile extraction succeeds.
- `src/job_hunter/preferences.py` extracts a compact preference profile from the CV stored in Relay Profile; `src/job_hunter/ranking.py` then uses preferred roles, seniority, must-have signals, location fit, avoid signals, and source quality to rank eligible jobs before Gemini. If profile extraction or diversity selection fails, the pipeline falls back to the stable deterministic global ranking and logs the fallback without exposing private profile text.
- `skip` evaluations are persisted but never sent to Telegram. Telegram sections are ordered by effective match score descending, unknown decisions are omitted, and only scores strictly greater than 60 are eligible for digest or retry delivery.
- `src/job_hunter/prefilter.py` — cheap deterministic filtering before spending Gemini calls.
- `src/job_hunter/evaluation.py` — model-based scoring and rationale, through the AI provider port.
- `src/job_hunter/ai/` — the AI provider port: `port.py` (the vocabulary core modules use, including the call class that decides which credential and quota fund a call), `credentials.py`, `usage.py` (quota ledger and 429 circuit breaker), `limits.py` (published free-tier limits per model), and `gemini.py` — the only module that knows Gemini exists.
- `src/job_hunter/cover_letter.py` / `pdf.py` — cover letter drafting and PDF rendering, triggered on demand per job via the "Gen CL" Telegram button.
- `src/job_hunter/postgres_store.py` — Postgres persistence (`PostgresJobStore`: dedup, evaluation cache, delivery tracking) against the shared Supabase project.
- `src/job_hunter/telegram.py` — outbound-only Telegram Bot API delivery (digest message + PDF documents).
- `src/job_hunter/gmail_sync.py` — read-only Gmail intake that classifies job signals and stages discovered jobs or review-needed events in the shared Postgres state.
- `src/job_hunter/cli.py` — entrypoints for the ingestion stages (`crawl-source`, `extract-facets`, `recheck-freshness`, `recover-posting`), `sync-gmail`, and the on-demand `generate-cover-letter`. There is no `run` entrypoint any more (#189): nothing drives crawl-to-delivery as one process.
State now lives in Postgres (the shared Supabase project), not on the Actions runner, so `scripts/restore_state.py` and the artifact restore/upload steps it describes no longer exist.

### R2 automated discovery and company watch

R2 adds source-independent job identity, public canonical resolution, provenance, and a lightweight company-watch loop while keeping the existing filter, rank, evaluation, and delivery boundaries intact:

```text
Gmail + existing sources + YC + specialist-domain search + company watch
  -> canonical resolution + provenance/dedupe
  -> existing filter/rank/evaluate/deliver
  -> high_priority/package_match may promote company
```

Every discovered source copy is retained as provenance in Postgres before one logical job proceeds through deduplication. Canonical resolution uses public URLs and may recognize direct ATS listings, public redirects or embedded links, a known watch ATS target, or one targeted public search result. An unresolved lookup keeps the original candidate rather than blocking the run.

Gmail contributes only staged job signals from the read-only intake; its message bodies are not logged by the R2 discovery flow. YC uses public job pages. Wellfound, Welcome to the Jungle, and configured portfolio domains are reached through public targeted search queries. R2 does not perform authenticated scraping, sign into job platforms, or bypass access controls.

An evaluated job can promote its company to a watch only when its final decision is `high_priority` or `package_match`, it has no hard blockers, and it satisfies the configured package threshold. Promotion helps find future public postings; it never submits an application.

#### Manual company watch configuration

Add manual watch entries to the user's search profile (stored in Postgres) when you know an employer's public ATS board or careers page:

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

When `markets:` exists in the user's search profile, it is authoritative.
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

Normal tuning — shifting how much search volume a market gets, which job boards are preferred first within a market, or which query phrasing is tried first — should change `query_share`, the order of `source_domains`, or the order of `query_templates` for the relevant market in the user's search profile. It should not require code changes.

## Required GitHub secrets

Set these under **Settings -> Secrets and variables -> Actions** on your fork/repo:

| Secret | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat id to deliver the digest/PDFs to |
| `GMAIL_CLIENT_ID` | OAuth client ID used only by the Gmail intelligence sync |
| `GMAIL_CLIENT_SECRET` | OAuth client secret used only by the Gmail intelligence sync |
| `GMAIL_REFRESH_TOKEN` | Refresh token printed by the local Gmail OAuth bootstrap |
| `JOB_HUNTER_USER_ID` | UUID of the platform user a run acts for. Required — the pipeline and webhook read/write Postgres as this user. |
| `SUPABASE_URL` | Base URL of the Supabase project. Required. |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase project's publishable API key, sent as the `apikey` header. Public by design, but required. |
| `SUPABASE_SIGNING_KEY_B64` | Base64-encoded private ES256 JWK used to mint per-user access tokens. It can mint a token for any user — treat it as the platform's most sensitive secret. Required. |
| `PLATFORM_GEMINI_API_KEY` | The platform's own Gemini key, which funds shared objective facet extraction for every user (issue #128). Not a user credential, and deliberately not in Relay Profile. **Effectively required for a useful run:** scoring is fed a posting's facets rather than its description (#126), so with this unset no posting is ever read and no newly discovered job can be scored — a run then delivers only what earlier runs already enriched, and soon nothing. It is unset-safe rather than optional: the run completes, logs a warning, queues the jobs it could not read, and never falls back to a user's key. |

**As of this writing the four Supabase secrets above do not exist yet in either GitHub repository settings or
the Vercel project.** Both the workflows and the Telegram webhook are non-functional until an
operator creates them — see [Cutover runbook](#cutover-runbook-order-matters) below.

The Gemini API key, the Brave Search API key, and your CV and cover letter text are **not** repository secrets. They are per-user values read from Postgres at run time — see [CV, cover letter, and provider keys](#cv-cover-letter-and-provider-keys).

## Cutover runbook (order matters)

> **Run the data migration BEFORE anything deletes the artifact.** The only copy of production
> history is the last `job-hunter-state` artifact, and GitHub expires artifacts after 90 days.
> Once it expires there is no way to recover pre-Postgres history — do not let step 6 happen
> before steps 1-5 are done and verified.

1. Create the four Supabase secrets — `JOB_HUNTER_USER_ID`, `SUPABASE_URL`,
   `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64` — in **both** GitHub repository settings
   and the Vercel project. Merge order relative to this step is irrelevant, but the migration
   below cannot run without them.
2. Apply migrations to the hosted Supabase project: `supabase db push`.
3. Download the latest `job-hunter-state` artifact (from the most recent successful workflow run,
   before it expires) and run `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py` against it.
4. Check the per-table row counts the migration script reports against the source SQLite
   database's counts for every carried table.
5. Trigger the daily workflow manually and confirm it completes end to end against Postgres.
6. **Only then** let the artifact expire naturally. Do not delete it by hand, and do not skip
   ahead to this step before 1-5 are verified.

## Optional GitHub Actions variables

**Nothing here is required.** A run needs a Gemini API key and nothing else: the published free-tier limits for each supported model are defaults in code (`src/job_hunter/ai/limits.py`), and a model with no entry there runs under the most conservative known limits and logs that it did so.

Set these under **Settings -> Secrets and variables -> Actions -> Variables** tab only if you need to override a default. They are rate limits, not credentials, so they belong in Variables, not Secrets:

| Variable | Purpose |
| --- | --- |
| `GEMINI_FREE_RPM` | Override the requests-per-minute default for `GEMINI_MODEL` |
| `GEMINI_FREE_TPM` | Override the input-tokens-per-minute default |
| `GEMINI_FREE_RPD` | Override the requests-per-day default |

Each overrides only the dimension it names; the other two keep their published defaults. The bot enforces its own ceiling at 80% of whichever value is in force. Set one when your project's limits are not the published ones, or when the table in code has gone stale — and see [Gemini API key and free-tier quota setup](#gemini-api-key-and-free-tier-quota-setup) below for where to read the real numbers.

## CV, cover letter, and provider keys

Your CV text, your cover letter text, and your Gemini and Brave Search API keys are per-user
values, read from Postgres at run time rather than the environment. They used to be set through
Relay's Profile screen; Relay is deleted (issue #286) and nothing has replaced that screen yet.

- **Provider keys.** `config.py` still requires a stored Gemini credential at startup
  (`Missing per-user Job Hunter configuration: gemini`) and treats Brave as optional, exactly as
  before. For the single pre-launch user, the row already stored from before Relay's deletion
  keeps working — deleting the app did not delete the data — but there is currently no UI to view,
  rotate, or set one for a new user, and BYOK is being dropped in favor of the platform paying for
  AI itself (`PLATFORM_GEMINI_API_KEY`, issue #128). See issue #293 for dropping the per-user
  Gemini requirement in code.
- **CV and cover letter.** Same story: the last text saved through Relay is still what a run
  reads, but there is no UI to replace it until the new app rebuilds that screen.

## Telegram bot setup

1. In Telegram, message **@BotFather** and send `/newbot`. Follow the prompts to name your bot; BotFather returns a bot token — this is `TELEGRAM_BOT_TOKEN`.
2. Send any message to your new bot (or add it to a group/channel you want the digest posted to).
3. Find your chat id without exposing the token in git: call `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser or with `curl` locally (substitute your real token only in that local command, never in a committed file), and read the `chat.id` field from the JSON response for your message.
4. Store the bot token and chat id as the `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` GitHub secrets above. Do not put either value in the user's search profile, `.env`, or any committed file.

## Gemini API key and free-tier quota setup

This bot is designed to run entirely on the Gemini API free tier, at €0 cost. Follow this sequence exactly, in order, both on first setup and any time you change the Gemini project or model:

1. **Keep the Job Hunter Gemini Google Cloud project unlinked from Cloud Billing.** This is an operator-enforced deployment gate, not something the bot's code can verify or turn off. The bot's 80% usage ceilings and its 429 circuit breaker (see below) reduce how much of the free-tier quota gets used, but they cannot make overspending impossible: those guardrails run in application code and have no way to detect or block a linked billing account. If Cloud Billing is ever linked to this project, quota limits can stop being a hard wall and calls could be billed instead of rejected. Confirm "No billing account" in Google Cloud Console's **Billing** page for the project behind your API key, not just in AI Studio.
2. Create a free-tier API key for that unbilled project at [Google AI Studio](https://aistudio.google.com/).
3. Save the key in Relay under **Profile -> Provider credentials -> Gemini**, signed in as the account whose UUID is `JOB_HUNTER_USER_ID`. It is stored per user, not as a repository secret.
4. That is the whole required setup. The free-tier limits for `GEMINI_MODEL` (defaulting to `gemini-3.5-flash-lite` if unset) come from the table in `src/job_hunter/ai/limits.py`, and a model missing from it runs under the most conservative known limits with a warning in the log. The 429 circuit breaker below is the real safety net either way.
5. Optional: if your project's limits differ from the published ones, open **Rate Limits** in AI Studio for the same project and model, read off RPM (requests/minute), input TPM (tokens/minute) and RPD (requests/day), and set whichever of `GEMINI_FREE_RPM`, `GEMINI_FREE_TPM` and `GEMINI_FREE_RPD` you need as GitHub Actions **variables** (see [Optional GitHub Actions variables](#optional-github-actions-variables)). Each overrides only its own dimension.
6. Whenever the Gemini project or `GEMINI_MODEL` changes, re-check any override you set — a stale, too-high value would let the app under-protect itself against the real provider limit. Overrides you have not set need no attention: they follow the model.
7. Each normal bot run logs one structured `ai_usage` line (RPD/RPM peak/TPM peak percentages, call count, and token totals) to the GitHub Actions run output. Those percentages are of the provider quota in force (the model's defaults, or your overrides), not of some smaller internal number — read them directly against 100%. Because the app stops itself at 80% of quota and the Gemini project stays unbilled, this line is diagnostic only; there is no matching Telegram message.
8. If Gemini returns HTTP 429 (quota exceeded), the bot does not retry that call automatically and does not fall back to any paid path. It records a pause, defers or skips the affected work for the rest of that run, and Telegram carries a warning; the deferred work is picked up again on a later run once the provider's quota window has reset. Free tier is the only mode this bot runs in — a 429 means "wait," never "switch to paid."

## Local dry run

There is no single `run` command any more (#189): drive one ingestion stage directly, against
the Postgres connection its own docstring in `cli.py` names. Copy `.env.example` to `.env` for
the Supabase group of variables and load it into your shell:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test,webhook]'
cp .env.example .env
# edit .env with your values, then:
set -a; source .env; set +a
python -m job_hunter crawl-source --limit 5
```

Swap `crawl-source` for `extract-facets`, `recheck-freshness` or `recover-posting` to run that
stage instead; each is independent and bounded by its own `--limit`. Cover letter/PDF generation
is a separate on-demand step (`python -m job_hunter generate-cover-letter --job-id <id>`).

## Gmail intelligence setup

Gmail intelligence reads job-related messages into the shared Postgres state before normal job discovery. It uses the Gmail read-only OAuth scope: Gmail is never modified, and full email bodies are not stored. The sync stores only the privacy-minimized message metadata and extracted job/application signals needed by the bot.

Create an OAuth client for the Gmail API, then run the local bootstrap with the client credentials available only in your shell:

```bash
export GMAIL_CLIENT_ID='...'
export GMAIL_CLIENT_SECRET='...'
python scripts/gmail_oauth_bootstrap.py
```

The bootstrap opens the Google consent flow and prints a refresh token. Store that printed value as the GitHub Actions secret `GMAIL_REFRESH_TOKEN`; also add `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET` as GitHub secrets. Never commit any of these values.

The Gmail OAuth variables stay environment-backed; only they are needed in your shell. The sync's Gemini calls use the Gemini key stored in Relay Profile for `JOB_HUNTER_USER_ID`, the same key the main pipeline uses. Use the following local commands after loading the Gmail variables:

```bash
python -m job_hunter sync-gmail --dry-run
python -m job_hunter sync-gmail --force-backfill
```

`--dry-run` classifies and extracts without advancing the Gmail cursor or persisting Gmail-derived state. `--force-backfill` repeats the 120-day backfill idempotently and is non-destructive. A completed sync with individual message errors keeps its cursor so those messages retry on the next sync; setup, authorization, profile, or listing failures return a nonzero status.

The first successful Gmail setup performs a 120-day historical backfill. Historical processing is resumable and intentionally bounded to 100 previously unprocessed messages per sync invocation, so a large mailbox may need multiple workflow runs to finish. Successfully processed message IDs are stored in Postgres and skipped on later runs. In GitHub Actions the Gmail step also has a 10-minute fail-open timeout; if it reaches that safety limit, the normal Job Hunter pipeline continues and the next run resumes the remaining Gmail backlog.

## Ingestion stage schedules

There is no daily workflow and no manual GitHub Actions dispatch for ingestion any more (#189):
`crawl-source`, `extract-facets`, `recheck-freshness` and `recover-posting` are Render cron
services defined in [`render.yaml`](../../render.yaml), each on its own schedule, each reading and
writing Postgres directly via `PostgresJobStore` for the duration of its own drain. There is no
single writer lock across them the way `concurrency: group: job-hunter-state` used to provide for
the old single-process run; see `search_budget.py`'s module docstring for the one place that gap
is known to matter.

## Adding ATS board slugs

Edit the user's search profile's `ats` section to add direct board adapters, keyed by ATS provider, with a list of board identifiers:

```yaml
ats:
  ashby: ["acme-inc"]
  lever: ["acme"]
  greenhouse: ["acmeinc"]
```

- `ashby`: the board slug from `https://jobs.ashbyhq.com/<slug>`.
- `lever`: the company slug from `https://jobs.lever.co/<slug>`.
- `greenhouse`: the board token from `https://boards.greenhouse.io/<token>` or, on modern boards, `https://job-boards.greenhouse.io/<token>`.

Each configured slug adds one additional source adapter queried on every run, alongside the built-in Remotive, Arbeitnow, and DuckDuckGo-search sources.

## v1 safety boundary

The bot prepares application-ready material — it does not submit anything on your behalf. Out of scope for v1, by design:

- Automated submission to employer application forms.
- Answering legal attestations, visa/work-authorization questions, salary commitments, notice period, or demographic questions.
- CAPTCHA/2FA handling or browser automation.

You remain responsible for reviewing and submitting every application yourself.

## Troubleshooting

### Gemini quota / rate limits

The pipeline does not implement a Gemini-quota circuit breaker. Each daily run uses one compact profile-extraction call, then up to `max_jobs_per_run` (default 35) independent evaluation calls. A separate on-demand cover-letter call happens only when "Gen CL" is tapped for a given job. If quota or rate limits interrupt the run, each affected job fails independently and can be retried on the next run without blocking the rest. If you see repeated Gemini failures in the Actions log, check your API key's quota/rate limit in Google AI Studio.

### Telegram delivery errors

A failed Telegram send (bad token, bot not started, wrong chat id, message too large) is logged and does not crash the run or discard evaluation results. The job stays evaluated and marked undelivered in Postgres, and later runs retry only the missing Telegram deliveries without re-calling Gemini. Retry eligibility follows the search profile's inclusive `match_score_floor` (default 80), so changing the floor changes new deliveries and retries consistently, and ready-to-apply jobs retry both the digest message and PDF until both succeed. Verify `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are correct and that you've sent at least one message to the bot (see [Telegram bot setup](#telegram-bot-setup)).

### Flaky web sources

Public source APIs and job boards occasionally time out or return errors. Each source adapter fails open — an exception during discovery for one source is logged and the rest of that source is abandoned, while the postings it had already produced are kept and the run continues with the remaining sources — so a single flaky source does not abort the whole run. Check the Actions log for `discovery failed` warnings to see which source had trouble on a given run.

# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, etc.) when working with code in this repository.

## What this is

A daily, mostly hands-off job-hunting assistant that runs on GitHub Actions. It discovers public remote job postings, deduplicates them in Postgres, evaluates each against a candidate profile with Gemini, and delivers a digest via Telegram. Cover letters + PDFs are generated on demand, triggered by tapping "Gen CL" on a job's Telegram card, not automatically for strong matches. **It never submits applications** — see "v1 safety boundary" in README.md.

## Project direction and architectural constraints

This repository is part of a larger job-seeking ecosystem together with [`amitbaz/interviewer-app`](https://github.com/amitbaz/interviewer-app).

Current state:
- Job Hunter Bot and Interviewer App both persist to the same Supabase/Postgres project.
- They do not yet share a data model, only a database — the exact shared schema for
  candidate/profile data, jobs, evaluations, applications, application status, and related
  interview-preparation context is not defined yet.

Target direction:
- Both applications should eventually operate within the same Supabase ecosystem with a
  deliberately shared domain model, not just a shared database.

Migration rules:
1. **Postgres is the persistence layer.** The shared Supabase project lives at the repository
   root under `supabase/`; its migrations define Job Hunter's tables (`public.job_hunter_*`, see
   `supabase/migrations/202609060002_job_hunter_discovery_state.sql`) and sixteen `security invoker`
   SQL functions across three migrations (`supabase/migrations/202609060004_job_hunter_store_functions.sql`,
   `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`, and
   `supabase/migrations/20260907104935_job_hunter_gmail_candidate_eligibility.sql`, which drops
   `job_hunter_unmaterialized_inbound_jobs` and adds `job_hunter_gmail_candidate_complete` and
   `job_hunter_eligible_inbound_jobs`) for operations PostgREST cannot express in one call.
   (A later migration, `202609070004_job_hunter_upsert_job_distinct_timestamps.sql`, re-creates
   `job_hunter_upsert_job` rather than adding a new function.) Job Hunter's runtime reads and writes these tables through
   `PostgresJobStore` (`src/job_hunter/postgres_store.py`), reaching PostgREST with a
   short-lived, per-user ES256 token; row-level security decides which rows are visible.
   `tests/integration/test_supabase_isolation.py` proves those policies hold by writing and
   deleting a throwaway row in a local stack.
2. **Do not bypass `PostgresJobStore` opportunistically while implementing unrelated features.**
3. Feature development must continue independently of any further schema evolution.
4. Prefer boundaries that make future persistence changes easier.
5. When touching persistence-heavy code, avoid leaking Postgres/PostgREST-specific behavior into new domain/business logic where practical.
6. A schema or storage-layer change must be treated as an explicit architectural task with its own design and implementation plan.
7. Maintain backward compatibility with the currently deployed GitHub Actions workflow until a migration phase explicitly replaces it.
8. Documentation describing future architecture must not be interpreted as meaning that architecture already exists.

**Current production source of truth:** Postgres, via `PostgresJobStore` (`src/job_hunter/postgres_store.py`) against the shared Supabase project.

Current ecosystem:

```text
Job Hunter Bot
  discovery / ranking / job evaluation
          |
          | PostgresJobStore (per-user token, RLS)
          v
       Supabase
          ^
          |
    Interviewer App
  interview preparation / practice
```

## Development workflow

For every non-trivial feature, fix, or architectural change:

1. Understand the existing implementation before proposing changes.
2. Brainstorm/design the change before implementation.
3. Write the approved design under `docs/superpowers/specs/`.
4. Write an implementation plan before modifying production code.
5. Work on a dedicated feature/fix branch unless the user explicitly instructs otherwise.
6. Keep unrelated refactoring out of the change.
7. Run the relevant tests before considering the work complete.

Architectural migration work must never be silently bundled into an unrelated feature.

## Subagent usage

Use subagents only when they provide clear value through genuinely independent parallel work.

Do not spawn subagents for:
- simple repository exploration or searches
- reading a small number of files
- single-file or narrowly scoped changes
- sequential work where one task depends on the previous one
- work that can be completed efficiently with a few direct tool calls

Prefer completing straightforward work in the main agent context. Avoid duplicate exploration across subagents.

## Commands

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[test,webhook]'   # webhook extra too: the full suite imports flask

pytest -q                          # run full test suite
pytest tests/test_pipeline.py -q   # single file
pytest tests/test_pipeline.py::test_name -q  # single test

python -m job_hunter run                       # full pipeline run
python -m job_hunter run --scheduled           # only runs at the scheduled_hour in the user's search profile
```

Local dry run (skips Telegram, no Telegram creds needed): copy `.env.example` to `.env`, fill in `GEMINI_API_KEY`, `CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64`, set `JOB_HUNTER_DRY_RUN=1`, then `set -a; source .env; set +a` before running. `JOB_HUNTER_DRY_RUN` truthy values are `1/true/yes` (case-insensitive); anything else is treated as unset/false.

CI (`.github/workflows/job-hunter-ci.yml`) runs `pytest -q` on Python 3.12 — no lint step configured. It triggers on every pull request, and on pushes to `main` only. The push trigger is deliberately scoped to `main`: without it, a commit on a pull request branch starts both workflows against the same commit and costs twice the Actions minutes, which matters while the repository is private and subject to the monthly cap.

## Testing Guidelines

Pytest is the test runner. Run the full suite with `pytest -q`, a single file with `pytest <path> -q`, or a single test with `pytest <path>::<test_name> -q`.

Follow **red -> green -> refactor**: write a failing test, make it pass minimally, then improve both implementation and test. New behavior and bug fixes should be test-driven whenever practical. Preserve existing behavior with regression tests before changing code that is not already covered.

Before considering a change complete, run the relevant focused tests while iterating and then run the full `pytest -q` suite.

## Source Code Documentation

- Document public modules, exported functions and types, API routes, and complex domain models: state their purpose, inputs and outputs, side effects, failure behavior, and important invariants.
- Write comments for intent and trade-offs—especially decisions, constraints, edge cases, security, or performance rationale that code alone cannot convey. Prefer clearer code over comments that merely restate it.
- Keep documentation close to the code it describes and update or remove it in the same change when behavior changes.
- Use examples for non-obvious APIs or workflows when they make correct usage clearer; keep examples minimal, runnable in context, and aligned with the current interface.
- Do not leave stale, speculative, or redundant comments. Use actionable TODOs only when they include the reason and a tracked next step.
- Treat documentation as part of code review: verify it is accurate, necessary, and helpful to a future maintainer.

## Architecture

Pipeline, in `pipeline.py::run_pipeline`:

```
all sources -> enrich/dedupe -> profession gate + prefilter -> deterministic or profile-aware rank
  -> source-diverse top <=max_jobs_per_run shortlist (stable-ranking fallback on error)
  -> Gemini, stopping once daily_offer_limit offers are in hand -> decision filter -> score-sorted Telegram
  -> Telegram digest delivery (telegram.py)
```

Cover letter generation + PDF rendering (`cover_letter.py`/`pdf.py`) is not part of the daily pipeline above — it runs on demand, one job at a time, when "Gen CL" is tapped on that job's Telegram card. This fires a `repository_dispatch` event that runs `.github/workflows/generate-cover-letter.yml` (`python -m job_hunter generate-cover-letter --job-id <id>`).

Key modules:
- `src/job_hunter/sources/` — one adapter per job source, all implementing a common `discover()` interface (`base.py`), which **yields jobs as it finds them** rather than returning a finished list. Built-ins now include Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, and DuckDuckGo query expansion, plus optional Ashby/Lever/Greenhouse ATS boards. Each source **fails open**: an exception during discovery is caught in `discovery.py::_iter_source_jobs`, logged, and the rest of that source is abandoned — the jobs it had already yielded are kept, and later sources still run. `LearnedAtsSource` and `CompanyWatchSource` keep a whole board / a whole watch as their unit of work, because their aggregator verdict and health writes are per board and per watch; every other source yields per posting, page or query.
- `src/job_hunter/discovery.py`, `discovery_queries.py`, `ranking.py` — aggregate, generate expanded search queries, and rank candidates before Gemini. `generate_search_queries()` expands each role/template across configured ATS domains.
- `src/job_hunter/hiring_scope.py` — reads a posting's *explicitly stated* hiring regions ("open to candidates based in the US and Europe") from its text alone. It is deliberately self-contained: no market, no candidate, no scoring. `market_policy.py::attribute_market` consumes it as a bonus that outranks a listing variant's location label, and as a filter that drops markets the posting's stated regions exclude. Keep it that way — a posting's eligible regions are a shared, cacheable property of the posting, whereas whether a given candidate may work there is per-user, and only the first belongs in this module.
- `PrefilterResult.reason_code` identifies deterministic rejection causes; `DiscoveryStats.profession_rejected` tracks off-target professions. Telegram delivery fails closed for unknown decisions.
- `DiscoveryStats` also carries the cost half of each source's scorecard: `elapsed_by_source`
  and `requests_by_source`, keyed by the source *instance* (`discovery.source_cost_label`, so
  `lever:acme` and `lever:globex` stay apart and a source yielding nothing is still reported),
  plus `total_elapsed_seconds` for discovery as a whole — deliberately not the sum of the parts,
  since the difference is work happening around the sources rather than inside them. Time comes
  from the `clock` injected into `collect_candidates`; requests are counted at the shared
  `HttpClient` (`request_count`) and attributed to whichever source is running, so a new adapter
  is measured without doing anything. Both are charged per step of the source's iteration, in
  `_iter_source_jobs`, not around the `discover()` call: since sources yield, that call does no
  work, and bracketing it would score every source at zero. The caller's own per-job handling
  happens between steps and is excluded, so the figure keeps meaning "what this source cost" —
  and the running totals are written after every step, so a caller that stops a source part way
  still sees what it spent. Give a new source a `source_label` (`JobSource` declares it):
  a class attribute, or a property including the board for adapters configured one instance per
  board. Two caveats when reading the figures. The request count is everything that source sent
  through the shared client, and `cli.py` hands the same client to `SupabaseClient`, so for the
  store-backed sources (company watch, learned ATS, staged Gmail) it counts Postgres traffic as
  well as job-board fetches. And a cost label keys the source *instance*, which is deliberately
  not always the `source` string its jobs carry — a targeted search emits `search:<backend>`, a
  learned-ATS scan emits one string per provider — so cost and yield line up per source for the
  feeds and boards but not for those two. Dividing yield by cost for them needs per-instance
  yield, which is #119's problem, not this instrumentation's.
- `src/job_hunter/postgres_store.py` — Postgres persistence (`PostgresJobStore`, against the shared Supabase project): job dedup (`upsert_job`), re-evaluation gating (`needs_evaluation` — a job is only re-evaluated if it hasn't been evaluated before or its description changed), evaluation caching, and delivery tracking (`mark_delivered`). `pending_delivery_job_ids()` retries undelivered Telegram work without re-calling Gemini, but only for jobs scoring `>60`. Discovery persists in batches, through `upsert_logical_jobs`, `needs_evaluation_bulk`, `set_job_markets`, `set_job_statuses`, and `upsert_ats_boards` — `collect_candidates` calls these instead of looping the single-job methods. The single-job methods (`upsert_job`, `needs_evaluation`, `mark_delivered`, etc.) remain for the Telegram webhook and cover-letter paths, which handle one job at a time — and for `collect_candidates`'s own canonical-resolution tail, which is deliberately still per-job and costs roughly six to eight requests per resolved job (unbounded for jobs already on a supported ATS URL, since those bypass the `max_canonical_resolutions_per_run` shortlist): that tail is the branch's largest remaining scaling risk and the next thing to batch. New bulk work should use the batch methods rather than looping the single-job ones.
- `src/job_hunter/config.py` — loads the user's search profile (from Postgres, via `load_settings(store)`) + required env vars into a `Settings`/`SearchPolicy` (see `models.py`). Candidate profile and cover letter template are base64-encoded secrets (`CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64`), decoded in memory only — never write decoded plaintext to the repo or logs.
- `src/job_hunter/cli.py` — `python -m job_hunter run` entrypoint. `--scheduled` gates execution on `should_run_scheduled` (pipeline.py), comparing current local hour in `settings.timezone` against `settings.scheduled_hour`.
- `src/job_hunter/preferences.py` extracts a compact preference profile from the candidate profile. When that succeeds, `pipeline.py` uses `rank_jobs(..., preferences)` plus `select_diverse_candidates()` to enforce profile-aware ranking with per-source diversity. The shortlist knobs are `max_jobs_per_run` (code default 35, set to 100 in the user's search profile), `source_minimum_per_run` (0) and `source_max_share` (0.5) — the user's search profile (stored in Postgres) is what a real run uses, so read the values there rather than the code defaults. If preference extraction or shortlist selection fails, the pipeline falls back to the stable deterministic global ranking and logs the fallback without exposing private profile text.
- `daily_offer_limit` (5, 10 or 20; default 10) is the delivery budget on top of that shortlist: `run_pipeline` walks the selected candidates in rank order and stops evaluating once that many offers — ready-to-apply plus possible-match — have been produced, so Gemini spend follows what the user asked for. Candidates never reached are left unevaluated and are neither queued nor discarded; they rank again on the next run. The `evaluation_capacity` log line reports `daily_offer_limit`, `delivered_offers` and `deferred_by_offer_cap` separately from `deferred_by_budget`, because a cap-limited run and a market-limited run need telling apart.
- Per-job evaluation failures are caught individually inside the loop (not fail-open at the run level) so one bad job doesn't abort the run; each increments `summary.errors`.
- Cover letter + PDF generation is not part of the daily pipeline. It is triggered on demand, one job at a time, by tapping "Gen CL" on that job's Telegram card (`generate-cover-letter.yml` -> `python -m job_hunter generate-cover-letter --job-id <id>`), regardless of decision.

## State persistence

State now lives in Postgres (the shared Supabase project), not on the ephemeral Actions runner.
There is no artifact to restore or upload: `store.py`, `github_state.py`, and
`scripts/restore_state.py` were deleted along with the SQLite path, and neither workflow uploads
or restores a `job-hunter-state` artifact any more. `concurrency: group: job-hunter-state` is
still kept in both workflows deliberately — it no longer guards a file, but the read-then-update
pairs (company watch, ATS registry, search budget) assume a single writer, and that assumption is
now the only thing behind it.

The daily workflow fires on two cron triggers (`5 7 * * *` and `5 8 * * *` UTC) to cover both sides of the `Europe/Berlin` DST transition; `--scheduled` makes only one of them actually run the pipeline on any given day.

## Required secrets/env

`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64` — see README.md for setup. In dry-run mode, Telegram vars are optional.

Also required now that the store is ported to Postgres:
`JOB_HUNTER_USER_ID`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`.
The last is the private JWK of the project's ES256 signing key and can mint a token for any
user — it is the most sensitive secret the platform has. See
`docs/superpowers/specs/2026-09-06-job-hunter-per-user-jwt-design.md`.

These four secrets must exist in **both** GitHub repository settings and the Vercel project —
as of this writing they do not exist in either place yet, so both the workflows and the webhook
are non-functional until an operator creates them (see the runbook in README.md).

# AGENTS.md

This file provides guidance to AI coding agents (Claude Code, Codex, etc.) when working with code in this repository.

## What this is

A daily, mostly hands-off job-hunting assistant that runs on GitHub Actions. It discovers public remote job postings, deduplicates them in SQLite, evaluates each against a candidate profile with Gemini, and delivers a digest via Telegram. Cover letters + PDFs are generated on demand, triggered by tapping "Gen CL" on a job's Telegram card, not automatically for strong matches. **It never submits applications** — see "v1 safety boundary" in README.md.

## Project direction and architectural constraints

This repository is part of a larger job-seeking ecosystem together with [`amitbaz/interviewer-app`](https://github.com/amitbaz/interviewer-app).

Current state:
- Job Hunter Bot uses SQLite for persistence.
- Interviewer App uses Supabase/Postgres.
- The two applications do not currently share a backend or database.

Target direction:
- Gradually migrate Job Hunter Bot persistence and shared domain data to Supabase/Postgres.
- Both applications should eventually operate within the same Supabase ecosystem.
- Shared concepts are expected to include candidate/profile data, jobs, evaluations, applications, application status, and related interview-preparation context where appropriate, but the exact shared schema is not defined yet.

Migration rules:
1. **Do not assume Supabase is currently available in this repository.**
2. **Do not replace SQLite opportunistically while implementing unrelated features.**
3. Feature development must continue independently of the migration.
4. Prefer boundaries that make future persistence replacement easier.
5. When touching persistence-heavy code, avoid leaking SQLite-specific behavior into new domain/business logic where practical.
6. A SQLite -> Supabase migration must be treated as an explicit architectural task with its own design and implementation plan.
7. Maintain backward compatibility with the currently deployed GitHub Actions workflow until a migration phase explicitly replaces it.
8. Documentation describing the future architecture must not be interpreted as meaning that architecture already exists.

**Current production source of truth:** SQLite at `var/job_hunter.sqlite3` unless overridden by `JOB_HUNTER_DB_PATH`.

**Future source of truth:** Supabase/Postgres, only after the relevant migration phase has been implemented and validated.

Target ecosystem:

```text
Job Hunter Bot
  discovery / ranking / job evaluation
          |
          | future shared data layer
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
pip install -e '.[test]'

pytest -q                          # run full test suite
pytest tests/test_pipeline.py -q   # single file
pytest tests/test_pipeline.py::test_name -q  # single test

python -m job_hunter run                       # full pipeline run
python -m job_hunter run --scheduled           # only runs at config/search.yml's scheduled_hour
python -m job_hunter run --config path/to.yml  # alternate config
```

Local dry run (skips Telegram, no Telegram creds needed): copy `.env.example` to `.env`, fill in `GEMINI_API_KEY`, `CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64`, set `JOB_HUNTER_DRY_RUN=1`, then `set -a; source .env; set +a` before running. `JOB_HUNTER_DRY_RUN` truthy values are `1/true/yes` (case-insensitive); anything else is treated as unset/false.

CI (`.github/workflows/ci.yml`) runs `pytest -q` on Python 3.12 for every push/PR — no lint step configured.

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
  -> source-diverse top <=35 shortlist (stable-ranking fallback on error) -> Gemini -> decision filter -> score-sorted Telegram
  -> Telegram digest delivery (telegram.py)
```

Cover letter generation + PDF rendering (`cover_letter.py`/`pdf.py`) is not part of the daily pipeline above — it runs on demand, one job at a time, when "Gen CL" is tapped on that job's Telegram card. This fires a `repository_dispatch` event that runs `.github/workflows/generate-cover-letter.yml` (`python -m job_hunter generate-cover-letter --job-id <id>`).

Key modules:
- `src/job_hunter/sources/` — one adapter per job source, all implementing a common `discover()` interface (`base.py`). Built-ins now include Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, and DuckDuckGo query expansion, plus optional Ashby/Lever/Greenhouse ATS boards. Each source **fails open**: an exception during discovery is caught in `run_pipeline`, logged, and that source is skipped — the rest of the run continues.
- `src/job_hunter/discovery.py`, `discovery_queries.py`, `ranking.py` — aggregate, generate expanded search queries, and rank candidates before Gemini. `generate_search_queries()` expands each role/template across configured ATS domains.
- `PrefilterResult.reason_code` identifies deterministic rejection causes; `DiscoveryStats.profession_rejected` tracks off-target professions. Telegram delivery fails closed for unknown decisions.
- `src/job_hunter/store.py` — SQLite persistence: job dedup (`upsert_job`), re-evaluation gating (`needs_evaluation` — a job is only re-evaluated if it hasn't been evaluated before or its description changed), evaluation caching, and delivery tracking (`mark_delivered`). `pending_delivery_job_ids()` retries undelivered Telegram work without re-calling Gemini, but only for jobs scoring `>60`. DB path defaults to `var/job_hunter.sqlite3`, overridable via `JOB_HUNTER_DB_PATH`.
- `src/job_hunter/config.py` — loads `config/search.yml` + required env vars into a `Settings`/`SearchPolicy` (see `models.py`). Candidate profile and cover letter template are base64-encoded secrets (`CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64`), decoded in memory only — never write decoded plaintext to the repo or logs.
- `src/job_hunter/cli.py` — `python -m job_hunter run` entrypoint. `--scheduled` gates execution on `should_run_scheduled` (pipeline.py), comparing current local hour in `settings.timezone` against `settings.scheduled_hour`.
- `src/job_hunter/preferences.py` extracts a compact preference profile from the candidate profile. When that succeeds, `pipeline.py` uses `rank_jobs(..., preferences)` plus `select_diverse_candidates()` to enforce profile-aware ranking with per-source diversity (`max_jobs_per_run` default 35, `source_minimum_per_run` default 2, `source_max_share` default 0.5). If preference extraction or shortlist selection fails, the pipeline falls back to the stable deterministic global ranking and logs the fallback without exposing private profile text.
- Per-job evaluation failures are caught individually inside the loop (not fail-open at the run level) so one bad job doesn't abort the run; each increments `summary.errors`.
- Cover letter + PDF generation is not part of the daily pipeline. It is triggered on demand, one job at a time, by tapping "Gen CL" on that job's Telegram card (`generate-cover-letter.yml` -> `python -m job_hunter generate-cover-letter --job-id <id>`), regardless of decision.

## GitHub Actions state persistence

Actions runners are ephemeral — `var/job_hunter.sqlite3` does not persist between runs on its own. `.github/workflows/daily.yml`:
1. Restores the DB from the most recent `job-hunter-state` artifact via `scripts/restore_state.py` before running (silently starts fresh if none exists/expired).
2. Re-uploads the resulting DB as `job-hunter-state` (90-day retention) after the run, even on failure, as long as the DB file exists.

The daily workflow fires on two cron triggers (`5 7 * * *` and `5 8 * * *` UTC) to cover both sides of the `Europe/Berlin` DST transition; `--scheduled` makes only one of them actually run the pipeline on any given day.

## Required secrets/env

`GEMINI_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64` — see README.md for setup. In dry-run mode, Telegram vars are optional.

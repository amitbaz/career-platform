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
   `supabase/migrations/202609060002_job_hunter_discovery_state.sql`) and twenty-seven
   SQL functions — all `security invoker` except `job_hunter_get_provider_credentials`,
   `job_hunter_merge_postings`, `job_hunter_merge_jobs`, `job_hunter_collapse_job_rows`,
   `job_hunter_upsert_job` and `job_hunter_find_job_by_identity`, which are
   `security definer`; of those, only the credential retrieval and the identity read are
   reachable by `authenticated`, and neither writes anything (#179) — sixteen of them from three migrations (`supabase/migrations/202609060004_job_hunter_store_functions.sql`,
   `supabase/migrations/202609070003_job_hunter_batch_discovery_writes.sql`, and
   `supabase/migrations/20260907104935_job_hunter_gmail_candidate_eligibility.sql`, which drops
   `job_hunter_unmaterialized_inbound_jobs` and adds `job_hunter_gmail_candidate_complete` and
   `job_hunter_eligible_inbound_jobs`) for operations PostgREST cannot express in one call.
   (A later migration, `202609070004_job_hunter_upsert_job_distinct_timestamps.sql`, re-creates
   `job_hunter_upsert_job` rather than adding a new function; so does
   `20260908120000_job_hunter_job_merge_redirects.sql` with `job_hunter_merge_jobs`, which now
   records where a deleted duplicate went in `job_hunter_job_merges`;
   `20260908170000_job_hunter_job_facets.sql` adds `job_hunter_job_facets`, holding the
   objective facets `facets.py` extracts, with dedicated columns and indexes
   because hiring-eligible regions, remote policy, seniority and compensation have to be
   filterable in a query rather than parsed out of every row;
   `20260909110000_job_hunter_facets_on_postings.sql` then re-keys that table on the
   posting — it drops `user_id` and `job_id`, opens reads to every authenticated user, and
   migrates the rows that existed onto their job's posting, so two users who discover the
   same advertisement cause one extraction between them (#175).
   `20260909100000_job_hunter_postings.sql` adds `job_hunter_postings` — one row per job
   advertisement, keyed by fingerprint and shared by every user who discovers it, which
   `job_hunter_jobs.posting_id` points at. It, `job_hunter_job_facets`,
   `job_hunter_companies` (#198), `job_hunter_posting_merges` (#176) and `job_hunter_ats_boards`
   (#203) are the five Job Hunter tables shared between users: none has a `user_id`, and any
   authenticated user may read any row of any of them. **None may be written by any user**
   (#179): `insert`, `update` and `delete` are revoked from `anon`, `authenticated` and
   `service_role` on all five, the write policies are dropped, and every write arrives over
   ingestion's direct Postgres connection as the privileged role. That is the pattern of
   record for any table shared between users, and
   `supabase/tests/pgtap/job_hunter_shared_writes.sql` is what fails when it stops holding —
   including through a `security definer` function, which consults neither grants nor
   policies and is therefore a write path in its own right. (The two
   `job_hunter_platform_*` tables also have no `user_id`, but they are the platform key's
   own ledger and no user reaches them at all.) It adds
   `job_hunter_upsert_posting`, and re-creates both `job_hunter_upsert_job`, to write the
   posting and the job row in one call, and `job_hunter_merge_jobs`, so a merged job keeps
   the posting whose description it kept (#174).
   `20260909130000_job_hunter_posting_batches.sql` adds `job_hunter_posting_staging` and
   `job_hunter_merge_posting_batch`, which persists a whole crawl batch of postings with one
   set-based statement instead of one upsert per listing, plus
   `job_hunter_preferred_description`, which states the "better description wins" ladder once;
   it re-creates `job_hunter_upsert_job` again so a payload that already names its posting
   keeps it (#182).
   `20260909150000_job_hunter_read_posting_facts.sql` moves the readers onto the posting
   (#177): `job_hunter_needs_evaluation` and `job_hunter_eligible_inbound_jobs` both decide
   whether the work already done on a job is still current from the posting's description
   hash and content confidence, and `job_hunter_upsert_job` is re-created once more so a job
   row always points at the posting its description came from — without that, an update that
   keeps a better description from a second fingerprint leaves the row pointing at the weaker
   posting everything now reads it from. `job_hunter_jobs` keeps its duplicated columns and
   keeps the same values in them, so each ticket in the sequence is reversible on its own.
   What did not move *then* was matching — `url` on the hydrated `Job`, and the URL and
   identity predicates of `job_hunter_eligible_inbound_jobs` — because those ask which of a
   user's rows covers an advertisement, and a job row accumulated evidence from every
   posting merged into it while `posting_id` named one of them. That migration states the
   condition under which its own decision expires: *"The reason this holds today is that
   merging across fingerprints is still a per-user operation."* #176 met it, and #178 moved
   the columns. Match on the posting; decide currency from the posting.
   `20260909180000_job_hunter_posting_merges.sql` moves cross-identity merging onto the
   posting (#176). The fingerprint is source-scoped, so the same advertisement on an
   aggregator and on the employer's ATS is two postings; collapsing them was
   `job_hunter_merge_jobs`, per user, which meant every user repeated the decision and two
   could reach different conclusions. It adds `job_hunter_posting_merges` — a third shared
   table, reads open to every authenticated user and **no write policy at all** —
   `job_hunter_resolve_posting`, which turns a stale posting id into the survivor in one
   lookup, and `job_hunter_merge_postings`, which is `security definer` because re-pointing
   *every* affected user's job row is what the ticket is for and an invoker is scoped by
   RLS to its own rows. That function is revoked from `anon`, `authenticated` and
   `service_role`: the only way in is `job_hunter_merge_jobs`, itself made `security
   definer` so it can execute it, which confines a posting merge to two job rows the caller
   already owns rather than any two posting ids they can name. Making it definer is safe
   because it never relied on RLS — every statement carries its own `user_id` predicate,
   and RLS on each per-user table it touches is exactly that same predicate. It is not full
   closure: a user can still upsert two job rows resolving to two chosen postings and merge
   those. Taking shared-table writes off PostgREST entirely is #179, and needs #178 first.
   The merged-away posting keeps its row, so its
   fingerprint stays claimed and the next crawl of that source cannot resurrect a competing
   posting; its facets are discarded rather than stamped onto the survivor (#125's rule at
   the posting level). `job_hunter_upsert_posting` and `job_hunter_merge_posting_batch` are
   re-created to resolve through the redirect, and `job_hunter_merge_jobs` to delegate to
   it: it is now the per-user consequence of one global decision, not a second authority.
   `20260909200000_job_hunter_ats_boards.sql` shares learned ATS boards between users (#203):
   `job_hunter_ats_registry` held facts about a board — that it exists, is reachable, is an
   aggregator not worth crawling — once per user, and the most expensive of those facts to
   relearn is a rejection, since a missing one silently re-crawls a board already known
   worthless instead of failing loudly. It adds `job_hunter_ats_boards` — a fifth shared
   table, board identity and health, keyed on `(provider, board_identifier)` — and narrows
   `job_hunter_ats_registry` to what stays per-user: `eligible_jobs_seen` and
   `last_eligible_at`, which are yield against a user's own search profile, not board health.
   The backfill classifies every existing rejection before promoting it: a config-denylist
   rejection is that one user's policy and promotes as active, never as rejected; an
   aggregator-detection rejection is evidence about the board and promotes with its reason
   intact; anything unclassifiable (a health-backoff deactivation, which carries no reason at
   all) also promotes as active, because a board wrongly promoted as active self-corrects on
   the next crawl while one wrongly promoted as rejected is unreachable by design.
   `20260909210000_job_hunter_job_membership.sql` finishes the sequence (#178): every
   posting-level column comes off `job_hunter_jobs`, which becomes one user's membership of
   a posting — user, posting, market, status, first and last seen — unique on
   `(user_id, posting_id)`, with `posting_id` not null and the fingerprint's uniqueness now
   on the posting. Identity resolution moves with the columns: `job_hunter_upsert_job`
   resolves a listing against `job_hunter_postings` (canonical URL, ATS triple, normalized
   company/title/location via the new `job_hunter_find_posting_by_identity`) and merges what
   it finds there, so the decision is made once for everyone; it is `security definer` for
   that reason, which is what keeps `job_hunter_merge_postings` revoked from
   `authenticated` rather than callable on any two posting ids. `job_hunter_merge_jobs` is
   left as the per-user entry point and does nothing but merge the postings behind two of a
   caller's rows, and the new internal `job_hunter_collapse_job_rows` folds the membership
   rows a posting merge would otherwise duplicate — for every affected user, not only the
   caller. An additional user now costs one narrow row per posting.) Job Hunter's runtime reads and writes these tables through
   `PostgresJobStore` (`src/job_hunter/postgres_store.py`), reaching PostgREST with a
   short-lived, per-user ES256 token; row-level security decides which rows are visible.

   **The one exception is ingestion (#182).** A crawl also holds a direct, pooled Postgres
   connection as a privileged role (`src/job_hunter/pg.py`, configured by `SUPABASE_DB_URL`),
   because `COPY` and set-based statements are the two things PostgREST cannot express and a
   crawl needs both. It is used only for the shared postings tables, which have no user
   dimension. Matching, delivery and every per-user read stay on PostgREST under row-level
   security, and `job_hunter_posting_staging` and `job_hunter_merge_posting_batch` are
   deliberately unreachable by `anon` and `authenticated` — neither a grant nor a policy lets
   a user near them.

   **`SUPABASE_DB_URL` must connect as the role that owns the migrations.**
   The shared-table writers are revoked from every named role and granted back
   to none, so only the owner can execute them; that is `postgres` today. A
   least-privilege ingestion role would need `grant execute` on them before it
   could write anything, and the symptom of forgetting is a run that logs
   individual postings failing rather than one that says it cannot write.

   **The connection is not optional for writing (#179).** Since the shared tables are
   writable only by the privileged role, a deployment with no `SUPABASE_DB_URL` cannot
   discover or enrich at all: `run_pipeline` asks `store.can_write_shared_rows` and skips
   ingestion and enrichment entirely rather than paying for a crawl the database will refuse.
   It still starts and still delivers, from the postings that already exist, and it says so —
   `cli.py` warns at startup and the run summary reports `postings_written=0` alongside the
   facet counters, because a stale corpus and a quiet job market are otherwise the same log
   line. Do not read this as the pre-#179 fallback: that one was slower and converged on the
   same state, this one is scoring-only over a corpus that cannot change.
   `tests/integration/test_supabase_isolation.py` proves those policies hold by writing and
   deleting a throwaway row in a local stack.

   Since #183 that same privileged side owns the four pgmq stage queues.
   `20260909190000_job_hunter_stage_queues.sql` enables `pgmq` and `pg_cron`, creates one
   queue per stage, and adds `job_hunter_stage_attempts`,
   `job_hunter_stage_dead_letters`, `job_hunter_schedule_stage_enqueue` and
   `job_hunter_stage_queue_metrics`. Both extensions are enabled by migration and never
   from the dashboard: a dashboard change leaves production running ahead of the local
   stack the tests use, and nothing reports that until CI or production fails. The generic
   runner and failure vocabulary live in `stage_queue.py`; `postgres_stage_queue.py`
   is the pgmq adapter; `resolve_persist.py` is the first stage handler. None imports a
   user-scoped store, matching, scoring, or credentials. `PostgresJobStore` stages and
   enqueues the current crawl batch, then asks the bounded runner to consume visible
   `resolve_persist` work. A deployment without the direct connection still takes the
   existing per-listing fallback.
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

Local dry run (skips Telegram, no Telegram creds needed): copy `.env.example` to `.env`, then `set -a; source .env; set +a` before running. `.env.example` is grouped by the surface each variable serves; a dry run needs the Supabase group (`JOB_HUNTER_USER_ID`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`). The three free-tier limits (`GEMINI_FREE_RPM`, `GEMINI_FREE_TPM`, `GEMINI_FREE_RPD`) are optional overrides: the published limits per model are defaults in code (`src/job_hunter/ai/limits.py`). Everything under "Optional overrides" can stay blank — each takes the code default stated in its comment, so never copy a default into a value there. The webhook group is not needed for a run.

Set `JOB_HUNTER_DRY_RUN=1` to skip Telegram delivery; truthy values are `1/true/yes` (case-insensitive), anything else is treated as unset/false. The Gemini key and the CV and cover letter text are not env vars: they are saved in Relay's Profile view for `JOB_HUNTER_USER_ID` and read from Postgres at run time.

CI (`.github/workflows/job-hunter-ci.yml`) runs `pytest -q` on Python 3.12 — no lint step configured. It triggers on every pull request, and on pushes to `main` only. The push trigger is deliberately scoped to `main`: without it, a commit on a pull request branch starts both workflows against the same commit and costs twice the Actions minutes, which matters while the repository is private and subject to the monthly cap.

## Testing Guidelines

Pytest is the test runner. Run the full suite with `pytest -q`, a single file with `pytest <path> -q`, or a single test with `pytest <path>::<test_name> -q`.

Follow **red -> green -> refactor**: write a failing test, make it pass minimally, then improve both implementation and test. New behavior and bug fixes should be test-driven whenever practical. Preserve existing behavior with regression tests before changing code that is not already covered.

Before considering a change complete, run the relevant focused tests while iterating and then run the full `pytest -q` suite.

**Give a store-backed test a fingerprint nobody else uses.** `conftest.py` clears every
`job_hunter_*` table between tests except `job_hunter_postings`, which it cannot: a posting has
no owner and there is deliberately no delete policy on it (#174). A posting therefore outlives
the test that created it, and its identity columns and description are only *improved* by a
later upsert, never overwritten. Two tests sharing a fingerprint — the same `source_job_id`, or
the same company/title/location when neither passes a `url` — resolve to one posting, and since
#177 the readers take their facts from it, so the second test reads the first one's data. The
same happens across runs and across two suites running at once under different seed users.
Build the fingerprint from a `uuid.uuid4()` unless the test is specifically about two payloads
resolving to the same posting.

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
  -> per job: objective facet extraction if the posting has not been read yet
     (facets.py, once per posting ever, candidate-blind)
     then facet-decided hard blockers (hard_blockers.py, per user, no provider call)
     then subjective scoring from those facets for whatever they did not settle
     (evaluation.py, per user, never sees the description)
  -> decision classification -> match_score_floor -> daily_offer_limit -> score-sorted Telegram
  -> facet backfill over what scoring did not need (rediscovered jobs + the shortlist tail)
  -> Telegram digest delivery (telegram.py)
```

Cover letter generation + PDF rendering (`cover_letter.py`/`pdf.py`) is not part of the daily pipeline above — it runs on demand, one job at a time, when "Gen CL" is tapped on that job's Telegram card. This fires a `repository_dispatch` event that runs `.github/workflows/generate-cover-letter.yml` (`python -m job_hunter generate-cover-letter --job-id <id>`).

Key modules:
- `src/job_hunter/sources/` — one adapter per job source, all implementing a common `discover()` interface (`base.py`), which **yields jobs as it finds them** rather than returning a finished list. Built-ins now include Remotive, Arbeitnow, Jobicy, Himalayas, Remote OK, We Work Remotely, Hacker News, and DuckDuckGo query expansion, plus optional Ashby/Lever/Greenhouse ATS boards. Each source **fails open**: an exception during discovery is caught in `discovery.py::_iter_source_jobs`, logged, and the rest of that source is abandoned — the jobs it had already yielded are kept, and later sources still run. `LearnedAtsSource` and `CompanyWatchSource` fetch a whole board / a whole watch at a time, because their aggregator verdict is computed over the entire board; every other source yields per posting, page or query. Their postings are still yielded one at a time, so a caller can stop inside a board — which is why their health writes (`record_ats_scan_success`, `record_watch_success`) and their raw counts happen **after** the postings have been handed over, not before. Writing them first would stamp `last_checked_at` on a board that was never finished, demoting it in the oldest-first ranking, and claim a harvest nothing received. An unfinished board is left untouched and comes up again next run. Each source is also bounded by a wall-clock budget (`JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS`, default 1800s): `_iter_source_jobs` checks it *between* those units and never mid-request, so a source that overruns is cut off at a unit boundary, the jobs it already yielded are kept and flow through the pipeline normally, and later sources still run. A source whose own unit outlasts the whole budget cannot be bounded by it — the request timeout is what bounds that one — and `discovery.budget_applied` derives that from the source's longest observed unit rather than from a per-adapter declaration, so it stays true for an adapter nobody annotated.
- `src/job_hunter/discovery.py`, `discovery_queries.py`, `ranking.py` — aggregate, generate expanded search queries, and rank candidates before Gemini. `generate_search_queries()` expands each role/template across configured ATS domains.
- `src/job_hunter/facets.py` — objective extraction (#125): reads a posting's **facets** — the
  requirements it states and the depth each demands, its disclosed compensation, its
  hiring-eligible regions, its remote and relocation policy, its seniority, its stack — once per
  posting, and `PostgresJobStore.save_job_facets` stores them on the **posting** (#175): the
  store still takes a job id, because that is what the pipeline holds, and resolves it to
  `job_hunter_jobs.posting_id` itself. Facets are properties of the posting, identical for
  every user, so one extraction serves every later run *of every user* — a second user's run
  reads what the first user's run paid for, and counts it in `RunSummary.facets_reused`. That
  sharing is load-bearing and enforced by the interface, not by convention: `extract_facets`
  takes a frozen `PostingFacts` built from the `Job` alone, the module imports nothing per-user,
  and `tests/test_facets.py` fails if it ever does. Keep the candidate-aware prompt in
  `evaluation.py` and this one here; merging them would put the constraint back on care alone.
  Facets are what scoring reads (#126): `evaluation.py` is handed the stated requirements
  and their depth instead of the description, and a job whose facets are missing is left
  unscored rather than scored against an empty requirements list — see
  `docs/superpowers/specs/2026-09-08-scoring-from-facets-design.md`.
  Two facets never reach the model: Ashby's structured `isRemote` supplies `remote_policy`
  directly (a *false* flag does not — it separates neither hybrid nor onsite; and Greenhouse is
  deliberately excluded, because its adapter derives `remote` from the substring "remote" in the
  location label, so "Hybrid Remote" would be pinned as fully remote forever), and
  `hiring_scope.determine_hiring_scope` supplies `hiring_regions` when the posting states an
  explicit scope. `source_supplied` records which came free. Invalidation is
  `job_hunter_postings.description_hash` — since #175 the posting's, not each user's job row's,
  so an edited advertisement costs one re-read rather than one per user — compared against
  `description_hash_at_extraction`, the same mechanism that gates re-evaluation. There is no
  second notion of a changed posting. The extraction itself still reads the text on the job
  row it was dispatched for; #177 moves that read onto the posting too. Extraction is run from
  `pipeline.py::_extract_facets_for_run`, over this run's shortlist and retry queue first and
  then rediscovered jobs, every one of which survived the non-AI filters; it is bounded per run
  by `max_jobs_per_run` minus whatever the run's own inline reads already spent, which is
  what makes the existing corpus drain over consecutive runs with no migration script.
  A failure of any kind leaves the job unenriched with **no** marker, so a later run
  retries it — never record a placeholder, and never let a failure mark a posting
  permanently bad. Failures are counted in `RunSummary.facet_extraction_failed`, apart
  from scoring's own counters, and reported on the `facet_extraction` log line.
  The read declares `CallClass.SHARED_EXTRACTION`, which is what funds it from the
  **platform key** and meters it in the platform ledger rather than the user's (#128).
  There is no path by which it can reach a user's credential — not on an exhausted
  allowance, not through configuration — so never "fix" a deployment with no platform key
  by pointing extraction at the user's. Every platform refusal (spent allowance, an active
  platform pause, no platform key at all) reaches the pipeline as
  `PlatformAllowanceExhausted`, which defers *only* the postings nobody has read yet: the
  run finishes, jobs already carrying facets are still scored, the deferred ones are
  queued and counted in `RunSummary.scoring_deferred_by_read_budget`, and a later run
  drains them. It is logged as a deferral, never as an extraction failure, and it must
  stay countable apart from `facet_extraction_failed` — an exhausted day is a correct run,
  not a broken extractor. Rolling capacity on the platform key is waited out only
  `_READ_CAPACITY_WAITS` times before the read gives up the same way: that window is
  shared with every other user's run, so an unbounded wait there can cost this run its
  whole digest. Scoring's wait stays unbounded, because it paces against the user's own key.
- `src/job_hunter/hard_blockers.py` — decides the two objective hard blockers from the facets
  scoring is about to be given, with no provider call (#127): compensation disclosed below the
  user's floor, and a role that is not remote or requires relocation contrary to the user's
  policy. The facts are shared (the posting's facets, read once for everybody); the thresholds
  are per-user (`SearchPolicy.salary_floor_eur`, or the attributed market's currency, floor,
  remote and relocation rules), so the comparison is made per user at the scoring seam and its
  result is written only to that user's evaluation row — never cached across users. Everything
  about it **fails open**: thin content (`partial_unknown`, where a facet may have been read
  from a search-result snippet — `evaluate_job` withholds a confident decision on the same
  material), an `unknown` policy, undisclosed pay, a disclosed minimum with no maximum, a
  foreign currency or a non-annual period all send the job on to scoring. Only the disclosed
  *maximum* is compared, matching the scoring prompt's own rule, and that prompt keeps both
  rules — this removes calls, it does not remove the model's authority over what the facets
  cannot settle. It reads the same `JobFacets` object `_facets_for_scoring` just returned, so
  there is no second, staler view of the posting to disagree with it and no extra store read.
  `pipeline.py::_facet_decided_blockers` is the seam; a block builds the same `Evaluation`
  shape a model block produces (`decision="blocked"`, zero scores, empty `model`) and
  everything downstream — the merge-following write, company promotion, the score floor, the
  digest, the decision counters — handles it identically. Counted in
  `RunSummary.blocked_by_facets`, deliberately *not* in `evaluation_attempted`/`evaluated`,
  which exist to detect a run where every fresh AI scoring call failed.
- `src/job_hunter/evaluation.py` — subjective scoring, the per-user half (#126). Takes a
  `JobFacets` and a `CandidateContext` and returns an `Evaluation`: the six score components,
  the total, hard blockers, strengths, gaps, the notes, the decision and the rationale. It
  never receives `job.description`; the posting reaches it as the facets `facets.py` already
  read. Whether *this* candidate supports each stated requirement is decided here, because
  that answer differs per user and cannot be shared — the response carries one
  `candidate_support` verdict per stated requirement, in order, and the stored requirement
  keeps the posting's own text and depth from the facets. A response with a different number
  of verdicts is rejected outright rather than partially read. The module, the
  `job_hunter_evaluations` table and the `job_evaluation` purpose keep the word "evaluation"
  although CONTEXT.md reserves it for the pre-split combined call; the artefact is still an
  `Evaluation`, and renaming it would be a rename with no behavioural content.

- `src/job_hunter/hiring_scope.py` — reads a posting's *explicitly stated* hiring regions ("open to candidates based in the US and Europe") from its text alone. It is deliberately self-contained: no market, no candidate, no scoring. `market_policy.py::attribute_market` consumes it as a bonus that outranks a listing variant's location label, and as a filter that drops markets the posting's stated regions exclude. Keep it that way — a posting's eligible regions are a shared, cacheable property of the posting, whereas whether a given candidate may work there is per-user, and only the first belongs in this module.
- `PrefilterResult.reason_code` identifies deterministic rejection causes; `DiscoveryStats.profession_rejected` tracks off-target professions. Telegram delivery fails closed for unknown decisions.
- `DiscoveryStats.newly_discovered` counts the rows a run inserted, and is reported as
  `newly_discovered=` on the `discovery:` log line next to `raw=` and `unique=`. It accumulates
  across all three of `collect_candidates`' upserts rather than reading one of them, so a new
  insert site cannot go uncounted; in practice nearly every insert lands in the *raw* persist,
  since the unique jobs are upserted afterwards and are already on the table by then. It counts
  rows, not `unique` jobs, and may exceed `unique`: `_dedupe` and the store resolve identity by
  different rules, and rows is what capacity planning wants, because shared facet extraction is
  paid per row. The `#114` sizing this feeds is why it is measured rather than estimated from an
  assumed posting lifetime.
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
  `DiscoveryStats` also records how each source's run *ended* — `source_outcomes`, one of
  `completed`, `cut_off` or `failed` — and `longest_step_by_source`, the longest single unit
  it ran. Cut off and failed are kept apart deliberately: a slow source needs a schedule, a
  smaller unit of work or to run alongside others, while a broken one needs fixing or
  removing. One caveat: a source that overruns on its *last* unit is recorded as cut off
  rather than completed, because from outside there is no way to tell "nothing left" from
  "one more unit" without paying for that unit.
- `src/job_hunter/stage_queue.py`, `postgres_stage_queue.py`, `resolve_persist.py` — the
  user-free stage boundary (#183). The runner owns completion policy: success deletes,
  transient failures back off and then dead-letter, permanent failures dead-letter on the
  first attempt, quota exhaustion changes visibility without changing attempt count, and
  an interrupted process acknowledges nothing. The pgmq adapter owns only short queue-state
  transactions and reports queue/visible/dead-letter depth for every stage. The
  `resolve_persist` handler consumes a `batch_id` and runs the existing set-based posting
  merge; do not make queue payloads carry a `user_id`.
- `src/job_hunter/postgres_store.py` — Postgres persistence (`PostgresJobStore`, against the shared Supabase project): job dedup (`upsert_job`), re-evaluation gating (`needs_evaluation` — a job is only re-evaluated if it hasn't been evaluated before or its description changed), evaluation caching, and delivery tracking (`mark_delivered`). `pending_delivery_job_ids(match_score_floor)` retries undelivered Telegram work without re-calling Gemini, applying the profile's inclusive floor. Discovery persists in batches, through `upsert_logical_jobs`, `needs_evaluation_bulk`, `set_job_markets`, `set_job_statuses`, `upsert_ats_boards`, and `record_ats_eligible_jobs` — `collect_candidates` calls these instead of looping the single-job methods. Since #183 the crawl stages and enqueues its postings through `merge_posting_batch`; a bounded `resolve_persist` consumer hands each job upsert the posting the merge resolved, so `job_hunter_upsert_job` no longer resolves one per listing. The single-job methods (`upsert_job`, `needs_evaluation`, `mark_delivered`, etc.) remain for the Telegram webhook and cover-letter paths, which handle one job at a time. `collect_candidates`'s canonical-resolution tail no longer uses them: it pays only for a job whose resolution actually changed something, and those jobs' writes are collected during the loop and flushed after it as one staged posting merge plus `upsert_logical_jobs`, `set_job_markets` and `needs_evaluation_bulk` — three PostgREST round trips per resolved job (1170.8s for 1,221 of them in run 34289288702) became four calls for the whole run. The loop records its outcomes in order and a single walk afterwards decides eligibility, so deferring the writes cannot reorder what the run delivers. A job already on a supported ATS URL resolves to what it already was and writes nothing at all (#160): its row, market, board and `needs_evaluation` answer all come from the batched phases, and `discovery.py::_resolution_fingerprint` is what tells the two cases apart — a future resolution step that mutates another stored field must be added there or its change will not be written. Board registration left the tail with #160 (batched through `upsert_ats_boards`, so no board is registered twice in a run) and eligibility recording left it with #151. Those jobs still bypass the `max_canonical_resolutions_per_run` shortlist, which bounds network resolutions only. New bulk work should use the batch methods rather than looping the single-job ones.
- `src/job_hunter/ai/` — the AI provider port (#73). `port.py` holds the vocabulary core
  modules are allowed to know: `AIProvider`, `CallClass` (who funds a call and whether its
  answer is shared), the purposes, and the provider-neutral errors (`AIIncompleteResponse`,
  `AIBudgetExceeded`, `AITemporaryCapacity`, `AIQuotaPaused`). `credentials.py` is the seam a
  credential comes from — the class alone decides which: `USER_SUBJECTIVE` gets the user's
  key, `SHARED_EXTRACTION` gets the platform key or nothing, and a user credential is
  refused for extraction on every branch, including quota exhaustion (#128). `usage.py` is
  the provider-neutral quota ledger and circuit breaker, over *two* ledgers: the per-user
  tables, and the global platform ones reached through `PlatformUsageLedger`
  (`job_hunter_platform_ai_usage`, `job_hunter_platform_ai_quota_state`, which carry no
  `user_id` because one shared key has one allowance and one day's total). Keep them apart:
  spending either against the other's ceiling, or pausing one on the other's 429, is the
  bug the split exists to prevent. `limits.py` holds the published free-tier
  limits per model, so a run needs only an API key; the platform key takes those same
  published limits with the core reserve set to zero, since `job_facets` is the only
  purpose it ever funds. `gemini.py` is the **only** module that
  knows Gemini exists: it builds Google's request, picks the header, and translates a 429 body
  into the port's pause kinds. A second provider is a new file there plus wiring in `cli.py` —
  no core module changes. The paper review behind the interface's shape is
  `docs/superpowers/specs/2026-09-08-ai-provider-port-paper-review.md`.
- `src/job_hunter/config.py` — loads the user's search profile, provider credentials and source documents (all from Postgres, via `load_settings(store)`) plus the remaining env vars into a `Settings`/`SearchPolicy` (see `models.py`). The Gemini key, the Brave key, the candidate profile and the cover letter template are per-user rows read through RLS and held in memory only — never write them to the repo or logs. A missing Gemini key, CV or cover letter raises `RuntimeConfigurationError` before any provider call. The **platform** key is the one credential that is *not* per-user: it comes from `PLATFORM_GEMINI_API_KEY` in the environment, because it funds work that belongs to no user (#128). Leaving it unset is supported — the run then extracts no facets — and is never a reason to fall back to the user's key.
- `src/job_hunter/cli.py` — `python -m job_hunter run` entrypoint. `--scheduled` gates execution on `should_run_scheduled` (pipeline.py), comparing current local hour in `settings.timezone` against `settings.scheduled_hour`.
- `src/job_hunter/preferences.py` extracts a compact preference profile from the candidate profile. When that succeeds, `pipeline.py` uses `rank_jobs(..., preferences)` plus `select_diverse_candidates()` to enforce profile-aware ranking with per-source diversity. The shortlist knobs are `max_jobs_per_run` (code default 35, set to 100 in the user's search profile), `source_minimum_per_run` (0) and `source_max_share` (0.5) — the user's search profile (stored in Postgres) is what a real run uses, so read the values there rather than the code defaults. If preference extraction or shortlist selection fails, the pipeline falls back to the stable deterministic global ranking and logs the fallback without exposing private profile text.
- Delivery policy is applied after decision classification, so it never changes what `high_priority`, `package_match`, or `possible_match` mean. `match_score_floor` (50 through 95; default 80) withholds lower-scored jobs before they become digest items or consume delivery budget. It withholds every tier, not just offers: a `blocked` job under the floor no longer reaches the "Needs review / blockers" group either, where the old hardcoded floor of 60 would have let it through. Only a withheld *offer* increments `withheld_by_score_floor` — a withheld `skip` or `blocked` stays on `summary.skipped`, because the point of the counter is to show that the floor is set too high, and a job that was never going to be an offer says nothing about that. `daily_offer_limit` (5, 10 or 20; default 10) is the delivery budget on top of that floor: `run_pipeline` walks the selected candidates in rank order and stops evaluating once that many offers — ready-to-apply plus possible-match — have been produced, so Gemini spend follows what the user asked for. Candidates never reached are left unevaluated and are neither queued nor discarded; they rank again on the next run. The `evaluation_capacity` log line reports `match_score_floor`, `withheld_by_score_floor`, `daily_offer_limit`, `delivered_offers`, and `deferred_by_offer_cap` separately from `deferred_by_budget`, because a floor-limited, cap-limited, and market-limited run need telling apart.
- Per-job failures are caught individually inside the loop (not fail-open at the run level) so one bad job doesn't abort the run; each increments `summary.errors`. That guard covers the whole per-job unit of work, not just the Gemini call: persisting the evaluation, promoting the company, building the digest item and recording the delivery are all inside it, because a store write that escaped the loop once killed a run part-way through and discarded its digest (#145).
- A job selected for evaluation can be **merged away before its evaluation is written**: discovery merges duplicates while it is still building the shortlist, and `job_hunter_merge_jobs` deletes the duplicate row. `job_hunter_job_merges` records where it went, `PostgresJobStore.resolve_merged_job_id` reads that, and `save_evaluation`/`mark_delivered` retry against the survivor when a write hits SQLSTATE 23503 — so the clean case still costs one request. `save_evaluation` returns the id it actually wrote against; everything downstream in the loop must use that id rather than the selected one.
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

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — see README.md for setup. In dry-run mode, Telegram vars are optional.

Per-user runtime material is stored in Relay, not in the environment. Saving or replacing the CV and cover letter text happens in Relay's **Profile** view via **Replace source information**; the Gemini and Brave Search API keys are saved in the **Provider credentials** panel on the same page. Gemini is required — a run stops at startup without it. Brave Search is optional: without it, Brave-backed source discovery is skipped and search falls back to DuckDuckGo. `BRAVE_MONTHLY_QUERY_LIMIT`, `GEMINI_MODEL`, the optional `GEMINI_FREE_*` overrides and
`JOB_HUNTER_SOURCE_TIME_BUDGET_SECONDS` remain environment variables.

Gmail OAuth stays environment-backed (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`), but `sync-gmail` calls Gemini with the stored per-user key, exactly like the main pipeline. Relay's own deployment-level Gemini API key (configured in `apps/relay/.env.example`) is a separate server-side setting for Relay's interview features and is unchanged.

Also required now that the store is ported to Postgres:
`JOB_HUNTER_USER_ID`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`.
The last is the private JWK of the project's ES256 signing key and can mint a token for any
user — it is the most sensitive secret the platform has. See
`docs/superpowers/specs/2026-09-06-job-hunter-per-user-jwt-design.md`.

These four secrets must exist in **both** GitHub repository settings and the Vercel project —
as of this writing they do not exist in either place yet, so both the workflows and the webhook
are non-functional until an operator creates them (see the runbook in README.md).

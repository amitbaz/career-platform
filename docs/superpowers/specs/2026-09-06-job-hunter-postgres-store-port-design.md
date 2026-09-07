# Job Hunter: porting the store to Postgres and dropping the state artifact

Design for issue #70. Priority 3 of 12 under epic #34. Depends on #66 (the Postgres
schema and RLS policies, shipped in `55db425`) and #69 (per-user JWTs and the
PostgREST client, shipped in `35a51c2`).

## The problem

Job Hunter keeps all of its state in one SQLite file. That file travels between
scheduled GitHub Actions runs as a build artifact: each run downloads the previous
run's copy, works on it, and uploads it again with 90-day retention. Once the
repository is public those uploads are downloadable by anyone, so the artifact would
publish three months of the owner's job-search history. Removing it is a hard
prerequisite for publishing (#75), not a later cleanup.

The destination already exists. Migration `202609060002_job_hunter_discovery_state.sql`
creates eighteen `public.job_hunter_*` tables, every row stamped with a `user_id` and
guarded by four row-level security policies keyed on `auth.uid()`. `SupabaseClient`
already authenticates as one specific user by minting a short-lived ES256 token.

What is missing is the layer in between. The app speaks SQLite — joins, correlated
subqueries, `ON CONFLICT DO UPDATE`, read-then-write inside a transaction, integer row
ids — and PostgREST offers none of those. This design covers re-expressing that
persistence layer, moving the owner's existing history across, and cutting the file
loose.

## Scope

In scope:

- Reimplement `JobStore` against Postgres behind its existing method names.
- Fold in the three modules that currently bypass the store: `navigation_store.py`,
  `search_budget.py`, and `gmail_linkedin_cleanup.py`.
- Grow `SupabaseClient` with upsert, paging, and RPC.
- Add SQL functions for the queries PostgREST cannot express in one call.
- Add unique constraints to the five tables that lack them.
- Migrate the owner's existing history from the last state artifact.
- Remove the artifact upload and restore from both workflows; delete the code that
  becomes dead.
- Update the documentation that names SQLite as the source of truth.

Out of scope, owned by later tickets: search configuration moving into the database
(#71), credentials and documents out of repository secrets (#72), provider-neutral AI
usage accounting (#73), multi-user run fan-out (#76), Telegram multi-user identity
(#77). `JOB_HUNTER_USER_ID` stays a single environment variable.

## Decisions

### Transport: PostgREST plus SQL functions

The store reaches Postgres over PostgREST using the per-user token minted by
`AccessTokenMinter`, not through a direct Postgres driver.

The alternative — connecting with `psycopg` and a database URL — is roughly half the
work. The existing SQL ports almost mechanically, transactions and joins work, and the
client and function work below both disappear. It is rejected because a connection
string is an account-level credential. The entire point of #66 and #69 was that this
process can only ever act as one user, enforced by the database rather than by
application care. A database password undoes that, and the app would regain the ability
to read every user's rows at exactly the moment the repository goes public.

The cost of this decision is that six operations need server-side SQL, and the client
needs three capabilities it does not have.

### Job ids become uuids, and that is visible to callers

The ticket says callers are unchanged. That holds for method names, argument order and
return shapes, but not for the id type: SQLite auto-increment integers become uuids.
This leaks past the store because ids surface in three places outside it — the
`--job-id` CLI argument, the `JOB_ID` payload of the cover-letter `repository_dispatch`,
and the `job_id` embedded in stored Telegram navigation cards.

Job ids become `str` throughout. `NavigationCard.job_id` changes from `int` to `str`.
`navigation_sort_key` currently ties on `item.job_id` as an integer
(`telegram_navigation.py:57`) and needs a string-safe tiebreak.

### Latest-row semantics change from id to timestamp

The Postgres tables have no auto-increment ordering, so "the most recent evaluation"
can no longer mean the highest id. #66 already decided this: order by `evaluated_at`.
Three call sites depend on it — `needs_evaluation`, `get_evaluation`, `get_material`
— plus the `MAX(id)` subquery inside `pending_delivery_job_ids`.

This introduces a tie that SQLite did not have: two evaluations written in the same
run can share an `evaluated_at`. Writers set `evaluated_at` from a single run clock, so
this is reachable. Break the tie deterministically on `created_at` then `id`.

### Retried writes can duplicate rows on five tables

`HttpClient` retries POST on 5xx (`http.py:103-111`). The #69 design assumed this was
safe because "every table has a user-scoped unique key, so a retried insert conflicts
rather than duplicating". That is false. Five tables have no unique constraint at all:

- `job_hunter_evaluations`
- `job_hunter_materials`
- `job_hunter_deliveries`
- `job_hunter_ai_usage`
- `job_hunter_search_api_usage`

A single transient 502 against any of them silently writes the row twice. For
`ai_usage` and `search_api_usage` that corrupts quota accounting; for `deliveries` it
can double-send; for `evaluations` it wastes AI spend and skews "latest evaluation".

Fix by making the writes idempotent rather than by disabling retries, because disabling
retries trades duplication for lost writes. Each of the five gets a natural key and a
unique constraint in a new migration, and its write becomes an upsert:

| Table | Unique key |
| --- | --- |
| `job_hunter_evaluations` | `(user_id, job_id, evaluated_at)` |
| `job_hunter_materials` | `(user_id, job_id, generated_at)` |
| `job_hunter_deliveries` | `(user_id, job_id, delivery_type, delivered_at)` |
| `job_hunter_ai_usage` | `(user_id, run_id, model, purpose, occurred_at)` |
| `job_hunter_search_api_usage` | `(user_id, provider, occurred_at)` |

`ai_usage.run_id` is nullable and null values do not collide in a unique index, so the
writer must supply a non-null discriminator; `GEMINI_RUN_ID` is already passed by the
daily workflow.

### Client additions

Three capabilities, added to `SupabaseClient` and each proven against the local stack
before any store method uses them:

1. **`upsert(table, rows, *, on_conflict)`** — POST with
   `Prefer: resolution=merge-duplicates` and an `on_conflict=` query parameter naming
   the target constraint columns.
2. **Paging** — the local PostgREST caps a response at 1000 rows
   (`supabase/config.toml:18`) with no error on truncation. `select` gains transparent
   paging via `Range` headers so a caller cannot silently receive a partial table.
3. **`rpc(function, payload)`** — POST to `/rest/v1/rpc/{name}`. The functions are
   `security invoker`, so row-level security still applies and the token still decides
   which rows are visible.

Nothing else. The client stays a thin PostgREST wrapper.

### SQL functions

Six operations cannot be done in one PostgREST call without pulling whole tables across
the wire or losing atomicity. They become `security invoker` functions in a new
migration, so RLS continues to apply inside them:

| Function | Replaces | Why |
| --- | --- | --- |
| `job_hunter_upsert_job` | `upsert_job`, `upsert_logical_job` | Read-then-write returning `(id, is_new, description_changed)`; needs one atomic statement |
| `job_hunter_pending_delivery_jobs` | `pending_delivery_job_ids` | Latest-evaluation-per-job plus two `EXISTS` anti-joins |
| `job_hunter_pending_review_events` | `pending_review_events` | Join to `gmail_messages` with no declared FK, plus an anti-join on `review_deliveries` |
| `job_hunter_merge_jobs` | `merge_jobs` | Multi-table survivor selection and re-parenting; must be one transaction |
| `job_hunter_unmaterialized_inbound_jobs` | `list_unmaterialized_inbound_jobs` | Currently cross-products two whole tables in Python |
| `job_hunter_find_job_by_identity` | `find_job_by_identity` | Currently scans all of `jobs` and filters in Python |

Deliberately **not** functions, because PostgREST expresses them directly:

- `list_due_company_watches` / `list_due_ats_boards` — `julianday()` comparison becomes
  `active=eq.true&or=(paused_until.is.null,paused_until.lte.<ts>)`.
- `needs_evaluation` — one request using PostgREST resource embedding across the
  `evaluations → jobs` foreign key. **Verify first:** that key is composite
  (`(job_id, user_id) → jobs(id, user_id)`), and PostgREST may not resolve an embed
  across it. If it does not, this falls back to two requests, or joins the function list.
- `release_legacy_gmail_semantic_failures` — dynamic `IN` lists become `in.(...)`.
- Every `CASE WHEN` update computed from the current row (`record_watch_failure`,
  `upsert_ats_board`, `record_ats_scan_failure`). These become read-then-update over two
  requests. Safe here: one user, and both workflows share
  `concurrency: group: job-hunter-state`, so two writers never run concurrently.

### Dialect traps

- **`LIKE` case sensitivity.** SQLite's `LIKE` is case-insensitive for ASCII; Postgres'
  is not. `backfill_ats_identity` builds `LIKE` chains over known ATS hostnames
  (`store.py:421-432`) and must use `ilike` to keep matching.
- **Two-argument `MIN`/`MAX`.** `_merge_jobs` uses the SQLite spelling
  (`store.py:1010-1011`); Postgres needs `LEAST`/`GREATEST`.
- **`updated_at` has no trigger.** #66 chose not to add one. Python sets it explicitly
  on every write to the four tables that have the column.
- **`lastrowid` and `rowcount`** have no PostgREST equivalent. Inserts return the row via
  `Prefer: return=representation`; affected-row counts come from the returned array
  length.
- **Timestamps** move from ISO-8601 text to `timestamptz`. Code that compares or sorts
  timestamps as strings (`store.py:996-997`) must stop.

### Read-only mode and the webhook

`JobStore(path, read_only=True)` exists so the Vercel Telegram webhook can open a
downloaded artifact snapshot without creating tables. With Postgres there is no snapshot
and no schema creation, so the flag loses its meaning and is removed. The webhook reads
live Postgres using the same per-user token.

This deletes the whole artifact-reading path: `github_state.py`,
`scripts/restore_state.py`, `GitHubArtifactStateLoader`,
`GitHubArtifactNavigationRepository`, and the `GITHUB_STATE_TOKEN`,
`GITHUB_STATE_ARTIFACT_NAME` and `GITHUB_STATE_CACHE_DIR` settings.
`github_dispatch.py` is unaffected — it only posts `repository_dispatch`.

The Vercel deployment gains the four Supabase environment variables.

### Testing against the real local stack

There is no `conftest.py` anywhere in the app today. 294 sites across 28 test files
construct a store directly, 89 of them as `JobStore(":memory:")`.

Add `apps/job-hunter/tests/conftest.py` with a `store` fixture that builds a
Postgres-backed store against the local Supabase stack, using the fixed-UUID seed users
from `supabase/seed.sql`, and truncates that user's `job_hunter_*` rows between tests.
Test files stop constructing stores themselves and take the fixture.

An in-memory fake of PostgREST was considered and rejected. The semantics this port
depends on most — conflict resolution, ordering ties, row-level security, check
constraints — are exactly what a hand-written fake models badly, so a fake would let the
suite stay green while production broke.

Consequences, accepted:

- `supabase start` becomes a prerequisite for running the Job Hunter suite locally.
- The suite gets substantially slower.
- `job-hunter-ci.yml` must boot the stack for the main `test` job, not only the
  `isolation` job.
- Tests that poke `store._conn` or import `sqlite3` (five test files) are rewritten
  against the client.
- `tests/test_store_read_only.py` and the SQLite schema-migration tests in
  `tests/test_store.py` are deleted along with the machinery they cover.

Tests that exercise pure logic and do not touch persistence stay as they are.

### Schema migration machinery is dropped, not ported

`_init_db`, `_migrate_jobs_to_r2_schema`, `_migrate_jobs_to_r3_schema`,
`_backfill_raw_model_score` and `_add_missing_columns` exist because the production
database travelled as an artifact and could not be reached by a versioned migration
tool. Supabase migrations replace all of it. `backfill_ats_identity` is a public
per-run data backfill, not schema work, and is ported.

## Data migration

One-shot script, `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py`, run manually
against a SQLite file restored from the last `job-hunter-state` artifact. It runs before
the artifact is removed, and the artifact is not deleted until the port is proven.

It builds an old-integer-id to new-uuid map as it inserts, in foreign-key order, and
reports per-table row counts for verification. It is re-runnable: every insert is an
upsert against the table's user-scoped unique key.

Tables carried over:

| Carried | Reason |
| --- | --- |
| `jobs`, `job_sources` | The core record; ids seed the map |
| `evaluations`, `materials`, `deliveries` | AI spend already paid for; deliveries prevent re-notifying |
| `company_watch`, `ats_registry` | Learned discovery state, expensive to rebuild |
| `gmail_sync_state`, `gmail_messages`, `inbound_job_candidates` | Prevents reprocessing the mailbox |
| `application_events`, `review_deliveries` | Application history |
| `telegram_navigation_sessions` | See below |
| `search_api_usage` | Monthly Brave query budget |

Tables skipped, because the next run rebuilds them: `pending_ai_work` (re-enqueued from
jobs needing evaluation), `gemini_quota_state` (a pause window), and
`candidate_context_cache` (regenerates from the profile).

`search_api_usage` is carried specifically so the Brave free-tier counter does not reset
mid-month, which would risk overspending the monthly limit in the cutover month.

### Telegram navigation sessions need their card contents rewritten

Navigation sessions live 30 days (`pipeline.py:76`), so up to a month of digest messages
have live buttons at any time. A button carries only `action|session_id|index` — the
64-byte Telegram callback limit means the card content lives in a database row. If the
row is missing, every button on that message answers "This job list has expired."

Copying the rows is necessary but not sufficient. Each row stores its cards as JSON with
`job_id` embedded in each card. Previous/Next only re-render from that JSON and would
work, but "Gen CL" passes the embedded id to the cover-letter run, which looks it up in
the database. An un-remapped integer id makes those buttons appear alive and fail
silently on the one action that does work.

The migration therefore rewrites each card's `job_id` through the same id map it builds
for every other table. Cards whose job did not migrate are dropped from the session.

## Workflow changes

Both `job-hunter-daily.yml` and `job-hunter-generate-cover-letter.yml` lose their
restore step and their upload step. The ticket text mentions only the daily workflow,
but the cover-letter workflow uploads the same 90-day artifact under the same name, so
the public-repository argument applies to both.

Also removed: `permissions: actions: read`, which existed only so the restore script
could list artifacts.

`concurrency: group: job-hunter-state` is **kept**. It no longer protects a file, but the
read-then-update pairs described above assume one writer at a time.

Four secrets are added to both workflows: `JOB_HUNTER_USER_ID`, `SUPABASE_URL`,
`SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64`. None are wired into any workflow
today.

Note the repository's standing warning: workflows set
`defaults.run.working-directory: apps/job-hunter`, but `hashFiles`/`upload-artifact`
paths are repo-root relative (`AGENTS.md:57-58`). Removing those four lines removes the
trap.

## Verification

- The five duplicate-write holes: a pgTAP test asserting each of the five new unique
  constraints exists, plus a Python test that issuing the same write twice leaves one row.
- Each new SQL function: a pgTAP test that it returns the right rows and that a second
  user calling it sees nothing.
- The ported store: the existing behavioural tests, rewritten onto the shared fixture and
  running against the local stack.
- Paging: a test that inserts more than 1000 rows and asserts `select` returns all of them.
- The data migration: per-table row counts before and after, and a test that a session
  whose cards reference migrated jobs comes back with remapped ids.
- The acceptance criterion: a full `daily` run against Postgres with no SQLite file
  present, and a `grep` proving no artifact step remains in either workflow.

## Documentation to update

`apps/job-hunter/AGENTS.md` — rule 1 (application code may now target the tables), rule 2,
rule 7, the "current production source of truth" and "future source of truth" statements,
the `store.py` module description, the whole "GitHub Actions state persistence" section,
and the note that the four Supabase variables are "not yet required by any runtime path".
Also correct the stale `pip install -e '.[test]'` line to `'.[test,webhook]'`.

`apps/job-hunter/README.md` — every reference to SQLite as the source of truth and to the
artifact round-trip.

Root `AGENTS.md` — the load-bearing-paths warning about `hashFiles`/`upload-artifact`,
now that no workflow uploads an artifact.

## Risks

- **Single large diff.** The port, three folded-in modules, the test rewrite and the
  workflow cutover ship together. Splitting was considered and rejected: job ids change
  type globally, so there is no half-migrated state, and a merged-but-unused Postgres
  store would leave the artifact uploading history in the meantime.
- **The suite gets slow and needs a running stack.** Accepted as the price of testing
  against real Postgres semantics.
- **Round-trip count.** Read-then-update pairs and per-job calls turn one file operation
  into several HTTP requests. Acceptable for a single-user nightly batch; paging and the
  six functions remove the pathological cases.
- **Token expiry mid-run.** Tokens last 300 seconds and re-mint 60 seconds before expiry
  on every request, so a long run is safe, but a rejected token currently raises without
  a re-mint-and-retry path. Worth adding if it appears in practice.
- **`python -m pytest` runs against a stale global install** at `~/job-hunter-bot`. Use
  `pnpm job-hunter:test` or `apps/job-hunter/.venv/bin/python -m pytest`, or a rewritten
  store will appear to pass against the wrong tree.

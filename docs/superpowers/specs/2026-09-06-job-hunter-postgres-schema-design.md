# Job Hunter Postgres schema and RLS design

Date: 2026-09-06
Issue: #66 (child of epic #34, private multi-user alpha)
Scope: migrations and database tests only. No Python changes, no data movement.

## Problem

Job Hunter keeps all state in one SQLite file with 18 tables. No table has an owner
column because there was only ever one user. The multi-user alpha requires that state
to live in the shared Supabase Postgres, with every row owned by a platform user and
isolation enforced by the database through row-level security (RLS), not by filters
in application code.

## Goal

One migration that recreates Job Hunter's discovery state as user-owned Postgres
tables under the shared platform schema, plus a runnable test proving that a request
authenticated as user A cannot read or write user B's rows on every table.

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Tables with no natural owner today (ATS registry, company watch, quota state, context cache, search API usage) | Per-user, like every other table | Shared tables would need a service-role writer, which is the model #66 rejects. Duplication is cheap for a handful of users. |
| Namespace | `public` schema, `job_hunter_` prefix on every table | Avoids collisions (`application_events` vs `opportunity_events`, generic `jobs`, `materials`) without a second PostgREST-exposed schema. |
| Gemini-specific ledger names | `job_hunter_ai_usage`, `job_hunter_ai_quota_state` with `provider text not null default 'gemini'` | #73 makes the ledger provider-agnostic. A default column costs nothing now and avoids a rename migration later. |
| Primary keys | `uuid default gen_random_uuid()` | Platform convention. Telegram callback data carries session id plus index, never a job id, so the wider key has no cost there. |
| Same-user parent/child links | Composite foreign keys `(parent_id, user_id)` backed by `unique (id, user_id)` on the parent | Platform convention from `opportunities`. A child cannot reference another user's parent even if a policy were wrong. |
| RLS policy form | `(select auth.uid()) = user_id`, `to authenticated`, four per-operation policies named `select_own`, `insert_own`, `update_own`, `delete_own` | Names match existing tables. The wrapped form is the current Supabase recommendation: the planner evaluates it once per statement instead of once per row. Existing tables use the bare form and can migrate later. |
| Tests | pgTAP under `supabase/tests`, run by `supabase test db`, wired to CI | The existing psql verify script is not run by anything. pgTAP is the Supabase-supported harness. |
| Search profile, credentials, runs, Telegram identity mapping | Out of scope | Owned by #71, #72, #76, #77. |

## Type mapping from SQLite

| SQLite | Postgres |
|---|---|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `id uuid primary key default gen_random_uuid()` |
| ISO-8601 `TEXT` timestamps | `timestamptz` |
| JSON stored as `TEXT` | `jsonb` with the same default (`'{}'` or `'[]'`) |
| `INTEGER` 0/1 flags (`active`) | `boolean` |
| `INTEGER` tri-state `remote` (NULL/0/1) | `boolean` nullable |
| `REAL` | `double precision` |
| Composite or text primary keys | `uuid id` plus `unique (user_id, <natural key>)` |

Check constraints are added only where the source code shows a closed vocabulary:
`delivery_type in ('telegram_message', 'telegram_document')` and
`promotion_source in ('manual', 'automatic')`. All other status and classification
columns stay plain text because their values are owned by Python and are open-ended.

## Tables

Every table has `user_id uuid not null references auth.users(id) on delete cascade`
and `created_at timestamptz not null default now()`. Tables that had `updated_at` in
SQLite keep it, also `timestamptz not null default now()`; no trigger maintains it,
matching the platform. Columns not listed keep their SQLite name and mapped type.

| Table | Natural key | Links | Indexes beyond keys |
|---|---|---|---|
| `job_hunter_jobs` | `(user_id, fingerprint)` | parent: `unique (id, user_id)` | `(user_id, last_seen_at desc)`, `(user_id, status)` |
| `job_hunter_job_sources` | `(job_id, identity_key)` | `(job_id, user_id)` to jobs, cascade | `(user_id, job_id)` |
| `job_hunter_company_watch` | `(user_id, normalized_company_name)` | `(discovered_from_job_id, user_id)` to jobs, nullable | `(user_id, active, paused_until)` |
| `job_hunter_ats_registry` | `(user_id, provider, board_identifier)` | none | `(user_id, active, paused_until)` |
| `job_hunter_evaluations` | none, append-only | `(job_id, user_id)` to jobs | `(job_id, evaluated_at desc)`, `(user_id, evaluated_at desc)` |
| `job_hunter_materials` | none, append-only | `(job_id, user_id)` to jobs | `(job_id, generated_at desc)` |
| `job_hunter_deliveries` | none, append-only | `(job_id, user_id)` to jobs | `(job_id, delivery_type)` |
| `job_hunter_ai_usage` | none, append-only | none | `(user_id, provider, occurred_at desc)` |
| `job_hunter_ai_quota_state` | `(user_id, provider, model)` | none | none |
| `job_hunter_candidate_context_cache` | `(user_id, cache_key)` | none | none |
| `job_hunter_pending_ai_work` | `(user_id, work_type, job_id)` | `(job_id, user_id)` to jobs, cascade | none |
| `job_hunter_gmail_sync_state` | `(user_id, account_id)` | none | none |
| `job_hunter_gmail_messages` | `(user_id, message_id)` | none | `(user_id, occurred_at desc)` |
| `job_hunter_inbound_job_candidates` | `(user_id, origin, source_message_id, source_candidate_key)` | none | `(user_id, last_seen_at desc)` |
| `job_hunter_application_events` | `(user_id, source_message_id)` | `(job_id, user_id)` to jobs, nullable; parent: `unique (id, user_id)` | `(user_id, occurred_at desc)` |
| `job_hunter_review_deliveries` | `(user_id, event_id)` | `(event_id, user_id)` to application_events | none |
| `job_hunter_telegram_navigation_sessions` | `(user_id, session_id)` | none | `(expires_at)` |
| `job_hunter_search_api_usage` | none, append-only | none | `(user_id, provider, occurred_at desc)` |

The `(job_id, evaluated_at desc)` index replaces SQLite's "highest autoincrement id is
the current evaluation" convention. The porting ticket (#70) must order by
`evaluated_at`, not by id.

Foreign keys without `on delete cascade` in SQLite stay without it, except the two
that already cascade (`job_sources`, `pending_ai_work`).

## RLS

Every table:

```sql
alter table public.job_hunter_<name> enable row level security;

create policy select_own on public.job_hunter_<name>
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_<name>
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_<name>
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_<name>
  for delete to authenticated using ((select auth.uid()) = user_id);
```

All four policies are created on every table, including append-only ones. Which
operations the Python code actually performs is an application concern; the database
contract is "you may do anything to your own rows and nothing to anyone else's".

No table-level grants are added. The Supabase default grants to `authenticated` on
`public` apply, as they do for every existing table.

## Tests

File: `supabase/tests/pgtap/job_hunter_isolation.sql`, pgTAP, one transaction ending in
`rollback`. pgTAP files live in their own subdirectory because `supabase test db`
with no path argument runs every `.sql` under `supabase/tests` recursively, and the
existing psql verify script there emits no TAP plan and would be reported as a
failure.

Setup inserts two rows into `auth.users`, A and B, with fixed uuids.

For each of the 18 tables, driven by a table-name array so a table cannot be
forgotten:

1. RLS is enabled (`pg_class.relrowsecurity`).
2. Exactly the four expected policies exist (`pg_policies`).
3. As A (`set local role authenticated`, `request.jwt.claims` with `sub` = A): insert
   one minimal row succeeds.
4. As B: `select` returns no rows; `update ... returning` returns no rows;
   `delete ... returning` returns no rows.
5. As B: inserting a row with `user_id` = A fails with SQLSTATE `42501`.
6. As `anon`: `select` fails with SQLSTATE `42501`.
7. Back as A: the row is still there and readable.

One composite-key test on `job_hunter_job_sources`: as B, inserting a source with
B's own `user_id` but `job_id` pointing at A's job fails with a foreign-key violation
(SQLSTATE `23503`), proving the same-user chain holds independently of RLS.

Per-table minimal rows are supplied by a small plpgsql helper that knows each table's
required columns.

## Harness

- `supabase/config.toml` created with `supabase init`, trimmed to the sections the
  local stack needs. `project_id` set to the repo name. No secrets.
- Root `package.json` gains `"db:test": "supabase test db supabase/tests/pgtap"`.
  Requires Docker and a running local stack (`supabase start`).
- `.github/workflows/relay-ci.yml` gains a `db` job, triggered by the same
  `supabase/**` paths, that installs the Supabase CLI via `supabase/setup-cli`, runs
  `supabase start`, then `pnpm db:test`.
- The existing `supabase/tests/202608310001_planned_practice_sessions.verify.sql` is
  left as is and is not executed by the new harness because the script targets only
  the `pgtap` subdirectory.

## Documentation

- `apps/job-hunter/AGENTS.md` migration rule 1 is rewritten: the shared Supabase
  project and its migrations live at the repository root under `supabase/`; Job Hunter
  still runs on SQLite until #70 ports the store. Rules 2 through 8 stand.
- The migration file header states that `supabase db push` is required after merge,
  per the PR template.

## Verification

- `supabase db reset` applies all migrations cleanly on a fresh local stack.
- `pnpm db:test` passes: every table passes the isolation assertions.
- `pnpm relay:test` still passes (existing text-based migration tests are unaffected).
- CI `db` job green on the PR.

## Explicitly not done here

- No Python changes. `JobStore` still targets SQLite.
- No owner-history backfill from SQLite. That is #70.
- No changes to existing Relay tables or their bare `auth.uid()` policies.
- No search profile, credential, run, or Telegram identity tables.

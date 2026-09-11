-- Cross-user isolation for every Job Hunter table.
--
-- Two auth users, A and B, are seeded. For each table the check proves:
-- RLS is on, the four expected policies exist, A can insert, B sees
-- nothing on select/update/delete, B cannot insert a row that claims A's
-- user_id (SQLSTATE 42501, "new row violates row-level security policy"),
-- anon sees nothing and cannot insert, and A's row survives all of that.
--
-- RLS filters rather than errors: a select with no matching policy
-- returns zero rows, it does not raise. Only writes that fail a policy's
-- WITH CHECK raise 42501. The assertions below match that behaviour.
--
-- Four tables are deliberately absent: job_hunter_postings (#174),
-- job_hunter_job_facets (#175), job_hunter_companies (#198) and
-- job_hunter_ats_boards (#203). Each holds one row per thing in the world --
-- an advertisement, what it says, the employer behind it, an ATS job board --
-- rather than one per user, and shared readability is the property they
-- have, asserted in their own files. Anything with a user_id belongs here.
-- job_hunter_ats_registry stays: since #203 it holds only a user's own
-- eligible-job yield against a shared board.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

-- Role switching --------------------------------------------------------------

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_anon() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', json_build_object('role', 'anon')::text, true);
  execute 'set local role anon';
end $$;

create function pg_temp.become_postgres() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

-- Tables under test -------------------------------------------------------------

create view pg_temp.job_hunter_tables as
select unnest(array[
  'job_hunter_jobs',
  'job_hunter_job_sources',
  'job_hunter_company_watch',
  'job_hunter_ats_registry',
  'job_hunter_evaluations',
  'job_hunter_materials',
  'job_hunter_deliveries',
  'job_hunter_pending_ai_work',
  'job_hunter_ai_usage',
  'job_hunter_ai_quota_state',
  'job_hunter_candidate_context_cache',
  'job_hunter_gmail_sync_state',
  'job_hunter_gmail_messages',
  'job_hunter_inbound_job_candidates',
  'job_hunter_application_events',
  'job_hunter_review_deliveries',
  'job_hunter_telegram_navigation_sessions',
  'job_hunter_search_profiles',
  'job_hunter_search_profile_markets',
  'job_hunter_job_merges'
]) as table_name;

-- Minimal row per table -----------------------------------------------------------
-- Child tables create their own parent row for the same owner first.

-- A membership row needs an advertisement to be a membership of (#178), so
-- every seeded job row gets its own posting. The postings are shared and
-- carry no user_id, which is why this helper takes none.
--
-- SECURITY DEFINER since #179, and only for the shared parents: a posting and
-- an ATS board are now writable by the privileged role alone, so seeding one
-- while acting as a user would fail with 42501 and every per-user isolation
-- check below would be measuring a fixture that never loaded. The functions
-- are owned by postgres, so definer here means "seed this the way ingestion
-- would". Every per-user row stays an ordinary invoker insert, made while
-- acting as its owner, because passing that table's own policy is the thing
-- under test.
create function pg_temp.job_hunter_seed_posting() returns uuid
language plpgsql security definer as $$
declare
  v_id uuid;
begin
  insert into public.job_hunter_postings (fingerprint, first_seen_at, last_seen_at)
  values (gen_random_uuid()::text, now(), now()) returning id into v_id;
  return v_id;
end $$;

create function pg_temp.job_hunter_seed_ats_board(p_board_identifier text) returns void
language plpgsql security definer as $$
begin
  insert into public.job_hunter_ats_boards
    (provider, board_identifier, first_seen_at, last_seen_at)
  values ('greenhouse', p_board_identifier, now(), now())
  on conflict (provider, board_identifier) do nothing;
end $$;

create function pg_temp.job_hunter_seed_row(p_table text, p_owner uuid) returns uuid
language plpgsql as $$
declare
  v_id uuid;
  v_job uuid;
  v_event uuid;
  v_profile uuid;
begin
  case p_table
    when 'job_hunter_jobs' then
      insert into public.job_hunter_jobs (user_id, posting_id, first_seen_at, last_seen_at)
      values (p_owner, pg_temp.job_hunter_seed_posting(), now(), now()) returning id into v_id;
    when 'job_hunter_job_sources' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_job_sources (user_id, job_id, source, identity_key, first_seen_at, last_seen_at)
      values (p_owner, v_job, 'test', gen_random_uuid()::text, now(), now()) returning id into v_id;
    when 'job_hunter_company_watch' then
      -- Manual watches only since #204: promotion_source and
      -- discovered_from_job_id moved off this table entirely.
      insert into public.job_hunter_company_watch (user_id, company_name, normalized_company_name, first_seen_at)
      values (p_owner, 'Acme', gen_random_uuid()::text, now()) returning id into v_id;
    when 'job_hunter_ats_registry' then
      declare
        v_board_identifier text := gen_random_uuid()::text;
      begin
        perform pg_temp.job_hunter_seed_ats_board(v_board_identifier);
        insert into public.job_hunter_ats_registry (user_id, provider, board_identifier)
        values (p_owner, 'greenhouse', v_board_identifier) returning id into v_id;
      end;
    when 'job_hunter_evaluations' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_evaluations (user_id, job_id, evaluated_at)
      values (p_owner, v_job, now()) returning id into v_id;
    when 'job_hunter_materials' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_materials (user_id, job_id, generated_at)
      values (p_owner, v_job, now()) returning id into v_id;
    when 'job_hunter_deliveries' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_deliveries (user_id, job_id, delivery_type, delivered_at)
      values (p_owner, v_job, 'telegram_message', now()) returning id into v_id;
    when 'job_hunter_pending_ai_work' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_pending_ai_work (user_id, job_id, work_type)
      values (p_owner, v_job, 'evaluate') returning id into v_id;
    when 'job_hunter_ai_usage' then
      insert into public.job_hunter_ai_usage (user_id, run_id, occurred_at, model, purpose, status)
      values (p_owner, 'test-run', now(), 'gemini-test', 'job_evaluation', 'success') returning id into v_id;
    when 'job_hunter_ai_quota_state' then
      insert into public.job_hunter_ai_quota_state (user_id, model)
      values (p_owner, gen_random_uuid()::text) returning id into v_id;
    when 'job_hunter_candidate_context_cache' then
      insert into public.job_hunter_candidate_context_cache (user_id, cache_key, profile_hash, model, schema_version, context_json)
      values (p_owner, gen_random_uuid()::text, 'hash', 'gemini-test', '1', '{}'::jsonb) returning id into v_id;
    when 'job_hunter_gmail_sync_state' then
      insert into public.job_hunter_gmail_sync_state (user_id, account_id)
      values (p_owner, gen_random_uuid()::text) returning id into v_id;
    when 'job_hunter_gmail_messages' then
      insert into public.job_hunter_gmail_messages (user_id, message_id, occurred_at, classification, confidence, processed_at)
      values (p_owner, gen_random_uuid()::text, now(), 'JOB_ALERT', 0.9, now()) returning id into v_id;
    when 'job_hunter_inbound_job_candidates' then
      insert into public.job_hunter_inbound_job_candidates (user_id, source_message_id, source_candidate_key, last_seen_at)
      values (p_owner, gen_random_uuid()::text, 'k', now()) returning id into v_id;
    when 'job_hunter_application_events' then
      insert into public.job_hunter_application_events (user_id, event_type, occurred_at, source_message_id, confidence)
      values (p_owner, 'REVIEW_NEEDED', now(), gen_random_uuid()::text, 0.9) returning id into v_id;
    when 'job_hunter_review_deliveries' then
      v_event := pg_temp.job_hunter_seed_row('job_hunter_application_events', p_owner);
      insert into public.job_hunter_review_deliveries (user_id, event_id, delivered_at)
      values (p_owner, v_event, now()) returning id into v_id;
    when 'job_hunter_telegram_navigation_sessions' then
      insert into public.job_hunter_telegram_navigation_sessions (user_id, session_id, cards_json, expires_at)
      values (p_owner, gen_random_uuid()::text, '[]'::jsonb, now() + interval '1 hour') returning id into v_id;
    when 'job_hunter_search_profiles' then
      -- job_hunter_search_profiles is unique(user_id): the markets check
      -- below seeds its own parent profile for the same owner, and this
      -- table's own check_isolation call seeds twice too (once as the
      -- owner, once as the attacker impersonating the owner). Select the
      -- existing row (RLS-scoped to the caller, same as any other select
      -- here) before inserting, so seeding is idempotent per owner instead
      -- of colliding on the unique constraint.
      select id into v_id from public.job_hunter_search_profiles where user_id = p_owner;
      if v_id is null then
        insert into public.job_hunter_search_profiles
          (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
           source_max_share, salary_floor_eur, max_search_queries_per_run,
           max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
        values (p_owner, 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75) returning id into v_id;
      end if;
    when 'job_hunter_search_profile_markets' then
      v_profile := pg_temp.job_hunter_seed_row('job_hunter_search_profiles', p_owner);
      insert into public.job_hunter_search_profile_markets
        (user_id, profile_id, market_id, query_share, currency, gross_base_floor,
         remote_policy, relocation_policy, sponsorship_policy)
      values (p_owner, v_profile, gen_random_uuid()::text, 0.5, 'EUR', 90000,
              'preferred', 'selective', 'not_required') returning id into v_id;
    when 'job_hunter_job_merges' then
      -- duplicate_id names a deleted row and carries no foreign key, so a
      -- fresh uuid is a faithful seed; survivor_id does need a real job.
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_job_merges (user_id, duplicate_id, survivor_id)
      values (p_owner, gen_random_uuid(), v_job) returning id into v_id;
    else
      raise exception 'no seed row defined for table %', p_table;
  end case;
  return v_id;
end $$;

-- The isolation check ---------------------------------------------------------------

create function pg_temp.check_isolation(p_table text, p_a uuid, p_b uuid) returns setof text
language plpgsql as $$
declare
  v_count int;
begin
  perform pg_temp.become_postgres();

  return next ok(
    (select relrowsecurity from pg_class where oid = ('public.' || p_table)::regclass),
    p_table || ': row level security is enabled');

  return next is(
    (select array_agg(policyname::text order by policyname)
       from pg_policies where schemaname = 'public' and tablename = p_table),
    array['delete_own', 'insert_own', 'select_own', 'update_own'],
    p_table || ': exactly the four *_own policies exist');

  perform pg_temp.authenticate_as(p_a);
  return next lives_ok(
    format('select pg_temp.job_hunter_seed_row(%L, %L)', p_table, p_a),
    p_table || ': A inserts own row');

  perform pg_temp.authenticate_as(p_b);
  return next is_empty(
    format('select 1 from public.%I', p_table),
    p_table || ': B selects nothing');
  return next is_empty(
    format('update public.%I set user_id = user_id returning 1', p_table),
    p_table || ': B updates nothing');
  return next is_empty(
    format('delete from public.%I returning 1', p_table),
    p_table || ': B deletes nothing');
  return next throws_ok(
    format('select pg_temp.job_hunter_seed_row(%L, %L)', p_table, p_a),
    '42501', null,
    p_table || ': B cannot insert a row owned by A');

  perform pg_temp.become_anon();
  return next is_empty(
    format('select 1 from public.%I', p_table),
    p_table || ': anon selects nothing');
  return next throws_ok(
    format('select pg_temp.job_hunter_seed_row(%L, %L)', p_table, p_a),
    '42501', null,
    p_table || ': anon cannot insert');

  perform pg_temp.authenticate_as(p_a);
  execute format('select count(*) from public.%I', p_table) into v_count;
  return next cmp_ok(v_count, '>=', 1, p_table || ': A still sees own row');

  perform pg_temp.become_postgres();
end $$;

-- Run ------------------------------------------------------------------------

-- The tables this file deliberately does not cover. The platform key's
-- ledger and pause (issue #128) hold no user_id at all: they meter one
-- globally shared key, so per-user isolation is not the property they have.
-- They are checked in job_hunter_platform_ai_usage.sql, which asserts the
-- property they do have -- reachable only with the job_hunter_runner claim.
-- Naming them here rather than loosening the guard below keeps "a new table
-- with no test fails" true.
create view pg_temp.job_hunter_platform_tables as
select unnest(array[
  'job_hunter_platform_ai_usage',
  'job_hunter_platform_ai_quota_state',
  'job_hunter_platform_search_usage'
]) as table_name;

-- The shared tables, for the same reason in reverse. A posting (issue #174)
-- is one advertisement in the world, not one user's copy of it; its facets
-- (issue #175) are what that advertisement says to everybody; and a company
-- (issue #198) is the employer behind it, which is the same employer to
-- everyone. None has a user_id, every authenticated user may read every
-- row, and so per-user isolation is the property they deliberately do not
-- have. What they do have -- one row per fingerprint, one set of facets per
-- posting, one row per employer, readable by anyone authenticated and
-- writable by nobody but the privileged ingestion role (issue #179) -- is
-- asserted in job_hunter_postings.sql, job_hunter_job_facets.sql,
-- job_hunter_companies.sql and job_hunter_shared_writes.sql.
--
-- Where a merged-away posting went (issue #176) is shared for the same
-- reason: a merge decided once for everyone is useless if only its author
-- can see it. It arrived with no write policy at all, because every write
-- happens inside the security-definer job_hunter_merge_postings, and since
-- #179 that is the shape all five carry. It is asserted in
-- job_hunter_posting_merges.sql.
-- This view is the enforced list. `apps/job-hunter/AGENTS.md` describes the
-- same set in prose, and prose does not fail -- which is not a hypothetical:
-- when #198 added job_hunter_companies it updated this file and did not
-- update that paragraph, so the inventory an agent reads before touching a
-- migration went on naming two shared tables while there were three, and
-- nothing anywhere went red. Two branches editing that paragraph do not
-- conflict either, because they disagree in wording rather than in text.
--
-- So: adding a shared table means adding it here, and the guard below then
-- fails until it is. Update the AGENTS.md paragraph in the same commit, by
-- hand, and do not trust a clean merge to have kept it true. #215 is filed
-- to derive that inventory rather than write it twice.
--
-- job_hunter_sources (issue #184) joins this list for the same reason as
-- the others: a source's kind and display obligation are true for every
-- user, not one user's private note about it.
--
-- job_hunter_company_watch_health (issue #204) joins for the same reason: a
-- company's careers-page endpoint and whether it is reachable is true for
-- everyone who might watch that company, not one user's private discovery.
-- Unlike the others above it never opened writes to authenticated at all --
-- it adopts the #179 privileged-writer pattern from the day it was created.
create view pg_temp.job_hunter_shared_tables as
select unnest(array[
  'job_hunter_postings',
  'job_hunter_job_facets',
  'job_hunter_companies',
  'job_hunter_posting_merges',
  'job_hunter_ats_boards',
  'job_hunter_sources',
  'job_hunter_company_watch_health'
]) as table_name;

-- What being on that list obliges (#179): reads open to authenticated, writes
-- revoked from every role a user can hold. This is driven from the list rather
-- than written out per table, so a sixth shared table added above with its
-- write grants still open fails here rather than being closed by whoever
-- happens to remember. The behavioural half -- the 42501 a user actually gets,
-- and the definer functions that would otherwise write these tables on their
-- behalf -- is job_hunter_shared_writes.sql.
select is(
  (select array_agg(t.table_name order by t.table_name)
     from pg_temp.job_hunter_shared_tables t
    where has_table_privilege('authenticated', 'public.' || t.table_name, 'select')),
  (select array_agg(t.table_name order by t.table_name)
     from pg_temp.job_hunter_shared_tables t),
  'every shared table is readable by authenticated: that is what makes it shared');

select is(
  (select array_agg(t.table_name || ' ' || v.verb || ' ' || r.role_name
                    order by t.table_name, v.verb, r.role_name)
     from pg_temp.job_hunter_shared_tables t
     cross join (values ('insert'), ('update'), ('delete')) as v(verb)
     cross join (values ('anon'), ('authenticated'), ('service_role')) as r(role_name)
    where has_table_privilege(r.role_name, 'public.' || t.table_name, v.verb)),
  null,
  'and none of them may be written by any role a user can hold');

-- Ingestion's own scratch and operational state (issues #182 and #183), which
-- is neither per-user nor user-readable. No role a user can hold reaches it;
-- the batch and queue suites assert the useful properties each table has.
--
-- job_hunter_source_crawls and job_hunter_source_cursors (issue #184) belong
-- here rather than on the shared-tables list above: they are the scheduler's
-- own operational state, RLS is on with no policy at all, and every grant is
-- revoked -- nobody holding a user's session reaches them, same as the rest
-- of this list.
create view pg_temp.job_hunter_ingestion_tables as
select unnest(array[
  'job_hunter_posting_staging',
  'job_hunter_stage_attempts',
  'job_hunter_stage_dead_letters',
  'job_hunter_source_crawls',
  'job_hunter_source_cursors',
  'job_hunter_crawl_targets',
  'job_hunter_worker_runs',
  'job_hunter_worker_schedules',
  'job_hunter_ingestion_timing_config'
]) as table_name;

-- Guard: every job_hunter_ table this tree's migrations create is on one of
-- the lists above, so a table added to a migration without a test fails here.
--
-- The reference set is the TREE, not the database. Do not "simplify" it back
-- to pg_tables. The local Supabase stack is shared by every worktree on the
-- machine, so it also holds tables from other branches' unmerged migrations,
-- and measured against it this guard failed branches for changes they did
-- not contain (#207). A guard that is red for somebody else's reason teaches
-- everyone to ignore red, which is when a real coverage gap slips through.
-- This tree makes no claim about a table its migrations do not create, so
-- neither does the guard; its own tables stay fully enforced.
--
-- The list comes from scripts/pgtap_stage.py, which `pnpm db:test` runs: it
-- derives the tree's tables on the host (pgTAP cannot read the migrations)
-- and writes them into a staged copy of this directory, never into the tree.
-- A bare `supabase test db` therefore finds no list -- psql reads an empty
-- string without stopping -- and the first assertion below fails and says so.
\set tree_public_tables `cat tree_public_tables.txt`

create view pg_temp.job_hunter_tree_tables as
select table_name
  from regexp_split_to_table(:'tree_public_tables', '\s+') as table_name
 where table_name like 'job\_hunter\_%';

create view pg_temp.job_hunter_covered_tables as
select table_name from pg_temp.job_hunter_tables
union all
select table_name from pg_temp.job_hunter_platform_tables
union all
select table_name from pg_temp.job_hunter_shared_tables
union all
select table_name from pg_temp.job_hunter_ingestion_tables;

select ok(
  exists (select 1 from pg_temp.job_hunter_tree_tables),
  'this tree''s job_hunter_* tables were read from tree_public_tables.txt '
  '(if not: run pnpm db:test, which derives that file from supabase/migrations)');

-- A failure here lists the uncovered tables as "have", so it can be acted on
-- without inspecting the local Supabase stack. The description also names
-- any job_hunter_* table the database holds that this tree does not create
-- -- another worktree's, ignored on purpose -- so the ignoring is visible,
-- not silent.
select is(
  (select coalesce(array_agg(t.table_name order by t.table_name), '{}')
     from pg_temp.job_hunter_tree_tables t
    where t.table_name not in (select table_name from pg_temp.job_hunter_covered_tables)),
  '{}'::text[],
  'every public.job_hunter_* table this tree''s migrations create is covered by an isolation check'
  || coalesce(
       ' (ignored, in the database but not created by this tree: '
       || (select string_agg(p.tablename::text, ', ' order by p.tablename)
             from pg_tables p
            where p.schemaname = 'public' and p.tablename like 'job\_hunter\_%'
              and p.tablename::text not in (select table_name from pg_temp.job_hunter_tree_tables))
       || ')',
       ''));

-- And the reverse: a list entry this tree's migrations no longer create is a
-- leftover, e.g. a table since dropped, and fails here by name.
select is(
  (select coalesce(array_agg(c.table_name order by c.table_name), '{}')
     from pg_temp.job_hunter_covered_tables c
    where c.table_name not in (select table_name from pg_temp.job_hunter_tree_tables)),
  '{}'::text[],
  'every table on the lists above is one this tree''s migrations create');

select is(
  (select count(*)::int from pg_temp.job_hunter_tables), 20,
  'twenty Job Hunter tables are under test');

select pg_temp.check_isolation(
  t.table_name,
  'aaaaaaaa-0000-0000-0000-000000000001',
  'bbbbbbbb-0000-0000-0000-000000000002')
from pg_temp.job_hunter_tables t;

-- Composite same-user foreign key: B cannot attach a child to A's parent
-- even with B's own user_id, independently of RLS. The chain
-- (job_id, user_id) -> job_hunter_jobs (id, user_id) has no matching row.
-- A's job is inserted as postgres (RLS does not apply to the table owner)
-- so no role switch is needed before the temp table exists. The temp table
-- is owned by postgres, so `authenticated` needs an explicit select grant
-- on it; without one the insert below fails with 42501 on the read side
-- and never reaches the foreign key this test is about.
select pg_temp.become_postgres();
create temporary table _a_job (id uuid);
with ins as (
  insert into public.job_hunter_jobs (user_id, posting_id, first_seen_at, last_seen_at)
  values ('aaaaaaaa-0000-0000-0000-000000000001',
          pg_temp.job_hunter_seed_posting(), now(), now())
  returning id
)
insert into _a_job select id from ins;
grant select on _a_job to authenticated;

select pg_temp.authenticate_as('bbbbbbbb-0000-0000-0000-000000000002');
select throws_ok(
  $$ insert into public.job_hunter_job_sources (user_id, job_id, source, identity_key, first_seen_at, last_seen_at)
     select 'bbbbbbbb-0000-0000-0000-000000000002', id, 'test', 'k', now(), now() from _a_job $$,
  '23503', null,
  'composite foreign key rejects a child pointing at another user''s job');
select pg_temp.become_postgres();

select * from finish();
rollback;

-- Shared ATS board registry (issue #203).
--
-- Board identity and health (job_hunter_ats_boards) are shared, and since #179
-- they carry the same shape as job_hunter_postings and job_hunter_companies:
-- reads open to every authenticated user, writes revoked from every role a
-- user can hold, and the crawl writing them as the privileged ingestion role.
-- Eligible-job yield (job_hunter_ats_registry) is per-user and asserted for
-- isolation in job_hunter_isolation.sql; this file only proves the shared half
-- and the shape of what got left behind on the per-user table.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('dddddddd-0000-0000-0000-000000000007', 'ats-boards-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('dddddddd-0000-0000-0000-000000000008', 'ats-boards-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

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

-- Shape -----------------------------------------------------------------

select has_table('public', 'job_hunter_ats_boards', 'a board has a shared table of its own');

select columns_are('public', 'job_hunter_ats_boards', array[
  'id',
  'provider',
  'board_identifier',
  'company_name',
  'market_hint',
  'first_seen_at',
  'last_seen_at',
  'last_checked_at',
  'last_success_at',
  'last_job_count',
  'consecutive_failures',
  'active',
  'paused_until',
  'rejected_reason',
  'created_at'
], 'board health columns, nothing per-user');

select hasnt_column('public', 'job_hunter_ats_boards', 'user_id',
                    'a board is the same to everyone: it has no owner');
select hasnt_column('public', 'job_hunter_ats_boards', 'eligible_jobs_seen',
                    'eligible yield is per-user and stays on job_hunter_ats_registry');
select hasnt_column('public', 'job_hunter_ats_boards', 'last_eligible_at',
                    'eligible yield is per-user and stays on job_hunter_ats_registry');

select col_is_unique('public', 'job_hunter_ats_boards',
                     array['provider', 'board_identifier'],
                     'one row per board, whoever discovered it');

select has_index('public', 'job_hunter_ats_boards', 'job_hunter_ats_boards_due_idx',
                 'the due-for-scan scan is an index scan');
select has_index('public', 'job_hunter_ats_boards', 'job_hunter_ats_boards_rejected_idx',
                 'the rejected-boards scan is an index scan');

-- job_hunter_ats_registry lost its board-health columns ---------------------

select hasnt_column('public', 'job_hunter_ats_registry', 'active',
                    'board health moved to job_hunter_ats_boards');
select hasnt_column('public', 'job_hunter_ats_registry', 'rejected_reason',
                    'board health moved to job_hunter_ats_boards');
select hasnt_column('public', 'job_hunter_ats_registry', 'company_name',
                    'board health moved to job_hunter_ats_boards');
select has_column('public', 'job_hunter_ats_registry', 'eligible_jobs_seen',
                  'eligible-job yield stays per-user');
select has_column('public', 'job_hunter_ats_registry', 'last_eligible_at',
                  'eligible-job yield stays per-user');

-- Access ----------------------------------------------------------------

select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_ats_boards'::regclass),
  true,
  'row level security is on');

-- Reads open, writes closed (#179). A board rejection is the most expensive
-- fact in this table to relearn, and it is also the one that, written by the
-- wrong hand, makes a working board unreachable for everybody -- so learning
-- it is ingestion's job and nobody else's.
select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_ats_boards'::regclass),
  array['select_authenticated'],
  'reads are open to authenticated users; nobody but the privileged role writes a board row');

-- Behaviour ---------------------------------------------------------------

-- The crawl learns the board, as the privileged role.
select lives_ok(
  $$ insert into public.job_hunter_ats_boards
       (provider, board_identifier, company_name, first_seen_at, last_seen_at)
     values ('greenhouse', 'pgtap-shared-board', 'Acme', now(), now()) $$,
  'the crawl that discovered the board stores what it says');

select lives_ok(
  $$ update public.job_hunter_ats_boards
        set active = false, rejected_reason = 'aggregator: pgtap fixture'
      where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board' $$,
  'and records a board rejection');

-- Both users read the rejection, and neither paid to rediscover the board.
-- This is the whole point of #203, and #179 does not touch it: only the
-- writing narrowed.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000007');

select is(
  (select rejected_reason from public.job_hunter_ats_boards
    where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board'),
  'aggregator: pgtap fixture',
  'the user whose crawl found it reads the rejection');

select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000008');

select is(
  (select rejected_reason from public.job_hunter_ats_boards
    where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board'),
  'aggregator: pgtap fixture',
  'a second user reads the rejection nobody charged them to learn');

select throws_ok(
  $$ update public.job_hunter_ats_boards
        set last_checked_at = now()
      where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board' $$,
  '42501', null,
  'a user cannot write board health, which decides what every crawl visits');

select throws_ok(
  $$ delete from public.job_hunter_ats_boards
      where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board' $$,
  '42501', null,
  'nor take a shared board away from the others');

-- anon may read nothing and write nothing, same as every other shared table.
select pg_temp.become_anon();

select is(
  (select count(*)::int from public.job_hunter_ats_boards
    where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board'),
  0,
  'anon has no session and no policy grants it select -- filtered, not errored');

select throws_ok(
  $$ insert into public.job_hunter_ats_boards
       (provider, board_identifier, first_seen_at, last_seen_at)
     values ('greenhouse', 'pgtap-anon-board', now(), now()) $$,
  '42501',
  null,
  'anon cannot write a shared board row');

select pg_temp.become_postgres();

-- A per-user registry row must point at an existing shared board.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000007');

select throws_ok(
  $$ insert into public.job_hunter_ats_registry
       (user_id, provider, board_identifier)
     values ('dddddddd-0000-0000-0000-000000000007', 'greenhouse', 'pgtap-no-such-board') $$,
  '23503',
  null,
  'a per-user registry row cannot reference a board nobody has discovered');

select lives_ok(
  $$ insert into public.job_hunter_ats_registry
       (user_id, provider, board_identifier)
     values ('dddddddd-0000-0000-0000-000000000007', 'greenhouse', 'pgtap-shared-board') $$,
  'a per-user registry row may reference the shared board that already exists');

select * from finish();
rollback;

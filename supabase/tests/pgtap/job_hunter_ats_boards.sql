-- Shared ATS board registry (issue #203).
--
-- Board identity and health (job_hunter_ats_boards) are shared, the same
-- open-write shape job_hunter_postings and job_hunter_companies carry until
-- #179 narrows all three together. Eligible-job yield
-- (job_hunter_ats_registry) is per-user and asserted for isolation in
-- job_hunter_isolation.sql; this file only proves the shared half and the
-- shape of what got left behind on the per-user table.

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

select is(
  (select array_agg(polname::text order by polname)
     from pg_policy where polrelid = 'public.job_hunter_ats_boards'::regclass),
  array['insert_authenticated', 'select_authenticated', 'update_authenticated'],
  'read and write are open to authenticated users; nobody may delete a shared board row');

-- Behaviour ---------------------------------------------------------------

select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000007');

select lives_ok(
  $$ insert into public.job_hunter_ats_boards
       (provider, board_identifier, company_name, first_seen_at, last_seen_at)
     values ('greenhouse', 'pgtap-shared-board', 'Acme', now(), now()) $$,
  'the user who discovered the board may store what it says');

select lives_ok(
  $$ update public.job_hunter_ats_boards
        set active = false, rejected_reason = 'aggregator: pgtap fixture'
      where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board' $$,
  'a user may record a board rejection');

-- A second user, who never discovered the board, reads the rejection
-- anyway and does not pay to rediscover it. This is the whole point of the
-- ticket.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000008');

select is(
  (select rejected_reason from public.job_hunter_ats_boards
    where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board'),
  'aggregator: pgtap fixture',
  'a second user reads the rejection the first user paid to learn');

select lives_ok(
  $$ update public.job_hunter_ats_boards
        set last_checked_at = now()
      where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board' $$,
  'a second user may refresh board health it did not originally record');

-- Nobody may take a shared board away from the others.
select lives_ok(
  $$ delete from public.job_hunter_ats_boards
      where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board' $$,
  'a delete is refused silently by row-level security rather than erroring');

select is(
  (select count(*)::int from public.job_hunter_ats_boards
    where provider = 'greenhouse' and board_identifier = 'pgtap-shared-board'),
  1,
  'and the row is still there: no user may delete another user''s board');

-- anon may read but never write, same as every other shared table.
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

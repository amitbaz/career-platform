-- Behaviour and isolation for the batch discovery write functions.
--
-- These three exist because discovery persists thousands of jobs per run
-- and one PostgREST round trip per job spent the whole GitHub Actions
-- budget (issue #97). Each is `security invoker`, so row level security
-- still applies inside it and the caller's own token decides what it sees.
--
-- Two users are seeded, following the convention in
-- job_hunter_store_functions.sql: every fixture row is created while
-- acting as its owner (nothing is inserted as superuser, RLS is never
-- disabled), and the same two ids are reused here rather than invented
-- fresh, so a failure here and a failure there both trace to the same
-- fixtures.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('11111111-0000-0000-0000-00000000000a', 'store-fn-a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('22222222-0000-0000-0000-00000000000b', 'store-fn-b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims',
    json_build_object('sub', p_user, 'role', 'authenticated')::text, true);
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_postgres() returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

-- Two jobs in one array come back in input order, tagged by position.
select is(
  (select count(*)::int from public.job_hunter_upsert_jobs(
     jsonb_build_array(
       jsonb_build_object('fingerprint', 'batch-fp-1', 'source', 'test',
                          'company', 'Acme', 'title', 'Frontend Engineer',
                          'location', 'Remote', 'remote', true,
                          'description', 'one', 'url', 'https://example.test/1'),
       jsonb_build_object('fingerprint', 'batch-fp-2', 'source', 'test',
                          'company', 'Acme', 'title', 'Backend Engineer',
                          'location', 'Remote', 'remote', true,
                          'description', 'two', 'url', 'https://example.test/2')))),
  2,
  'job_hunter_upsert_jobs returns one row per input element');

-- input_index must be tied to the element that produced it, not just be
-- the right set of values -- an implementation that swapped which element
-- produced which index would otherwise still pass. Two distinct
-- identities are upserted individually first, then upserted again
-- together (idempotently, since identity resolution is stable) so the
-- batch's per-index id can be checked against a known-good id per job.
create temporary table temp_order_a as
select id from public.job_hunter_upsert_job(
  jsonb_build_object('fingerprint', 'batch-ord-a', 'source', 'test',
                     'company', 'Acme', 'title', 'A', 'location', 'Remote',
                     'remote', true, 'description', 'x', 'url', 'https://example.test/ord-a'));

create temporary table temp_order_b as
select id from public.job_hunter_upsert_job(
  jsonb_build_object('fingerprint', 'batch-ord-b', 'source', 'test',
                     'company', 'Acme', 'title', 'B', 'location', 'Remote',
                     'remote', true, 'description', 'y', 'url', 'https://example.test/ord-b'));

create temporary table temp_order_batch as
select * from public.job_hunter_upsert_jobs(
  jsonb_build_array(
    jsonb_build_object('fingerprint', 'batch-ord-a', 'source', 'test',
                       'company', 'Acme', 'title', 'A', 'location', 'Remote',
                       'remote', true, 'description', 'x', 'url', 'https://example.test/ord-a'),
    jsonb_build_object('fingerprint', 'batch-ord-b', 'source', 'test',
                       'company', 'Acme', 'title', 'B', 'location', 'Remote',
                       'remote', true, 'description', 'y', 'url', 'https://example.test/ord-b')));

select isnt(
  (select id from temp_order_a),
  (select id from temp_order_b),
  'sanity check: the two ordering-test jobs really are distinct identities');

select is(
  (select array_agg(input_index order by input_index) from temp_order_batch),
  array[0, 1],
  'input_index is zero-based and matches input order');

select is(
  (select id from temp_order_batch where input_index = 0),
  (select id from temp_order_a),
  'input_index 0 carries the id of the first input job');

select is(
  (select id from temp_order_batch where input_index = 1),
  (select id from temp_order_b),
  'input_index 1 carries the id of the second input job');

-- An empty array is a no-op, not an error.
select is(
  (select count(*)::int from public.job_hunter_upsert_jobs('[]'::jsonb)),
  0,
  'an empty array returns no rows');

-- A non-array argument is rejected rather than silently doing nothing.
select throws_ok(
  $$select * from public.job_hunter_upsert_jobs('{"fingerprint":"x"}'::jsonb)$$,
  null,
  'a non-array p_jobs raises');

-- Two elements of one chunk that share an identity collapse to one job,
-- exactly as two sequential single-job calls would: two result rows,
-- one shared id, and the second row's is_new = false proving it merged
-- rather than the batch silently dropping the duplicate element.
create temporary table temp_dup_batch as
select * from public.job_hunter_upsert_jobs(
  jsonb_build_array(
    jsonb_build_object('fingerprint', 'batch-dup-a', 'source', 'test',
                       'company', 'Dup Co', 'title', 'Engineer', 'location', 'Remote',
                       'remote', true, 'description', 'first',
                       'canonical_url', 'https://example.test/same'),
    jsonb_build_object('fingerprint', 'batch-dup-b', 'source', 'other',
                       'company', 'Dup Co', 'title', 'Engineer', 'location', 'Remote',
                       'remote', true, 'description', 'second',
                       'canonical_url', 'https://example.test/same')));

select is(
  (select count(*)::int from temp_dup_batch),
  2,
  'two elements sharing a canonical URL still return two result rows');

select is(
  (select count(distinct id)::int from temp_dup_batch),
  1,
  'two elements sharing a canonical URL resolve to one job id');

select is(
  (select array_agg(input_index order by input_index) from temp_dup_batch),
  array[0, 1],
  'the two merged rows are still tagged with input_index 0 and 1');

select is(
  (select is_new from temp_dup_batch where input_index = 1),
  false,
  'the second row of a merged pair reports is_new = false');

-- The function is security invoker, so it can never be a way around RLS.
select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_upsert_jobs'),
  false,
  'job_hunter_upsert_jobs is security invoker');

-- RLS isolation: user B batch-upserting a job that matches one of user
-- A's identities must create B's own row, not merge into or return A's,
-- and must not be able to see A's row afterward.
create temporary table temp_rls_user_a as
select id from public.job_hunter_upsert_jobs(
  jsonb_build_array(
    jsonb_build_object('fingerprint', 'batch-rls-a', 'source', 'test',
                       'company', 'Acme', 'title', 'Eng A', 'location', 'Remote',
                       'remote', true, 'description', 'a',
                       'canonical_url', 'https://example.test/rls-shared')));

select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');

create temporary table temp_rls_user_b as
select id from public.job_hunter_upsert_jobs(
  jsonb_build_array(
    jsonb_build_object('fingerprint', 'batch-rls-b', 'source', 'test',
                       'company', 'Acme', 'title', 'Eng B', 'location', 'Remote',
                       'remote', true, 'description', 'b',
                       'canonical_url', 'https://example.test/rls-shared')));

select isnt(
  (select id from temp_rls_user_b),
  (select id from temp_rls_user_a),
  'user B batch-upserting a job matching user A''s identity creates B''s own row, not a merge into A''s');

select is(
  (select count(*)::int from public.job_hunter_jobs where id = (select id from temp_rls_user_a)),
  0,
  'user B''s ids return nothing for user A''s job -- RLS holds');

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select is(
  (select count(*)::int from public.job_hunter_jobs where id = (select id from temp_rls_user_a)),
  1,
  'user A can still see her own job after user B''s batch call');

-- needs_evaluation ----------------------------------------------------------

-- A job with no evaluation needs one.
with created as (
  select id from public.job_hunter_upsert_jobs(
    jsonb_build_array(jsonb_build_object(
      'fingerprint', 'needs-fp-1', 'source', 'test', 'company', 'Acme',
      'title', 'Engineer', 'location', 'Remote', 'remote', true,
      'description', 'unevaluated', 'url', 'https://example.test/n1')))
)
select is(
  (select needs from public.job_hunter_needs_evaluation(
     array(select id from created))),
  true,
  'a job with no evaluation needs evaluation');

-- An id belonging to another user returns no row at all.
select is(
  (select count(*)::int from public.job_hunter_needs_evaluation(
     array['99999999-0000-0000-0000-000000000009'::uuid])),
  0,
  'an unreadable id returns no row rather than a verdict');

select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_needs_evaluation'),
  false,
  'job_hunter_needs_evaluation is security invoker');

select * from finish();
rollback;

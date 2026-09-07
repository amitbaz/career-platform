-- Behaviour and isolation for the batch discovery write functions.
--
-- These three exist because discovery persists thousands of jobs per run
-- and one PostgREST round trip per job spent the whole GitHub Actions
-- budget (issue #97). Each is `security invoker`, so row level security
-- still applies inside it and the caller's own token decides what it sees.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

set local role postgres;
select set_config('request.jwt.claims',
  json_build_object('sub', 'aaaaaaaa-0000-0000-0000-000000000001', 'role', 'authenticated')::text,
  true);
set local role authenticated;

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

select is(
  (select array_agg(input_index order by input_index)
     from public.job_hunter_upsert_jobs(
       jsonb_build_array(
         jsonb_build_object('fingerprint', 'batch-fp-3', 'source', 'test',
                            'company', 'Acme', 'title', 'A', 'location', 'Remote',
                            'remote', true, 'description', 'x', 'url', 'https://example.test/3'),
         jsonb_build_object('fingerprint', 'batch-fp-4', 'source', 'test',
                            'company', 'Acme', 'title', 'B', 'location', 'Remote',
                            'remote', true, 'description', 'y', 'url', 'https://example.test/4')))),
  array[0, 1],
  'input_index is zero-based and matches input order');

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
-- exactly as two sequential single-job calls would.
select is(
  (select count(distinct id)::int from public.job_hunter_upsert_jobs(
     jsonb_build_array(
       jsonb_build_object('fingerprint', 'batch-dup-a', 'source', 'test',
                          'company', 'Dup Co', 'title', 'Engineer', 'location', 'Remote',
                          'remote', true, 'description', 'first',
                          'canonical_url', 'https://example.test/same'),
       jsonb_build_object('fingerprint', 'batch-dup-b', 'source', 'other',
                          'company', 'Dup Co', 'title', 'Engineer', 'location', 'Remote',
                          'remote', true, 'description', 'second',
                          'canonical_url', 'https://example.test/same')))),
  1,
  'two elements sharing a canonical URL resolve to one job id');

-- The function is security invoker, so it can never be a way around RLS.
select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_upsert_jobs'),
  false,
  'job_hunter_upsert_jobs is security invoker');

select * from finish();
rollback;

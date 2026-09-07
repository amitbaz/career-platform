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

-- clock_timestamp() vs. now(): two elements in one batch call must get
-- strictly increasing first_seen_at and created_at, not tied. now() is the
-- transaction's start time -- constant across every iteration of one call
-- to job_hunter_upsert_jobs -- so under now() these two rows would carry
-- identical timestamps. clock_timestamp() advances on every call within a
-- transaction, matching what two separate sequential single-job requests
-- would have produced. This is what lets the merge-survivor tiebreak below
-- (first_seen_at asc, id asc, in job_hunter_upsert_job) preserve input
-- order for jobs batched together.
-- Materialized first, then joined in a separate statement -- joining
-- directly against job_hunter_upsert_jobs(...) in the same statement's
-- FROM clause returned zero rows in manual testing (the set-returning
-- function call apparently doesn't compose reliably with a join here),
-- while the two-step form below (matching temp_order_batch's pattern
-- above) reads back cleanly.
create temporary table temp_clock_ids as
select * from public.job_hunter_upsert_jobs(
    jsonb_build_array(
      jsonb_build_object('fingerprint', 'clock-fp-1', 'source', 'test',
                         'company', 'Clock Co', 'title', 'Engineer One',
                         'location', 'Remote', 'remote', true,
                         'description', 'one', 'url', 'https://example.test/clock-1'),
      jsonb_build_object('fingerprint', 'clock-fp-2', 'source', 'test',
                         'company', 'Clock Co', 'title', 'Engineer Two',
                         'location', 'Remote', 'remote', true,
                         'description', 'two', 'url', 'https://example.test/clock-2')));

create temporary table temp_clock_batch as
select temp_clock_ids.input_index, j.first_seen_at, j.created_at
  from temp_clock_ids
  join public.job_hunter_jobs j on j.id = temp_clock_ids.id
 order by temp_clock_ids.input_index;

select ok(
  (select first_seen_at from temp_clock_batch where input_index = 0) <
  (select first_seen_at from temp_clock_batch where input_index = 1),
  'two jobs upserted in one batch call get strictly increasing first_seen_at');

select ok(
  (select created_at from temp_clock_batch where input_index = 0) <
  (select created_at from temp_clock_batch where input_index = 1),
  'two jobs upserted in one batch call get strictly increasing created_at');

-- When two elements of one batch canonicalize to the same identity, the
-- earlier-input job must be the merge survivor. A tied first_seen_at (the
-- bug this pins) lets survivor selection fall through to `id asc` on
-- random uuids, which has no relationship to input order -- so this checks
-- an observable field only the earlier-input job's row carries, rather
-- than just that a merge happened at all (already covered above).
create temporary table temp_survivor_batch as
select * from public.job_hunter_upsert_jobs(
  jsonb_build_array(
    jsonb_build_object('fingerprint', 'survivor-fp-a', 'source', 'test',
                       'company', 'Survivor Co', 'title', 'Engineer',
                       'location', 'Remote', 'remote', true, 'description', 'first',
                       'canonical_url', 'https://example.test/survivor'),
    jsonb_build_object('fingerprint', 'survivor-fp-b', 'source', 'test',
                       'company', 'Loser Co', 'title', 'Engineer',
                       'location', 'Remote', 'remote', true, 'description', 'second',
                       'canonical_url', 'https://example.test/survivor')));

select is(
  (select count(distinct id)::int from temp_survivor_batch),
  1,
  'sanity check: the two survivor-test jobs really did merge into one');

select is(
  (select company from public.job_hunter_jobs
    where id = (select id from temp_survivor_batch limit 1)),
  'Survivor Co',
  'the earlier-input job (input_index 0) is the merge survivor, not whichever id sorts lower');

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

-- An id that does not exist at all returns no row at all.
select is(
  (select count(*)::int from public.job_hunter_needs_evaluation(
     array['99999999-0000-0000-0000-000000000009'::uuid])),
  0,
  'a nonexistent id returns no row rather than a verdict');

-- An id that DOES exist, but belongs to another user, also returns no row --
-- this is the RLS case proper, distinct from "no such row" above: it proves
-- a real job that RLS filters is silent rather than mistakenly answered.
select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');

select is(
  (select count(*)::int from public.job_hunter_needs_evaluation(
     array(select id from temp_rls_user_a))),
  0,
  'user A''s real job id returns no row when user B asks -- RLS holds, not just a missing-row coincidence');

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_needs_evaluation'),
  false,
  'job_hunter_needs_evaluation is security invoker');

-- The asymmetric null comparison, pinned independently of Python -----------
--
-- Both `job_hunter_jobs.description_hash` and
-- `job_hunter_evaluations.description_hash_at_eval` are `not null default
-- ''`, so a real NULL can never reach either column through the normal
-- upsert/save-evaluation paths -- the coalesce on the job side is
-- defensive, not reachable in production today. To pin the deliberate
-- asymmetry described in the migration comment (job side coalesced,
-- evaluation side not) at the SQL layer regardless, this relaxes the
-- evaluation column's NOT NULL constraint for the rest of this rolled-back
-- transaction only, inserts a genuine NULL, and asserts the current
-- (asymmetric) verdict. A "tidied" comparison that coalesced the
-- evaluation side too (`coalesce(e.description_hash_at_eval, '') is
-- distinct from j.description_hash`) would report `false` here instead --
-- do not "fix" this test to match that if it starts failing.
select pg_temp.become_postgres();
alter table public.job_hunter_evaluations alter column description_hash_at_eval drop not null;
select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

create temporary table temp_asym_job as
select id from public.job_hunter_upsert_jobs(
  jsonb_build_array(jsonb_build_object(
    'fingerprint', 'needs-fp-asym', 'source', 'test', 'company', 'Acme',
    'title', 'Engineer', 'location', 'Remote', 'remote', true,
    'description', '', 'url', 'https://example.test/n-asym')));

update public.job_hunter_jobs set description_hash = '' where id = (select id from temp_asym_job);

insert into public.job_hunter_evaluations
  (user_id, job_id, status, description_hash_at_eval, content_confidence_at_eval, evaluated_at)
values
  ((select auth.uid()), (select id from temp_asym_job), 'ok', null, '', now());

select is(
  (select needs from public.job_hunter_needs_evaluation(
     array(select id from temp_asym_job))),
  true,
  'a NULL description_hash_at_eval against an empty stored job hash counts as changed -- the asymmetry is deliberate, not a bug');

-- Not restored: the whole transaction rolls back below, so the relaxed
-- constraint never reaches the real schema, and re-tightening it here
-- would fail anyway with the NULL row still present.

-- set_job_markets -------------------------------------------------------

-- Each of three rows gets its own market, not one market applied to all --
-- an implementation that wrote the first row's market everywhere would
-- fail two of the three checks below.
create temporary table temp_market_jobs as
select input_index, id from public.job_hunter_upsert_jobs(
  jsonb_build_array(
    jsonb_build_object('fingerprint', 'market-fp-1', 'source', 'test', 'company', 'Acme',
                       'title', 'Engineer 1', 'location', 'Remote', 'remote', true,
                       'description', 'market1', 'url', 'https://example.test/m1'),
    jsonb_build_object('fingerprint', 'market-fp-2', 'source', 'test', 'company', 'Acme',
                       'title', 'Engineer 2', 'location', 'Remote', 'remote', true,
                       'description', 'market2', 'url', 'https://example.test/m2'),
    jsonb_build_object('fingerprint', 'market-fp-3', 'source', 'test', 'company', 'Acme',
                       'title', 'Engineer 3', 'location', 'Remote', 'remote', true,
                       'description', 'market3', 'url', 'https://example.test/m3')));

select public.job_hunter_set_job_markets(
  jsonb_build_array(
    jsonb_build_object('id', (select id from temp_market_jobs where input_index = 0), 'market_id', 'israel'),
    jsonb_build_object('id', (select id from temp_market_jobs where input_index = 1), 'market_id', 'eu_remote'),
    jsonb_build_object('id', (select id from temp_market_jobs where input_index = 2), 'market_id', 'us_remote')));

select is(
  (select market_id from public.job_hunter_jobs where id = (select id from temp_market_jobs where input_index = 0)),
  'israel',
  'job_hunter_set_job_markets writes the first row its own market');

select is(
  (select market_id from public.job_hunter_jobs where id = (select id from temp_market_jobs where input_index = 1)),
  'eu_remote',
  'job_hunter_set_job_markets writes the second row its own, distinct market');

select is(
  (select market_id from public.job_hunter_jobs where id = (select id from temp_market_jobs where input_index = 2)),
  'us_remote',
  'job_hunter_set_job_markets writes the third row its own, distinct market');

-- market_id is `not null default ''`: the store's Python wrapper turns a
-- caller's `None` into `''` before this function ever sees it (see
-- set_job_markets in postgres_store.py), so an empty string -- not JSON
-- null -- is the real-world "unattributed" value this function must
-- accept and write through, clearing a prior attribution.
select public.job_hunter_set_job_markets(
  jsonb_build_array(
    jsonb_build_object('id', (select id from temp_market_jobs where input_index = 0), 'market_id', '')));

select is(
  (select market_id from public.job_hunter_jobs where id = (select id from temp_market_jobs where input_index = 0)),
  '',
  'an empty-string market_id clears a prior attribution');

-- A genuine JSON null is rejected by the not-null column constraint,
-- exactly like any other write to this column -- proving this function
-- does not quietly coerce null into a different stored value. The id
-- used must match a real, readable row, or the update simply matches no
-- rows and the constraint never fires.
select throws_ok(
  format(
    $$select public.job_hunter_set_job_markets(
        jsonb_build_array(jsonb_build_object('id', %L, 'market_id', null)))$$,
    (select id from temp_market_jobs where input_index = 2)),
  '23502',
  null,
  'a JSON null market_id is rejected by the not-null column constraint');

-- An empty array issues no update and raises nothing.
select lives_ok(
  $$select public.job_hunter_set_job_markets('[]'::jsonb)$$,
  'an empty array is a no-op');

select is(
  (select market_id from public.job_hunter_jobs where id = (select id from temp_market_jobs where input_index = 1)),
  'eu_remote',
  'an empty-array call leaves unrelated rows exactly as they were');

-- RLS isolation: user B calling with user A's job ids changes nothing of
-- A's rows, and A can still read her own (unaltered) values afterward.
select pg_temp.authenticate_as('22222222-0000-0000-0000-00000000000b');

-- A job id the caller cannot read is silently a no-op rather than an
-- error: user B calls with the id of one of user A's jobs.
select public.job_hunter_set_job_markets(
  jsonb_build_array(jsonb_build_object(
    'id', (select id from temp_market_jobs where input_index = 1), 'market_id', 'us_remote')));

select pg_temp.authenticate_as('11111111-0000-0000-0000-00000000000a');

select is(
  (select market_id from public.job_hunter_jobs where id = (select id from temp_market_jobs where input_index = 1)),
  'eu_remote',
  'user B calling with user A''s job id leaves A''s market untouched -- RLS holds');

select is(
  (select p.prosecdef from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
    where n.nspname = 'public' and p.proname = 'job_hunter_set_job_markets'),
  false,
  'job_hunter_set_job_markets is security invoker');

select * from finish();
rollback;

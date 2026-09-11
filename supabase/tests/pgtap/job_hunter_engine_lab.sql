-- Engine Lab measurement ledger (issue #257).
--
-- What has to be true and cannot be asserted from the Python side alone:
--
--   * an impression, a judgement, or any of the five version columns can
--     never be silently omitted;
--   * a judgement's two verdicts are separate columns, at most one
--     judgement exists per impression;
--   * there is no identity or login scheme in this schema at all (see the
--     migration's "Superseded" note) -- nobody signed in as `authenticated`
--     or `anon` can read or write either table, full stop, since there is
--     no per-reviewer session for RLS to key off. Only a trusted connection
--     (service_role, or the postgres role this test runs as before
--     switching roles) can reach them.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed ------------------------------------------------------------------

insert into public.job_hunter_postings
  (id, fingerprint, first_seen_at, last_seen_at)
values
  ('eeeeeeee-0000-0000-0000-000000000001', 'engine-lab-pgtap-posting-1', now(), now())
on conflict (id) do nothing;

create function pg_temp.become_anon() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', jsonb_build_object('role', 'anon')::text, true);
  execute 'set local role anon';
end $$;

create function pg_temp.become_authenticated(p_user uuid) returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config(
    'request.jwt.claims',
    jsonb_build_object('sub', p_user, 'role', 'authenticated')::text,
    true
  );
  execute 'set local role authenticated';
end $$;

create function pg_temp.become_postgres() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

-- Shape -------------------------------------------------------------------

select has_table('public', 'job_hunter_engine_lab_impressions', 'impressions has a table');
select has_table('public', 'job_hunter_engine_lab_judgements', 'judgements has a table');

select columns_are('public', 'job_hunter_engine_lab_impressions', array[
  'id', 'reviewer_id', 'posting_id', 'cohort',
  'profile_version', 'posting_version', 'matching_version',
  'explanation_version', 'configuration_version', 'shown_at'
], 'an impression carries the card, its cohort and every version');

select columns_are('public', 'job_hunter_engine_lab_judgements', array[
  'id', 'impression_id', 'reviewer_id', 'worth_applying',
  'why_line_judgement', 'problem_reason', 'judged_at'
], 'a judgement carries both verdicts independently');

select col_is_unique('public', 'job_hunter_engine_lab_judgements', array['impression_id'],
  'at most one judgement per impression');

select is(
  (select count(*)::int from pg_policies
    where schemaname = 'public'
      and tablename in ('job_hunter_engine_lab_impressions', 'job_hunter_engine_lab_judgements')),
  0,
  'neither table has any RLS policy at all -- there is no per-reviewer identity to key one off'
);

select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_engine_lab_impressions'::regclass),
  true, 'row-level security is on for impressions');
select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_engine_lab_judgements'::regclass),
  true, 'row-level security is on for judgements');

-- Not-null / check constraints ------------------------------------------------

select pg_temp.become_postgres();

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, posting_version, matching_version, explanation_version, configuration_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'profile_version cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, matching_version, explanation_version, configuration_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'posting_version cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version, explanation_version, configuration_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'v1', 'e1', 'c1')$$,
  '23502', null, 'matching_version cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version, matching_version, configuration_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'v1', 'm1', 'c1')$$,
  '23502', null, 'explanation_version cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version, matching_version, explanation_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'v1', 'm1', 'e1')$$,
  '23502', null, 'configuration_version cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, profile_version, posting_version, matching_version, explanation_version, configuration_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'p1', 'v1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'cohort cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version, matching_version, explanation_version, configuration_version)
    values ('r1', 'eeeeeeee-0000-0000-0000-000000000001', 'not_a_real_cohort', 'p1', 'v1', 'm1', 'e1', 'c1')$$,
  '23514', null, 'cohort is limited to the four known buckets'
);

insert into public.job_hunter_engine_lab_impressions
  (id, reviewer_id, posting_id, cohort, profile_version, posting_version, matching_version, explanation_version, configuration_version)
values
  ('eeeeeeee-1000-0000-0000-000000000001', 'r1', 'eeeeeeee-0000-0000-0000-000000000001',
   'intended', 'p1', 'v1', 'm1', 'e1', 'c1');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, why_line_judgement)
    values ('eeeeeeee-1000-0000-0000-000000000001', 'r1', 'helpful')$$,
  '23502', null, 'worth_applying cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying)
    values ('eeeeeeee-1000-0000-0000-000000000001', 'r1', true)$$,
  '23502', null, 'why_line_judgement cannot be silently omitted'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying, why_line_judgement)
    values ('eeeeeeee-1000-0000-0000-000000000001', 'r1', true, 'sort_of')$$,
  '23514', null, 'why_line_judgement is limited to helpful or flawed'
);

insert into public.job_hunter_engine_lab_judgements
  (impression_id, reviewer_id, worth_applying, why_line_judgement)
values
  ('eeeeeeee-1000-0000-0000-000000000001', 'r1', true, 'helpful');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying, why_line_judgement)
    values ('eeeeeeee-1000-0000-0000-000000000001', 'r1', false, 'flawed')$$,
  null, null, 'a second judgement on the same impression is refused, not recorded as an edit'
);

-- No identity scheme: neither role can reach either table at all -----------

select pg_temp.become_anon();
select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions),
  0, 'anon reads no impressions -- there is no policy granting it any'
);
select is(
  (select count(*)::int from public.job_hunter_engine_lab_judgements),
  0, 'anon reads no judgements -- there is no policy granting it any'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version, matching_version, explanation_version, configuration_version)
    values ('intruder', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'v1', 'm1', 'e1', 'c1')$$,
  '42501', null, 'anon cannot write an impression'
);

select pg_temp.become_authenticated('eeeeeeee-a000-0000-0000-0000000000a1');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions),
  0, 'an ordinary signed-in user reads no impressions -- there is no per-reviewer session here'
);
select is(
  (select count(*)::int from public.job_hunter_engine_lab_judgements),
  0, 'an ordinary signed-in user reads no judgements'
);
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version, matching_version, explanation_version, configuration_version)
    values ('intruder', 'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'p1', 'v1', 'm1', 'e1', 'c1')$$,
  '42501', null, 'an ordinary signed-in user cannot write an impression either'
);

-- Ground truth: the trusted (postgres) connection sees everything ----------

select pg_temp.become_postgres();
select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions),
  1, 'the trusted connection reads across the whole ledger'
);
select is(
  (select count(*)::int from public.job_hunter_engine_lab_judgements),
  1, 'the trusted connection reads every judgement too'
);

select pg_temp.become_postgres();
select * from finish();
rollback;

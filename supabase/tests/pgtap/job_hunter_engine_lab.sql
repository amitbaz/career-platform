-- Engine Lab review page and measurement ledger (issue #257).
--
-- What has to be true and cannot be asserted from the Python side alone:
--
--   * an impression, a judgement, or any of the five version columns can
--     never be silently omitted -- every one of them is `not null` with no
--     default that would let a caller skip it unnoticed;
--   * a judgement's two verdicts (worth_applying, why_line_judgement) are
--     separate columns, and at most one judgement exists per impression;
--   * nobody without an explicit `engine_lab_reviewer`/`engine_lab_admin`/
--     `job_hunter_runner` claim can read or write any of the three tables --
--     a plain signed-in user (the claim every Relay/product user carries)
--     is refused outright, which is what "only the owner and explicitly
--     invited collaborators" means as a mechanism rather than a sentence.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed ------------------------------------------------------------------

insert into public.job_hunter_postings
  (id, fingerprint, first_seen_at, last_seen_at)
values
  ('eeeeeeee-0000-0000-0000-000000000001', 'engine-lab-pgtap-posting-1', now(), now())
on conflict (id) do nothing;

insert into public.job_hunter_engine_lab_collaborators
  (user_id, email, token_hash, invited_by)
values
  ('eeeeeeee-0000-0000-0000-0000000000a1', 'reviewer-a@test.local', 'hash-a', 'owner@test.local'),
  ('eeeeeeee-0000-0000-0000-0000000000a2', 'reviewer-b@test.local', 'hash-b', 'owner@test.local')
on conflict (user_id) do nothing;

create function pg_temp.authenticate_as(
  p_user uuid,
  p_runner boolean default false,
  p_admin boolean default false,
  p_reviewer boolean default false
) returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config(
    'request.jwt.claims',
    jsonb_build_object(
      'sub', p_user,
      'role', 'authenticated',
      'job_hunter_runner', p_runner,
      'engine_lab_admin', p_admin,
      'engine_lab_reviewer', p_reviewer
    )::text,
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

select has_table('public', 'job_hunter_engine_lab_collaborators', 'collaborators has a table');
select has_table('public', 'job_hunter_engine_lab_impressions', 'impressions has a table');
select has_table('public', 'job_hunter_engine_lab_judgements', 'judgements has a table');

select columns_are('public', 'job_hunter_engine_lab_collaborators', array[
  'user_id', 'email', 'display_name', 'token_hash', 'invited_at', 'invited_by', 'revoked_at'
], 'collaborators carries exactly who was invited and by whom');

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
      and tablename in ('job_hunter_engine_lab_impressions', 'job_hunter_engine_lab_judgements')
      and cmd in ('UPDATE', 'DELETE')),
  0,
  'impressions and judgements are insert-only -- nothing may edit or erase the ledger'
);

select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_engine_lab_collaborators'::regclass),
  true, 'row-level security is on for collaborators');
select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_engine_lab_impressions'::regclass),
  true, 'row-level security is on for impressions');
select is(
  (select relrowsecurity from pg_class where oid = 'public.job_hunter_engine_lab_judgements'::regclass),
  true, 'row-level security is on for judgements');

-- Nothing can be silently omitted ------------------------------------------

select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-0000000000a1', false, false, true);

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, posting_version, matching_version,
       explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'p1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'profile_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, matching_version,
       explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'posting_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'e1', 'c1')$$,
  '23502', null, 'matching_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'c1')$$,
  '23502', null, 'explanation_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'e1')$$,
  '23502', null, 'configuration_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'cohort cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'not_a_real_cohort', 'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '23514', null, 'cohort is limited to the four known buckets');

-- A real impression, then its judgement -------------------------------------

insert into public.job_hunter_engine_lab_impressions
  (id, reviewer_id, posting_id, cohort, profile_version, posting_version,
   matching_version, explanation_version, configuration_version)
values
  ('eeeeeeee-1111-0000-0000-000000000001', 'eeeeeeee-0000-0000-0000-0000000000a1',
   'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'v1', 'p1', 'm1', 'e1', 'c1');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, why_line_judgement)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-0000-0000-0000-0000000000a1', 'helpful')$$,
  '23502', null, 'worth_applying cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-0000-0000-0000-0000000000a1', true)$$,
  '23502', null, 'why_line_judgement cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying, why_line_judgement)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-0000-0000-0000-0000000000a1', true, 'sort_of')$$,
  '23514', null, 'why_line_judgement is limited to helpful or flawed');

insert into public.job_hunter_engine_lab_judgements
  (impression_id, reviewer_id, worth_applying, why_line_judgement)
values
  ('eeeeeeee-1111-0000-0000-000000000001', 'eeeeeeee-0000-0000-0000-0000000000a1',
   true, 'helpful');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying, why_line_judgement)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-0000-0000-0000-0000000000a1', false, 'flawed')$$,
  '23505', null, 'a second judgement on the same impression is refused, not recorded as an edit');

-- Access ----------------------------------------------------------------

-- A plain signed-in user (no engine-lab claim at all) is refused outright.
select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-0000000000a1');

select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators), 0,
  'a plain signed-in user cannot read the collaborator list');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions), 0,
  'a plain signed-in user cannot read impressions');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_judgements), 0,
  'a plain signed-in user cannot read judgements');
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '42501', null, 'a plain signed-in user cannot write an impression');

-- A reviewer cannot write, or see, another reviewer's rows.
select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-0000000000a2', false, false, true);

select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions
    where reviewer_id = 'eeeeeeee-0000-0000-0000-0000000000a1'),
  0, 'one reviewer cannot see another reviewer''s impressions');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-0000-0000-0000-0000000000a1', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '42501', null, 'a reviewer cannot write an impression claiming to be someone else');

-- A runner can read across reviewers, but still cannot invite anyone.
select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-000000000099', true);

select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions),
  1, 'a trusted runner reads across every reviewer, for the daily summary');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators),
  2, 'a trusted runner can read the collaborator list, to check a login token');
select throws_ok(
  $$insert into public.job_hunter_engine_lab_collaborators
      (user_id, email, token_hash, invited_by)
    values ('eeeeeeee-0000-0000-0000-0000000000a3', 'reviewer-c@test.local', 'hash-c', 'owner@test.local')$$,
  '42501', null, 'a runner claim alone cannot invite a collaborator');

-- Only the admin claim can invite. `engine_lab.admin_client` mints both
-- claims together (the platform's own `job_hunter_runner` claim is never
-- turned off for it, unlike `reviewer_client`), so the read-back below
-- needs both too, matching how the CLI actually authenticates.
select pg_temp.authenticate_as('eeeeeeee-0000-0000-0000-000000000099', true, true, false);

insert into public.job_hunter_engine_lab_collaborators
  (user_id, email, token_hash, invited_by)
values
  ('eeeeeeee-0000-0000-0000-0000000000a3', 'reviewer-c@test.local', 'hash-c', 'owner@test.local');

select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators
    where email = 'reviewer-c@test.local'),
  1, 'the admin claim can invite a new collaborator');

select pg_temp.become_postgres();
select * from finish();
rollback;

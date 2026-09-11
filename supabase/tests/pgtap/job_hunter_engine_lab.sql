-- Engine Lab review page and measurement ledger (issue #257).
--
-- What has to be true and cannot be asserted from the Python side alone:
--
--   * only a caller carrying the job_hunter_runner claim can ever produce an
--     owner -- job_hunter_engine_lab_bootstrap_owner refuses every ordinary
--     signed-in session outright, whatever p_user_id/p_email it passes, and
--     refuses a second, different email once an owner exists even from a
--     runner caller;
--   * only the owner may invite; an invited row has no user_id until the
--     invitee's own verified email claims it, and nobody can claim someone
--     else's invite or a revoked one;
--   * an impression, a judgement, or any of the five version columns can
--     never be silently omitted;
--   * a judgement's two verdicts are separate columns, at most one
--     judgement exists per impression, and nobody without a real
--     collaborator row can read or write any of the three tables -- a
--     plain signed-in user (real Supabase Auth, no exploit needed) is
--     refused outright.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed ------------------------------------------------------------------

insert into public.job_hunter_postings
  (id, fingerprint, first_seen_at, last_seen_at)
values
  ('eeeeeeee-0000-0000-0000-000000000001', 'engine-lab-pgtap-posting-1', now(), now())
on conflict (id) do nothing;

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local',        '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-a000-0000-0000-0000000000a2', 'impostor@test.local',     '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-a000-0000-0000-0000000000a3', 'second-owner@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-a000-0000-0000-0000000000a4', 'collaborator@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-a000-0000-0000-0000000000a5', 'uninvited@test.local',    '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-a000-0000-0000-0000000000a6', 'revoked@test.local',      '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('eeeeeeee-a000-0000-0000-0000000000a7', 'second-collaborator@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid, p_email text, p_runner boolean default false) returns void
language plpgsql as $$
begin
  execute 'reset role';
  perform set_config(
    'request.jwt.claims',
    jsonb_build_object(
      'sub', p_user, 'role', 'authenticated', 'email', p_email, 'job_hunter_runner', p_runner
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
  'email', 'user_id', 'is_owner', 'invited_at', 'invited_by', 'revoked_at'
], 'collaborators is keyed by email, since an invitee has no user_id yet');

select columns_are('public', 'job_hunter_engine_lab_impressions', array[
  'id', 'reviewer_id', 'posting_id', 'cohort',
  'profile_version', 'posting_version', 'matching_version',
  'explanation_version', 'configuration_version', 'shown_at'
], 'an impression carries the card, its cohort and every version');

select columns_are('public', 'job_hunter_engine_lab_judgements', array[
  'id', 'impression_id', 'reviewer_id', 'worth_applying',
  'why_line_judgement', 'problem_reason', 'judged_at'
], 'a judgement carries both verdicts independently');

select has_function('public', 'job_hunter_engine_lab_caller_is_owner', array[]::text[], 'owner check exists');
select has_function('public', 'job_hunter_engine_lab_is_active_collaborator', array['uuid'], 'membership check exists');
select has_function('public', 'job_hunter_engine_lab_bootstrap_owner', array['uuid', 'text'], 'owner bootstrap exists');
select has_function('public', 'job_hunter_engine_lab_invite', array['text'], 'invite exists');
select has_function('public', 'job_hunter_engine_lab_claim_invite', array[]::text[], 'claim exists');

select col_is_unique('public', 'job_hunter_engine_lab_judgements', array['impression_id'],
  'at most one judgement per impression');

select is(
  (select count(*)::int from pg_policies
    where schemaname = 'public'
      and tablename = 'job_hunter_engine_lab_collaborators'
      and cmd in ('INSERT', 'UPDATE', 'DELETE')),
  0,
  'collaborators has no direct write policy at all -- every write goes through a security-definer function'
);
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

-- Bootstrapping the owner ---------------------------------------------------
--
-- The database's own gate is the job_hunter_runner claim, not the caller's
-- own email: an ordinary signed-in session can never call this RPC
-- successfully, whatever p_user_id/p_email it passes -- only a
-- platform-minted token (AccessTokenMinter, run server-side) carries the
-- claim at all. See the reviewed security fix on PR #282.

-- An ordinary authenticated session -- even claiming its own real email --
-- has no job_hunter_runner claim and is refused outright.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a2', 'impostor@test.local');
select throws_ok(
  $$select public.job_hunter_engine_lab_bootstrap_owner('eeeeeeee-a000-0000-0000-0000000000a2', 'owner@test.local')$$,
  null, null,
  'an ordinary session with no job_hunter_runner claim cannot bootstrap ownership at all'
);
select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators where is_owner),
  0, 'the impostor''s attempt left no owner behind'
);

-- Only a runner-claimed caller can bootstrap the real owner.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local', true);
select public.job_hunter_engine_lab_bootstrap_owner('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local');
select is(
  (select user_id from public.job_hunter_engine_lab_collaborators where email = 'owner@test.local'),
  'eeeeeeee-a000-0000-0000-0000000000a1'::uuid,
  'a runner-claimed caller successfully bootstraps the real owner'
);

-- Re-bootstrapping the same owner is idempotent (e.g. logging in again).
select public.job_hunter_engine_lab_bootstrap_owner('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators where is_owner),
  1, 'bootstrapping the same owner twice does not create a second owner row'
);

-- A second, genuinely different email can never also become owner, even
-- from a runner-claimed caller.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a3', 'second-owner@test.local', true);
select throws_ok(
  $$select public.job_hunter_engine_lab_bootstrap_owner('eeeeeeee-a000-0000-0000-0000000000a3', 'second-owner@test.local')$$,
  null, null,
  'a second, different email can never also claim ownership once one exists, even for a runner caller'
);
-- The rejected caller has no collaborator row, so RLS would hide the real
-- owner row from them anyway; check the ground truth as postgres instead.
select pg_temp.become_postgres();
select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators where is_owner),
  1, 'still exactly one owner'
);

-- Inviting and claiming ------------------------------------------------------

-- A non-owner cannot invite.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a3', 'second-owner@test.local');
select throws_ok(
  $$select public.job_hunter_engine_lab_invite('collaborator@test.local')$$,
  null, null,
  'only the owner may invite a collaborator'
);

select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local');
select public.job_hunter_engine_lab_invite('collaborator@test.local');
select is(
  (select user_id from public.job_hunter_engine_lab_collaborators where email = 'collaborator@test.local'),
  null,
  'a fresh invite has no user_id until the invitee signs in'
);
select is(
  (select invited_by from public.job_hunter_engine_lab_collaborators where email = 'collaborator@test.local'),
  'eeeeeeee-a000-0000-0000-0000000000a1'::uuid,
  'the invite records who invited them'
);

-- Someone nobody invited cannot claim anything.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a5', 'uninvited@test.local');
select is(
  (select public.job_hunter_engine_lab_claim_invite()),
  false,
  'claiming with an email nobody invited does nothing'
);

-- The real invitee claims their own invite.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a4', 'collaborator@test.local');
select is(
  (select public.job_hunter_engine_lab_claim_invite()),
  true,
  'the invited email successfully claims its own row'
);
select is(
  (select user_id from public.job_hunter_engine_lab_collaborators where email = 'collaborator@test.local'),
  'eeeeeeee-a000-0000-0000-0000000000a4'::uuid,
  'the invite is now bound to the real signed-in user'
);
-- Claiming again (a later login) is idempotent.
select is(
  (select public.job_hunter_engine_lab_claim_invite()),
  true,
  're-claiming on a later login is a harmless no-op, not an error'
);

-- A revoked invite can never be claimed, even by the right email. Seeded
-- directly as postgres: there is no insert policy for `authenticated` on
-- this table at all (every real write goes through a security-definer
-- function), so a plain insert under a signed-in role would be refused.
select pg_temp.become_postgres();
insert into public.job_hunter_engine_lab_collaborators (email, invited_by, revoked_at)
values ('revoked@test.local', 'eeeeeeee-a000-0000-0000-0000000000a1', now());
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a6', 'revoked@test.local');
select is(
  (select public.job_hunter_engine_lab_claim_invite()),
  false,
  'a revoked invite cannot be claimed'
);

-- Nothing can be silently omitted --------------------------------------------

select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a4', 'collaborator@test.local');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, posting_version, matching_version,
       explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'p1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'profile_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, matching_version,
       explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'posting_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'e1', 'c1')$$,
  '23502', null, 'matching_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'c1')$$,
  '23502', null, 'explanation_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'e1')$$,
  '23502', null, 'configuration_version cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '23502', null, 'cohort cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'not_a_real_cohort', 'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '23514', null, 'cohort is limited to the four known buckets');

-- A real impression, then its judgement -------------------------------------

insert into public.job_hunter_engine_lab_impressions
  (id, reviewer_id, posting_id, cohort, profile_version, posting_version,
   matching_version, explanation_version, configuration_version)
values
  ('eeeeeeee-1111-0000-0000-000000000001', 'eeeeeeee-a000-0000-0000-0000000000a4',
   'eeeeeeee-0000-0000-0000-000000000001', 'intended', 'v1', 'p1', 'm1', 'e1', 'c1');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, why_line_judgement)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-a000-0000-0000-0000000000a4', 'helpful')$$,
  '23502', null, 'worth_applying cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-a000-0000-0000-0000000000a4', true)$$,
  '23502', null, 'why_line_judgement cannot be silently omitted');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying, why_line_judgement)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-a000-0000-0000-0000000000a4', true, 'sort_of')$$,
  '23514', null, 'why_line_judgement is limited to helpful or flawed');

insert into public.job_hunter_engine_lab_judgements
  (impression_id, reviewer_id, worth_applying, why_line_judgement)
values
  ('eeeeeeee-1111-0000-0000-000000000001', 'eeeeeeee-a000-0000-0000-0000000000a4',
   true, 'helpful');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_judgements
      (impression_id, reviewer_id, worth_applying, why_line_judgement)
    values ('eeeeeeee-1111-0000-0000-000000000001',
            'eeeeeeee-a000-0000-0000-0000000000a4', false, 'flawed')$$,
  '23505', null, 'a second judgement on the same impression is refused, not recorded as an edit');

-- Access ----------------------------------------------------------------

-- A plain signed-in user with no collaborator row at all is refused outright.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a5', 'uninvited@test.local');

select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators), 0,
  'a non-collaborator cannot read the collaborator list');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions), 0,
  'a non-collaborator cannot read impressions');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_judgements), 0,
  'a non-collaborator cannot read judgements');
select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a5', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '42501', null, 'a non-collaborator cannot write an impression');

-- A collaborator cannot write, or see, another reviewer's rows, but the
-- owner can see across everyone.
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local');
select public.job_hunter_engine_lab_invite('second-collaborator@test.local');
select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a7', 'second-collaborator@test.local');
select public.job_hunter_engine_lab_claim_invite();

select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions
    where reviewer_id = 'eeeeeeee-a000-0000-0000-0000000000a4'),
  0, 'one collaborator cannot see another collaborator''s impressions');

select throws_ok(
  $$insert into public.job_hunter_engine_lab_impressions
      (reviewer_id, posting_id, cohort, profile_version, posting_version,
       matching_version, explanation_version, configuration_version)
    values ('eeeeeeee-a000-0000-0000-0000000000a4', 'eeeeeeee-0000-0000-0000-000000000001',
            'intended', 'v1', 'p1', 'm1', 'e1', 'c1')$$,
  '42501', null, 'a collaborator cannot write an impression claiming to be someone else');

select pg_temp.authenticate_as('eeeeeeee-a000-0000-0000-0000000000a1', 'owner@test.local');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_impressions),
  1, 'the owner reads across every collaborator, for the daily summary');
select is(
  (select count(*)::int from public.job_hunter_engine_lab_collaborators),
  4, 'the owner can read the full collaborator list'
);

select pg_temp.become_postgres();
select * from finish();
rollback;

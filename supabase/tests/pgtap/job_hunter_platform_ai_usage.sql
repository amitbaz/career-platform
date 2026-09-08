-- The platform key's ledger is global, and only a runner can reach it (#128).
--
-- Two properties are load-bearing and neither is visible from the Python side:
--
--   * the ledger carries no user_id, so one run sees the platform key's whole
--     day rather than its own user's slice of it. Without that, every user's
--     run would believe the shared allowance was untouched.
--   * a signed-in user cannot read or write it. Platform consumption is
--     operator accounting, not user data, and the tables are reachable only
--     with the job_hunter_runner claim a trusted batch process carries.

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

insert into auth.users
  (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('dddddddd-0000-0000-0000-000000000001', 'platform-runner-a@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('dddddddd-0000-0000-0000-000000000002', 'platform-runner-b@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

create function pg_temp.authenticate_as(p_user uuid, p_runner boolean default false)
returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config(
    'request.jwt.claims',
    jsonb_build_object(
      'sub', p_user,
      'role', 'authenticated',
      'job_hunter_runner', p_runner
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

-- Shape ---------------------------------------------------------------------

select has_table('public', 'job_hunter_platform_ai_usage',
                 'the platform key keeps its own ledger');
select has_table('public', 'job_hunter_platform_ai_quota_state',
                 'the platform key keeps its own pause');

select columns_are('public', 'job_hunter_platform_ai_usage', array[
  'id',
  'provider',
  'occurred_at',
  'model',
  'purpose',
  'status',
  'estimated_input_tokens',
  'prompt_tokens',
  'output_tokens',
  'thinking_tokens',
  'cached_tokens',
  'total_tokens',
  'http_status',
  'error_code',
  'created_at'
], 'the platform ledger records the same attempt facts as the per-user one');

-- The absent column is the design. Spelled out separately from columns_are so
-- a failure names the reason rather than a diff of fifteen strings.
select hasnt_column('public', 'job_hunter_platform_ai_usage', 'user_id',
                    'platform spend belongs to no user, so it is not keyed by one');
select hasnt_column('public', 'job_hunter_platform_ai_quota_state', 'user_id',
                    'a platform pause is global, not one user''s');

select col_is_unique('public', 'job_hunter_platform_ai_usage',
                     array['provider', 'model', 'purpose', 'occurred_at'],
                     'a retried write converges instead of double-counting a call');
select col_is_unique('public', 'job_hunter_platform_ai_quota_state',
                     array['provider', 'model'],
                     'one pause per provider model');

select has_index('public', 'job_hunter_platform_ai_usage',
                 'job_hunter_platform_ai_usage_window_idx',
                 'the daily and rolling windows every preflight reads are indexed');

select is(
  (select relrowsecurity from pg_class
    where oid = 'public.job_hunter_platform_ai_usage'::regclass),
  true,
  'row-level security is on for the platform ledger'
);
select is(
  (select relrowsecurity from pg_class
    where oid = 'public.job_hunter_platform_ai_quota_state'::regclass),
  true,
  'row-level security is on for the platform pause'
);

-- A ledger a run can erase cannot answer how close the key is to its ceiling.
select is(
  (select count(*)::int from pg_policies
    where schemaname = 'public'
      and tablename in ('job_hunter_platform_ai_usage',
                        'job_hunter_platform_ai_quota_state')
      and cmd = 'DELETE'),
  0,
  'nothing may delete platform ledger or pause rows'
);

-- Access --------------------------------------------------------------------

select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000001', true);

insert into public.job_hunter_platform_ai_usage
  (provider, occurred_at, model, purpose, status, estimated_input_tokens)
values
  ('gemini', '2026-09-08T12:00:00Z', 'gemini-platform-pgtap', 'job_facets', 'success', 100);

select is(
  (select count(*)::int from public.job_hunter_platform_ai_usage
    where model = 'gemini-platform-pgtap'),
  1,
  'a runner records what the platform key spent'
);

insert into public.job_hunter_platform_ai_quota_state
  (provider, model, paused_until, reason)
values ('gemini', 'gemini-platform-pgtap', '2026-09-09T07:00:00Z', 'daily_quota');

-- The property the per-user ledger cannot have: a second runner, acting for a
-- different user, sees the same allowance rather than a fresh one.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000002', true);

select is(
  (select count(*)::int from public.job_hunter_platform_ai_usage
    where model = 'gemini-platform-pgtap'),
  1,
  'another user''s run sees the platform key''s whole day, not its own slice'
);
select is(
  (select reason from public.job_hunter_platform_ai_quota_state
    where model = 'gemini-platform-pgtap'),
  'daily_quota',
  'a platform pause is visible to every runner that would spend the key'
);

-- A signed-in user is not a runner.
select pg_temp.authenticate_as('dddddddd-0000-0000-0000-000000000001', false);

select is(
  (select count(*)::int from public.job_hunter_platform_ai_usage),
  0,
  'a signed-in user cannot read platform consumption'
);
select is(
  (select count(*)::int from public.job_hunter_platform_ai_quota_state),
  0,
  'a signed-in user cannot read the platform pause'
);
select throws_ok(
  $$insert into public.job_hunter_platform_ai_usage
      (provider, occurred_at, model, purpose, status, estimated_input_tokens)
    values ('gemini', '2026-09-08T13:00:00Z', 'gemini-platform-pgtap', 'job_facets', 'success', 1)$$,
  '42501',
  null,
  'a signed-in user cannot write platform consumption'
);

select pg_temp.become_postgres();
select * from finish();
rollback;

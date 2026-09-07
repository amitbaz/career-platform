-- RLS isolation test for job_hunter_search_profiles and job_hunter_search_profile_markets
begin;
create extension if not exists pgtap with schema extensions;
select plan(13);

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'user_a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'user_b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
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

-- Tests -----------------------------------------------------------------------

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001'::uuid);

insert into public.job_hunter_search_profiles
  (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
   source_max_share, salary_floor_eur, max_search_queries_per_run,
   max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75)
returning id as profile_id \gset

select ok(
  (select count(*) from public.job_hunter_search_profiles) = 1,
  'user_a can insert their own profile'
);

select pg_temp.authenticate_as('bbbbbbbb-0000-0000-0000-000000000002'::uuid);

select is(
  (select count(*)::int from public.job_hunter_search_profiles),
  0,
  'user_b cannot see user_a''s profile'
);

select throws_ok(
  $$ insert into public.job_hunter_search_profiles
       (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
        source_max_share, salary_floor_eur, max_search_queries_per_run,
        max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
     values
       ('aaaaaaaa-0000-0000-0000-000000000001', 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75) $$,
  '42501',
  null,
  'user_b cannot insert a profile owned by user_a'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001'::uuid);

insert into public.job_hunter_search_profile_markets
  (user_id, profile_id, market_id, query_share, currency, gross_base_floor,
   remote_policy, relocation_policy, sponsorship_policy)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', :'profile_id', 'germany_eu', 0.5, 'EUR', 90000,
   'preferred', 'selective', 'not_required')
returning id as market_id_pk \gset

select ok(
  (select count(*) from public.job_hunter_search_profile_markets) = 1,
  'user_a can insert a market row for their own profile'
);

select pg_temp.authenticate_as('bbbbbbbb-0000-0000-0000-000000000002'::uuid);

select is(
  (select count(*)::int from public.job_hunter_search_profile_markets),
  0,
  'user_b cannot see user_a''s market row'
);

select throws_ok(
  format(
    $$ insert into public.job_hunter_search_profile_markets
         (user_id, profile_id, market_id, query_share, currency, gross_base_floor,
          remote_policy, relocation_policy, sponsorship_policy)
       values
         ('bbbbbbbb-0000-0000-0000-000000000002', %L, 'israel_remote', 0.5, 'ILS', 420000,
          'required', 'none', 'not_required') $$,
    :'profile_id'
  ),
  '23503',
  null,
  'user_b cannot attach a market row to user_a''s profile (foreign key, not just RLS)'
);

select pg_temp.become_anon();

select is_empty(
  $$ select 1 from public.job_hunter_search_profiles $$,
  'anon cannot read search profiles'
);

select is_empty(
  $$ select 1 from public.job_hunter_search_profile_markets $$,
  'anon cannot read search profile markets'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001'::uuid);

select is(
  (select count(*)::int from public.job_hunter_search_profiles),
  1,
  'user_a''s profile is still there and readable'
);

select bag_eq(
  $$ select policyname from pg_policies where tablename = 'job_hunter_search_profiles' $$,
  ARRAY['select_own', 'insert_own', 'update_own', 'delete_own'],
  'job_hunter_search_profiles has exactly the four expected RLS policies'
);

select bag_eq(
  $$ select policyname from pg_policies where tablename = 'job_hunter_search_profile_markets' $$,
  ARRAY['select_own', 'insert_own', 'update_own', 'delete_own'],
  'job_hunter_search_profile_markets has exactly the four expected RLS policies'
);

select ok(
  (select relrowsecurity from pg_class where relname = 'job_hunter_search_profiles'),
  'RLS is enabled on job_hunter_search_profiles'
);

select ok(
  (select relrowsecurity from pg_class where relname = 'job_hunter_search_profile_markets'),
  'RLS is enabled on job_hunter_search_profile_markets'
);

select * from finish();
rollback;

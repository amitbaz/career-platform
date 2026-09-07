-- Delivery policy on job_hunter_search_profiles (issue #117).
--
-- The promise that a profile saved before this change keeps working without a
-- data migration rests on the columns' database defaults, and the promise that
-- an out-of-range value is never persisted rests on their check constraints.
-- Neither is observable from the application seam: the store writes whatever
-- the model holds and reads the row back untouched, so a test there would pass
-- against a table with no defaults and no checks. They are asserted here.
begin;
create extension if not exists pgtap with schema extensions;
select plan(4);

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'user_a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now())
on conflict (id) do nothing;

-- A profile written without the delivery-policy columns, i.e. every row that
-- existed before the migration ran.
insert into public.job_hunter_search_profiles
  (user_id, timezone, scheduled_hour, max_jobs_per_run, source_minimum_per_run,
   source_max_share, salary_floor_eur, max_search_queries_per_run,
   max_canonical_resolutions_per_run, max_learned_ats_boards_per_run)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'Europe/Berlin', 9, 35, 0, 0.5, 90000, 30, 80, 75);

select is(
  (select daily_offer_limit from public.job_hunter_search_profiles),
  10,
  'a profile saved without a daily offer limit reads back as 10'
);

select is(
  (select match_score_floor from public.job_hunter_search_profiles),
  80,
  'a profile saved without a match-score floor reads back as 80'
);

select throws_ok(
  $$ update public.job_hunter_search_profiles set daily_offer_limit = 7 $$,
  '23514',
  null,
  'a daily offer limit outside {5, 10, 20} is rejected'
);

select throws_ok(
  $$ update public.job_hunter_search_profiles set match_score_floor = 100 $$,
  '23514',
  null,
  'a match-score floor outside the accepted band is rejected'
);

select * from finish();
rollback;

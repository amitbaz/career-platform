-- Local development and test fixtures. Applied by `supabase db reset` and by
-- the first `supabase start`; never runs against the hosted project.
--
-- A pool of 8 user pairs. Each store-backed test run claims one pair and
-- uses it as its two seed users, so runs isolate by `user_id` -- which RLS
-- already scopes every query by -- instead of taking turns on one shared pair.
-- `apps/job-hunter/tests/seed_pool.py` owns the pool and does the claiming;
-- `tests/test_seed_pool.py` asserts this file and that module still agree.
--
-- Pair 0 is `aaaaaaaa-...0001` / `bbbbbbbb-...0002`, the pair that predates
-- the pool. The pgTAP isolation suite (supabase/tests/pgtap/) names those two
-- literals, so pair 0 has to keep them.
--
-- Emails are `a<slot>@test.local` / `b<slot>@test.local` only so each row has
-- a distinct one; nothing authenticates with them. Job Hunter's tests mint a
-- JWT for the user id directly.

insert into auth.users (
  id, email, instance_id, aud, role,
  raw_app_meta_data, raw_user_meta_data, created_at, updated_at
)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'a0@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'b0@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0001-000000000001', 'a1@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0001-000000000002', 'b1@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0002-000000000001', 'a2@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0002-000000000002', 'b2@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0003-000000000001', 'a3@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0003-000000000002', 'b3@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0004-000000000001', 'a4@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0004-000000000002', 'b4@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0005-000000000001', 'a5@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0005-000000000002', 'b5@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0006-000000000001', 'a6@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0006-000000000002', 'b6@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('aaaaaaaa-0000-0000-0007-000000000001', 'a7@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0007-000000000002', 'b7@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now())
on conflict (id) do nothing;

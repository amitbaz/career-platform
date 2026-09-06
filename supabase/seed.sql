-- Local development and test fixtures. Applied by `supabase db reset` and by
-- the first `supabase start`; never runs against the hosted project.
--
-- Two users, A and B, with the same fixed UUIDs the pgTAP isolation suite
-- uses (supabase/tests/pgtap/job_hunter_isolation.sql). Job Hunter's Python
-- integration test signs a token for each and proves that a run for A cannot
-- reach B's rows through the live policies.

insert into auth.users (
  id, email, instance_id, aud, role,
  raw_app_meta_data, raw_user_meta_data, created_at, updated_at
)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'a@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'b@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
   '{}', '{}', now(), now())
on conflict (id) do nothing;

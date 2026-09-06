-- Cross-user isolation for every Job Hunter table.
--
-- Two auth users, A and B, are seeded. For each table the check proves:
-- RLS is on, the four expected policies exist, A can insert, B sees
-- nothing on select/update/delete, B cannot insert a row that claims A's
-- user_id (SQLSTATE 42501, "new row violates row-level security policy"),
-- anon sees nothing and cannot insert, and A's row survives all of that.
--
-- RLS filters rather than errors: a select with no matching policy
-- returns zero rows, it does not raise. Only writes that fail a policy's
-- WITH CHECK raise 42501. The assertions below match that behaviour.
begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

-- Seed users ----------------------------------------------------------------

insert into auth.users (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'a@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'b@test.local', '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now());

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

-- Tables under test -------------------------------------------------------------

create view pg_temp.job_hunter_tables as
select unnest(array[
  'job_hunter_jobs',
  'job_hunter_job_sources',
  'job_hunter_company_watch',
  'job_hunter_ats_registry',
  'job_hunter_evaluations',
  'job_hunter_materials',
  'job_hunter_deliveries',
  'job_hunter_pending_ai_work'
]) as table_name;

-- Minimal row per table -----------------------------------------------------------
-- Child tables create their own parent row for the same owner first.

create function pg_temp.job_hunter_seed_row(p_table text, p_owner uuid) returns uuid
language plpgsql as $$
declare
  v_id uuid;
  v_job uuid;
begin
  case p_table
    when 'job_hunter_jobs' then
      insert into public.job_hunter_jobs (user_id, fingerprint, first_seen_at, last_seen_at)
      values (p_owner, gen_random_uuid()::text, now(), now()) returning id into v_id;
    when 'job_hunter_job_sources' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_job_sources (user_id, job_id, source, identity_key, first_seen_at, last_seen_at)
      values (p_owner, v_job, 'test', gen_random_uuid()::text, now(), now()) returning id into v_id;
    when 'job_hunter_company_watch' then
      insert into public.job_hunter_company_watch (user_id, company_name, normalized_company_name, promotion_source, first_seen_at)
      values (p_owner, 'Acme', gen_random_uuid()::text, 'manual', now()) returning id into v_id;
    when 'job_hunter_ats_registry' then
      insert into public.job_hunter_ats_registry (user_id, provider, board_identifier, first_seen_at, last_seen_at)
      values (p_owner, 'greenhouse', gen_random_uuid()::text, now(), now()) returning id into v_id;
    when 'job_hunter_evaluations' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_evaluations (user_id, job_id, evaluated_at)
      values (p_owner, v_job, now()) returning id into v_id;
    when 'job_hunter_materials' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_materials (user_id, job_id, generated_at)
      values (p_owner, v_job, now()) returning id into v_id;
    when 'job_hunter_deliveries' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_deliveries (user_id, job_id, delivery_type, delivered_at)
      values (p_owner, v_job, 'telegram_message', now()) returning id into v_id;
    when 'job_hunter_pending_ai_work' then
      v_job := pg_temp.job_hunter_seed_row('job_hunter_jobs', p_owner);
      insert into public.job_hunter_pending_ai_work (user_id, job_id, work_type)
      values (p_owner, v_job, 'evaluate') returning id into v_id;
    else
      raise exception 'no seed row defined for table %', p_table;
  end case;
  return v_id;
end $$;

-- The isolation check ---------------------------------------------------------------

create function pg_temp.check_isolation(p_table text, p_a uuid, p_b uuid) returns setof text
language plpgsql as $$
declare
  v_count int;
begin
  perform pg_temp.become_postgres();

  return next ok(
    (select relrowsecurity from pg_class where oid = ('public.' || p_table)::regclass),
    p_table || ': row level security is enabled');

  return next is(
    (select array_agg(policyname::text order by policyname)
       from pg_policies where schemaname = 'public' and tablename = p_table),
    array['delete_own', 'insert_own', 'select_own', 'update_own'],
    p_table || ': exactly the four *_own policies exist');

  perform pg_temp.authenticate_as(p_a);
  return next lives_ok(
    format('select pg_temp.job_hunter_seed_row(%L, %L)', p_table, p_a),
    p_table || ': A inserts own row');

  perform pg_temp.authenticate_as(p_b);
  return next is_empty(
    format('select 1 from public.%I', p_table),
    p_table || ': B selects nothing');
  return next is_empty(
    format('update public.%I set user_id = user_id returning 1', p_table),
    p_table || ': B updates nothing');
  return next is_empty(
    format('delete from public.%I returning 1', p_table),
    p_table || ': B deletes nothing');
  return next throws_ok(
    format('select pg_temp.job_hunter_seed_row(%L, %L)', p_table, p_a),
    '42501', null,
    p_table || ': B cannot insert a row owned by A');

  perform pg_temp.become_anon();
  return next is_empty(
    format('select 1 from public.%I', p_table),
    p_table || ': anon selects nothing');
  return next throws_ok(
    format('select pg_temp.job_hunter_seed_row(%L, %L)', p_table, p_a),
    '42501', null,
    p_table || ': anon cannot insert');

  perform pg_temp.authenticate_as(p_a);
  execute format('select count(*) from public.%I', p_table) into v_count;
  return next cmp_ok(v_count, '>=', 1, p_table || ': A still sees own row');

  perform pg_temp.become_postgres();
end $$;

-- Run ------------------------------------------------------------------------

select pg_temp.check_isolation(
  t.table_name,
  'aaaaaaaa-0000-0000-0000-000000000001',
  'bbbbbbbb-0000-0000-0000-000000000002')
from pg_temp.job_hunter_tables t;

select * from finish();
rollback;

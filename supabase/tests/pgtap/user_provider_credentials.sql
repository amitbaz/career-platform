begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

insert into auth.users
  (id, email, instance_id, aud, role, raw_app_meta_data, raw_user_meta_data, created_at, updated_at)
values
  ('aaaaaaaa-0000-0000-0000-000000000001', 'credentials-a@test.local',
   '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated', '{}', '{}', now(), now()),
  ('bbbbbbbb-0000-0000-0000-000000000002', 'credentials-b@test.local',
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

create function pg_temp.become_anon() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', jsonb_build_object('role', 'anon')::text, true);
  execute 'set local role anon';
end $$;

create function pg_temp.become_postgres() returns void language plpgsql as $$
begin
  execute 'reset role';
  perform set_config('request.jwt.claims', '', true);
end $$;

select has_table('private', 'user_provider_credentials', 'private credential registry exists');
select has_function('public', 'set_user_provider_credential', array['text', 'text'], 'set RPC exists');
select has_function('public', 'delete_user_provider_credential', array['text'], 'delete RPC exists');
select has_function('public', 'list_user_provider_credentials', array[]::text[], 'status RPC exists');
select has_function('public', 'job_hunter_get_provider_credentials', array[]::text[], 'runner retrieval RPC exists');

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001');
select lives_ok(
  $$ select * from public.set_user_provider_credential('gemini', 'gemini-a-first') $$,
  'user A can create a Gemini credential'
);

select pg_temp.become_postgres();
select is(
  (select array_agg(column_name::text order by ordinal_position)
     from information_schema.columns
    where table_schema = 'private'
      and table_name = 'user_provider_credentials'),
  array['user_id', 'provider', 'vault_secret_id', 'created_at', 'updated_at'],
  'registry stores only ownership, provider, Vault UUID, and timestamps'
);
select set_config(
  'test.user_provider_vault_secret_id',
  (select vault_secret_id::text
     from private.user_provider_credentials
    where user_id = 'aaaaaaaa-0000-0000-0000-000000000001'
      and provider = 'gemini'),
  true
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001');
select lives_ok(
  $$ select * from public.set_user_provider_credential('gemini', 'gemini-a-final') $$,
  'user A can replace a Gemini credential'
);

select pg_temp.become_postgres();
select is(
  (select count(*)::int
     from private.user_provider_credentials
    where user_id = 'aaaaaaaa-0000-0000-0000-000000000001'
      and provider = 'gemini'),
  1,
  'replacing a credential keeps one registry row'
);
select is(
  (select count(*)::int
     from vault.secrets
    where id = current_setting('test.user_provider_vault_secret_id')::uuid),
  1,
  'replacing a credential keeps one Vault row'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001');
select results_eq(
  $$ select provider, configured from public.list_user_provider_credentials() order by provider $$,
  $$ values ('brave'::text, false), ('gemini'::text, true) $$,
  'status returns both providers without values'
);
select throws_ok(
  $$ select * from public.set_user_provider_credential('openai', 'not-supported') $$,
  '22023', null, 'unknown providers are rejected'
);
select throws_ok(
  $$ select * from public.set_user_provider_credential('gemini', '   ') $$,
  '22023', null, 'blank values are rejected'
);

select pg_temp.authenticate_as('bbbbbbbb-0000-0000-0000-000000000002');
select results_eq(
  $$ select provider, configured from public.list_user_provider_credentials() order by provider $$,
  $$ values ('brave'::text, false), ('gemini'::text, false) $$,
  'user B cannot observe user A metadata'
);
select lives_ok(
  $$ select * from public.delete_user_provider_credential('gemini') $$,
  'deleting an absent own credential is idempotent'
);
select lives_ok(
  $$ select * from public.set_user_provider_credential('gemini', 'gemini-b-final') $$,
  'user B can create a distinct Gemini credential'
);

select pg_temp.become_anon();
select throws_ok(
  $$ select * from public.list_user_provider_credentials() $$,
  '42501', null, 'anonymous callers cannot list credential status'
);

select pg_temp.become_postgres();
select is(
  (select count(*)::int from private.user_provider_credentials),
  2,
  'user B adds only its own registry state'
);
select is(
  (select count(*)::int
     from information_schema.role_table_grants
    where table_schema = 'private'
      and table_name = 'user_provider_credentials'
      and grantee in ('anon', 'authenticated')),
  0,
  'browser roles have no direct registry grants'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001');
select lives_ok(
  $$ select * from public.delete_user_provider_credential('gemini') $$,
  'user A can delete its Gemini credential'
);

select pg_temp.become_postgres();
select is(
  (select count(*)::int
     from private.user_provider_credentials
    where user_id = 'aaaaaaaa-0000-0000-0000-000000000001'),
  0,
  'deleting the credential removes its registry row'
);
select is(
  (select count(*)::int
     from vault.secrets
    where id = current_setting('test.user_provider_vault_secret_id')::uuid),
  0,
  'deleting the credential removes its Vault row'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001');
select lives_ok(
  $$ select * from public.set_user_provider_credential('gemini', 'gemini-a-final') $$,
  'user A can recreate its Gemini credential'
);

select pg_temp.become_postgres();
select set_config(
  'test.user_a_final_vault_secret_id',
  (select vault_secret_id::text
     from private.user_provider_credentials
    where user_id = 'aaaaaaaa-0000-0000-0000-000000000001'
      and provider = 'gemini'),
  true
);
select set_config(
  'test.user_b_final_vault_secret_id',
  (select vault_secret_id::text
     from private.user_provider_credentials
    where user_id = 'bbbbbbbb-0000-0000-0000-000000000002'
      and provider = 'gemini'),
  true
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001', false);
select throws_ok(
  $$ select * from public.job_hunter_get_provider_credentials() $$,
  '42501', null, 'ordinary user A cannot read decrypted credentials'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001', true);
select results_eq(
  $$ select provider, secret from public.job_hunter_get_provider_credentials() order by provider $$,
  $$ values ('gemini'::text, 'gemini-a-final'::text) $$,
  'runner A reads only A credentials'
);

select pg_temp.authenticate_as('bbbbbbbb-0000-0000-0000-000000000002', true);
select results_eq(
  $$ select provider, secret from public.job_hunter_get_provider_credentials() order by provider $$,
  $$ values ('gemini'::text, 'gemini-b-final'::text) $$,
  'runner B reads only B credentials'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001', true);
select set_config(
  'request.jwt.claims',
  jsonb_build_object(
    'sub', 'aaaaaaaa-0000-0000-0000-000000000001',
    'role', 'authenticated'
  )::text,
  true
);
select throws_ok(
  $$ select * from public.job_hunter_get_provider_credentials() $$,
  '42501', null, 'runner claim is required'
);

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001', false);
select throws_ok(
  $$ select * from public.job_hunter_get_provider_credentials() $$,
  '42501', null, 'JSON false runner claim is rejected'
);

select set_config(
  'request.jwt.claims',
  jsonb_build_object(
    'sub', 'aaaaaaaa-0000-0000-0000-000000000001',
    'role', 'authenticated',
    'job_hunter_runner', 'true'
  )::text,
  true
);
select throws_ok(
  $$ select * from public.job_hunter_get_provider_credentials() $$,
  '42501', null, 'string runner claim is rejected'
);

select pg_temp.become_postgres();
delete from auth.users where id = 'aaaaaaaa-0000-0000-0000-000000000001';
select is(
  (select count(*)::int
     from private.user_provider_credentials
    where user_id = 'aaaaaaaa-0000-0000-0000-000000000001'),
  0,
  'deleting user A removes its registry row'
);
select is(
  (select count(*)::int
     from vault.secrets
    where id = current_setting('test.user_a_final_vault_secret_id')::uuid),
  0,
  'deleting user A removes its Vault row'
);
select is(
  (select count(*)::int
     from private.user_provider_credentials
    where user_id = 'bbbbbbbb-0000-0000-0000-000000000002'),
  1,
  'deleting user A preserves user B registry row'
);
select is(
  (select count(*)::int
     from vault.secrets
    where id = current_setting('test.user_b_final_vault_secret_id')::uuid),
  1,
  'deleting user A preserves user B Vault row'
);

select * from finish();
rollback;

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

-- The pinned population every privilege check below asserts over. A renamed or
-- dropped function makes the existence check fail loudly instead of quietly
-- shrinking the checked set to nothing.
create function pg_temp.credential_functions()
returns table(schema_name text, function_name text)
language sql immutable as $fn$
  values ('private'::text, 'delete_user_provider_vault_secret'::text),
         ('public',        'delete_user_provider_credential'),
         ('public',        'list_user_provider_credentials'),
         ('public',        'set_user_provider_credential');
$fn$;

-- Every role that holds EXECUTE on a function, with PUBLIC (grantee 0) spelled
-- out and the owner's own implicit grant dropped. A null proacl means "never
-- touched", which for a function means PUBLIC still holds EXECUTE, so it falls
-- back to acldefault rather than reading as an empty, vacuously safe set.
create function pg_temp.execute_grantees(p_schema text, p_name text)
returns text[] language sql stable as $fn$
  select coalesce(array_agg(g order by g), array[]::text[])
    from (
      select distinct case when acl.grantee = 0
                           then 'PUBLIC'
                           else pg_get_userbyid(acl.grantee) end as g
        from pg_proc p
        join pg_namespace n on n.oid = p.pronamespace
        cross join lateral aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) as acl
       where n.nspname = p_schema
         and p.proname = p_name
         and acl.privilege_type = 'EXECUTE'
         and acl.grantee <> p.proowner
    ) as grantees;
$fn$;

select has_table('private', 'user_provider_credentials', 'private credential registry exists');
select has_function('public', 'set_user_provider_credential', array['text', 'text'], 'set RPC exists');
select has_function('public', 'delete_user_provider_credential', array['text'], 'delete RPC exists');
select has_function('public', 'list_user_provider_credentials', array[]::text[], 'status RPC exists');
select has_function('public', 'job_hunter_get_provider_credentials', array[]::text[], 'runner retrieval RPC exists');

-- Privilege surface ---------------------------------------------------------
-- All four credential functions are `security definer`, so whoever can execute
-- one runs privileged code that reaches the private registry and the Vault.
-- Three properties must hold for each, and each is asserted over the pinned
-- list above rather than a `like` pattern that could match nothing.

select is(
  (select array_agg(n.nspname || '.' || p.proname order by n.nspname, p.proname)
     from pg_temp.credential_functions() f
     join pg_namespace n on n.nspname = f.schema_name
     join pg_proc p on p.pronamespace = n.oid and p.proname = f.function_name),
  array['private.delete_user_provider_vault_secret',
        'public.delete_user_provider_credential',
        'public.list_user_provider_credentials',
        'public.set_user_provider_credential'],
  'exactly the four expected credential functions exist, so the privilege checks below are not asserting over an empty set');

-- `set search_path = ''` is stored in proconfig as the literal search_path="".
-- Without it a caller could point search_path at a schema of their own and
-- swap an object out from under the definer.
select is(
  (select array_agg(f.schema_name || '.' || f.function_name order by f.schema_name, f.function_name)
     from pg_temp.credential_functions() f
     join pg_namespace n on n.nspname = f.schema_name
     join pg_proc p on p.pronamespace = n.oid and p.proname = f.function_name
    where not ('search_path=""' = any(coalesce(p.proconfig, array[]::text[])))),
  null,
  'every credential function pins search_path to the empty string');

select is(
  (select array_agg(f.schema_name || '.' || f.function_name order by f.schema_name, f.function_name)
     from pg_temp.credential_functions() f
    where 'PUBLIC' = any(pg_temp.execute_grantees(f.schema_name, f.function_name))),
  null,
  'no credential function is executable by PUBLIC');

select is(
  (select array_agg(f.schema_name || '.' || f.function_name order by f.schema_name, f.function_name)
     from pg_temp.credential_functions() f
    where 'anon' = any(pg_temp.execute_grantees(f.schema_name, f.function_name))),
  null,
  'no credential function is executable by anon');

-- The exact grantee set, owner excluded. `service_role` is here because
-- Supabase's default privileges on schema public grant EXECUTE on every new
-- function to it and the migration only revokes public and anon; it is the
-- secret backend key, never a browser role. Pinning it means a future change
-- to that default -- in either direction -- fails this test instead of
-- silently widening who can reach the Vault.
select is(
  pg_temp.execute_grantees('public', 'set_user_provider_credential'),
  array['authenticated', 'service_role'],
  'only authenticated and service_role may execute public.set_user_provider_credential');
select is(
  pg_temp.execute_grantees('public', 'delete_user_provider_credential'),
  array['authenticated', 'service_role'],
  'only authenticated and service_role may execute public.delete_user_provider_credential');
select is(
  pg_temp.execute_grantees('public', 'list_user_provider_credentials'),
  array['authenticated', 'service_role'],
  'only authenticated and service_role may execute public.list_user_provider_credentials');

-- The Vault-deleting trigger function is reachable only through the trigger it
-- backs: the migration grants it to nobody, so no role but its owner holds
-- EXECUTE and no client can call it directly to destroy another user's secret.
select is(
  pg_temp.execute_grantees('private', 'delete_user_provider_vault_secret'),
  array[]::text[],
  'no role but the owner may execute private.delete_user_provider_vault_secret');

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
select throws_ok(
  $$ select * from public.set_user_provider_credential('gemini', 'anon-should-never-store-this') $$,
  '42501', null, 'anonymous callers cannot store a credential'
);
select throws_ok(
  $$ select * from public.delete_user_provider_credential('gemini') $$,
  '42501', null, 'anonymous callers cannot delete a credential'
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

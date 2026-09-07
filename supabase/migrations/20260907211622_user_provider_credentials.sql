create extension if not exists supabase_vault with schema vault;

create schema if not exists private;
revoke all on schema private from public, anon, authenticated;

create table private.user_provider_credentials (
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null check (provider in ('gemini', 'brave')),
  vault_secret_id uuid not null unique references vault.secrets(id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, provider)
);

revoke all on private.user_provider_credentials from public, anon, authenticated;

create function private.delete_user_provider_vault_secret()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  delete from vault.secrets where id = old.vault_secret_id;
  return old;
end;
$$;

revoke execute on function private.delete_user_provider_vault_secret() from public, anon, authenticated;

create trigger delete_user_provider_vault_secret
after delete on private.user_provider_credentials
for each row execute function private.delete_user_provider_vault_secret();

create function public.set_user_provider_credential(p_provider text, p_secret text)
returns table(provider text, configured boolean, updated_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user_id uuid := (select auth.uid());
  v_provider text := lower(btrim(p_provider));
  v_vault_secret_id uuid;
  v_updated_at timestamptz;
begin
  if v_user_id is null then
    raise exception using errcode = '42501', message = 'authentication required';
  end if;

  if v_provider is null or v_provider not in ('gemini', 'brave') then
    raise exception using errcode = '22023', message = 'unsupported provider';
  end if;

  if p_secret is null
     or btrim(p_secret) = ''
     or octet_length(p_secret) > 16384 then
    raise exception using errcode = '22023', message = 'invalid secret';
  end if;

  select credentials.vault_secret_id
    into v_vault_secret_id
    from private.user_provider_credentials as credentials
   where credentials.user_id = v_user_id
     and credentials.provider = v_provider;

  if v_vault_secret_id is null then
    v_vault_secret_id := vault.create_secret(p_secret);

    insert into private.user_provider_credentials (user_id, provider, vault_secret_id)
    values (v_user_id, v_provider, v_vault_secret_id)
    returning user_provider_credentials.updated_at into v_updated_at;
  else
    perform vault.update_secret(v_vault_secret_id, p_secret);

    update private.user_provider_credentials as credentials
       set updated_at = now()
     where credentials.user_id = v_user_id
       and credentials.provider = v_provider
    returning credentials.updated_at into v_updated_at;
  end if;

  return query select v_provider, true, v_updated_at;
end;
$$;

create function public.delete_user_provider_credential(p_provider text)
returns table(provider text, configured boolean, updated_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user_id uuid := (select auth.uid());
  v_provider text := lower(btrim(p_provider));
begin
  if v_user_id is null then
    raise exception using errcode = '42501', message = 'authentication required';
  end if;

  if v_provider is null or v_provider not in ('gemini', 'brave') then
    raise exception using errcode = '22023', message = 'unsupported provider';
  end if;

  delete from private.user_provider_credentials as credentials
   where credentials.user_id = v_user_id
     and credentials.provider = v_provider;

  return query select v_provider, false, null::timestamptz;
end;
$$;

create function public.list_user_provider_credentials()
returns table(provider text, configured boolean, updated_at timestamptz)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user_id uuid := (select auth.uid());
begin
  if v_user_id is null then
    raise exception using errcode = '42501', message = 'authentication required';
  end if;

  return query
  select providers.provider,
         credentials.vault_secret_id is not null,
         credentials.updated_at
    from (values ('gemini'::text), ('brave'::text)) as providers(provider)
    left join private.user_provider_credentials as credentials
      on credentials.user_id = v_user_id
     and credentials.provider = providers.provider;
end;
$$;

revoke execute on function public.set_user_provider_credential(text, text) from public, anon;
revoke execute on function public.delete_user_provider_credential(text) from public, anon;
revoke execute on function public.list_user_provider_credentials() from public, anon;
grant execute on function public.set_user_provider_credential(text, text) to authenticated;
grant execute on function public.delete_user_provider_credential(text) to authenticated;
grant execute on function public.list_user_provider_credentials() to authenticated;

create function public.job_hunter_get_provider_credentials()
returns table(provider text, secret text)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_user_id uuid := (select auth.uid());
begin
  if v_user_id is null
     or coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) <> 'true'::jsonb then
    raise exception using errcode = '42501', message = 'trusted Job Hunter runner required';
  end if;

  return query
  select credentials.provider, decrypted.decrypted_secret
    from private.user_provider_credentials as credentials
    join vault.decrypted_secrets as decrypted
      on decrypted.id = credentials.vault_secret_id
   where credentials.user_id = v_user_id
   order by credentials.provider;
end;
$$;

revoke execute on function public.job_hunter_get_provider_credentials() from public, anon;
grant execute on function public.job_hunter_get_provider_credentials() to authenticated;

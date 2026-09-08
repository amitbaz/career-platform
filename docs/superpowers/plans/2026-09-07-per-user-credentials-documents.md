# Per-user Credentials and Documents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Supabase the per-user source of truth for Job Hunter documents and Gemini/Brave credentials while preventing browser read-back and removing the four user-owned GitHub Actions secrets.

**Architecture:** Keep CV and cover-letter text in the existing RLS-protected `public.source_documents` table. Store provider values in Supabase Vault behind a private registry and four narrowly granted RPCs; ordinary sessions get management/status operations, while only a short-lived same-user JWT carrying `job_hunter_runner: true` can retrieve decrypted values. Relay supplies a deliberately small Profile UI, and all three Job Hunter commands load the database-backed values through one store/config boundary.

**Tech Stack:** PostgreSQL 17, Supabase Vault and PostgREST RPC, pgTAP, Next.js/TypeScript/React, Supabase JS, Vitest/Testing Library, Python 3.12, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-07-per-user-credentials-documents-design.md`

## Global Constraints

- The only credential providers in this change are `gemini` and `brave`.
- Gemini is required; Brave is optional and preserves the DuckDuckGo fallback.
- Gmail OAuth, Telegram, GitHub dispatch, Supabase bootstrap/signing values, AI provider abstraction, scheduling, and full onboarding stay out of scope.
- Relay's existing process-global `GEMINI_API_KEY` remains unchanged; this issue migrates Job Hunter's repository secret, not Relay's deployment key.
- Documents remain owner-readable through `public.source_documents`; only provider keys are write-only from an ordinary browser session.
- No response, log, exception, `repr`, Vault name, or Vault description may contain provider values, document text, access tokens, or key fragments.
- Ownership always comes from `auth.uid()`; no browser request accepts or forwards a user ID.
- All privileged SQL functions pin `search_path = ''`, schema-qualify every object, revoke default `PUBLIC`/`anon` execution, and explicitly grant only `authenticated`.
- Existing Job Hunter store functions stay security-invoker functions; `public.job_hunter_get_provider_credentials()` is the single named security-definer exception and must verify the runner claim.
- There is no legacy fallback to `GEMINI_API_KEY`, `BRAVE_SEARCH_API_KEY`, `CANDIDATE_PROFILE_B64`, or `COVER_LETTER_TEMPLATE_B64` in Job Hunter.
- Use pnpm only for the JavaScript workspace. Follow red → green → refactor and commit after each independently reviewable task.
- Do not delete hosted GitHub secrets until the replacement path has passed local tests, hosted migration verification, and a live same-user retrieval smoke test.

---

### Task 1: Vault registry and browser-safe credential operations

**Files:**
- Create: `supabase/migrations/<CLI-generated timestamp>_user_provider_credentials.sql` by running the required migration command below
- Create: `supabase/tests/pgtap/user_provider_credentials.sql`

**Interfaces:**
- Consumes: Supabase Auth's `auth.uid()`, `vault.create_secret(text, text, text)`, `vault.update_secret(uuid, text, text, text)`, and `vault.secrets`.
- Produces: `private.user_provider_credentials`; `public.set_user_provider_credential(text, text)`; `public.delete_user_provider_credential(text)`; `public.list_user_provider_credentials()`.

- [ ] **Step 1: Prepare and verify the local database baseline**

Run:

```bash
pnpm db:key
supabase start
pnpm db:test
```

Expected: the existing pgTAP suite passes against the running local stack. If `supabase start` reports an existing healthy stack, continue without restarting it.

- [ ] **Step 2: Create the migration through the Supabase CLI**

Run:

```bash
supabase migration new user_provider_credentials
```

Expected: the CLI prints one new path ending in `_user_provider_credentials.sql`. Use that exact generated path for every migration edit in Tasks 1 and 2; do not hand-invent or rename its timestamp.

- [ ] **Step 3: Write the failing management-boundary pgTAP test**

Create `supabase/tests/pgtap/user_provider_credentials.sql` with fixed users A and B, temporary helpers for ordinary authenticated, runner-authenticated, anonymous, and postgres roles, and these exact assertions:

```sql
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

select pg_temp.authenticate_as('aaaaaaaa-0000-0000-0000-000000000001');
select lives_ok(
  $$ select * from public.set_user_provider_credential('gemini', 'gemini-a-first') $$,
  'user A can create a Gemini credential'
);
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

select pg_temp.become_anon();
select throws_ok(
  $$ select * from public.list_user_provider_credentials() $$,
  '42501', null, 'anonymous callers cannot list credential status'
);

select pg_temp.become_postgres();
select is(
  (select count(*)::int from private.user_provider_credentials),
  1,
  'user B did not alter user A registry state'
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

select * from finish();
rollback;
```

Add assertions after the first save that the registry row contains only `user_id`, provider, Vault UUID, and timestamps; after replacing `gemini`, assert one registry row and one Vault row remain; after deleting it, assert both counts are zero. Use only fake test values such as `gemini-a-first` and never real credentials.

- [ ] **Step 4: Run the test and verify it fails for missing schema/functions**

Run:

```bash
pnpm db:test
```

Expected: FAIL because `private.user_provider_credentials` and the three management RPCs do not exist.

- [ ] **Step 5: Implement the private registry, lifecycle trigger, and management RPCs**

In the CLI-generated migration, implement this structure. Keep every object schema-qualified and use the exact signatures consumed by later tasks:

```sql
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
```

Implement the public functions with these contracts:

```sql
create function public.set_user_provider_credential(p_provider text, p_secret text)
returns table(provider text, configured boolean, updated_at timestamptz)
language plpgsql security definer set search_path = '';

create function public.delete_user_provider_credential(p_provider text)
returns table(provider text, configured boolean, updated_at timestamptz)
language plpgsql security definer set search_path = '';

create function public.list_user_provider_credentials()
returns table(provider text, configured boolean, updated_at timestamptz)
language plpgsql security definer set search_path = '';
```

For all three functions:

- derive `v_user_id` from `(select auth.uid())` and raise SQLSTATE `42501` when null;
- normalize with `lower(btrim(p_provider))` and allow only `gemini`/`brave`;
- reject null/blank secrets and `octet_length(p_secret) > 16384` with SQLSTATE `22023`;
- call `vault.create_secret(p_secret)` for first insert and `vault.update_secret(vault_id, p_secret)` for replacement;
- never set Vault `name` or `description`;
- have delete remove the registry row so the trigger removes Vault state;
- have list produce exactly two rows from `values ('gemini'), ('brave')`, left joined to the calling user's registry rows;
- return metadata only from management functions.

End the migration with explicit privileges:

```sql
revoke execute on function public.set_user_provider_credential(text, text) from public, anon;
revoke execute on function public.delete_user_provider_credential(text) from public, anon;
revoke execute on function public.list_user_provider_credentials() from public, anon;
grant execute on function public.set_user_provider_credential(text, text) to authenticated;
grant execute on function public.delete_user_provider_credential(text) to authenticated;
grant execute on function public.list_user_provider_credentials() to authenticated;
```

- [ ] **Step 6: Reset the local schema and make the management tests pass**

Run:

```bash
supabase db reset
pnpm db:test
```

Expected: PASS, including create, replace, status-only reads, idempotent delete, no browser grants, and Vault cleanup.

- [ ] **Step 7: Commit the browser-safe credential foundation**

```bash
git add supabase/migrations/*_user_provider_credentials.sql supabase/tests/pgtap/user_provider_credentials.sql
git commit -m "feat: add write-only provider credential storage"
```

---

### Task 2: Runner-only credential retrieval

**Files:**
- Modify: `supabase/migrations/<same CLI-generated timestamp>_user_provider_credentials.sql`
- Modify: `supabase/tests/pgtap/user_provider_credentials.sql`
- Modify: `supabase/tests/pgtap/job_hunter_store_functions.sql:58-109`

**Interfaces:**
- Consumes: the Task 1 private registry and a JWT with `sub`, `role: authenticated`, and `job_hunter_runner: true`.
- Produces: `public.job_hunter_get_provider_credentials() -> table(provider text, secret text)`.

- [ ] **Step 1: Add failing runner-claim and cross-user tests**

Extend `user_provider_credentials.sql` so both users own different fake Gemini values, then assert:

```sql
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
```

Also set `request.jwt.claims` without `job_hunter_runner`, with JSON `false`, and with a string value; each must raise `42501`. Delete user A as postgres and assert both A's registry row and its Vault row are gone while B's remain.

Update `job_hunter_store_functions.sql` so it still proves every existing store/normalizer function is security invoker, but explicitly asserts that `job_hunter_get_provider_credentials` is the only `public.job_hunter_*` security-definer function. Add the new name to the exact function inventory.

- [ ] **Step 2: Run the database suite and verify retrieval tests fail**

Run:

```bash
pnpm db:test
```

Expected: FAIL because `public.job_hunter_get_provider_credentials()` is absent.

- [ ] **Step 3: Implement the single runner-only retrieval function**

Append this exact interface to the same migration:

```sql
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
```

Do not accept a user parameter. Do not catch the authorization exception. Do not expose Vault IDs.

- [ ] **Step 4: Reapply and verify the full database security contract**

Run:

```bash
supabase db reset
pnpm db:test
```

Expected: PASS for ordinary-user denial, runner A/B isolation, malformed-claim denial, registry/Vault lifecycle, and the one-function security-definer allowlist.

- [ ] **Step 5: Commit runner-only retrieval**

```bash
git add supabase/migrations/*_user_provider_credentials.sql supabase/tests/pgtap/user_provider_credentials.sql supabase/tests/pgtap/job_hunter_store_functions.sql
git commit -m "feat: restrict provider key reads to trusted runs"
```

---

### Task 3: Relay credential repository and authenticated API

**Files:**
- Modify: `apps/relay/src/lib/types.ts`
- Create: `apps/relay/src/lib/repositories/provider-credentials.ts`
- Create: `apps/relay/src/lib/repositories/provider-credentials.test.ts`
- Create: `apps/relay/src/app/api/profile/credentials/route.ts`
- Create: `apps/relay/src/app/api/profile/credentials/route.test.ts`

**Interfaces:**
- Consumes: Task 1 management RPCs through the cookie-authenticated Supabase server client.
- Produces: `ProviderCredential = "gemini" | "brave"`; `ProviderCredentialStatus`; repository functions `listProviderCredentials`, `setProviderCredential`, `deleteProviderCredential`; route methods `GET`, `PUT`, `DELETE`.

- [ ] **Step 1: Define the shared status types**

Add to `apps/relay/src/lib/types.ts`:

```ts
export type ProviderCredential = "gemini" | "brave";

export type ProviderCredentialStatus = {
  provider: ProviderCredential;
  configured: boolean;
  updatedAt: string | null;
};
```

- [ ] **Step 2: Write failing repository tests**

Create `provider-credentials.test.ts` with a fake `supabase.rpc` and these test names/assertions:

```ts
it("maps the two status rows without exposing an unknown field", async () => {
  // RPC returns both rows: unconfigured Brave and configured Gemini with an updated_at value.
  // Expect camel-cased ProviderCredentialStatus objects and no `secret` key.
});

it("passes only provider and secret to the set RPC", async () => {
  // Expect set_user_provider_credential with { p_provider: "gemini", p_secret: "submitted" }.
});

it("passes only provider to the delete RPC", async () => {
  // Expect delete_user_provider_credential with { p_provider: "brave" }.
});

it("wraps database failures without preserving the submitted value", async () => {
  // Reject with RepositoryError("Could not save that provider credential.", code).
  // Assert the error message does not contain "submitted".
});
```

Run:

```bash
pnpm --filter relay test -- src/lib/repositories/provider-credentials.test.ts
```

Expected: FAIL because the repository does not exist.

- [ ] **Step 3: Implement the server-only repository**

Create `provider-credentials.ts` with these exact exports:

```ts
import "server-only";
import type { SupabaseClient } from "@supabase/supabase-js";
import type { ProviderCredential, ProviderCredentialStatus } from "@/lib/types";
import { RepositoryError } from "@/lib/repositories/profile";

type StatusRow = { provider: unknown; configured: unknown; updated_at: unknown };

export async function listProviderCredentials(
  supabase: SupabaseClient,
): Promise<ProviderCredentialStatus[]>;

export async function setProviderCredential(
  supabase: SupabaseClient,
  provider: ProviderCredential,
  secret: string,
): Promise<ProviderCredentialStatus>;

export async function deleteProviderCredential(
  supabase: SupabaseClient,
  provider: ProviderCredential,
): Promise<ProviderCredentialStatus>;
```

Map only `provider`, `configured`, and `updated_at`; reject unexpected provider/status shapes with a `RepositoryError`. Never spread an RPC row into an API-facing object.

- [ ] **Step 4: Make repository tests pass**

Run:

```bash
pnpm --filter relay test -- src/lib/repositories/provider-credentials.test.ts
```

Expected: PASS.

- [ ] **Step 5: Write failing route tests for auth, validation, and non-disclosure**

Create `route.test.ts` beside the credentials route. Mock `requireUser` and all three repository operations, then cover:

```ts
it("GET requires authentication", async () => { /* expect 401 */ });
it("GET returns status metadata only", async () => { /* exact status array */ });
it("PUT rejects unknown providers", async () => { /* expect 400 and no repository call */ });
it("PUT rejects blank and values over 16384 bytes", async () => { /* expect 400 */ });
it("PUT returns saved status without echoing the submitted value", async () => { /* JSON must not contain it */ });
it("DELETE rejects unknown providers", async () => { /* expect 400 */ });
it("DELETE is status-only", async () => { /* configured false, updatedAt null */ });
it("sanitizes repository errors", async () => { /* expect generic 500, no submitted value */ });
```

Run:

```bash
pnpm --filter relay test -- src/app/api/profile/credentials/route.test.ts
```

Expected: FAIL because the route does not exist.

- [ ] **Step 6: Implement the authenticated route**

Create `route.ts` with `runtime = "nodejs"`, a provider guard, byte-length validation using `Buffer.byteLength(secret, "utf8")`, and the existing `requireUser()` boundary:

```ts
const providers = new Set<ProviderCredential>(["gemini", "brave"]);
const MAX_PROVIDER_SECRET_BYTES = 16_384;

export async function GET() {
  const { supabase } = await requireUser();
  return NextResponse.json({ credentials: await listProviderCredentials(supabase) });
}

export async function PUT(request: Request) {
  const { provider, secret } = await request.json() as Record<string, unknown>;
  // Validate provider, non-blank string, and byte limit before the repository call.
  const credential = await setProviderCredential(supabase, provider, secret);
  return NextResponse.json({ credential });
}

export async function DELETE(request: Request) {
  const { provider } = await request.json() as Record<string, unknown>;
  const credential = await deleteProviderCredential(supabase, provider);
  return NextResponse.json({ credential });
}
```

Wrap every method so `UNAUTHENTICATED` maps to `401`, validation maps to `400`, and all other failures return `{ error: "Could not update provider credentials." }` with `500`. Do not log request bodies or caught error objects in this route.

- [ ] **Step 7: Run route, repository, and existing profile tests**

Run:

```bash
pnpm --filter relay test -- src/lib/repositories/provider-credentials.test.ts src/app/api/profile/credentials/route.test.ts src/app/api/profile/route.test.ts
```

Expected: PASS.

- [ ] **Step 8: Commit the Relay server boundary**

```bash
git add apps/relay/src/lib/types.ts apps/relay/src/lib/repositories/provider-credentials.ts apps/relay/src/lib/repositories/provider-credentials.test.ts apps/relay/src/app/api/profile/credentials/route.ts apps/relay/src/app/api/profile/credentials/route.test.ts
git commit -m "feat: add provider credential management API"
```

---

### Task 4: Minimal Profile credential controls

**Files:**
- Modify: `apps/relay/src/app/api-client.ts`
- Create: `apps/relay/src/app/profile-credentials.tsx`
- Create: `apps/relay/src/app/profile-credentials.test.tsx`
- Modify: `apps/relay/src/app/relay-shell.tsx:1-61,836`
- Modify: `apps/relay/src/app/page.test.tsx`

**Interfaces:**
- Consumes: Task 3 route methods.
- Produces: `fetchProviderCredentials()`, `saveProviderCredential()`, `removeProviderCredential()`, and `<ProfileCredentials />`.

- [ ] **Step 1: Add failing typed-client tests through the component contract**

Create `profile-credentials.test.tsx`, mock the three `api-client` functions, and assert:

```tsx
it("shows configured state without rendering a saved value", async () => {});
it("starts both password fields blank", async () => {});
it("saves Gemini, clears the input, and updates status", async () => {});
it("replaces a configured Brave key without pre-filling it", async () => {});
it("deletes a configured key only after an explicit click", async () => {});
it("keeps provider errors local and never renders the submitted value", async () => {});
```

Use fake values such as `submitted-gemini-value`, and assert each is absent from `document.body.textContent` after the request settles.

Run:

```bash
pnpm --filter relay test -- src/app/profile-credentials.test.tsx
```

Expected: FAIL because the component and client wrappers do not exist.

- [ ] **Step 2: Add the typed browser API wrappers**

Append to `api-client.ts`:

```ts
const PROVIDER_CREDENTIALS_URL = "/api/profile/credentials";

export async function fetchProviderCredentials(): Promise<ProviderCredentialStatus[]> {
  return (await api<{ credentials: ProviderCredentialStatus[] }>(PROVIDER_CREDENTIALS_URL)).credentials;
}

export async function saveProviderCredential(
  provider: ProviderCredential,
  secret: string,
): Promise<ProviderCredentialStatus> {
  return (await api<{ credential: ProviderCredentialStatus }>(PROVIDER_CREDENTIALS_URL, {
    method: "PUT",
    body: JSON.stringify({ provider, secret }),
  })).credential;
}

export async function removeProviderCredential(
  provider: ProviderCredential,
): Promise<ProviderCredentialStatus> {
  return (await api<{ credential: ProviderCredentialStatus }>(PROVIDER_CREDENTIALS_URL, {
    method: "DELETE",
    body: JSON.stringify({ provider }),
  })).credential;
}
```

Import `ProviderCredential` and `ProviderCredentialStatus` from `@/lib/types`.

- [ ] **Step 3: Implement the deliberately small component**

Create `profile-credentials.tsx` as a self-contained client component. It loads status only when mounted, owns one blank string per provider, and uses password inputs:

```tsx
"use client";

const providerCopy = {
  gemini: { label: "Gemini", help: "Required for Job Hunter runs." },
  brave: { label: "Brave Search", help: "Optional. DuckDuckGo remains available without it." },
} satisfies Record<ProviderCredential, { label: string; help: string }>;

export function ProfileCredentials() {
  // Load status with fetchProviderCredentials().
  // Keep inputs in { gemini: "", brave: "" } and clear in finally after every save attempt.
  // Render only configured/not configured/updated-at metadata.
}
```

For each provider render: label, help text, metadata status, `<input type="password" autoComplete="new-password">`, Save/Replace, and Delete only when configured. Use existing Profile card/button classes; do not add navigation, animations, provider tutorials, model controls, or a design-system refactor.

- [ ] **Step 4: Make the component tests pass**

Run:

```bash
pnpm --filter relay test -- src/app/profile-credentials.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Mount the component in Profile and update shell test routing**

Import `ProfileCredentials` into `relay-shell.tsx` and render it immediately after the existing professional-profile article in the `view === "profile"` branch:

```tsx
<ProfileCredentials />
```

In `page.test.tsx`, add a default `/api/profile/credentials` handler returning both providers as not configured so existing navigation tests do not gain unrelated fetch failures:

```ts
"/api/profile/credentials": () => ({
  body: {
    credentials: [
      { provider: "gemini", configured: false, updatedAt: null },
      { provider: "brave", configured: false, updatedAt: null },
    ],
  },
}),
```

Add one shell-level assertion that opening Profile shows the `Provider credentials` heading; keep interaction behavior in the focused component test.

- [ ] **Step 6: Run focused and full Relay checks**

Run:

```bash
pnpm --filter relay test -- src/app/profile-credentials.test.tsx src/app/page.test.tsx
pnpm relay:lint
```

Expected: tests and lint PASS.

- [ ] **Step 7: Commit the minimal management UI**

```bash
git add apps/relay/src/app/api-client.ts apps/relay/src/app/profile-credentials.tsx apps/relay/src/app/profile-credentials.test.tsx apps/relay/src/app/relay-shell.tsx apps/relay/src/app/page.test.tsx
git commit -m "feat: add minimal credential controls to Profile"
```

---

### Task 5: Trusted token claim and Job Hunter persistence readers

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/supabase_auth.py:25-71`
- Modify: `apps/job-hunter/src/job_hunter/supabase_client.py:167-186`
- Modify: `apps/job-hunter/src/job_hunter/models.py:345-358`
- Modify: `apps/job-hunter/src/job_hunter/postgres_store.py:29-48,1642-1688`
- Modify: `apps/job-hunter/tests/test_supabase_auth.py:27-36`
- Create: `apps/job-hunter/tests/test_postgres_store_runtime_config.py`

**Interfaces:**
- Consumes: Task 2 retrieval RPC and existing `source_documents` RLS.
- Produces: runner-claimed `AccessTokenMinter`; `ProviderCredentials`; `PostgresJobStore.get_provider_credentials()`; `PostgresJobStore.get_source_documents()`.

- [ ] **Step 1: Write the failing runner-claim token assertion**

Update `test_token_carries_the_expected_claims`:

```python
assert claims["sub"] == USER_A
assert claims["role"] == "authenticated"
assert claims["job_hunter_runner"] is True
assert set(claims) == {"sub", "role", "job_hunter_runner", "exp"}
```

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_supabase_auth.py -q
```

Expected: FAIL because the claim is absent.

- [ ] **Step 2: Add the claim without changing token lifetime or caching**

Modify only the JWT payload in `AccessTokenMinter._mint()`:

```python
{
    "sub": self._user_id,
    "role": "authenticated",
    "job_hunter_runner": True,
    "exp": expires_at,
}
```

Update the class docstring to state that this minter is for trusted Job Hunter processes, not browser sessions. Run `tests/test_supabase_auth.py` and expect all tests to pass.

- [ ] **Step 3: Define a non-revealing provider-credentials model**

Add beside `Settings` in `models.py`:

```python
@dataclass(slots=True, frozen=True)
class ProviderCredentials:
    gemini_api_key: str | None = field(default=None, repr=False)
    brave_search_api_key: str | None = field(default=None, repr=False)
```

Change the existing `Settings` secret/document fields to `field(repr=False)` and add optional Brave state after `gemini_quota` so existing keyword-based fixtures remain valid:

```python
@dataclass(slots=True)
class Settings:
    gemini_api_key: str = field(repr=False)
    candidate_profile: str = field(repr=False)
    cover_letter_template: str = field(repr=False)
    timezone: str
    scheduled_hour: int
    policy: SearchPolicy
    gemini_quota: GeminiQuotaSettings
    brave_search_api_key: str | None = field(default=None, repr=False)
    dry_run: bool = False
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-3.6-flash"
    output_dir: str = "var"
```

- [ ] **Step 4: Write failing store-reader tests**

Create `test_postgres_store_runtime_config.py` with a purpose-built fake client and these cases:

```python
def test_get_provider_credentials_maps_runner_rpc_rows():
    # gemini and brave rows -> ProviderCredentials with both values.

def test_get_provider_credentials_allows_missing_optional_brave():
    # gemini-only row -> brave_search_api_key is None.

def test_get_provider_credentials_rejects_unknown_or_duplicate_provider_rows():
    # malformed privileged response must not be silently accepted.

def test_get_source_documents_returns_latest_cv_and_cover_letter():
    # Fake rows are newest-first with an older duplicate; first per kind wins.

def test_get_source_documents_ignores_null_content_but_not_other_users_in_code():
    # The select has no caller-supplied user id; RLS is the boundary.
```

Assert the document call is exactly:

```python
client.select(
    "source_documents",
    params={"select": "kind,content,updated_at", "order": "updated_at.desc"},
)
```

and the credential call is exactly `client.rpc("job_hunter_get_provider_credentials")`.

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_postgres_store_runtime_config.py -q
```

Expected: FAIL because the model and methods do not exist.

- [ ] **Step 5: Implement the two store readers**

Add to the search-profile/configuration section of `PostgresJobStore`:

```python
def get_provider_credentials(self) -> ProviderCredentials:
    rows = self._client.rpc("job_hunter_get_provider_credentials")
    # Validate provider/secret strings, reject duplicates and unknown names,
    # then return ProviderCredentials without logging rows.

def get_source_documents(self) -> dict[str, str]:
    rows = self._client.select(
        "source_documents",
        params={"select": "kind,content,updated_at", "order": "updated_at.desc"},
    )
    # Keep the first non-null content for each allowed kind.
```

Never add a `user_id` parameter or application-side ownership filter. Update `SupabaseClient.rpc` documentation: most RPCs are security-invoker, while `job_hunter_get_provider_credentials` is the audited runner-claim security-definer exception.

- [ ] **Step 6: Run trusted-boundary unit tests**

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_supabase_auth.py tests/test_postgres_store_runtime_config.py tests/test_supabase_client.py -q
```

Expected: PASS and no credential values in captured logs or model representations.

- [ ] **Step 7: Commit the trusted runtime bridge**

```bash
git add apps/job-hunter/src/job_hunter/supabase_auth.py apps/job-hunter/src/job_hunter/supabase_client.py apps/job-hunter/src/job_hunter/models.py apps/job-hunter/src/job_hunter/postgres_store.py apps/job-hunter/tests/test_supabase_auth.py apps/job-hunter/tests/test_postgres_store_runtime_config.py
git commit -m "feat: load user runtime material through trusted Supabase access"
```

---

### Task 6: Database-backed Job Hunter configuration and all consumers

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/config.py:3-28,62-167`
- Modify: `apps/job-hunter/src/job_hunter/cli.py:8,33-37,83-173`
- Modify: `apps/job-hunter/src/job_hunter/sources/__init__.py:91-110,138-175`
- Modify: `apps/job-hunter/src/job_hunter/pipeline.py:79-99,644-677`
- Modify: `apps/job-hunter/tests/test_config.py`
- Modify: `apps/job-hunter/tests/test_cli.py`
- Modify: `apps/job-hunter/tests/test_sources.py:409-484`
- Modify: `apps/job-hunter/tests/test_pipeline.py:2198-2230`
- Modify: `apps/job-hunter/tests/test_brave_budget.py:218-240`
- Modify as mechanically required by the `Settings` constructor: tests under `apps/job-hunter/tests/` that construct `Settings` positionally rather than by keyword

**Interfaces:**
- Consumes: Task 5 store readers and `ProviderCredentials`.
- Produces: `RuntimeConfigurationError`; `load_provider_credentials(store)`; database-backed `load_settings(store)` and `load_gmail_settings(store)`; `Settings.brave_search_api_key`; Brave consumers with no key environment reads.

- [ ] **Step 1: Replace environment fixtures with fake store material in failing config tests**

Add a fake store that supplies search profile rows, documents, and `ProviderCredentials`. Replace `_set_required_bot_env` with a helper that sets only Telegram/quota/runtime variables. Add the exact cases `test_load_settings_reads_documents_and_provider_keys_from_store`, `test_load_settings_does_not_read_four_legacy_environment_variables`, `test_load_settings_names_missing_gemini_cv_and_cover_letter_without_values`, `test_load_settings_accepts_missing_brave_key`, `test_load_gmail_settings_reads_gemini_from_store_without_loading_documents`, and `test_load_gmail_settings_does_not_require_candidate_documents`.

For the no-legacy test, set all four old variables to sentinel values and assert none appear in returned settings. For the missing-data test, assert the message is exactly:

```text
Missing per-user Job Hunter configuration: gemini, cv, cover_letter
```

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_config.py -q
```

Expected: FAIL because the loaders still read the environment and `load_gmail_settings` has no store argument.

- [ ] **Step 2: Implement focused provider/document loading**

In `config.py`, add:

```python
class RuntimeConfigurationError(RuntimeError):
    """Raised when required per-user runtime material is absent."""


def load_provider_credentials(store: "PostgresJobStore") -> ProviderCredentials:
    return store.get_provider_credentials()


def _load_required_documents(store: "PostgresJobStore") -> tuple[str, str]:
    documents = store.get_source_documents()
    # Build missing names in deterministic order: cv, cover_letter.
```

Have `load_settings(store)` call both helpers, aggregate the required-value checks, and populate the existing `Settings` fields plus `brave_search_api_key`. It must not call `_require_env` for any of the four legacy names. Have `load_gmail_settings(store)` call only `load_provider_credentials(store)` plus the existing Gmail/quota/model environment loaders, and raise the same sanitized error naming only `gemini` when that required credential is absent.

When `load_settings` has multiple missing values, collect them in deterministic order `gemini`, `cv`, `cover_letter` and raise one sanitized `RuntimeConfigurationError` rather than failing on the first database row.

- [ ] **Step 3: Make config tests pass**

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_config.py tests/test_supabase_settings.py tests/test_webhook_config.py -q
```

Expected: PASS. The webhook test continues proving it does not load Job Hunter runtime material.

- [ ] **Step 4: Write failing CLI tests for all three entry points**

Update CLI mocks to accept `load_gmail_settings(store)`. Add the exact cases `test_run_loads_settings_from_the_constructed_store`, `test_generate_cover_letter_loads_settings_from_the_constructed_store`, `test_sync_gmail_builds_the_store_before_loading_gmail_settings`, and `test_sync_gmail_uses_provider_credentials_without_loading_candidate_documents`.

Keep the existing dry-run guarantee: only tracker/store writes are wrapped; credential reads come from the real user-scoped store before the wrapper is created.

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_cli.py -q
```

Expected: FAIL where Gmail settings are still loaded before the store and without an argument.

- [ ] **Step 5: Reorder Gmail bootstrap without changing its write semantics**

Change `_sync_gmail` to this order:

```python
def _sync_gmail(args: argparse.Namespace) -> int:
    http = HttpClient()
    real_store = PostgresJobStore(_build_client(http))
    settings = load_gmail_settings(real_store)
    # Existing DryRunStore wrapping, tracker construction, and sync call follow unchanged.
```

The `_run` and `_generate_cover_letter` paths already construct their store before `load_settings(store)`; keep that shared configuration path.

- [ ] **Step 6: Replace direct Brave environment reads with `Settings`**

Use `settings.brave_search_api_key` in `build_brave_budget` and `build_sources`. Change canonical resolution to receive the already-loaded key:

```python
def _targeted_canonical_candidates(
    http: HttpClient,
    job: Job,
    breaker: CircuitBreaker,
    brave_api_key: str | None,
    brave_budget: BraveRequestBudget | None = None,
) -> list[Job]:
    backend = build_search_backend(
        http,
        brave_api_key,
        enable_brave=brave_budget is not None,
        on_brave_attempt=brave_budget.reserve if brave_budget is not None else None,
    )
```

Pass `settings.brave_search_api_key` from `run_pipeline` into the resolver lambda. Keep `BRAVE_MONTHLY_QUERY_LIMIT` as the existing repository-level quota variable; only the key moves.

Update each affected `Settings` fixture by passing `brave_search_api_key="brave-key"` alongside its existing keyword arguments, or leave the new field at `None`. Remove every Brave-key `monkeypatch.setenv` and `monkeypatch.delenv` call from source, pipeline, and budget tests.

- [ ] **Step 7: Run the consumer regression set**

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_cli.py tests/test_sources.py tests/test_pipeline.py tests/test_brave_budget.py -q
```

Expected: PASS for daily, Gmail, cover-letter, Brave-enabled, and DuckDuckGo-fallback paths.

- [ ] **Step 8: Prove production code has no legacy credential reads**

Run:

```bash
rg -n 'os\.environ|getenv|_require_env' apps/job-hunter/src/job_hunter/config.py apps/job-hunter/src/job_hunter/sources/__init__.py apps/job-hunter/src/job_hunter/pipeline.py
rg -n 'GEMINI_API_KEY|BRAVE_SEARCH_API_KEY|CANDIDATE_PROFILE_B64|COVER_LETTER_TEMPLATE_B64' apps/job-hunter/src
```

Expected: the first command shows only approved non-key runtime/quota variables; the second returns no matches.

- [ ] **Step 9: Commit database-backed runtime configuration**

```bash
git add apps/job-hunter/src/job_hunter/config.py apps/job-hunter/src/job_hunter/cli.py apps/job-hunter/src/job_hunter/sources/__init__.py apps/job-hunter/src/job_hunter/pipeline.py apps/job-hunter/tests
git commit -m "feat: load Job Hunter user configuration from Supabase"
```

---

### Task 7: Remove workflow wiring and update active operational documentation

**Files:**
- Modify: `.github/workflows/job-hunter-daily.yml:27-63`
- Modify: `.github/workflows/job-hunter-generate-cover-letter.yml:25-42`
- Modify: `apps/job-hunter/tests/test_workflow.py`
- Modify: `apps/job-hunter/.env.example`
- Modify: `apps/job-hunter/README.md:48,117-134,198-235`
- Modify: `apps/job-hunter/AGENTS.md:104,142,162`

**Interfaces:**
- Consumes: Task 6 runtime behavior.
- Produces: workflows with only platform/Gmail/Telegram/quota configuration; active documentation that directs users to Relay for the four migrated values.

- [ ] **Step 1: Replace workflow secret-presence tests with absence tests**

Extend `test_workflow.py` to load both workflows and assert the four names are absent from every step's `env` mapping:

```python
USER_RUNTIME_SECRET_NAMES = {
    "GEMINI_API_KEY",
    "BRAVE_SEARCH_API_KEY",
    "CANDIDATE_PROFILE_B64",
    "COVER_LETTER_TEMPLATE_B64",
}

def test_user_runtime_secrets_are_not_injected_into_workflows():
    for workflow_path in (DAILY_WORKFLOW, COVER_LETTER_WORKFLOW):
        workflow = yaml.safe_load(workflow_path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                assert USER_RUNTIME_SECRET_NAMES.isdisjoint((step.get("env") or {}).keys())
```

Keep the existing quota/run-ID assertions and the Brave monthly-limit assertion.

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_workflow.py -q
```

Expected: FAIL while the workflows still inject the four names.

- [ ] **Step 2: Remove only the four migrated entries from workflow YAML**

Delete:

- `GEMINI_API_KEY` from Gmail, daily-run, and cover-letter steps;
- `BRAVE_SEARCH_API_KEY` from the daily-run step;
- `CANDIDATE_PROFILE_B64` and `COVER_LETTER_TEMPLATE_B64` from daily-run and cover-letter steps.

Do not remove `JOB_HUNTER_USER_ID`, Supabase settings, Gmail OAuth settings, Telegram settings, Gemini model/quota variables, `BRAVE_MONTHLY_QUERY_LIMIT`, or `GEMINI_RUN_ID`.

- [ ] **Step 3: Make workflow tests pass**

Run:

```bash
cd apps/job-hunter && .venv/bin/python -m pytest tests/test_workflow.py -q
```

Expected: PASS.

- [ ] **Step 4: Update active configuration documentation**

In `.env.example`, remove the four migrated names and correct the stale Supabase comment so it says the pipeline uses Postgres and the signing key mints trusted runner tokens.

In `README.md` and `AGENTS.md`:

- replace base64/GitHub-secret setup with “save or replace CV and cover-letter text in Relay Profile”;
- direct Gemini/Brave setup to the new Profile credential controls;
- state that Gemini is required and Brave is optional with DuckDuckGo fallback;
- explain that Gmail sync uses the stored per-user Gemini key but Gmail OAuth variables remain environment-backed;
- keep Relay's own deployment-level Gemini configuration separate;
- remove commands that instruct users to export the four legacy values.

Do not rewrite historical specs/plans that describe the state at their time; active docs are the migration target.

- [ ] **Step 5: Verify active files no longer prescribe legacy values**

Run:

```bash
rg -n 'GEMINI_API_KEY|BRAVE_SEARCH_API_KEY|CANDIDATE_PROFILE_B64|COVER_LETTER_TEMPLATE_B64' \
  .github/workflows apps/job-hunter/.env.example apps/job-hunter/README.md apps/job-hunter/AGENTS.md apps/job-hunter/src
```

Expected: no matches. Historical design documents and Relay's separate server-level `GEMINI_API_KEY` are intentionally outside this command.

- [ ] **Step 6: Commit workflow and runbook migration**

```bash
git add .github/workflows/job-hunter-daily.yml .github/workflows/job-hunter-generate-cover-letter.yml apps/job-hunter/tests/test_workflow.py apps/job-hunter/.env.example apps/job-hunter/README.md apps/job-hunter/AGENTS.md
git commit -m "docs: move Job Hunter user secrets to Relay"
```

---

### Task 8: Full verification and hosted cutover

**Files:**
- Verify: all files changed in Tasks 1-7
- External state after deployment: hosted Supabase migration and GitHub repository-secret inventory

**Interfaces:**
- Consumes: the complete implementation and one operator-supplied set of current user documents/keys.
- Produces: evidence for every #72 acceptance criterion and safe deletion of the four hosted repository secrets.

- [ ] **Step 1: Run all local automated checks from the worktree root**

Run:

```bash
pnpm test
pnpm db:test
pnpm relay:lint
pnpm relay:build
```

Expected: all commands PASS. `pnpm db:test` must execute against the local stack; skipped database security tests are not acceptable evidence.

- [ ] **Step 2: Run migration and security inspection commands**

Run:

```bash
supabase migration list --local
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: the credential migration is listed locally, the diff check is clean, and the worktree has no uncommitted files.

- [ ] **Step 3: Review the complete branch diff against the spec**

Verify explicitly:

- only the four approved user-owned values moved;
- ordinary authenticated sessions cannot call the decryption path;
- every RPC derives ownership from `auth.uid()`;
- no public function accepts a user ID;
- only `job_hunter_get_provider_credentials` is the named security-definer Job Hunter function;
- no secret appears in a response model, error, log, `repr`, Vault name, or Vault description;
- daily, Gmail, and cover-letter commands share the database provider source;
- no legacy environment fallback exists.

- [ ] **Step 4: Apply and verify the hosted migration before deleting anything**

After the branch is reviewed and the target deployment is ready, inspect pending hosted migrations and apply through the repository's established Supabase release procedure. Then verify the hosted migration list shows the new migration on both local and remote sides:

```bash
supabase migration list --linked
```

Expected: the credential migration appears in both columns. Do not continue on mismatch.

- [ ] **Step 5: Perform the operator-assisted credential lifecycle smoke test**

Using the signed-in owner's Relay Profile:

1. Save/update CV and cover-letter text.
2. Save a Gemini key and optionally a Brave key.
3. Reload Profile and verify only status/timestamp return; inputs stay blank.
4. Replace one fake/non-production test key first, verify status updates, then delete it and verify not configured.
5. Restore the real required Gemini value.
6. Invoke a non-delivering Job Hunter configuration check with the owner's `JOB_HUNTER_USER_ID` and trusted runner token.
7. Confirm the process loads documents/Gemini and never prints their values.

Expected: ordinary browser requests never receive a key, and the trusted same-user run succeeds. A token for a second test user must not retrieve the owner's key.

- [ ] **Step 6: Delete the four GitHub repository secrets only after Step 5 passes**

First inspect names:

```bash
gh secret list --repo amitbaz/career-platform
```

Delete the exact approved names:

```bash
gh secret delete GEMINI_API_KEY --repo amitbaz/career-platform
gh secret delete BRAVE_SEARCH_API_KEY --repo amitbaz/career-platform
gh secret delete CANDIDATE_PROFILE_B64 --repo amitbaz/career-platform
gh secret delete COVER_LETTER_TEMPLATE_B64 --repo amitbaz/career-platform
```

Then rerun `gh secret list --repo amitbaz/career-platform` and verify all four names are absent. These deletions are irreversible through GitHub; retain the provider values in their providers/password manager until the database-backed run is proven.

- [ ] **Step 7: Dispatch and observe real workflows**

Dispatch the daily workflow in a non-delivering/dry-run-safe configuration if available, and dispatch one cover-letter run only with a known test job. For each dispatch, capture the numeric Actions database ID into the task-specific shell variable `CREDENTIAL_RUN_ID`, then verify with:

```bash
gh run view "$CREDENTIAL_RUN_ID" --repo amitbaz/career-platform --json status,conclusion,url
```

Expected: `status` is `completed` and `conclusion` is `success`. An `in_progress` result is not completion evidence. Inspect failure logs only through sanitized output and never print environment values.

- [ ] **Step 8: Commit any verification-only documentation correction**

If verification required an active runbook correction, edit it with the actual verified behavior, rerun the relevant checks, and commit only that correction:

```bash
git add apps/job-hunter/README.md apps/job-hunter/AGENTS.md
git commit -m "docs: record credential cutover verification"
```

If no correction was needed, do not create an empty commit.

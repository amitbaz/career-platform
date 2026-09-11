-- Engine Lab review page and measurement ledger (issue #257).
--
-- 29999999000000 is a placeholder, not a timestamp -- see root AGENTS.md
-- "Migration filenames are allocated across the whole repository". Renumber
-- to the real YYYYMMDDHHMMSS at merge time.
--
-- Full design: apps/job-hunter/docs/superpowers/specs/2026-09-11-engine-lab-review-ledger-design.md
--
-- Identity is real Supabase Auth, not a platform-minted token: a reviewer
-- signs in with an emailed one-time code (GoTrue's own `/auth/v1/otp` and
-- `/auth/v1/verify`), so `auth.uid()` is their own `auth.users.id` and
-- `auth.jwt() ->> 'email'` is an email Supabase itself verified, not one a
-- caller can assert. "The owner" is not a database concept: the app (which
-- holds `ENGINE_LAB_OWNER_EMAIL`) decides who may bootstrap that role, and
-- `job_hunter_engine_lab_bootstrap_owner` re-checks the caller's own
-- verified email before acting on it, so no email but the one the app
-- already trusts can ever claim it.
--
-- One table, one row per invited person (owner included), keyed by email
-- because an invitee has no `auth.users` row -- and hence no `user_id` --
-- until they actually sign in for the first time. Three security-definer
-- functions are the only way to write it; there is deliberately no
-- `insert`/`update` policy granting `authenticated` anything on it directly,
-- the same "single audited exception" shape `job_hunter_get_provider_credentials`
-- already uses elsewhere in this schema:
--
--   * `job_hunter_engine_lab_bootstrap_owner(p_email)` -- claims the owner
--     role for the caller, but only once (first call wins) and only after
--     re-checking `auth.jwt() ->> 'email' = p_email`. The app calls this
--     exactly once, right after its own `ENGINE_LAB_OWNER_EMAIL` check
--     passes -- nothing here knows that env var, so nothing here can be
--     tricked into granting ownership to the wrong email; the app's check
--     is the only gate, and this function is just where the grant is
--     durably recorded.
--   * `job_hunter_engine_lab_invite(p_email)` -- owner-only (self-checked
--     via `job_hunter_engine_lab_caller_is_owner`), adds a pending row with
--     no `user_id` yet.
--   * `job_hunter_engine_lab_claim_invite()` -- any authenticated caller
--     backfills their own pending row's `user_id` by matching their own
--     verified email. Idempotent on repeat logins; a no-op (returns false)
--     for an email nobody invited or a revoked one.
--
-- Impressions and judgements are unchanged in shape from the first version
-- of this migration, but their RLS no longer reads a custom JWT claim --
-- there is no platform-minted token in this design at all. Both check
-- collaborator membership through `job_hunter_engine_lab_is_active_collaborator`,
-- a second security-definer helper, so the policy itself never has to
-- reason about `revoked_at` or self-joins.

create table public.job_hunter_engine_lab_collaborators (
  email      text primary key,
  user_id    uuid unique references auth.users(id),
  is_owner   boolean not null default false,
  invited_at timestamptz not null default now(),
  invited_by uuid references public.job_hunter_engine_lab_collaborators(user_id),
  revoked_at timestamptz
);

comment on table public.job_hunter_engine_lab_collaborators is
  'Who may open or write through the private Engine Lab review page (#257), '
  'keyed by email because an invitee has no user_id until their first '
  'sign-in. Written only through the three job_hunter_engine_lab_* '
  'security-definer functions -- there is no insert/update policy for '
  'authenticated at all.';

alter table public.job_hunter_engine_lab_collaborators enable row level security;

create or replace function public.job_hunter_engine_lab_caller_is_owner()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select coalesce(
    (select c.is_owner
       from public.job_hunter_engine_lab_collaborators c
      where c.user_id = (select auth.uid())
        and c.revoked_at is null),
    false
  );
$$;

comment on function public.job_hunter_engine_lab_caller_is_owner() is
  'Whether the calling session belongs to the Engine Lab owner. '
  'Security definer so RLS policies can call it without a recursive '
  'self-join on the table it reads.';

create or replace function public.job_hunter_engine_lab_is_active_collaborator(p_user_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.job_hunter_engine_lab_collaborators c
     where c.user_id = p_user_id
       and c.revoked_at is null
  );
$$;

comment on function public.job_hunter_engine_lab_is_active_collaborator(uuid) is
  'Whether p_user_id is a currently-invited (not revoked) Engine Lab '
  'collaborator. Security definer for the same reason as '
  'job_hunter_engine_lab_caller_is_owner.';

create policy self_or_owner_select on public.job_hunter_engine_lab_collaborators
  for select to authenticated
  using (
    user_id = (select auth.uid())
    or public.job_hunter_engine_lab_caller_is_owner()
  );
-- No insert/update/delete policy: every write goes through one of the
-- three functions below, each `security definer` and each auditable on
-- its own terms.

create or replace function public.job_hunter_engine_lab_bootstrap_owner(p_email text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_jwt_email text := (select auth.jwt() ->> 'email');
begin
  if v_uid is null then
    raise exception 'job_hunter_engine_lab_bootstrap_owner requires an authenticated caller';
  end if;
  if v_jwt_email is null or lower(v_jwt_email) <> lower(p_email) then
    -- The caller's own verified session email must match what is being
    -- claimed. This is what stops a parameter alone from ever granting
    -- ownership: whoever calls this can only ever bootstrap themselves.
    raise exception 'p_email does not match the authenticated session''s own email';
  end if;
  if exists (
    select 1 from public.job_hunter_engine_lab_collaborators
     where is_owner and revoked_at is null and lower(email) <> lower(p_email)
  ) then
    raise exception 'an owner is already bootstrapped';
  end if;

  insert into public.job_hunter_engine_lab_collaborators (email, user_id, is_owner)
  values (lower(p_email), v_uid, true)
  on conflict (email) do update
    set user_id = excluded.user_id, is_owner = true, revoked_at = null;
end
$$;

comment on function public.job_hunter_engine_lab_bootstrap_owner(text) is
  'Claims the owner role for the caller, once. The app (holder of '
  'ENGINE_LAB_OWNER_EMAIL) is the only real gate -- it calls this exactly '
  'once its own check passes; this function''s job is only to make that '
  'grant durable and to refuse a mismatched or repeat claim.';

create or replace function public.job_hunter_engine_lab_invite(p_email text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if not public.job_hunter_engine_lab_caller_is_owner() then
    raise exception 'only the owner may invite a collaborator';
  end if;

  insert into public.job_hunter_engine_lab_collaborators (email, invited_by)
  values (lower(p_email), (select auth.uid()))
  on conflict (email) do update
    set revoked_at = null
    where public.job_hunter_engine_lab_collaborators.revoked_at is not null;
end
$$;

comment on function public.job_hunter_engine_lab_invite(text) is
  'Owner-only: adds a pending collaborator by email, or un-revokes one who '
  'already exists. No user_id yet -- that is filled in by '
  'job_hunter_engine_lab_claim_invite on their first sign-in.';

create or replace function public.job_hunter_engine_lab_claim_invite()
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_email text := lower((select auth.jwt() ->> 'email'));
  v_updated int;
begin
  if v_uid is null or v_email is null then
    raise exception 'job_hunter_engine_lab_claim_invite requires an authenticated caller';
  end if;

  update public.job_hunter_engine_lab_collaborators
     set user_id = v_uid
   where email = v_email
     and revoked_at is null
     and (user_id is null or user_id = v_uid);
  get diagnostics v_updated = row_count;
  return v_updated > 0;
end
$$;

comment on function public.job_hunter_engine_lab_claim_invite() is
  'Backfills the caller''s own pending invite row with their real user_id, '
  'matched by their own verified email -- never a caller-supplied one. '
  'Idempotent on repeat logins; returns false for an email nobody invited '
  'or one that was revoked.';

create table public.job_hunter_engine_lab_impressions (
  id                    uuid primary key default gen_random_uuid(),
  reviewer_id           uuid not null references public.job_hunter_engine_lab_collaborators(user_id),
  posting_id            uuid not null references public.job_hunter_postings(id),
  cohort                text not null,
  profile_version       text not null,
  posting_version       text not null,
  matching_version      text not null,
  explanation_version   text not null,
  configuration_version text not null,
  shown_at              timestamptz not null default now(),

  constraint job_hunter_engine_lab_impressions_cohort_check
    check (cohort in ('intended', 'audit_hard_excluded', 'audit_unresolved', 'audit_below_threshold'))
);

comment on table public.job_hunter_engine_lab_impressions is
  'One row per Engine Lab review card actually shown, written before the '
  'card is rendered (#257) -- an ignored card still counts. cohort is '
  'written here but never returned to the reviewer until a judgement '
  'exists (concealment is enforced by the application, not by this table).';

create index job_hunter_engine_lab_impressions_reviewer_day_idx
  on public.job_hunter_engine_lab_impressions (reviewer_id, shown_at);

alter table public.job_hunter_engine_lab_impressions enable row level security;

create policy owner_or_self_select on public.job_hunter_engine_lab_impressions
  for select to authenticated
  using (
    reviewer_id = (select auth.uid())
    or public.job_hunter_engine_lab_caller_is_owner()
  );
create policy self_insert on public.job_hunter_engine_lab_impressions
  for insert to authenticated
  with check (
    reviewer_id = (select auth.uid())
    and public.job_hunter_engine_lab_is_active_collaborator((select auth.uid()))
  );
-- No update, no delete: an impression is immutable once written.

create table public.job_hunter_engine_lab_judgements (
  id                 uuid primary key default gen_random_uuid(),
  impression_id      uuid not null unique
    references public.job_hunter_engine_lab_impressions(id),
  reviewer_id        uuid not null references public.job_hunter_engine_lab_collaborators(user_id),
  worth_applying     boolean not null,
  why_line_judgement text not null,
  problem_reason     text,
  judged_at          timestamptz not null default now(),

  constraint job_hunter_engine_lab_judgements_why_line_check
    check (why_line_judgement in ('helpful', 'flawed'))
);

comment on table public.job_hunter_engine_lab_judgements is
  'At most one judgement per impression (#257). worth_applying (the '
  'internal match verdict) and why_line_judgement (the explanation verdict) '
  'are separate not-null columns on purpose -- they are never allowed to '
  'share a metric, and neither can be silently omitted.';

alter table public.job_hunter_engine_lab_judgements enable row level security;

create policy owner_or_self_select on public.job_hunter_engine_lab_judgements
  for select to authenticated
  using (
    reviewer_id = (select auth.uid())
    or public.job_hunter_engine_lab_caller_is_owner()
  );
create policy self_insert on public.job_hunter_engine_lab_judgements
  for insert to authenticated
  with check (
    reviewer_id = (select auth.uid())
    and public.job_hunter_engine_lab_is_active_collaborator((select auth.uid()))
  );
-- No update, no delete: re-judging is a new impression, never an edit to
-- this one's history.

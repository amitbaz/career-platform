-- The platform key's own ledger (issue #128).
--
-- Objective facet extraction reads a posting once for everyone's benefit, so
-- it is funded by a platform-owned provider key rather than by whichever
-- user's run happened to trigger it. That key needs its own accounting, and
-- it cannot live in job_hunter_ai_usage: that ledger is keyed by user_id with
-- own-rows row-level security, so platform spend recorded there would be
-- split across users, and no run could see the platform key's whole day. The
-- key has one global daily allowance; its ledger is global too.
--
-- Hence two tables that deliberately carry no user_id. Nothing here is any
-- user's data: a row says that the platform read one posting, and which
-- posting is not recorded (the user ledger records no prompt or response
-- either -- see PostgresJobStore.record_ai_usage).
--
-- Access is restricted to trusted Job Hunter runners, the same claim
-- job_hunter_get_provider_credentials requires. A browser session is
-- `authenticated` but carries no job_hunter_runner claim, so a signed-in user
-- can neither read the platform's consumption nor write to it.
--
-- There is no delete policy on either table, and none should be added: a
-- ledger a run can erase cannot answer "is the platform key approaching its
-- ceiling", which is the whole reason these rows exist. Rows are dropped by
-- an operator with direct database access, or not at all.

create table public.job_hunter_platform_ai_usage (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  occurred_at timestamptz not null,
  model text not null,
  purpose text not null,
  status text not null,
  estimated_input_tokens integer not null default 0,
  prompt_tokens integer,
  output_tokens integer,
  thinking_tokens integer,
  cached_tokens integer,
  total_tokens integer,
  http_status integer,
  error_code text,
  created_at timestamptz not null default now(),

  -- The upsert target. Every write on the hot path is an upsert so that
  -- HttpClient's POST retry converges instead of double-counting a call the
  -- ledger is meant to meter exactly. Two attempts sharing a microsecond
  -- would collapse into one row, which is the same theoretical loss the
  -- per-user ledger accepts for the same reason.
  unique (provider, model, purpose, occurred_at)
);

-- The window every preflight reads: one provider's rows over a day, or over
-- the trailing rolling minute.
create index job_hunter_platform_ai_usage_window_idx
  on public.job_hunter_platform_ai_usage (provider, occurred_at desc);

comment on table public.job_hunter_platform_ai_usage is
  'Consumption of the platform-owned provider key that funds shared '
  'objective extraction (issue #128). Deliberately not keyed by user_id: the '
  'key has one global allowance, and the work it pays for belongs to no one '
  'user. Read and written only by trusted Job Hunter runners.';

alter table public.job_hunter_platform_ai_usage enable row level security;

create policy runner_select on public.job_hunter_platform_ai_usage
  for select to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
create policy runner_insert on public.job_hunter_platform_ai_usage
  for insert to authenticated
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
-- Update is what makes the upsert above an upsert rather than a duplicate-key
-- failure; it is not an invitation to rewrite history.
create policy runner_update on public.job_hunter_platform_ai_usage
  for update to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb)
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);

-- The platform key's circuit breaker, held apart from the per-user one for
-- the reason the ledgers are apart: a 429 on the platform key says nothing
-- about any user's key, and pausing a user's scoring because shared
-- extraction ran out would charge them for the platform's exhaustion.
create table public.job_hunter_platform_ai_quota_state (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  model text not null,
  paused_until timestamptz,
  reason text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (provider, model)
);

comment on table public.job_hunter_platform_ai_quota_state is
  'Active provider-quota pause for the platform-owned key (issue #128), '
  'separate from job_hunter_ai_quota_state so an exhausted platform '
  'allowance never pauses a user''s own scoring.';

alter table public.job_hunter_platform_ai_quota_state enable row level security;

create policy runner_select on public.job_hunter_platform_ai_quota_state
  for select to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
create policy runner_insert on public.job_hunter_platform_ai_quota_state
  for insert to authenticated
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
create policy runner_update on public.job_hunter_platform_ai_quota_state
  for update to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb)
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);

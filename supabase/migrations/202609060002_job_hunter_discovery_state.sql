-- Job Hunter discovery state, ported from the single-user SQLite store
-- (apps/job-hunter/src/job_hunter/store.py, navigation_store.py,
-- search_budget.py) to the shared platform database. See
-- docs/superpowers/specs/2026-09-06-job-hunter-postgres-schema-design.md.
--
-- Every table is owned by a platform user and isolated with row level
-- security. Tables that had no owner in SQLite (ATS registry, company
-- watch, quota state, context cache, search API usage) become per-user:
-- a handful of alpha users re-learning boards independently is cheaper
-- and safer than a shared table that would need a service-role writer.
--
-- Naming: `job_hunter_` prefix keeps this noisy engine state visibly
-- separate from the Career Brain lifecycle tables (`opportunities`,
-- `opportunity_events`). Only user-marked applied jobs are later promoted
-- into `opportunities` (#80); nothing here is a shared opportunity.
--
-- Policies use `(select auth.uid())` so the planner evaluates the
-- function once per statement rather than once per row, and `to
-- authenticated` so anon requests skip policy evaluation entirely.
-- Existing platform tables use the bare `auth.uid()` form; they can be
-- brought in line separately.
--
-- Type mapping from SQLite: ISO text timestamps -> timestamptz, JSON text
-- -> jsonb, 0/1 integers -> boolean, autoincrement ids -> uuid. "Latest
-- evaluation" in SQLite meant highest autoincrement id; here it is the
-- newest `evaluated_at`, and the index below exists for that query.
--
-- After merge this migration must be applied with `supabase db push`
-- from the repository root (not the dashboard SQL editor, which skips
-- schema_migrations). No Python code targets these tables yet (#70).

-- jobs: the deduplicated posting record and root of every child table ------

create table public.job_hunter_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  fingerprint text not null,
  source text not null default '',
  source_job_id text,
  url text not null default '',
  canonical_url text not null default '',
  company text not null default '',
  title text not null default '',
  location text not null default '',
  remote boolean,
  description text not null default '',
  description_hash text not null default '',
  content_confidence text not null default '',
  ats_provider text,
  ats_board text,
  ats_job_id text,
  market_id text not null default '',
  status text not null default 'new',
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (user_id, fingerprint),
  unique (id, user_id)
);

create index job_hunter_jobs_user_last_seen_idx
  on public.job_hunter_jobs (user_id, last_seen_at desc);
create index job_hunter_jobs_user_status_idx
  on public.job_hunter_jobs (user_id, status);

alter table public.job_hunter_jobs enable row level security;
create policy select_own on public.job_hunter_jobs
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_jobs
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_jobs
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_jobs
  for delete to authenticated using ((select auth.uid()) = user_id);

-- job_sources: which source(s) each job was seen on ------------------------------

create table public.job_hunter_job_sources (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid not null,
  source text not null,
  source_job_id text,
  source_url text not null default '',
  identity_key text not null,
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique (job_id, identity_key),
  foreign key (job_id, user_id) references public.job_hunter_jobs (id, user_id) on delete cascade
);

create index job_hunter_job_sources_user_job_idx
  on public.job_hunter_job_sources (user_id, job_id);

alter table public.job_hunter_job_sources enable row level security;
create policy select_own on public.job_hunter_job_sources
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_job_sources
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_job_sources
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_job_sources
  for delete to authenticated using ((select auth.uid()) = user_id);

-- company_watch: employers whose careers pages are re-checked -----------------------

create table public.job_hunter_company_watch (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  company_name text not null,
  normalized_company_name text not null,
  careers_url text not null default '',
  ats_provider text,
  ats_identifier text,
  discovered_from_job_id uuid,
  promotion_source text not null check (promotion_source in ('manual', 'automatic')),
  confidence double precision not null default 0,
  active boolean not null default true,
  paused_until timestamptz,
  first_seen_at timestamptz not null,
  last_verified_at timestamptz,
  last_successful_check_at timestamptz,
  consecutive_failures integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, normalized_company_name),
  foreign key (discovered_from_job_id, user_id) references public.job_hunter_jobs (id, user_id)
);

create index job_hunter_company_watch_due_idx
  on public.job_hunter_company_watch (user_id, active, paused_until);

alter table public.job_hunter_company_watch enable row level security;
create policy select_own on public.job_hunter_company_watch
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_company_watch
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_company_watch
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_company_watch
  for delete to authenticated using ((select auth.uid()) = user_id);

-- ats_registry: known applicant-tracking-system boards and their health --------------

create table public.job_hunter_ats_registry (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  board_identifier text not null,
  company_name text not null default '',
  market_hint text not null default '',
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  last_checked_at timestamptz,
  last_success_at timestamptz,
  last_eligible_at timestamptz,
  last_job_count integer not null default 0,
  eligible_jobs_seen integer not null default 0,
  consecutive_failures integer not null default 0,
  active boolean not null default true,
  paused_until timestamptz,
  rejected_reason text,
  created_at timestamptz not null default now(),
  unique (user_id, provider, board_identifier)
);

create index job_hunter_ats_registry_due_idx
  on public.job_hunter_ats_registry (user_id, active, paused_until);

alter table public.job_hunter_ats_registry enable row level security;
create policy select_own on public.job_hunter_ats_registry
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_ats_registry
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_ats_registry
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_ats_registry
  for delete to authenticated using ((select auth.uid()) = user_id);

-- evaluations: append-only model verdicts per job; newest evaluated_at wins ----------

create table public.job_hunter_evaluations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid not null,
  total_score integer not null default 0,
  raw_model_score integer not null default 0,
  scores_json jsonb not null default '{}'::jsonb,
  decision text not null default '',
  hard_blockers_json jsonb not null default '[]'::jsonb,
  strengths_json jsonb not null default '[]'::jsonb,
  gaps_json jsonb not null default '[]'::jsonb,
  requirements_json jsonb not null default '{}'::jsonb,
  salary_note text not null default '',
  location_note text not null default '',
  rationale text not null default '',
  model text not null default '',
  status text not null default 'ok',
  market_id text not null default '',
  description_hash_at_eval text not null default '',
  content_confidence_at_eval text not null default '',
  evaluated_at timestamptz not null,
  created_at timestamptz not null default now(),
  foreign key (job_id, user_id) references public.job_hunter_jobs (id, user_id)
);

create index job_hunter_evaluations_job_latest_idx
  on public.job_hunter_evaluations (job_id, evaluated_at desc);
create index job_hunter_evaluations_user_latest_idx
  on public.job_hunter_evaluations (user_id, evaluated_at desc);

alter table public.job_hunter_evaluations enable row level security;
create policy select_own on public.job_hunter_evaluations
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_evaluations
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_evaluations
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_evaluations
  for delete to authenticated using ((select auth.uid()) = user_id);

-- materials: generated cover letters --------------------------------------------------

create table public.job_hunter_materials (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid not null,
  cover_letter_text text not null default '',
  generated_at timestamptz not null,
  created_at timestamptz not null default now(),
  foreign key (job_id, user_id) references public.job_hunter_jobs (id, user_id)
);

create index job_hunter_materials_job_latest_idx
  on public.job_hunter_materials (job_id, generated_at desc);

alter table public.job_hunter_materials enable row level security;
create policy select_own on public.job_hunter_materials
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_materials
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_materials
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_materials
  for delete to authenticated using ((select auth.uid()) = user_id);

-- deliveries: what was sent to Telegram, and when ------------------------------------------

create table public.job_hunter_deliveries (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid not null,
  delivery_type text not null check (delivery_type in ('telegram_message', 'telegram_document')),
  status text not null default 'sent',
  delivered_at timestamptz not null,
  telegram_message_id text,
  created_at timestamptz not null default now(),
  foreign key (job_id, user_id) references public.job_hunter_jobs (id, user_id)
);

create index job_hunter_deliveries_job_type_idx
  on public.job_hunter_deliveries (job_id, delivery_type);

alter table public.job_hunter_deliveries enable row level security;
create policy select_own on public.job_hunter_deliveries
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_deliveries
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_deliveries
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_deliveries
  for delete to authenticated using ((select auth.uid()) = user_id);

-- pending_ai_work: retry queue for evaluations/letters that hit a quota wall ---------------

create table public.job_hunter_pending_ai_work (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  work_type text not null,
  job_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, work_type, job_id),
  foreign key (job_id, user_id) references public.job_hunter_jobs (id, user_id) on delete cascade
);

alter table public.job_hunter_pending_ai_work enable row level security;
create policy select_own on public.job_hunter_pending_ai_work
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_pending_ai_work
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_pending_ai_work
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_pending_ai_work
  for delete to authenticated using ((select auth.uid()) = user_id);

-- ai_usage: append-only ledger of model calls. SQLite named this gemini_usage; ---------
-- the provider column anticipates the provider-agnostic ledger in #73.

create table public.job_hunter_ai_usage (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null default 'gemini',
  occurred_at timestamptz not null,
  run_id text,
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
  created_at timestamptz not null default now()
);

create index job_hunter_ai_usage_window_idx
  on public.job_hunter_ai_usage (user_id, provider, occurred_at desc);

alter table public.job_hunter_ai_usage enable row level security;
create policy select_own on public.job_hunter_ai_usage
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_ai_usage
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_ai_usage
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_ai_usage
  for delete to authenticated using ((select auth.uid()) = user_id);

-- ai_quota_state: per-model pause after a quota error -------------------------------------

create table public.job_hunter_ai_quota_state (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null default 'gemini',
  model text not null,
  paused_until timestamptz,
  reason text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, provider, model)
);

alter table public.job_hunter_ai_quota_state enable row level security;
create policy select_own on public.job_hunter_ai_quota_state
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_ai_quota_state
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_ai_quota_state
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_ai_quota_state
  for delete to authenticated using ((select auth.uid()) = user_id);

-- candidate_context_cache: structured candidate context keyed by profile hash + model --

create table public.job_hunter_candidate_context_cache (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  cache_key text not null,
  profile_hash text not null,
  model text not null,
  schema_version text not null,
  context_json jsonb not null,
  created_at timestamptz not null default now(),
  unique (user_id, cache_key)
);

alter table public.job_hunter_candidate_context_cache enable row level security;
create policy select_own on public.job_hunter_candidate_context_cache
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_candidate_context_cache
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_candidate_context_cache
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_candidate_context_cache
  for delete to authenticated using ((select auth.uid()) = user_id);

-- search_api_usage: append-only ledger for search-provider daily budgets -----------------

create table public.job_hunter_search_api_usage (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  provider text not null,
  occurred_at timestamptz not null,
  created_at timestamptz not null default now()
);

create index job_hunter_search_api_usage_window_idx
  on public.job_hunter_search_api_usage (user_id, provider, occurred_at desc);

alter table public.job_hunter_search_api_usage enable row level security;
create policy select_own on public.job_hunter_search_api_usage
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_search_api_usage
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_search_api_usage
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_search_api_usage
  for delete to authenticated using ((select auth.uid()) = user_id);

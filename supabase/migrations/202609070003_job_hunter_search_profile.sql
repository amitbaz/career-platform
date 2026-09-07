-- 202609070003_job_hunter_search_profile.sql
-- Per-user search configuration (issue #71). One profile row per user;
-- markets are a child table so each market stays independently RLS-checkable.

create table public.job_hunter_search_profiles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null unique references auth.users(id) on delete cascade,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  timezone text not null,
  scheduled_hour smallint not null,
  max_jobs_per_run integer not null,
  source_minimum_per_run integer not null,
  source_max_share double precision not null,
  thresholds jsonb not null default '{}',
  salary_floor_eur integer not null,

  target_titles text[] not null default '{}',
  positive_keywords text[] not null default '{}',
  blocked_title_keywords text[] not null default '{}',
  role_families text[] not null default '{}',
  search_query_templates text[] not null default '{}',
  search_domains text[] not null default '{}',
  specialist_search_domains text[] not null default '{}',
  specialist_query_templates text[] not null default '{}',
  search_queries text[] not null default '{}',
  yc_job_pages text[] not null default '{}',

  engineering_title_keywords text[] not null default '{}',
  engineering_title_phrases text[] not null default '{}',
  blocked_profession_title_phrases text[] not null default '{}',
  specialist_board_hosts text[] not null default '{}',
  frontend_signals text[] not null default '{}',
  backend_heavy_signals text[] not null default '{}',

  max_search_queries_per_run integer not null,
  max_canonical_resolutions_per_run integer not null,
  max_learned_ats_boards_per_run integer not null,
  learned_ats_denylist text[] not null default '{}',
  learned_ats_allowlist text[] not null default '{}',

  manual_company_watch jsonb not null default '[]',
  ats jsonb not null default '{}',

  unique (id, user_id)
);

alter table public.job_hunter_search_profiles enable row level security;

create policy select_own on public.job_hunter_search_profiles
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_search_profiles
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_search_profiles
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_search_profiles
  for delete to authenticated using ((select auth.uid()) = user_id);


create table public.job_hunter_search_profile_markets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  profile_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  market_id text not null,
  query_share double precision not null,
  locations text[] not null default '{}',
  allowed_languages text[] not null default '{}',
  currency text not null,
  gross_base_floor integer not null,
  location_floors jsonb not null default '{}',
  remote_policy text not null,
  relocation_policy text not null,
  sponsorship_policy text not null,
  direct_sources text[] not null default '{}',
  discovery_domains text[] not null default '{}',
  query_templates text[] not null default '{}',
  role_families text[] not null default '{}',
  enabled boolean not null default true,

  unique (profile_id, market_id),
  unique (id, user_id),
  foreign key (profile_id, user_id)
    references public.job_hunter_search_profiles (id, user_id) on delete cascade,

  constraint job_hunter_search_profile_markets_remote_policy_check
    check (remote_policy in ('preferred', 'required', 'allowed')),
  constraint job_hunter_search_profile_markets_relocation_policy_check
    check (relocation_policy in ('none', 'selective', 'allowed')),
  constraint job_hunter_search_profile_markets_sponsorship_policy_check
    check (sponsorship_policy in ('not_required', 'required'))
);

alter table public.job_hunter_search_profile_markets enable row level security;

create policy select_own on public.job_hunter_search_profile_markets
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_search_profile_markets
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_search_profile_markets
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_search_profile_markets
  for delete to authenticated using ((select auth.uid()) = user_id);

create index job_hunter_search_profile_markets_profile_id_idx
  on public.job_hunter_search_profile_markets (profile_id);
create index job_hunter_search_profile_markets_user_id_idx
  on public.job_hunter_search_profile_markets (user_id);

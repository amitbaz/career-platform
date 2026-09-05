-- Career Brain Release 1: explicit persisted practice plans.
--
-- A practice plan is the explicit persisted contract explaining what a
-- future practice session is trying to improve and why. `practice_plans`
-- carries full own-row CRUD, matching the existing
-- `opportunities`/`career_stories`/`coach_observations` pattern.
--
-- Release 1 does not define the prioritization formula: `priority_score`
-- and `priority_factors` are nullable/default placeholders Release 3 will
-- populate with its deterministic recommendation snapshot -- they are
-- never computed here. `generation_error` exists so a later AI-generation
-- step can fail safely without losing the plan row.
--
-- `practice_plan_opportunities` is the one exception to the append-only
-- pattern used elsewhere in this release: the set of opportunities a plan
-- serves is deliberately REPLACEABLE (see `setPracticePlanOpportunities` in
-- `src/lib/repositories/practice-plans.ts`, which deletes a plan's existing
-- links and inserts the requested set in their place), so it gets own-row
-- select/insert/delete RLS policies -- but no update policy, since a link
-- is always replaced by delete-then-insert, never patched in place.
--
-- At most one `primary` opportunity per plan is enforced twice: the
-- partial unique index below is the actual guarantee (it also protects
-- against races and any future caller that bypasses the repository); the
-- TypeScript guard in `setPracticePlanOpportunities` runs first and gives
-- an early, typed `RepositoryError` instead of a raw constraint-violation
-- error. Both layers are intentional, not redundant.
--
-- Both parent foreign keys on `practice_plan_opportunities` are
-- ownership-preserving composite keys into `practice_plans (id, user_id)`
-- and `opportunities (id, user_id)` -- the latter already carries that
-- uniqueness constraint from Task 2 (migration 202608300002).

create table public.practice_plans (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'draft'
    check (status in ('draft', 'ready', 'started', 'completed', 'cancelled', 'failed')),
  primary_focus text not null,
  secondary_focus text,
  rationale text not null default '',
  format text not null check (format in (
    'targeted_drill', 'story_work', 'self_presentation', 'behavioral',
    'technical_communication', 'role_prep', 'full_simulation', 'hands_on'
  )),
  estimated_minutes integer check (estimated_minutes is null or estimated_minutes between 1 and 180),
  success_criteria jsonb not null default '[]'::jsonb,
  priority_score numeric,
  priority_factors jsonb not null default '{}'::jsonb,
  generation_error text,
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (id, user_id)
);

create index practice_plans_user_updated_idx on public.practice_plans (user_id, updated_at desc);
create index practice_plans_user_status_idx on public.practice_plans (user_id, status);

alter table public.practice_plans enable row level security;

create policy select_own on public.practice_plans for select using (auth.uid() = user_id);
create policy insert_own on public.practice_plans for insert with check (auth.uid() = user_id);
create policy update_own on public.practice_plans for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy delete_own on public.practice_plans for delete using (auth.uid() = user_id);

create table public.practice_plan_opportunities (
  user_id uuid not null references auth.users(id) on delete cascade,
  practice_plan_id uuid not null,
  opportunity_id uuid not null,
  relevance text not null default 'supporting' check (relevance in ('primary', 'supporting')),
  created_at timestamptz not null default now(),
  primary key (practice_plan_id, opportunity_id),
  foreign key (practice_plan_id, user_id)
    references public.practice_plans (id, user_id) on delete cascade,
  foreign key (opportunity_id, user_id)
    references public.opportunities (id, user_id) on delete cascade
);

create index practice_plan_opportunities_user_idx on public.practice_plan_opportunities (user_id);
create index practice_plan_opportunities_opportunity_idx on public.practice_plan_opportunities (opportunity_id);

-- Enforces at most one primary opportunity per plan. See the header note --
-- this is the database-side half of a two-layer guard whose other half is
-- the TypeScript check in `setPracticePlanOpportunities`.
create unique index practice_plan_one_primary_opportunity_idx
on public.practice_plan_opportunities (practice_plan_id)
where relevance = 'primary';

alter table public.practice_plan_opportunities enable row level security;

-- Replaceable, not append-only: own-row select/insert/delete. No update
-- policy -- `setPracticePlanOpportunities` always replaces a plan's links
-- via delete-then-insert scoped to both `user_id` and `practice_plan_id`,
-- never an in-place patch.
create policy select_own on public.practice_plan_opportunities for select using (auth.uid() = user_id);
create policy insert_own on public.practice_plan_opportunities for insert with check (auth.uid() = user_id);
create policy delete_own on public.practice_plan_opportunities for delete using (auth.uid() = user_id);

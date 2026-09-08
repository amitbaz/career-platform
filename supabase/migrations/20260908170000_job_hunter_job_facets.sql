-- Objective facets on jobs (issue #125).
--
-- A facet is one structured objective fact about a posting: what it requires
-- and how deeply, what it discloses about pay, where it will hire, whether it
-- is remote, what seniority it is pitched at, what it is built on. Facets are
-- identical for every user, read once per posting, and reused by every later
-- run -- the "objective extraction" half of the enrichment split in
-- CONTEXT.md, as against the per-user "subjective scoring" that stays in
-- job_hunter_evaluations.
--
-- One row per job, cascading from job_hunter_jobs, with the same four
-- row-level-security policies as every other job_hunter_* table: facets are
-- shared *conceptually* -- the same posting yields the same answer whoever
-- asks -- but the job rows they hang off are still per-user, so access is
-- governed exactly as the rest of the job record is.
--
-- Invalidation deliberately introduces no second notion of a changed posting:
-- description_hash_at_extraction is compared against
-- job_hunter_jobs.description_hash, the same mechanism
-- job_hunter_evaluations.description_hash_at_eval already uses to decide
-- whether a job needs re-evaluating.
--
-- Dedicated columns rather than one jsonb document, because hiring-eligible
-- regions, remote policy, seniority and compensation have to be filterable in
-- a query without loading and parsing every row. Stated requirements stay in
-- jsonb: they are read as a set, never filtered on one at a time.

create table public.job_hunter_job_facets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id uuid not null,

  -- The description these facets were read from. Compared against
  -- job_hunter_jobs.description_hash to decide whether they are still current.
  description_hash_at_extraction text not null default '',

  seniority text not null default 'unknown'
    check (seniority in (
      'intern', 'junior', 'mid', 'senior', 'staff', 'principal', 'lead',
      'manager', 'unknown'
    )),
  remote_policy text not null default 'unknown'
    check (remote_policy in ('remote', 'hybrid', 'onsite', 'unknown')),
  relocation_policy text not null default 'unknown'
    check (relocation_policy in ('offered', 'required', 'not_offered', 'unknown')),

  -- The regions the posting states it will hire in. Empty means the posting
  -- said nothing determinate, never "eligible nowhere" -- the same reading
  -- hiring_scope.HiringScope documents.
  hiring_regions text[] not null default '{}',
  stack text[] not null default '{}',

  compensation_disclosed boolean not null default false,
  compensation_currency text not null default '',
  compensation_min bigint,
  compensation_max bigint,
  compensation_period text not null default ''
    check (compensation_period in ('', 'hour', 'day', 'month', 'year')),

  -- [{"requirement": text, "depth": familiarity|experience|deep_expert,
  --   "kind": must_have|preferred}, ...]
  requirements_json jsonb not null default '[]'::jsonb,

  -- Which facet names were taken from structured source data or deterministic
  -- code rather than from the model, so the split can be measured instead of
  -- assumed.
  source_supplied text[] not null default '{}',

  model text not null default '',
  extracted_at timestamptz not null,
  created_at timestamptz not null default now(),

  -- One set of facets per job: a posting has one set of objective facts, and
  -- re-extraction after a description change replaces it rather than
  -- appending a second answer to the same question.
  unique (job_id),
  foreign key (job_id, user_id)
    references public.job_hunter_jobs (id, user_id) on delete cascade
);

-- The four filters the acceptance criteria name. user_id leads each btree
-- index because row-level security scopes every read by it anyway, so a
-- filter on a facet is always a filter on (user_id, facet).
create index job_hunter_job_facets_user_remote_policy_idx
  on public.job_hunter_job_facets (user_id, remote_policy);
create index job_hunter_job_facets_user_seniority_idx
  on public.job_hunter_job_facets (user_id, seniority);
create index job_hunter_job_facets_user_compensation_idx
  on public.job_hunter_job_facets (user_id, compensation_max);
-- Regions are a set membership test (`hiring_regions && '{europe}'`), which
-- btree cannot answer; GIN can.
create index job_hunter_job_facets_hiring_regions_idx
  on public.job_hunter_job_facets using gin (hiring_regions);
create index job_hunter_job_facets_user_job_idx
  on public.job_hunter_job_facets (user_id, job_id);

comment on table public.job_hunter_job_facets is
  'Objective facets read once per posting and reused by every later run: '
  'stated requirements and depth, disclosed compensation, hiring-eligible '
  'regions, remote and relocation policy, seniority and stack. Invalidated '
  'through job_hunter_jobs.description_hash, the same mechanism that gates '
  're-evaluation.';

alter table public.job_hunter_job_facets enable row level security;
create policy select_own on public.job_hunter_job_facets
  for select to authenticated using ((select auth.uid()) = user_id);
create policy insert_own on public.job_hunter_job_facets
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy update_own on public.job_hunter_job_facets
  for update to authenticated using ((select auth.uid()) = user_id) with check ((select auth.uid()) = user_id);
create policy delete_own on public.job_hunter_job_facets
  for delete to authenticated using ((select auth.uid()) = user_id);

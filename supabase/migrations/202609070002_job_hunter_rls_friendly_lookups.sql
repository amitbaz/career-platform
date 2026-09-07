-- Make the store functions' identity lookups usable under row-level security.
--
-- 202609070001 indexed the predicates these functions run, and measured as
-- `service_role` the fix worked: an identity lookup went from a 539 ms
-- sequential scan to a 1.9 ms index scan. The production run still timed out
-- with SQLSTATE 57014, because `service_role` is not the role that runs them.
--
-- Under RLS Postgres puts each table behind a security barrier, and a qual
-- that calls a function which is not LEAKPROOF cannot be pushed below it --
-- pushing it down could leak values from rows the caller may not see. Every
-- one of these lookups compares `job_hunter_normalize_*(column)`, so as
-- `authenticated` the quals stay above the barrier as a join filter and the
-- expression indexes are unreachable. Measured on the same query and data:
--
--   service_role  : BitmapOr over three index scans   cost      3,204
--   authenticated : nested loop over a seq scan       cost 11,659,493
--
-- Replacing the same predicate with plain column equality changes the
-- authenticated plan to a merge anti join over an index-only scan, cost
-- 2,300 -- because `text = text` is leakproof and does push down.
--
-- So the normalized values become stored generated columns and the functions
-- compare those. The normalizers stay the single definition of what
-- normalization means; the columns just cache their output where the planner
-- can reach it. `GENERATED ALWAYS AS ... STORED` requires the called function
-- to be IMMUTABLE, which all four already are, and Postgres recomputes them
-- on write, so they cannot drift from the source columns the way a trigger
-- or an application-maintained copy could.

alter table public.job_hunter_jobs
  add column if not exists normalized_identity text
    generated always as (
      public.job_hunter_normalize_text(company) || '|' ||
      public.job_hunter_normalize_text(title) || '|' ||
      public.job_hunter_normalize_text(location)
    ) stored,
  add column if not exists canonical_url_of_url text
    generated always as (public.job_hunter_canonicalize_url(url)) stored,
  add column if not exists normalized_company text
    generated always as (public.job_hunter_normalize_company(company)) stored,
  add column if not exists normalized_title text
    generated always as (public.job_hunter_normalize_tokens(title)) stored;

-- The expression indexes 202609070001 added are unreachable under RLS for
-- exactly the reason above. Replace them with plain-column equivalents
-- rather than leaving three unused indexes to be maintained on every write.
drop index if exists public.job_hunter_jobs_user_identity_idx;
drop index if exists public.job_hunter_jobs_user_canonical_of_url_idx;
drop index if exists public.job_hunter_jobs_user_normalized_triple_idx;

create index if not exists job_hunter_jobs_user_normalized_identity_idx
  on public.job_hunter_jobs (user_id, normalized_identity);

create index if not exists job_hunter_jobs_user_canonical_of_url_col_idx
  on public.job_hunter_jobs (user_id, canonical_url_of_url);

create index if not exists job_hunter_jobs_user_normalized_company_title_idx
  on public.job_hunter_jobs (user_id, normalized_company, normalized_title);

-- Rewritten to compare the generated columns. The match rules are unchanged:
-- the gmail source tuple, equal canonical URLs when both sides have one, or
-- an identical normalized company|title|location triple that is not entirely
-- empty. Only the side of each comparison that reads a stored job row moves
-- to a column; the candidate side is still computed, which is correct --
-- there are a few hundred candidates and the planner evaluates them once per
-- outer row, not once per job.
create or replace function public.job_hunter_unmaterialized_inbound_jobs()
returns setof jsonb
language sql
security invoker
set search_path = ''
as $$
  -- Two things are load-bearing here, and both are about making the quals
  -- pushable below the RLS security barrier.
  --
  -- 1. The candidate side is materialized first. Every normalizer call now
  --    happens once per candidate rather than inside the join, so the inner
  --    predicate is column = column. A qual that still called a normalizer
  --    -- on either side -- is not leakproof, and one such branch keeps the
  --    whole OR above the barrier.
  -- 2. The single OR becomes three separate NOT EXISTS. `not exists (A or B
  --    or C)` and `not exists(A) and not exists(B) and not exists(C)` are the
  --    same statement, but only the second gives each branch its own index.
  --
  -- Measured as `authenticated` on 18,308 jobs and 341 candidates:
  -- cost 2,361,820 / 1841 ms as one ORed anti-join over a sequential scan,
  -- against cost 1,522 / 8.5 ms as three anti-joins over their indexes.
  with cand as materialized (
    select c.*,
           'gmail:' || c.source_platform as match_source,
           public.job_hunter_canonicalize_url(c.url) as match_canonical_url,
           public.job_hunter_normalize_text(c.company) || '|' ||
           public.job_hunter_normalize_text(c.title) || '|' ||
           public.job_hunter_normalize_text(c.location) as match_identity
      from public.job_hunter_inbound_job_candidates c
     where c.user_id = (select auth.uid())
  )
  select to_jsonb(cand) - 'match_source' - 'match_canonical_url' - 'match_identity'
    from cand
   where not exists (
           select 1 from public.job_hunter_jobs j
            where j.user_id = (select auth.uid())
              and j.source = cand.match_source
              and j.source_job_id = cand.source_candidate_key
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            where j.user_id = (select auth.uid())
              and cand.url <> '' and j.url <> ''
              and j.canonical_url_of_url = cand.match_canonical_url
         )
     and not exists (
           select 1 from public.job_hunter_jobs j
            where j.user_id = (select auth.uid())
              and cand.match_identity <> '||'
              and j.normalized_identity = cand.match_identity
         )
   order by cand.created_at, cand.id;
$$;

-- Same treatment. `p_company`/`p_title` are normalized once into locals
-- already, so only the stored side changes.
create or replace function public.job_hunter_find_job_by_identity(
  p_company text, p_title text, p_location text
) returns setof uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_company text := public.job_hunter_normalize_company(p_company);
  v_title text := public.job_hunter_normalize_tokens(p_title);
begin
  if v_company = '' or v_title = '' then
    return;
  end if;

  return query
  with matches as (
    select j.id as job_id, j.location as job_location, j.created_at as job_created_at
      from public.job_hunter_jobs j
     where j.user_id = v_uid
       and j.normalized_company = v_company
       and j.normalized_title = v_title
       and public.job_hunter_locations_compatible(p_location, j.location)
  )
  select m.job_id
    from matches m
   where not exists (
           select 1
             from matches l
             join matches r on l.job_id < r.job_id
            where not public.job_hunter_locations_compatible(l.job_location, r.job_location)
         )
   order by m.job_created_at, m.job_id;
end $$;

analyze public.job_hunter_jobs;

-- job_hunter_match_jobs never excluded a closed posting or a rejected
-- membership row (issue #188).
--
-- #187 (20260910140000_job_hunter_match_jobs.sql) ranks and scores every
-- `job_hunter_jobs` row this user holds, with no read of `job_hunter_postings
-- .closed_at` (#186) and no read of `job_hunter_jobs.status`. Both were
-- harmless while nothing called the function for delivery: `run_pipeline`
-- built its shortlist from `discovery.eligible`, which a closed posting or a
-- `status = 'rejected'`/`'closed'` job (prefilter's own non-AI title/
-- employment-type/language gate, or a posting `discovery.py` found
-- unavailable -- see `discovery.py`'s prefilter phase, `set_job_statuses`)
-- never entered in the first place. #188 makes this function the one thing
-- that decides which jobs reach a user, over the *whole* corpus rather than
-- a pre-filtered `eligible` list, so both gaps become reachable: a job
-- discovery already rejected -- on this exact title, description and
-- policy -- could be scored and delivered anyway, silently reintroducing
-- exactly what the prefilter gate exists to keep out. #187's own design
-- doc already named prefilter's output as "a stored fact" this operation
-- should read, not recompute; `status` is that fact, and this is where it
-- gets read.
--
-- Two added predicates. Re-declares the function's SQL body unchanged
-- otherwise -- everything else here is a straight copy of
-- 20260910140000_job_hunter_match_jobs.sql's `job_hunter_match_jobs`.

create or replace function public.job_hunter_match_jobs(
  p_preferred_roles text[] default '{}',
  p_preferred_seniority text[] default '{}',
  p_must_have_signals text[] default '{}',
  p_nice_to_have_signals text[] default '{}',
  p_preferred_locations text[] default '{}',
  p_avoid_signals text[] default '{}'
) returns table (
  job_id uuid,
  posting_id uuid,
  score integer,
  hard_blockers text[],
  has_facets boolean
)
language sql
stable
security invoker
set search_path = ''
as $$
  with profile as (
    select sp.*
      from public.job_hunter_search_profiles sp
     where sp.user_id = (select auth.uid())
  ),
  markets as (
    select m.market_id, m.locations, m.remote_policy, m.relocation_policy,
           m.currency, m.gross_base_floor, m.location_floors,
           row_number() over (partition by m.profile_id order by m.position) - 1 as market_index,
           count(*) over (partition by m.profile_id) as market_count
      from public.job_hunter_search_profile_markets m
      join profile p on p.id = m.profile_id
  ),
  rows as (
    select
      j.id as row_job_id,
      j.posting_id as row_posting_id,
      p.title as row_title,
      p.description as row_description,
      p.location as row_location,
      p.remote as row_remote,
      p.source as row_source,
      p.url as row_url,
      p.company as row_company,
      p.normalized_company as row_normalized_company,
      p.content_confidence as row_content_confidence,
      mk.locations as market_locations,
      mk.remote_policy as market_remote_policy,
      mk.relocation_policy as market_relocation_policy,
      mk.currency as market_currency,
      mk.gross_base_floor as market_gross_base_floor,
      mk.location_floors as market_location_floors,
      mk.market_index,
      mk.market_count,
      f.posting_id is not null as row_has_facets,
      f.compensation_disclosed, f.compensation_currency, f.compensation_max, f.compensation_period,
      f.remote_policy as facet_remote_policy, f.relocation_policy as facet_relocation_policy,
      c.industry as company_industry, c.business_model as company_business_model,
      c.stage as company_stage, c.size_band as company_size_band,
      prof.salary_floor_eur, prof.specialist_board_hosts, prof.frontend_signals,
      prof.backend_heavy_signals, prof.preferred_industries, prof.excluded_industries,
      prof.preferred_business_models, prof.excluded_business_models,
      prof.preferred_company_stages, prof.preferred_company_sizes
    from public.job_hunter_jobs j
    join public.job_hunter_postings p on p.id = j.posting_id
    left join public.job_hunter_job_facets f on f.posting_id = j.posting_id
    left join public.job_hunter_companies c on c.identity = p.normalized_company
    join profile prof on true
    left join markets mk on mk.market_id = j.market_id
   where j.user_id = (select auth.uid())
     -- Issue #188: a posting a freshness re-check found gone must never be
     -- ranked into a result the caller might deliver.
     and p.closed_at is null
     -- Issue #188: prefilter already said no for this exact job under this
     -- exact policy (`status = 'rejected'`), or discovery found the
     -- specific listing unavailable (`status = 'closed'`, distinct from a
     -- closed posting -- this is set per membership row, before #186's
     -- posting-level freshness tracking existed). Either way, the decision
     -- already made is what this operation reads, not a decision to redo.
     and j.status not in ('rejected', 'closed')
  )
  select
    row_job_id,
    row_posting_id,
    greatest(0, least(100,
      public.job_hunter_role_seniority_fit(row_title, p_preferred_roles, p_preferred_seniority)
      + public.job_hunter_signal_coverage(row_title, row_description, p_must_have_signals, p_nice_to_have_signals)
      + public.job_hunter_market_location_fit(
          row_location, row_description, row_remote, market_locations, market_remote_policy, p_preferred_locations)
      + public.job_hunter_source_quality(row_source, row_url, specialist_board_hosts)
      + coalesce(greatest(0, market_count - market_index), 0)
      + public.job_hunter_company_fit(
          company_industry, company_business_model, company_stage, company_size_band,
          preferred_industries, excluded_industries, preferred_business_models,
          excluded_business_models, preferred_company_stages, preferred_company_sizes)
      - public.job_hunter_avoid_signal_penalty(row_title, row_location, row_description, p_avoid_signals)
      - public.job_hunter_backend_transition_penalty(
          row_title, row_description, frontend_signals, backend_heavy_signals)
    )) as score,
    case when not public.job_hunter_content_confidence_sufficient(row_content_confidence)
         then '{}'::text[]
         else public.job_hunter_hard_blockers(
                compensation_disclosed, compensation_currency, compensation_max, compensation_period,
                facet_remote_policy, facet_relocation_policy,
                case when market_currency is null then 'EUR' else market_currency end,
                case when market_currency is null then salary_floor_eur
                     else public.job_hunter_salary_floor_for_job(row_location, market_location_floors, market_gross_base_floor)
                end,
                case when market_currency is null then true else market_remote_policy = 'required' end,
                case when market_currency is null then false
                     else market_relocation_policy in ('selective', 'allowed') end
              )
    end as hard_blockers,
    row_has_facets as has_facets
  from rows
  order by score desc, lower(row_company), lower(row_title), row_job_id;
$$;

comment on function public.job_hunter_match_jobs(text[], text[], text[], text[], text[], text[]) is
  'The one matching operation (#187): every open, non-rejected membership '
  'row the caller holds, ranked by ranking.profile_priority_score''s SQL '
  'port and flagged with hard_blockers.hard_blockers_from_facets''s. '
  'RLS-scoped by auth.uid(); the caller bounds how many unblocked, '
  'has_facets rows it sends to the model. A closed posting (#186) or a row '
  'discovery''s prefilter already rejected (#188) is excluded outright -- '
  'never ranked, never scored, never a row a caller could deliver.';

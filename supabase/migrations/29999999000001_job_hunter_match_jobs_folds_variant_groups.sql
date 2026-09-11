-- job_hunter_match_jobs folds a variant group's membership rows into one
-- result (issue #61).
--
-- Re-created from 20260910150000 with one change: rows are ranked and
-- scored exactly as before, then folded by variant group (a posting with no
-- group is its own group of one, via coalesce(variant_group_id, posting_id))
-- before the final ordering. Within a group the best-scored membership row
-- the caller holds represents it -- deterministic tie-break, same ordering
-- as the un-folded ranking -- and carries the open locations of every
-- variant in that group the caller holds a row for (closed postings and
-- rejected/closed membership rows are already excluded by `rows`, so a
-- closed variant drops out of its group's locations on its own, and a group
-- whose every variant is closed never produces a row at all).
--
-- This deliberately reads locations only from the rows the caller's own
-- `rows` CTE already produced, not from every posting in the group
-- corpus-wide: eligibility stays per variant (#61's decision 4), so a
-- caller only ever sees the locations of the variants they are actually
-- eligible for.
--
-- Return columns change (`locations` is new), so this drops and recreates
-- rather than replacing in place.

drop function if exists public.job_hunter_match_jobs(text[], text[], text[], text[], text[], text[]);

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
  has_facets boolean,
  locations text[]
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
      coalesce(p.variant_group_id, p.id) as row_group_key,
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
  ),
  scored as (
    select
      rows.*,
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
      )) as row_score,
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
      end as row_hard_blockers
    from rows
  ),
  grouped_locations as (
    select row_group_key,
           array_agg(distinct row_location order by row_location) as group_locations
      from scored
     group by row_group_key
  ),
  ranked as (
    select s.*,
           row_number() over (
             partition by s.row_group_key
             order by s.row_score desc, lower(s.row_company), lower(s.row_title), s.row_job_id
           ) as row_rank
      from scored s
  )
  select
    r.row_job_id as job_id,
    r.row_posting_id as posting_id,
    r.row_score as score,
    r.row_hard_blockers as hard_blockers,
    r.row_has_facets as has_facets,
    gl.group_locations as locations
  from ranked r
  join grouped_locations gl on gl.row_group_key = r.row_group_key
  where r.row_rank = 1
  order by r.row_score desc, lower(r.row_company), lower(r.row_title), r.row_job_id;
$$;

comment on function public.job_hunter_match_jobs(text[], text[], text[], text[], text[], text[]) is
  'The one matching operation (#187), folded by variant group (#61): every '
  'open, non-rejected membership row the caller holds, ranked by '
  'ranking.profile_priority_score''s SQL port and flagged with '
  'hard_blockers.hard_blockers_from_facets''s, then reduced to one row per '
  'variant group -- the group''s best-ranked row, carrying every open '
  'location in the group the caller holds a row for. A posting with no '
  'variant group is its own group of one. RLS-scoped by auth.uid(); the '
  'caller bounds how many unblocked, has_facets rows it sends to the model.';

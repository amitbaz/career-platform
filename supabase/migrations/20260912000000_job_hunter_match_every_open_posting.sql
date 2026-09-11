-- Match every open posting without crawl-time membership or a title gate
-- (issue #243). Design: apps/job-hunter/docs/superpowers/specs/
-- 2026-09-11-match-every-open-posting-design.md.
--
-- Placeholder timestamp per AGENTS.md's migration-numbering rule; renumbered
-- to a real YYYYMMDDHHMMSS at PR time.
--
-- Three additions and one rewrite:
--
--   1. job_hunter_regions_for_locations -- the alias-to-region half of
--      hiring_scope.py, ported to SQL (the prose scope-cue extraction stays
--      Python-only and already ran once at enrichment time; its answer is
--      job_hunter_job_facets.hiring_regions).
--   2. job_hunter_hard_blockers gains a fourth check: a posting's stated
--      hiring regions against the regions the user's configured market
--      locations resolve to.
--   3. job_hunter_ensure_job_membership -- the insert-or-touch half of
--      job_hunter_upsert_job, without the identity-resolution machinery a
--      freshly-discovered listing needs. security invoker: job_hunter_jobs
--      is not one of the seven shared tables #179 moved behind the
--      privileged connection (confirmed: 20260909220000_
--      job_hunter_shared_table_writers.sql revokes nothing on
--      job_hunter_jobs itself), so this follows job_hunter_set_job_markets's
--      precedent (202609070003_job_hunter_batch_discovery_writes.sql)
--      rather than job_hunter_upsert_job's.
--   4. job_hunter_match_jobs is rewritten to drive from job_hunter_postings,
--      left-joining job_hunter_jobs rather than requiring it, and to bound
--      the newly-reachable full corpus with p_limit. job_hunter_match_state_
--      counts is new: counts (not full rows) of every ineligible/qualified/
--      unresolved posting, for the AC9 "an empty result must carry its
--      reason" report a bounded ranked call cannot give on its own.

-- job_hunter_regions_for_locations ---------------------------------------------
--
-- hiring_scope._REGION_ALIASES / _REGION_ACRONYMS, ported as data. Matching
-- configured place names (not prose) needs none of hiring_scope.py's
-- scope-cue/negation machinery -- regions_for_locations already reads
-- acronyms case-insensitively for exactly this reason ("configuration, not
-- prose"), so a bare "us" is a plain alias here too, with no pronoun guard.

create or replace function public.job_hunter_regions_for_locations(p_locations text[])
returns text[]
language sql
immutable
security invoker
set search_path = ''
as $$
  with joined as (
    select public.job_hunter_normalize_text(
             array_to_string(coalesce(p_locations, '{}'::text[]), ' ')
           ) as h
  ),
  aliases (region, alias) as (
    values
      ('north_america', 'north america'), ('north_america', 'united states'),
      ('north_america', 'united states of america'), ('north_america', 'canada'),
      ('north_america', 'new york'), ('north_america', 'new york city'),
      ('north_america', 'brooklyn'), ('north_america', 'san francisco'),
      ('north_america', 'bay area'), ('north_america', 'silicon valley'),
      ('north_america', 'seattle'), ('north_america', 'austin'),
      ('north_america', 'boston'), ('north_america', 'chicago'),
      ('north_america', 'denver'), ('north_america', 'los angeles'),
      ('north_america', 'toronto'), ('north_america', 'vancouver'),
      ('north_america', 'montreal'), ('north_america', 'us'),
      ('north_america', 'usa'), ('north_america', 'nyc'), ('north_america', 'sf'),

      ('europe', 'europe'), ('europe', 'european union'),
      ('europe', 'european economic area'), ('europe', 'germany'),
      ('europe', 'berlin'), ('europe', 'munich'), ('europe', 'hamburg'),
      ('europe', 'frankfurt'), ('europe', 'cologne'),
      ('europe', 'united kingdom'), ('europe', 'great britain'),
      ('europe', 'england'), ('europe', 'scotland'), ('europe', 'wales'),
      ('europe', 'london'), ('europe', 'manchester'), ('europe', 'ireland'),
      ('europe', 'dublin'), ('europe', 'netherlands'), ('europe', 'amsterdam'),
      ('europe', 'rotterdam'), ('europe', 'utrecht'), ('europe', 'france'),
      ('europe', 'paris'), ('europe', 'lyon'), ('europe', 'spain'),
      ('europe', 'madrid'), ('europe', 'barcelona'), ('europe', 'valencia'),
      ('europe', 'portugal'), ('europe', 'lisbon'), ('europe', 'porto'),
      ('europe', 'italy'), ('europe', 'milan'), ('europe', 'rome'),
      ('europe', 'poland'), ('europe', 'warsaw'), ('europe', 'krakow'),
      ('europe', 'czech republic'), ('europe', 'czechia'), ('europe', 'prague'),
      ('europe', 'austria'), ('europe', 'vienna'), ('europe', 'switzerland'),
      ('europe', 'zurich'), ('europe', 'geneva'), ('europe', 'belgium'),
      ('europe', 'brussels'), ('europe', 'denmark'), ('europe', 'copenhagen'),
      ('europe', 'sweden'), ('europe', 'stockholm'), ('europe', 'norway'),
      ('europe', 'oslo'), ('europe', 'finland'), ('europe', 'helsinki'),
      ('europe', 'estonia'), ('europe', 'tallinn'), ('europe', 'romania'),
      ('europe', 'bucharest'), ('europe', 'bulgaria'), ('europe', 'sofia'),
      ('europe', 'greece'), ('europe', 'athens'), ('europe', 'hungary'),
      ('europe', 'budapest'), ('europe', 'eu'), ('europe', 'uk'),
      ('europe', 'emea'), ('europe', 'eea'),

      ('middle_east', 'middle east'), ('middle_east', 'israel'),
      ('middle_east', 'tel aviv'), ('middle_east', 'jerusalem'),
      ('middle_east', 'haifa'),

      ('asia_pacific', 'asia pacific'), ('asia_pacific', 'southeast asia'),
      ('asia_pacific', 'south east asia'), ('asia_pacific', 'singapore'),
      ('asia_pacific', 'japan'), ('asia_pacific', 'tokyo'),
      ('asia_pacific', 'australia'), ('asia_pacific', 'sydney'),
      ('asia_pacific', 'melbourne'), ('asia_pacific', 'new zealand'),
      ('asia_pacific', 'india'), ('asia_pacific', 'bangalore'),
      ('asia_pacific', 'bengaluru'), ('asia_pacific', 'hyderabad'),
      ('asia_pacific', 'china'), ('asia_pacific', 'hong kong'),
      ('asia_pacific', 'south korea'), ('asia_pacific', 'seoul'),
      ('asia_pacific', 'indonesia'), ('asia_pacific', 'jakarta'),
      ('asia_pacific', 'philippines'), ('asia_pacific', 'manila'),
      ('asia_pacific', 'vietnam'), ('asia_pacific', 'thailand'),
      ('asia_pacific', 'malaysia'), ('asia_pacific', 'kuala lumpur'),
      ('asia_pacific', 'apac')
  )
  select coalesce(array_agg(distinct a.region order by a.region), '{}'::text[])
    from aliases a, joined j
   where j.h <> ''
     and j.h ~ ('\m' || public.job_hunter_regexp_escape(
                           public.job_hunter_normalize_text(a.alias)) || '\M');
$$;

comment on function public.job_hunter_regions_for_locations(text[]) is
  'The regions (north_america/europe/middle_east/asia_pacific) a set of '
  'configured place names belongs to -- the alias-to-region half of '
  'hiring_scope.py, for comparing against job_hunter_job_facets.hiring_regions '
  '(#243). An unrecognised or empty input resolves to {}, which callers must '
  'read as no constraint, never as no region.';

-- job_hunter_hard_blockers gains a hiring-region check --------------------------
--
-- Drop-and-recreate: the signature changes (two new parameters), so this is
-- not an in-place replace, matching the house style
-- (20260910170000's own header note).

drop function if exists public.job_hunter_hard_blockers(
  boolean, text, bigint, text, text, text, text, bigint, boolean, boolean);

create or replace function public.job_hunter_hard_blockers(
  p_compensation_disclosed boolean, p_compensation_currency text,
  p_compensation_max bigint, p_compensation_period text,
  p_facet_remote_policy text, p_facet_relocation_policy text,
  p_threshold_currency text, p_threshold_salary_floor bigint,
  p_threshold_remote_required boolean, p_threshold_relocation_allowed boolean,
  p_hiring_regions text[], p_market_regions text[]
) returns text[]
language sql
immutable
security invoker
set search_path = ''
as $$
  select array_remove(array[
    case when coalesce(p_compensation_disclosed, false)
              and p_compensation_max is not null
              and p_compensation_currency = p_threshold_currency
              and p_compensation_period = 'year'
              and p_compensation_max < p_threshold_salary_floor
         then format('disclosed compensation maximum %s %s is below the %s %s floor',
                      p_compensation_currency, p_compensation_max,
                      p_threshold_currency, p_threshold_salary_floor)
    end,
    case when p_threshold_remote_required and p_facet_remote_policy in ('hybrid', 'onsite')
         then format('posting states a %s role, and remote is required', p_facet_remote_policy)
    end,
    case when not p_threshold_relocation_allowed and p_facet_relocation_policy = 'required'
         then 'posting requires relocation'
    end,
    -- #243: a posting's explicit hiring-region statement, checked against the
    -- regions the user's configured market locations resolve to. Fails open
    -- (never fires) when either side is empty -- an unstated posting scope or
    -- an unresolved/unconfigured market location is absence of evidence, not
    -- a confirmed conflict (CONTEXT.md's "Unknown" rule; hiring_scope.
    -- HiringScope.permits documents the same fail-open reading).
    case when array_length(p_hiring_regions, 1) is not null
          and array_length(p_market_regions, 1) is not null
          and not (p_hiring_regions && p_market_regions)
         then format('posting states hiring regions {%s}, outside the market''s {%s}',
                      array_to_string(p_hiring_regions, ', '),
                      array_to_string(p_market_regions, ', '))
    end
  ], null);
$$;

comment on function public.job_hunter_hard_blockers(
  boolean, text, bigint, text, text, text, text, bigint, boolean, boolean, text[], text[]
) is
  'hard_blockers.hard_blockers_from_facets, plus #243''s hiring-region check: '
  'compensation disclosed below the user''s floor, a role that is not remote '
  'or requires relocation contrary to policy, and a posting whose stated '
  'hiring regions exclude every region the user''s configured market '
  'locations resolve to. Every check fails open on missing evidence.';

-- job_hunter_ensure_job_membership -----------------------------------------------
--
-- The insert-or-touch half of job_hunter_upsert_job (20260909210000, lines
-- 940-953), without the identity-resolution machinery a freshly-discovered
-- listing needs -- matching already knows the exact posting_id from its own
-- ranking. security invoker, following job_hunter_set_job_markets's
-- precedent: job_hunter_jobs already grants insert_own/update_own to
-- authenticated and is not one of the seven tables #179 moved behind the
-- privileged connection.
--
-- Deliberately not a re-sighting: unlike upsert_job, a second call for a
-- posting matching has already acted on does not bump last_seen_at. That
-- column means "the crawl last saw this posting for this user", which is not
-- what a second matching call over the same corpus is.

create or replace function public.job_hunter_ensure_job_membership(
  p_posting_id uuid, p_market_id text default ''
) returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_uid uuid := (select auth.uid());
  v_now timestamptz := clock_timestamp();
  v_job_id uuid;
begin
  insert into public.job_hunter_jobs as ins
    (user_id, posting_id, market_id, status, first_seen_at, last_seen_at, created_at)
  values (v_uid, p_posting_id, coalesce(p_market_id, ''), 'new', v_now, v_now, v_now)
  on conflict (user_id, posting_id) do nothing
  returning ins.id into v_job_id;

  if v_job_id is null then
    select j.id into v_job_id
      from public.job_hunter_jobs j
     where j.user_id = v_uid and j.posting_id = p_posting_id;
  end if;

  return v_job_id;
end $$;

comment on function public.job_hunter_ensure_job_membership(uuid, text) is
  'Per-user membership as an output of matching (#243), not a precondition: '
  'creates job_hunter_jobs only for a posting matching actually decided to '
  'act on (score or block), idempotently. Not a re-sighting -- a second call '
  'for the same posting returns the existing row unchanged.';

-- job_hunter_match_jobs, rewritten -------------------------------------------------
--
-- Drop-and-recreate: the signature gains p_limit and the return type gains
-- market_id, so this is not an in-place replace.

drop function if exists public.job_hunter_match_jobs(text[], text[], text[], text[], text[], text[]);

create or replace function public.job_hunter_match_jobs(
  p_preferred_roles text[] default '{}',
  p_preferred_seniority text[] default '{}',
  p_must_have_signals text[] default '{}',
  p_nice_to_have_signals text[] default '{}',
  p_preferred_locations text[] default '{}',
  p_avoid_signals text[] default '{}',
  p_limit integer default 100
) returns table (
  job_id uuid,
  posting_id uuid,
  market_id text,
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
           -- #243: computed once per configured market (typically one or
           -- two per user), not once per posting -- the hiring-region check
           -- below used to call this per row, which turned an 80-alias
           -- cross join into an O(corpus) cost for no reason: the same
           -- market locations produce the same regions on every row.
           public.job_hunter_regions_for_locations(m.locations) as market_regions,
           row_number() over (partition by m.profile_id order by m.position) - 1 as market_index,
           count(*) over (partition by m.profile_id) as market_count
      from public.job_hunter_search_profile_markets m
      join profile p on p.id = m.profile_id
  ),
  -- #243 / AC10: the never-discovered surface is the whole shared corpus,
  -- which the caller's own known rows never were -- so it is pre-bounded
  -- HERE, before a single expensive per-row ranking function runs, not only
  -- at the final `limit p_limit` (which would still have paid to *rank*
  -- every open posting first). Measured on a corpus of ~61k open postings:
  -- selecting this bounded candidate set costs ~35ms regardless of corpus
  -- size (an index-backed anti-join plus a top-N heapsort); the seven
  -- per-row ranking functions below cost roughly 1.4ms *per candidate row*,
  -- which is what actually has to stay bounded. `p_limit * 5` keeps that at
  -- a few hundred milliseconds even at `p_limit`'s default, while still
  -- giving the final fold (variant grouping, per-market reduction) enough
  -- candidates per posting to pick a good representative from -- corpus
  -- growth changes nothing here: the candidate set size is a function of
  -- p_limit alone. Freshest-first (`last_seen_at desc`) is the ordering the
  -- crawl itself already produces and needs no additional index to serve;
  -- it is a proxy for "worth ranking", not a claim about quality, which
  -- #260's ready pool owns.
  candidate_postings as (
    -- union all, not union: the two branches are mutually exclusive by
    -- construction (the second explicitly excludes anything the first
    -- would already carry), so there is nothing to deduplicate and no
    -- reason to pay for the sort/dedup union would add.
    (select p.id
       from public.job_hunter_postings p
       join public.job_hunter_jobs j2
         on j2.posting_id = p.id and j2.user_id = (select auth.uid())
      where p.closed_at is null)
    union all
    (select p.id
       from public.job_hunter_postings p
      where p.closed_at is null
        and not exists (
              select 1 from public.job_hunter_jobs j3
               where j3.posting_id = p.id and j3.user_id = (select auth.uid())
            )
      order by p.last_seen_at desc
      limit greatest(p_limit, 1) * 5)
  ),
  -- #243: driven by every open posting the caller already has membership in
  -- plus the bounded never-discovered candidate set above -- not the
  -- caller's own membership rows alone. left join job_hunter_jobs so a
  -- never-discovered posting still produces a row (job_id null). A row with
  -- an existing membership keeps its stored market attribution exactly as
  -- before (the lateral's first branch); a row with none is evaluated
  -- against every one of the user's configured markets, or the single
  -- synthetic no-market row when they have none -- see the design doc's
  -- "Matching without a prior membership row".
  rows as (
    select
      j.id as row_job_id,
      p.id as row_posting_id,
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
      mk.market_id as row_market_id,
      mk.locations as market_locations,
      mk.remote_policy as market_remote_policy,
      mk.relocation_policy as market_relocation_policy,
      mk.currency as market_currency,
      mk.gross_base_floor as market_gross_base_floor,
      mk.location_floors as market_location_floors,
      mk.market_regions,
      mk.market_index,
      mk.market_count,
      f.posting_id is not null as row_has_facets,
      f.compensation_disclosed, f.compensation_currency, f.compensation_max, f.compensation_period,
      f.remote_policy as facet_remote_policy, f.relocation_policy as facet_relocation_policy,
      f.hiring_regions as facet_hiring_regions,
      c.industry as company_industry, c.business_model as company_business_model,
      c.stage as company_stage, c.size_band as company_size_band,
      prof.salary_floor_eur, prof.specialist_board_hosts, prof.frontend_signals,
      prof.backend_heavy_signals, prof.preferred_industries, prof.excluded_industries,
      prof.preferred_business_models, prof.excluded_business_models,
      prof.preferred_company_stages, prof.preferred_company_sizes
    from candidate_postings cp
    join public.job_hunter_postings p on p.id = cp.id
    join profile prof on true
    left join public.job_hunter_jobs j
      on j.posting_id = p.id and j.user_id = (select auth.uid())
    left join public.job_hunter_job_facets f on f.posting_id = p.id
    left join public.job_hunter_companies c on c.identity = p.normalized_company
    left join lateral (
      select mk2.market_id, mk2.locations, mk2.remote_policy, mk2.relocation_policy,
             mk2.currency, mk2.gross_base_floor, mk2.location_floors, mk2.market_regions,
             mk2.market_index, mk2.market_count
        from markets mk2
       where (j.id is not null and mk2.market_id = j.market_id)
          or j.id is null
    ) mk on true
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
                  -- Unchanged from the pre-#243 fallback (20260910140000):
                  -- a user with no configured market -- whether never
                  -- configured at all, or attributed to a market_id that no
                  -- longer matches one -- is read as requiring remote and
                  -- disallowing relocation, same as before. Revisiting this
                  -- specific threshold is out of #243's scope: it is not a
                  -- missing *fact* the new hiring-region/facet checks are
                  -- about, and the still-active legacy pipeline's own test
                  -- coverage depends on it unchanged.
                  case when market_currency is null then true else market_remote_policy = 'required' end,
                  case when market_currency is null then false else market_relocation_policy in ('selective', 'allowed') end,
                  facet_hiring_regions,
                  market_regions
                )
      end as row_hard_blockers
    from rows
  ),
  -- #243: reduce the (posting x market) fan-out to one candidate per
  -- posting before variant-group folding -- otherwise a never-discovered
  -- posting evaluated against N configured markets would look like N
  -- distinct rows to the fold below. Same preference as the fold one level
  -- up: an eligible market over a higher score under a blocking one.
  market_reduced as (
    select s.*,
           row_number() over (
             partition by s.row_posting_id
             order by (s.row_has_facets and cardinality(s.row_hard_blockers) = 0) desc,
                      s.row_score desc
           ) as row_market_rank
      from scored s
  ),
  reduced as (
    select * from market_reduced where row_market_rank = 1
  ),
  grouped_locations_all as (
    select row_group_key,
           array_agg(distinct row_location order by row_location) as locations
      from reduced
     group by row_group_key
  ),
  grouped_locations_unblocked as (
    select row_group_key,
           array_agg(distinct row_location order by row_location) as locations
      from reduced
     where cardinality(row_hard_blockers) = 0
     group by row_group_key
  ),
  grouped_locations as (
    select a.row_group_key,
           coalesce(u.locations, a.locations) as group_locations
      from grouped_locations_all a
      left join grouped_locations_unblocked u on u.row_group_key = a.row_group_key
  ),
  ranked as (
    select s.*,
           row_number() over (
             partition by s.row_group_key
             order by (s.row_has_facets and cardinality(s.row_hard_blockers) = 0) desc,
                      s.row_score desc, lower(s.row_company), lower(s.row_title), s.row_posting_id
           ) as row_rank
      from reduced s
  ),
  final_rows as (
    select
      r.row_job_id as job_id,
      r.row_posting_id as posting_id,
      r.row_market_id as market_id,
      r.row_score as score,
      r.row_hard_blockers as hard_blockers,
      r.row_has_facets as has_facets,
      gl.group_locations as locations,
      r.row_company as company,
      r.row_title as title
    from ranked r
    join grouped_locations gl on gl.row_group_key = r.row_group_key
   where r.row_rank = 1
  ),
  -- #243's bound (AC10): every posting the caller already holds a membership
  -- row for comes back in full, exactly as before -- that set is naturally
  -- bounded by this user's own discovery history, which is the pre-#243
  -- behaviour this ticket does not touch. A never-discovered posting is new
  -- surface area (the whole shared corpus), so it is capped at p_limit,
  -- ranked, and unresolved (not row_has_facets) never counts against that
  -- cap at all -- see job_hunter_match_state_counts for how those are
  -- reported instead of returned.
  bounded as (
    (select * from final_rows where job_id is not null)
    union all
    (select * from final_rows where job_id is null and has_facets
      order by score desc
      limit p_limit)
  )
  select job_id, posting_id, market_id, score, hard_blockers, has_facets, locations
    from bounded
   order by score desc, lower(company), lower(title), job_id nulls last;
$$;

comment on function public.job_hunter_match_jobs(
  text[], text[], text[], text[], text[], text[], integer
) is
  'The matching operation (#187, #243), folded by variant group (#61): every '
  'open posting -- not only ones the caller already holds a membership row '
  'for -- ranked by ranking.profile_priority_score''s SQL port and flagged '
  'with hard_blockers.hard_blockers_from_facets''s (plus #243''s hiring-region '
  'check), then reduced to one row per posting (preferring an eligible '
  'configured market) and then to one row per variant group. Every row the '
  'caller already has a membership row for comes back in full; a '
  'never-discovered but has_facets posting is capped at p_limit, ranked -- '
  'an unresolved posting (no facets yet) is never included here at all, only '
  'counted by job_hunter_match_state_counts. RLS-scoped by auth.uid().';

-- job_hunter_match_state_counts ---------------------------------------------------
--
-- AC9 / AGENTS.md rule 5 ("an empty result must carry its reason"): a bounded
-- ranked call cannot itself say how many postings were ineligible or
-- unresolved without paging through the whole corpus. This runs the same
-- classification with no p_limit and returns aggregates only -- no row
-- bodies, so it stays cheap even though it necessarily scans every open
-- posting once.

create or replace function public.job_hunter_match_state_counts(
  p_preferred_roles text[] default '{}',
  p_preferred_seniority text[] default '{}',
  p_must_have_signals text[] default '{}',
  p_nice_to_have_signals text[] default '{}',
  p_preferred_locations text[] default '{}',
  p_avoid_signals text[] default '{}'
) returns table (
  state text,
  reason text,
  count integer
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
           -- #243: computed once per configured market (typically one or
           -- two per user), not once per posting -- the hiring-region check
           -- below used to call this per row, which turned an 80-alias
           -- cross join into an O(corpus) cost for no reason: the same
           -- market locations produce the same regions on every row.
           public.job_hunter_regions_for_locations(m.locations) as market_regions,
           row_number() over (partition by m.profile_id order by m.position) - 1 as market_index,
           count(*) over (partition by m.profile_id) as market_count
      from public.job_hunter_search_profile_markets m
      join profile p on p.id = m.profile_id
  ),
  rows as (
    select
      p.id as row_posting_id,
      p.title as row_title,
      p.description as row_description,
      p.location as row_location,
      p.remote as row_remote,
      p.source as row_source,
      p.url as row_url,
      coalesce(p.variant_group_id, p.id) as row_group_key,
      mk.locations as market_locations,
      mk.remote_policy as market_remote_policy,
      mk.relocation_policy as market_relocation_policy,
      mk.currency as market_currency,
      mk.gross_base_floor as market_gross_base_floor,
      mk.location_floors as market_location_floors,
      mk.market_regions,
      p.content_confidence as row_content_confidence,
      f.posting_id is not null as row_has_facets,
      f.compensation_disclosed, f.compensation_currency, f.compensation_max, f.compensation_period,
      f.remote_policy as facet_remote_policy, f.relocation_policy as facet_relocation_policy,
      f.hiring_regions as facet_hiring_regions,
      prof.salary_floor_eur
    from public.job_hunter_postings p
    join profile prof on true
    left join public.job_hunter_jobs j
      on j.posting_id = p.id and j.user_id = (select auth.uid())
    left join public.job_hunter_job_facets f on f.posting_id = p.id
    left join lateral (
      select mk2.market_id, mk2.locations, mk2.remote_policy, mk2.relocation_policy,
             mk2.currency, mk2.gross_base_floor, mk2.location_floors, mk2.market_regions
        from markets mk2
       where (j.id is not null and mk2.market_id = j.market_id)
          or j.id is null
    ) mk on true
   where p.closed_at is null
  ),
  classified as (
    select
      rows.row_posting_id, rows.row_group_key,
      rows.row_has_facets,
      case when not public.job_hunter_content_confidence_sufficient(row_content_confidence)
           then '{}'::text[]
           else public.job_hunter_hard_blockers(
                  compensation_disclosed, compensation_currency, compensation_max, compensation_period,
                  facet_remote_policy, facet_relocation_policy,
                  case when market_currency is null then 'EUR' else market_currency end,
                  case when market_currency is null then salary_floor_eur
                       else public.job_hunter_salary_floor_for_job(row_location, market_location_floors, market_gross_base_floor)
                  end,
                  -- Unchanged from the pre-#243 fallback -- see
                  -- job_hunter_match_jobs's own copy of this comment.
                  case when market_currency is null then true else market_remote_policy = 'required' end,
                  case when market_currency is null then false else market_relocation_policy in ('selective', 'allowed') end,
                  facet_hiring_regions,
                  market_regions
                )
      end as row_hard_blockers,
      row_content_confidence
    from rows
  ),
  reduced as (
    select c.*,
           row_number() over (
             partition by c.row_posting_id
             order by (c.row_has_facets and cardinality(c.row_hard_blockers) = 0) desc
           ) as row_market_rank
      from classified c
  ),
  states as (
    -- One row per posting (not per variant group -- state counts are about
    -- postings, and folding by group here would undercount every
    -- non-representative variant, which is exactly the information #259's
    -- enrichment backlog and this ticket's audit both need).
    select
      row_posting_id,
      case
        when not row_has_facets or not public.job_hunter_content_confidence_sufficient(row_content_confidence)
          then 'unresolved'
        when cardinality(row_hard_blockers) > 0
          then 'ineligible'
        else 'qualified'
      end as state,
      case
        when not row_has_facets then 'no_facets'
        when not public.job_hunter_content_confidence_sufficient(row_content_confidence)
          then 'low_content_confidence'
        else null
      end as unresolved_reason,
      row_hard_blockers
    from reduced
   where row_market_rank = 1
  ),
  reasons as (
    select state, unresolved_reason as reason from states where state = 'unresolved'
    union all
    select state, unnest(row_hard_blockers) as reason from states where state = 'ineligible'
    union all
    select state, null::text as reason from states where state = 'qualified'
  )
  select state, reason, count(*)::integer
    from reasons
   group by state, reason;
$$;

comment on function public.job_hunter_match_state_counts(
  text[], text[], text[], text[], text[], text[]
) is
  'Aggregate counts of every open posting''s classification -- ineligible, '
  'qualified or unresolved -- with a per-reason breakdown, for AC9''s "an '
  'empty result must carry its reason". Counted per posting, not folded by '
  'variant group. RLS-scoped by auth.uid().';

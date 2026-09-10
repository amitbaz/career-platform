-- One matching operation over stored facets (issue #187, epic #114).
--
-- Deterministic ranking and hard blockers move into SQL, so they answer in
-- milliseconds over a user's whole corpus and cost nothing before a model
-- call. This is a term-for-term port of `ranking.profile_priority_score` and
-- `hard_blockers.hard_blockers_from_facets`, not a rewrite: every helper here
-- has a named Python counterpart it must keep agreeing with, verified by the
-- equivalence test in `tests/test_matching.py`.
--
-- `job_hunter_match_jobs` is the one entry point: `security invoker`, scoped
-- by `(select auth.uid())` like every other reader in this file, following
-- the house style in `job_hunter_eligible_inbound_jobs`
-- (20260909150000_job_hunter_read_posting_facts.sql). It ranks and flags
-- every membership row the caller holds; the caller decides how many of the
-- unblocked rows to send to the model (#187's `limit`).
--
-- What does NOT move here: `market_eligibility.py` and `prefilter.py`'s
-- free-text employment-type/sponsorship/language checks, and
-- `market_policy.attribute_market`. Both already run once, at discovery
-- time, over the freshly-crawled listing; their output (`job_hunter_jobs
-- .market_id`, and whether a membership row exists at all) is what this
-- operation reads as a stored fact, not something it recomputes. See
-- docs/superpowers/specs/2026-09-10-answer-matching-as-one-operation-design.md.

-- Rounding -------------------------------------------------------------------
--
-- Python's `round()` is round-half-to-even; `numeric`'s `round()` in Postgres
-- is round-half-away-from-zero. Every ranking term that scales a ratio by a
-- weight (25/20/10ths) can land on an exact .5, so the two disagree unless
-- this is ported too. All callers here pass non-negative values.

create or replace function public.job_hunter_round_half_even(p_value numeric)
returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  select case
    when p_value - floor(p_value) = 0.5 then
      case when floor(p_value)::bigint % 2 = 0
           then floor(p_value)::int
           else ceil(p_value)::int
      end
    else round(p_value)::int
  end;
$$;

-- Word-set and phrase-list helpers -------------------------------------------
--
-- `job_hunter_words`: `normalize_text(x).split()` as an array.
-- `job_hunter_normalized_phrases`: `ranking._normalized_phrases` -- each
-- value normalized, emptied values dropped, deduplicated. Order is never
-- observed by any caller here (every consumer either takes a max over the
-- set or counts matches), so array_agg(distinct ...) is a faithful port
-- despite not preserving input order.

create or replace function public.job_hunter_words(p_text text)
returns text[]
language sql
immutable
security invoker
set search_path = ''
as $$
  select coalesce(
    array_remove(string_to_array(public.job_hunter_normalize_text(p_text), ' '), ''),
    '{}'::text[]);
$$;

create or replace function public.job_hunter_normalized_phrases(p_values text[])
returns text[]
language sql
immutable
security invoker
set search_path = ''
as $$
  select coalesce(
    array_agg(distinct n) filter (where n <> ''),
    '{}'::text[])
  from unnest(coalesce(p_values, '{}'::text[])) v(val),
       lateral (select public.job_hunter_normalize_text(v.val) as n) x;
$$;

-- ranking._role_seniority_fit -------------------------------------------------

create or replace function public.job_hunter_role_seniority_fit(
  p_title text, p_preferred_roles text[], p_preferred_seniority text[]
) returns integer
language plpgsql
immutable
security invoker
set search_path = ''
as $$
declare
  v_normalized_title text := public.job_hunter_normalize_text(p_title);
  v_title_words text[] := public.job_hunter_words(p_title);
  v_role text;
  v_role_norm text;
  v_role_words text[];
  v_best numeric := 0;
  v_overlap integer;
  v_seniority_score integer := 0;
begin
  foreach v_role in array coalesce(p_preferred_roles, '{}'::text[]) loop
    v_role_norm := public.job_hunter_normalize_text(v_role);
    if v_role_norm = '' then
      continue;
    end if;
    if v_role_norm = v_normalized_title then
      v_best := 1;
      exit;
    end if;
    -- Deduplicated, like Python's `set(role.split())`: a repeated word in the
    -- role phrase must not inflate the ratio's denominator (or, could it
    -- overlap the title, its numerator) beyond what the set-based original
    -- computes.
    select array_agg(distinct w) into v_role_words from unnest(public.job_hunter_words(v_role)) w;
    if array_length(v_role_words, 1) is null then
      continue;
    end if;
    select count(*) into v_overlap from unnest(v_role_words) w where w = any(v_title_words);
    if (v_overlap::numeric / array_length(v_role_words, 1)) > v_best then
      v_best := v_overlap::numeric / array_length(v_role_words, 1);
    end if;
  end loop;

  if exists (
    select 1 from unnest(coalesce(p_preferred_seniority, '{}'::text[])) s
     where public.job_hunter_normalize_text(s) <> ''
       and public.job_hunter_normalize_text(s) = any(v_title_words)
  ) then
    v_seniority_score := 10;
  end if;

  return least(35, public.job_hunter_round_half_even(25 * v_best) + v_seniority_score);
end;
$$;

-- ranking._signal_coverage ----------------------------------------------------

create or replace function public.job_hunter_signal_coverage(
  p_title text, p_description text, p_must_have text[], p_nice_to_have text[]
) returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  with haystack as (
    select public.job_hunter_normalize_text(coalesce(p_title, '') || ' ' || coalesce(p_description, '')) as h
  ),
  must as (select public.job_hunter_normalized_phrases(p_must_have) as arr),
  nice as (select public.job_hunter_normalized_phrases(p_nice_to_have) as arr)
  select least(30,
    case when array_length((select arr from must), 1) is null then 0
      else public.job_hunter_round_half_even(
        20.0 * (select count(*) from unnest((select arr from must)) m, haystack h where position(m in h.h) > 0)
        / array_length((select arr from must), 1))
    end
    +
    case when array_length((select arr from nice), 1) is null then 0
      else public.job_hunter_round_half_even(
        10.0 * (select count(*) from unnest((select arr from nice)) m, haystack h where position(m in h.h) > 0)
        / array_length((select arr from nice), 1))
    end
  );
$$;

-- ranking._avoid_signal_penalty ------------------------------------------------

create or replace function public.job_hunter_avoid_signal_penalty(
  p_title text, p_location text, p_description text, p_avoid_signals text[]
) returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  with haystack as (
    select public.job_hunter_normalize_text(
      coalesce(p_title, '') || ' ' || coalesce(p_location, '') || ' ' || coalesce(p_description, '')
    ) as h
  ),
  avoid as (select public.job_hunter_normalized_phrases(p_avoid_signals) as arr)
  select case when array_length((select arr from avoid), 1) is null then 0
    else public.job_hunter_round_half_even(
      10.0 * (select count(*) from unnest((select arr from avoid)) s, haystack h where position(s in h.h) > 0)
      / array_length((select arr from avoid), 1))
  end;
$$;

-- ranking._backend_transition_penalty ------------------------------------------
--
-- Unlike the preference-derived lists above, `frontend_signals` and
-- `backend_heavy_signals` come from the search profile and are matched
-- verbatim against the normalized haystack -- ranking.py never normalizes
-- them either, so neither does this.

create or replace function public.job_hunter_backend_transition_penalty(
  p_title text, p_description text, p_frontend_signals text[], p_backend_heavy_signals text[]
) returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  with norm as (
    select public.job_hunter_normalize_text(coalesce(p_title, '')) as t,
           public.job_hunter_normalize_text(coalesce(p_title, '') || ' ' || coalesce(p_description, '')) as h
  )
  select case
    when not exists (
      select 1 from norm where position('full stack' in norm.t) > 0 or position('full-stack' in norm.t) > 0
    ) then 0
    else
      case
        when (select count(*) from unnest(coalesce(p_backend_heavy_signals, '{}'::text[])) s, norm
               where position(s in norm.h) > 0) < 2 then 0
        when (select count(*) from unnest(coalesce(p_frontend_signals, '{}'::text[])) s, norm
               where position(s in norm.h) > 0) = 0 then 15
        else 6
      end
  end;
$$;

-- ranking.source_quality --------------------------------------------------------
--
-- `_ATS_HOSTS` is `ats_hosts.SUPPORTED_ATS_HOSTS` inlined as a constant --
-- adding a host there needs a matching edit here, exactly like every other
-- consumer that module's docstring already warns about.

create or replace function public.job_hunter_source_quality(
  p_source text, p_url text, p_specialist_board_hosts text[]
) returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  select case
    when exists (
      select 1 from unnest(array[
        'jobs.ashbyhq.com', 'jobs.lever.co', 'boards.greenhouse.io',
        'job-boards.greenhouse.io', 'boards.eu.greenhouse.io', 'job-boards.eu.greenhouse.io'
      ]) h where position(h in lower(coalesce(p_url, ''))) > 0
    ) then 10
    when p_source in ('ashby', 'lever', 'greenhouse') then 10
    when exists (
      select 1 from unnest(coalesce(p_specialist_board_hosts, '{}'::text[])) h
       where position(h in lower(coalesce(p_url, ''))) > 0
    ) then 8
    when p_source in ('remoteok', 'remotive', 'weworkremotely', 'arbeitnow') then 7
    when p_source = 'hackernews' then 5
    else 3
  end;
$$;

-- ranking._market_location_fit (+ _profile_location_fit fallback) -------------
--
-- `p_market_remote_policy is null` stands for both of ranking.py's fallback
-- cases -- no market_id at all, and a market_id the caller's markets no
-- longer name -- since job_hunter_match_jobs left-joins the market row and
-- passes nulls through in both.

create or replace function public.job_hunter_market_location_fit(
  p_location text, p_description text, p_remote boolean,
  p_market_locations text[], p_market_remote_policy text,
  p_preferred_locations text[]
) returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  with loc as (
    select public.job_hunter_normalize_text(coalesce(p_location, '') || ' ' || coalesce(p_description, '')) as h
  ),
  pref as (select public.job_hunter_normalized_phrases(p_preferred_locations) as arr)
  select case
    when p_market_remote_policy is null then
      case
        when p_remote is not true then 0
        when array_length((select arr from pref), 1) is null then 15
        when exists (select 1 from unnest((select arr from pref)) x, loc where position(x in loc.h) > 0) then 15
        when (select h from loc) = '' then 8
        when exists (
          select 1 from unnest(string_to_array((select h from loc), ' ')) w
           where w in ('remote', 'worldwide', 'global', 'anywhere', 'distributed')
        ) then 10
        else 5
      end
    else
      case
        when p_remote is true and p_market_remote_policy in ('preferred', 'required') then 15
        when p_remote is true then
          case when exists (
                 select 1 from unnest(coalesce(p_market_locations, '{}'::text[])) c, loc
                  where position(public.job_hunter_normalize_text(c) in loc.h) > 0
               ) then 10 else 8 end
        when exists (
          select 1 from unnest(coalesce(p_market_locations, '{}'::text[])) c, loc
           where position(public.job_hunter_normalize_text(c) in loc.h) > 0
        ) then
          case when p_market_remote_policy in ('preferred', 'required') then 12 else 10 end
        else 0
      end
  end;
$$;

-- ranking.company_fit -----------------------------------------------------------
--
-- `p_industry is null` stands for "no job_hunter_companies row" -- the
-- caller left-joins on `job_hunter_postings.normalized_company`, exactly
-- what `company_fit` looks up via `normalize_company_name(job.company)`.

create or replace function public.job_hunter_company_fit(
  p_industry text, p_business_model text, p_stage text, p_size_band text,
  p_preferred_industries text[], p_excluded_industries text[],
  p_preferred_business_models text[], p_excluded_business_models text[],
  p_preferred_stages text[], p_preferred_size_bands text[]
) returns integer
language sql
immutable
security invoker
set search_path = ''
as $$
  select case
    when coalesce(array_length(p_preferred_industries, 1), 0) = 0
     and coalesce(array_length(p_excluded_industries, 1), 0) = 0
     and coalesce(array_length(p_preferred_business_models, 1), 0) = 0
     and coalesce(array_length(p_excluded_business_models, 1), 0) = 0
     and coalesce(array_length(p_preferred_stages, 1), 0) = 0
     and coalesce(array_length(p_preferred_size_bands, 1), 0) = 0
      then 0
    when p_industry is null then 0
    when p_industry = any(coalesce(p_excluded_industries, '{}'::text[]))
      or p_business_model = any(coalesce(p_excluded_business_models, '{}'::text[]))
      then -20
    else
      (case when p_industry = any(coalesce(p_preferred_industries, '{}'::text[])) then 6 else 0 end)
      + (case when p_business_model = any(coalesce(p_preferred_business_models, '{}'::text[])) then 5 else 0 end)
      + (case when p_stage = any(coalesce(p_preferred_stages, '{}'::text[])) then 2 else 0 end)
      + (case when p_size_band = any(coalesce(p_preferred_size_bands, '{}'::text[])) then 2 else 0 end)
  end;
$$;

-- market_policy._phrase_in_text's `re.escape` ------------------------------------
--
-- A `location_floors` key goes into a `\m...\M` regex below; unescaped, a key
-- containing a regex metacharacter (a period, parentheses, ...) would match
-- more than the literal phrase, or -- unbalanced parentheses or brackets --
-- fail the query outright. `re.escape` is what market_policy.py does before
-- building the same regex in Python; this is its SQL twin.

create or replace function public.job_hunter_regexp_escape(p_text text)
returns text
language sql
immutable
security invoker
set search_path = ''
as $$
  select regexp_replace(coalesce(p_text, ''), '([.^$*+?()\[\]{}|\\])', '\\\1', 'g');
$$;

-- market_policy.salary_floor_for_job ---------------------------------------------
--
-- A city-specific floor wins when the job's normalized location names that
-- city as a whole phrase. `location_floors` is stored as jsonb, whose key
-- order is not guaranteed to match the Python dict's insertion order; the
-- Python original also returns the *first* match. Two configured cities
-- both naming the same job's location is not a case this engine's markets
-- produce today, so the two can only disagree on a collision neither side
-- is set up to have.

create or replace function public.job_hunter_salary_floor_for_job(
  p_location text, p_location_floors jsonb, p_gross_base_floor bigint
) returns bigint
language sql
immutable
security invoker
set search_path = ''
as $$
  select coalesce(
    (select round(value::numeric)::bigint
       from jsonb_each_text(coalesce(p_location_floors, '{}'::jsonb))
      where public.job_hunter_normalize_text(key) <> ''
        and public.job_hunter_normalize_text(coalesce(p_location, ''))
              ~ ('\m' || public.job_hunter_regexp_escape(public.job_hunter_normalize_text(key)) || '\M')
      limit 1),
    p_gross_base_floor
  );
$$;

-- hard_blockers.hard_blockers_from_facets ----------------------------------------

create or replace function public.job_hunter_hard_blockers(
  p_compensation_disclosed boolean, p_compensation_currency text,
  p_compensation_max bigint, p_compensation_period text,
  p_facet_remote_policy text, p_facet_relocation_policy text,
  p_threshold_currency text, p_threshold_salary_floor bigint,
  p_threshold_remote_required boolean, p_threshold_relocation_allowed boolean
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
    end
  ], null);
$$;

-- content_confidence.is_sufficient -----------------------------------------------

create or replace function public.job_hunter_content_confidence_sufficient(p_tier text)
returns boolean
language sql
immutable
security invoker
set search_path = ''
as $$
  select coalesce(p_tier, '') <> '' and p_tier <> 'partial_unknown';
$$;

-- job_hunter_match_jobs -----------------------------------------------------------
--
-- The one matching operation (#187): ranks and flags every membership row
-- the caller holds. `job_id`/`posting_id` identify the row; `score` is
-- `ranking.profile_priority_score`; `hard_blockers` is what
-- `pipeline._facet_decided_blockers` would compute for it, applied here at
-- zero cost so a blocked posting never reaches a provider call; `has_facets`
-- tells the caller whether this posting has been read at all -- a row
-- without facets is scoreable by neither this function nor `evaluate_job`,
-- and the caller must skip it rather than treat an absent fact as a pass.
--
-- Candidate preferences (`preferred_roles`, `must_have_signals`, ...) are not
-- a table: they are extracted from the CV once and cached content-addressed
-- in `job_hunter_candidate_context_cache`, so the caller reads them the way
-- `candidate_context.get_candidate_context` already does and passes the six
-- arrays in here as ordinary parameters.

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
  'The one matching operation (#187): every membership row the caller holds, '
  'ranked by ranking.profile_priority_score''s SQL port and flagged with '
  'hard_blockers.hard_blockers_from_facets''s. RLS-scoped by auth.uid(); the '
  'caller bounds how many unblocked, has_facets rows it sends to the model.';

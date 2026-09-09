-- The employer becomes an entity of its own (issue #198, epic #114).
--
-- Objective extraction records what an *advertisement* says. Nothing has so
-- far recorded what kind of company is behind it -- its industry, its
-- business model, its maturity, its rough size, where it is based -- and
-- that is the matching gap a job seeker feels most sharply: two postings
-- with identical titles, stacks and salaries can be a good match and a bad
-- one, and today they score the same.
--
-- It is also the cheapest cache the engine has. A posting's facets amortise
-- over one posting; a company's amortise over every role that employer
-- posts, for as long as the facts hold, across every user. The reference
-- run's eligible set was roughly 1,263 postings across a far smaller number
-- of employers.
--
-- Identity ---------------------------------------------------------------
--
-- Keyed on the normalization that already groups an *employer*:
-- job_identity.normalize_company_name and its SQL twin
-- job_hunter_normalize_company, which strip trailing legal suffixes so
-- "Acme Ltd" and "Acme" are one company. That is what
-- job_hunter_company_watch keys on and what canonical.py uses to decide two
-- records share an employer. It is deliberately *not* normalize_text, which
-- lowercases and collapses whitespace only and would make those two rows
-- into two employers -- satisfying "reuse the existing normalization" on its
-- face while violating it in fact. That the two normalizations disagree in
-- the live pipeline is a real pre-existing defect, tracked separately; this
-- migration does not fix it, it names the right one.
--
-- Provenance: a supplied fact must prefer silence -------------------------
--
-- Some of these facts are taken from structured data rather than asked of
-- the model -- today, the headquarters region, read off the employer's own
-- careers domain. Anything read that way has to be conservative to the point
-- of preferring to record nothing, and the reason is a property of this
-- table rather than of any one heuristic:
--
-- A fact the model produced is re-derived when the posting's description
-- hash moves. A fact taken from a source is written once and believed
-- forever, because a supplied facet is never re-asked. And this row is
-- shared, so a per-user wrong answer is annoying and self-correcting, while
-- a wrong answer here is permanent and invisible -- it is served to every
-- user, for the whole refresh interval, after which the same source
-- re-derives the same wrong value.
--
-- The first version of the domain heuristic got this wrong: it trusted any
-- host that was not one of six known ATS vendors, so a company whose only
-- posting the engine held came from a job board on a country domain was
-- recorded as headquartered wherever that board is. See `_employer_hosts` in
-- apps/job-hunter/src/job_hunter/company_facets.py, which now requires the
-- domain to spell the company's name. That condition looks over-strict in
-- isolation; the argument above is why it is not.

-- Sharing ----------------------------------------------------------------
--
-- Nothing here is per-user, so nothing here carries a user_id. Reads are
-- open to every authenticated user, exactly as job_hunter_postings and
-- job_hunter_job_facets are and for the same reason: the work is done once
-- for everyone, so everyone must be able to read the result. There is
-- deliberately no delete policy -- a company row is shared, so no single
-- user may remove one out from under the others. Narrowing the writes on
-- the shared tables to the platform identity is #179.

create table public.job_hunter_companies (
  id uuid primary key default gen_random_uuid(),

  -- job_hunter_normalize_company(display_name). The unique key: one row per
  -- employer, whoever wrote it.
  identity text not null unique,
  display_name text not null default '',

  industry text not null default 'unknown',
  business_model text not null default 'unknown',
  stage text not null default 'unknown',
  size_band text not null default 'unknown',
  headquarters_region text not null default 'unknown',

  -- Facet names taken from structured data rather than from the model, so
  -- the split between the two can be measured rather than assumed.
  source_supplied text[] not null default '{}',
  model text not null default '',

  -- Refresh is time-based, not hash-based. A company does not stop being a
  -- B2B marketplace because it edited a job advert, so there is deliberately
  -- no description_hash_at_extraction here and this row is not attached to
  -- the posting invalidation mechanism. extracted_at is the whole of it:
  -- a row older than the refresh interval is re-read, and otherwise left
  -- alone.
  extracted_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  -- Every value comes from a controlled vocabulary, and the database is
  -- where that is enforced rather than only in the extractor: a value nobody
  -- can filter on must not reach the table by any path. 'unknown' is a
  -- member of every one of them, because a missing fact is a first-class
  -- value here and never a negative one.
  constraint job_hunter_companies_industry_check check (industry in (
    'software', 'fintech', 'healthtech', 'biotech', 'retail', 'media',
    'gaming', 'education', 'energy', 'logistics', 'travel', 'mobility',
    'real_estate', 'security', 'telecom', 'manufacturing', 'agriculture',
    'government', 'nonprofit', 'professional_services', 'unknown')),
  constraint job_hunter_companies_business_model_check check (business_model in (
    'b2b_saas', 'b2c_product', 'marketplace', 'ecommerce', 'consultancy',
    'agency', 'hardware', 'deep_tech', 'open_source', 'nonprofit',
    'unknown')),
  constraint job_hunter_companies_stage_check check (stage in (
    'pre_seed', 'seed', 'series_a', 'series_b', 'series_c_plus', 'growth',
    'public', 'bootstrapped', 'established', 'unknown')),
  constraint job_hunter_companies_size_band_check check (size_band in (
    '1_10', '11_50', '51_200', '201_500', '501_1000', '1001_5000',
    '5001_plus', 'unknown')),
  constraint job_hunter_companies_headquarters_region_check check (
    headquarters_region in (
      'north_america', 'europe', 'middle_east', 'asia_pacific', 'unknown'))
);

alter table public.job_hunter_companies enable row level security;

create policy select_authenticated on public.job_hunter_companies
  for select to authenticated using (true);
create policy insert_authenticated on public.job_hunter_companies
  for insert to authenticated with check (true);
create policy update_authenticated on public.job_hunter_companies
  for update to authenticated using (true) with check (true);

-- Each dimension is filterable in a query without loading and parsing every
-- row, for the same reason the posting facets are: a future dashboard
-- browses the corpus by industry, business model and stage, and that has to
-- be an index scan.
create index job_hunter_companies_industry_idx
  on public.job_hunter_companies (industry);
create index job_hunter_companies_business_model_idx
  on public.job_hunter_companies (business_model);
create index job_hunter_companies_stage_idx
  on public.job_hunter_companies (stage);
create index job_hunter_companies_size_band_idx
  on public.job_hunter_companies (size_band);
create index job_hunter_companies_headquarters_region_idx
  on public.job_hunter_companies (headquarters_region);
-- The refresh scan: "which of these companies were last read before X".
create index job_hunter_companies_extracted_at_idx
  on public.job_hunter_companies (extracted_at);

comment on table public.job_hunter_companies is
  'Objective facts about an employer, read once per company and reused by '
  'every later run of every user: industry, business model, stage, '
  'approximate size and headquarters region. Keyed on '
  'job_hunter_normalize_company(display_name), refreshed on a long interval '
  'rather than invalidated by any posting''s description hash.';

comment on column public.job_hunter_companies.identity is
  'job_hunter_normalize_company(display_name) -- the suffix-stripping '
  'normalization that groups an employer, the same one '
  'job_hunter_company_watch keys on. Not normalize_text.';

comment on column public.job_hunter_companies.extracted_at is
  'When these facts were last read. Company facts change slowly, so a row is '
  'refreshed once it is older than the engine''s refresh interval and is '
  'otherwise left alone -- deliberately not tied to any posting''s '
  'description hash.';


-- Company preferences on the search profile -------------------------------
--
-- The user's search profile gains preferences over the same dimensions,
-- weighted like the preferences it already holds. One ranking consumes both
-- these and the posting's facets; there is no separate company-matching
-- path. Empty means "no opinion", which is why every column defaults to the
-- empty array rather than to a vocabulary value: stating nothing must leave
-- a user exactly where they were.
--
-- Values are not constrained at the column here. Unlike the extracted facts
-- above -- which are written by the engine and must never carry a value a
-- query cannot filter on -- a preference naming a vocabulary member that no
-- longer exists simply matches nothing, and refusing the whole profile
-- write over it would be a worse failure than ignoring it.

alter table public.job_hunter_search_profiles
  add column preferred_industries text[] not null default '{}',
  add column excluded_industries text[] not null default '{}',
  add column preferred_business_models text[] not null default '{}',
  add column excluded_business_models text[] not null default '{}',
  add column preferred_company_stages text[] not null default '{}',
  add column preferred_company_sizes text[] not null default '{}';

comment on column public.job_hunter_search_profiles.excluded_industries is
  'Industries this user will not work in. An excluded industry suppresses a '
  'company that is known to be in it; a company nothing is known about is '
  'never suppressed by one, because a missing fact is not a negative fact.';

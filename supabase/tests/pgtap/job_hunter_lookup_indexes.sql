-- The store functions in 202609060004 resolve identity by canonical URL, by
-- ATS triple, and by normalized company/title/location. Without an index on
-- each of those predicates every lookup is a sequential scan, which is
-- invisible against fixtures and fatal against real history -- the first
-- production run after the #70 data migration died with SQLSTATE 57014,
-- "canceling statement due to statement timeout".
--
-- These assertions pin the indexes by the expression they serve, so dropping
-- one, or rewriting a function's predicate so its index no longer matches,
-- fails here rather than in a timed-out production run.
--
-- The predicates run against job_hunter_postings since #178: identity is a
-- fact about the advertisement, so it is resolved once for everyone rather
-- than once per user, and the indexes have no user_id to lead with.

begin;
select plan(9);

select has_index('public', 'job_hunter_postings', 'job_hunter_postings_canonical_url_idx',
                 'canonical-URL lookup in job_hunter_upsert_job is indexed');

select has_index('public', 'job_hunter_postings', 'job_hunter_postings_ats_idx',
                 'ATS-triple lookup in job_hunter_upsert_job is indexed');

-- These three index plain generated columns rather than expressions. An
-- expression index cannot be used under RLS here: the qual calls a function
-- that is not LEAKPROOF, so the planner will not push it below the security
-- barrier, and as `authenticated` the lookup falls back to a sequential
-- scan. Comparing a stored column is plain text equality, which is
-- leakproof and does push down. See 202609070002.
select has_index('public', 'job_hunter_postings', 'job_hunter_postings_normalized_company_title_idx',
                 'normalized company/title lookup in job_hunter_find_posting_by_identity is indexed');

select has_index('public', 'job_hunter_postings', 'job_hunter_postings_canonical_of_url_idx',
                 'canonicalized-url branch of the inbound anti-join is indexed');

select has_index('public', 'job_hunter_postings', 'job_hunter_postings_normalized_identity_idx',
                 'normalized-identity branch of the inbound anti-join is indexed');

-- The generated columns are what makes those indexes reachable. Losing one,
-- or having it silently become an ordinary column an application must keep
-- in step, is the regression this guards.
select is(
  (select count(*)::int from information_schema.columns
    where table_schema = 'public' and table_name = 'job_hunter_postings'
      and column_name in ('normalized_identity','canonical_url_of_url',
                          'normalized_company','normalized_title')
      and is_generated = 'ALWAYS'),
  4,
  'all four normalized lookup columns are GENERATED ALWAYS, not application-maintained'
);

-- The per-user copies must be gone, not merely superseded: an index over a
-- column job_hunter_jobs no longer has costs a write on every membership row
-- and can never be read. This covers both generations of them -- the
-- RLS-unreachable expression indexes 202609070002 replaced, and the
-- plain-column ones #178 took away with the columns.
select is_empty(
  $$ select indexname from pg_indexes
      where schemaname = 'public' and tablename = 'job_hunter_jobs'
        and indexname in ('job_hunter_jobs_user_identity_idx',
                          'job_hunter_jobs_user_canonical_of_url_idx',
                          'job_hunter_jobs_user_normalized_triple_idx',
                          'job_hunter_jobs_user_canonical_url_idx',
                          'job_hunter_jobs_user_ats_idx',
                          'job_hunter_jobs_user_normalized_company_title_idx',
                          'job_hunter_jobs_user_canonical_of_url_col_idx',
                          'job_hunter_jobs_user_normalized_identity_idx',
                          'job_hunter_jobs_user_source_job_idx') $$,
  'no identity index survives on the membership table'
);

select has_index('public', 'job_hunter_postings', 'job_hunter_postings_source_job_idx',
                 'source-identity branch of the inbound anti-join is indexed');

-- An expression index is only legal while the functions it calls stay
-- IMMUTABLE. If one is ever redefined as STABLE the migration stops being
-- replayable on a fresh database, so pin the property rather than trusting it.
select is_empty(
  $$ select p.proname
       from pg_proc p
       join pg_namespace n on n.oid = p.pronamespace
      where n.nspname = 'public'
        and p.proname in ('job_hunter_normalize_company', 'job_hunter_normalize_tokens',
                          'job_hunter_normalize_text', 'job_hunter_canonicalize_url')
        and p.provolatile <> 'i' $$,
  'every normalizer used in an expression index is still IMMUTABLE'
);

select * from finish();
rollback;

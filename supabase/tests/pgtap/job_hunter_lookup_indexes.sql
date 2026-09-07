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

begin;
select plan(7);

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_user_canonical_url_idx',
                 'canonical-URL lookup in job_hunter_upsert_job is indexed');

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_user_ats_idx',
                 'ATS-triple lookup in job_hunter_upsert_job is indexed');

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_user_identity_idx',
                 'normalized company/title lookup in job_hunter_find_job_by_identity is indexed');

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_user_canonical_of_url_idx',
                 'canonicalized-url branch of the inbound anti-join is indexed');

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_user_normalized_triple_idx',
                 'normalized-identity branch of the inbound anti-join is indexed');

select has_index('public', 'job_hunter_jobs', 'job_hunter_jobs_user_source_job_idx',
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

-- Indexes for the lookups inside the Job Hunter store functions.
--
-- The functions added in 202609060004 resolve a job's identity by canonical
-- URL, by ATS triple, and by normalized company/title/location. Against the
-- fixtures they were written with -- tens of rows -- every one of those was
-- free. Against real history they are sequential scans: after issue #70's
-- data migration loaded 18,308 jobs, a single identity lookup measured
--
--   Seq Scan on job_hunter_jobs (cost=0.15..11982.40)
--     Rows Removed by Filter: 18308
--   Execution Time: 539.226 ms
--
-- and the first production run died with SQLSTATE 57014, "canceling
-- statement due to statement timeout", in both job_hunter_upsert_job and
-- job_hunter_unmaterialized_inbound_jobs. The latter is the worst of them:
-- it anti-joins every inbound candidate against every job and calls the
-- normalizers on both sides, so 341 candidates against 18,308 jobs is
-- roughly six million function evaluations in one statement.
--
-- Every predicate below is indexed as it is actually written. Three of the
-- lookups call a function on the column, so they need expression indexes --
-- a plain index on `company` cannot serve
-- `job_hunter_normalize_company(company) = $1`. All four normalizers are
-- IMMUTABLE, which is what makes that legal.
--
-- None of these are partial. A partial index on `where canonical_url <> ''`
-- would be smaller, but the planner only uses one when it can prove the
-- predicate holds, and here the guarantee lives in a PL/pgSQL `if` it cannot
-- see. At this row count the space saved is not worth an index that silently
-- does not get used.

-- job_hunter_upsert_job: canonical-URL candidate lookup.
create index if not exists job_hunter_jobs_user_canonical_url_idx
  on public.job_hunter_jobs (user_id, canonical_url);

-- job_hunter_upsert_job: ATS-triple candidate lookup.
create index if not exists job_hunter_jobs_user_ats_idx
  on public.job_hunter_jobs (user_id, ats_provider, ats_board, ats_job_id);

-- job_hunter_find_job_by_identity: normalized company + title.
create index if not exists job_hunter_jobs_user_identity_idx
  on public.job_hunter_jobs (
    user_id,
    public.job_hunter_normalize_company(company),
    public.job_hunter_normalize_tokens(title)
  );

-- job_hunter_unmaterialized_inbound_jobs, URL branch of the anti-join.
create index if not exists job_hunter_jobs_user_canonical_of_url_idx
  on public.job_hunter_jobs (user_id, public.job_hunter_canonicalize_url(url));

-- job_hunter_unmaterialized_inbound_jobs, normalized-identity branch. The
-- expression is the concatenation the function compares, character for
-- character -- an index on the three parts separately could not serve it.
create index if not exists job_hunter_jobs_user_normalized_triple_idx
  on public.job_hunter_jobs (
    user_id,
    (
      public.job_hunter_normalize_text(company) || '|' ||
      public.job_hunter_normalize_text(title) || '|' ||
      public.job_hunter_normalize_text(location)
    )
  );

-- job_hunter_unmaterialized_inbound_jobs, source-identity branch.
create index if not exists job_hunter_jobs_user_source_job_idx
  on public.job_hunter_jobs (user_id, source, source_job_id);

analyze public.job_hunter_jobs;

-- job_hunter_backfill_ats_triple_dupes (#249): the one-time backfill that
-- collapses postings sharing an ATS triple across more than one source
-- label, without touching a triple shared by postings under one source
-- label (#254's anomaly).

begin;
create extension if not exists pgtap with schema extensions;
select no_plan();

create function pg_temp.posting(
  p_fingerprint text,
  p_source text,
  p_ats_provider text,
  p_ats_board text,
  p_ats_job_id text,
  p_source_job_id text default null,
  p_first_seen_at timestamptz default now())
returns uuid language sql as $$
  insert into public.job_hunter_postings
    (fingerprint, source, source_job_id, url, company, title, location,
     description, description_hash, content_confidence,
     ats_provider, ats_board, ats_job_id,
     first_seen_at, last_seen_at, created_at)
  values
    (p_fingerprint, p_source, p_source_job_id, '', '', '', '',
     '', encode(sha256(''::bytea), 'hex'), '',
     p_ats_provider, p_ats_board, p_ats_job_id,
     p_first_seen_at, p_first_seen_at, p_first_seen_at)
  returning id;
$$;

-- Two source labels, same ATS job -----------------------------------------

select pg_temp.posting('jhbb-cross-1', 'ashby', 'ashby', 'bjak', 'cross-1',
                       p_source_job_id => 'cross-1', p_first_seen_at => now() - interval '1 day');
select pg_temp.posting('jhbb-cross-2', 'watch:ashby', 'ashby', 'bjak', 'cross-1',
                       p_source_job_id => 'cross-1');

-- One source label, colliding ats_job_id (#254's shape) --------------------
--
-- Two genuinely different postings -- distinct fingerprints, distinct
-- source_job_id -- that happen to share an ats_job_id. Merging these would
-- fold two different job ads into one; the backfill must leave them alone.

select pg_temp.posting('jhbb-single-1', 'greenhouse', 'greenhouse', 'alarmcom', 'single-shared',
                       p_source_job_id => 'real-job-a');
select pg_temp.posting('jhbb-single-2', 'greenhouse', 'greenhouse', 'alarmcom', 'single-shared',
                       p_source_job_id => 'real-job-b');

-- A closed posting sharing the cross-source triple --------------------------
--
-- Closed postings are not delivered and must not be pulled into a merge.

select pg_temp.posting('jhbb-closed', 'lever', 'lever', 'acme', 'closed-1',
                       p_source_job_id => 'closed-1');
update public.job_hunter_postings set closed_at = now(), closed_reason = 'closure_phrase'
 where fingerprint = 'jhbb-closed';
select pg_temp.posting('jhbb-closed-watch', 'watch:lever', 'lever', 'acme', 'closed-1',
                       p_source_job_id => 'closed-1');

create temp table backfill_result as
select * from public.job_hunter_backfill_ats_triple_dupes();

select is(
  (select groups_processed from backfill_result),
  1,
  'exactly one ATS-triple group qualifies: the cross-source pair');

select is(
  (select merged_pairs from backfill_result),
  1,
  'one merge collapses that pair');

-- The merge keeps the merged-away row -- see job_hunter_merge_postings'
-- header on why -- so the raw row count under this triple stays 2. What
-- must be one is where both now resolve to.
select is(
  (select public.job_hunter_resolve_posting(
            (select id from public.job_hunter_postings where fingerprint = 'jhbb-cross-1'))),
  (select public.job_hunter_resolve_posting(
            (select id from public.job_hunter_postings where fingerprint = 'jhbb-cross-2'))),
  'the two source labels now resolve to the same posting');

select is(
  (select count(*)::int from public.job_hunter_postings
    where ats_provider = 'greenhouse' and ats_board = 'alarmcom' and ats_job_id = 'single-shared'),
  2,
  'the single-source-label anomaly (#254) is left exactly as it was');

select is(
  (select count(*)::int from public.job_hunter_postings
    where ats_provider = 'lever' and ats_board = 'acme' and ats_job_id = 'closed-1'),
  2,
  'a closed posting sharing the triple with an open one is not merged');

-- Idempotent: nothing left to do on a second call ---------------------------

create temp table backfill_rerun as
select * from public.job_hunter_backfill_ats_triple_dupes();

select is(
  (select merged_pairs from backfill_rerun),
  0,
  're-running the backfill after it has already run merges nothing further');

select * from finish();
rollback;

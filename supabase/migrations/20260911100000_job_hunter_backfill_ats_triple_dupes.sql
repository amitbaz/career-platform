-- Backfill for #249: collapse postings that are the same ATS job discovered
-- under two source labels (`ashby` and `watch:ashby`, `lever` and
-- `watch:lever`, and so on), and re-key every existing posting whose
-- fingerprint predates the fix so a future crawl resolves onto it instead of
-- inserting a fresh duplicate.
--
-- The fingerprint fix (job_hunter/normalize.py, application code, not this
-- migration) stops new duplicates of this shape from being created going
-- forward. It does that by changing what `job_fingerprint` *computes* for a
-- listing with a complete, trustworthy ATS triple -- which means every
-- posting already on the table, not only the ~5,500 duplicates, still holds
-- the OLD fingerprint value it was written under. `job_hunter_upsert_posting`
-- and `job_hunter_merge_posting_batch` resolve a listing by looking its
-- fingerprint up with `on conflict (fingerprint)`; a value that has never
-- existed in the table does not conflict, so the very next crawl of an
-- otherwise perfectly fine, never-duplicated posting would insert a second
-- row for it. This migration is therefore two things done together:
--
--   1. merge the duplicates already on the table (unchanged goal), and
--   2. re-key every posting -- singleton or survivor -- onto the fingerprint
--      the fixed Python now computes for it, so (1) is not silently undone
--      by the next crawl of anything this fix touches.
--
-- "Trustworthy", used in both steps: `source_job_id` is either absent or
-- agrees with `ats_job_id`, exactly `normalize._ats_identity_trustworthy`'s
-- rule. #249's own investigation of the greenhouse-only groups (19 ats jobs
-- / 38 postings, sharing one source label already) found two genuinely
-- different advertisements whose `ats_job_id` had been cross-contaminated --
-- `source_job_id` and `url` differ and are each self-consistent, only
-- `ats_job_id` is wrong on one side. Trusting that triple would fold two
-- distinct job ads into one posting or one fingerprint; that defect is
-- unrelated to source relabeling and is tracked separately (#254), and both
-- steps below exclude anything shaped like it -- not only the single-source
-- groups #254 was found in, in case the same corruption ever appears across
-- two source labels too.
--
-- Locking, stated plainly rather than optimistically: each `select ... for
-- update` inside `job_hunter_merge_postings` holds its rows for the rest of
-- the *transaction*, not just for its own call, and step (2)'s UPDATE holds
-- whatever it touches until commit as well. Postgres row locks are
-- transaction-scoped, so nothing inside one call can bound that; `p_batch_limit`
-- below bounds it the only way available to a single SQL statement: how much
-- work one call is asked to do. Chunking across separate, actually-committing
-- transactions needs a caller that can COMMIT between calls, which a
-- migration file cannot -- see `scripts/backfill_ats_triple_dupes.py`, which
-- does exactly that against the corpus this migration was written for and is
-- the intended way to run this at that scale. This migration's own one-time
-- call below passes no limit: on the corpus #249 measured, one bounded-by-
-- lock-timeout pass is small enough to accept outright, and `lock_timeout`
-- fails it fast rather than blocking a deploy indefinitely if it collides
-- with a live crawl's write.
set local lock_timeout = '5s';

-- A function rather than an inline DO block so the same logic that runs once
-- here is also what the pgTAP test (job_hunter_backfill_ats_triple_dupes.sql)
-- exercises against seeded fixtures -- including the #254 anomaly this must
-- NOT touch -- and what the batch runner script calls repeatedly. An inline
-- block has no handle either could reach.
--
-- SECURITY INVOKER, deliberately, unlike job_hunter_merge_postings below it:
-- this never needs to be more privileged than whoever calls it, since it is
-- only ever called by the migration runner (the table owner), by pgTAP under
-- the same connection, or by the batch script over the same privileged DSN
-- ingestion itself uses. Definer would add an eighth entry to
-- job_hunter_store_functions.sql's "six security-definer exceptions, and no
-- more" guard for no reason -- job_hunter_merge_postings is already revoked
-- from every client role, so an unprivileged caller reaching this function
-- fails inside that call regardless of this function's own security mode.
create or replace function public.job_hunter_backfill_ats_triple_dupes(p_batch_limit int default null)
returns table (groups_processed int, merged_pairs int, fingerprints_rewritten int)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_group record;
  v_ids uuid[];
  v_survivor uuid;
  v_i int;
  v_merged_pairs int := 0;
  v_groups_processed int := 0;
  v_rewritten int := 0;
begin
  -- Step 1: merge. A posting already merged away keeps its own
  -- ats_provider/ats_board/ats_job_id "as they were"
  -- (job_hunter_merge_postings's header) -- that is provenance, not a
  -- going-forward duplicate. Left in, it would make every group here look
  -- unresolved forever: the survivor and the row it already absorbed would
  -- keep re-qualifying as a "pair" on every run. Excluding it is also what
  -- makes a second run of this function see nothing left to do.
  for v_group in
    select p.ats_provider, p.ats_board, p.ats_job_id,
           array_agg(p.id order by p.first_seen_at asc, p.id asc) as posting_ids
      from public.job_hunter_postings p
     where p.closed_at is null
       and coalesce(p.ats_provider, '') <> ''
       and coalesce(p.ats_board, '') <> ''
       and coalesce(p.ats_job_id, '') <> ''
       and (p.source_job_id is null or p.source_job_id = p.ats_job_id)
       and not exists (
             select 1 from public.job_hunter_posting_merges m
              where m.duplicate_id = p.id)
     group by p.ats_provider, p.ats_board, p.ats_job_id
    having count(*) > 1
       and count(distinct p.source) > 1
     order by min(p.first_seen_at), p.ats_provider, p.ats_board, p.ats_job_id
     limit p_batch_limit
  loop
    v_ids := v_group.posting_ids;
    v_survivor := v_ids[1];
    for v_i in 2 .. array_length(v_ids, 1) loop
      v_survivor := public.job_hunter_merge_postings(v_survivor, v_ids[v_i]);
      v_merged_pairs := v_merged_pairs + 1;
    end loop;
    v_groups_processed := v_groups_processed + 1;
  end loop;

  -- Step 2: re-key. Only a triple that resolves to EXACTLY ONE trustworthy,
  -- open, not-merged-away posting is touched -- which is what step 1 above
  -- just made true of every triple it processed, and was already true of
  -- every complete-ATS posting that was never a duplicate in the first
  -- place. A triple that still names more than one such posting here is
  -- exactly the #254 shape surviving step 1's exclusion (it is never
  -- "trustworthy" on both sides at once, so it was never merged), and is
  -- left on its old fingerprint rather than guessed at.
  with candidates as (
    select p.id,
           'id:' || lower(p.ats_provider) || ':' || lower(p.ats_board) || ':' || p.ats_job_id as raw
      from public.job_hunter_postings p
     where p.closed_at is null
       and coalesce(p.ats_provider, '') <> ''
       and coalesce(p.ats_board, '') <> ''
       and coalesce(p.ats_job_id, '') <> ''
       and (p.source_job_id is null or p.source_job_id = p.ats_job_id)
       and not exists (
             select 1 from public.job_hunter_posting_merges m
              where m.duplicate_id = p.id)
  ),
  unique_triples as (
    select raw, (array_agg(id))[1] as sole_id
      from candidates
     group by raw
    having count(*) = 1
  ),
  to_rewrite as (
    select u.sole_id as id,
           encode(sha256(convert_to(u.raw, 'UTF8')), 'hex') as new_fingerprint
      from unique_triples u
      join public.job_hunter_postings p on p.id = u.sole_id
     where p.fingerprint <> encode(sha256(convert_to(u.raw, 'UTF8')), 'hex')
     order by u.sole_id
     limit p_batch_limit
  )
  update public.job_hunter_postings p
     set fingerprint = t.new_fingerprint
    from to_rewrite t
   where p.id = t.id;
  get diagnostics v_rewritten = row_count;

  return query select v_groups_processed, v_merged_pairs, v_rewritten;
end
$$;

comment on function public.job_hunter_backfill_ats_triple_dupes(int) is
  'One-time backfill for #249, callable in bounded batches: merges open '
  'postings that share a trustworthy (ats_provider, ats_board, ats_job_id) '
  'triple across more than one source label into one survivor via '
  'job_hunter_merge_postings, then re-keys every trustworthy singleton or '
  'survivor onto the fingerprint the fixed job_fingerprint now computes for '
  'it, so a future crawl resolves onto it instead of inserting a duplicate. '
  '"Trustworthy" excludes the #254 shape -- see this migration''s header. '
  'p_batch_limit bounds one call''s own work, not the corpus: it does not '
  'commit between groups, so chunking across transactions is the caller''s '
  'job (scripts/backfill_ats_triple_dupes.py). Callable repeatedly and '
  'idempotently: once a group is merged and re-keyed, a later call finds '
  'nothing left to do for it.';

-- Same posture as job_hunter_merge_postings, which this calls: not something
-- any client role should be able to invoke.
revoke all on function public.job_hunter_backfill_ats_triple_dupes(int)
  from public, anon, authenticated, service_role;

do $$
declare
  v_result record;
begin
  select * into v_result from public.job_hunter_backfill_ats_triple_dupes();
  raise notice 'job_hunter_backfill_ats_triple_dupes: merged % pair(s) across % ATS-triple group(s), rewrote % fingerprint(s)',
    v_result.merged_pairs, v_result.groups_processed, v_result.fingerprints_rewritten;
end $$;

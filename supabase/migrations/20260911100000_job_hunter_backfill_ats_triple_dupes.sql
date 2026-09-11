-- Backfill for #249: collapse postings that are the same ATS job discovered
-- under two source labels (`ashby` and `watch:ashby`, `lever` and
-- `watch:lever`, and so on).
--
-- The fingerprint fix (job_hunter/normalize.py, application code, not this
-- migration) stops new duplicates of this shape from being created. This
-- migration clears the ones already on the table, using the merge primitive
-- #176 already built: `job_hunter_merge_postings` folds one posting into
-- another, re-points every affected user's job row, and discards the
-- duplicate's facets.
--
-- Scope, deliberately narrower than "every open posting sharing an ATS
-- triple": only a group whose postings carry MORE THAN ONE distinct
-- `source` value is merged here. #249's own investigation of the
-- greenhouse-only groups (19 ats jobs / 38 postings, sharing one source
-- label already) found two genuinely different advertisements whose
-- `ats_job_id` had been cross-contaminated -- their `source_job_id` and
-- `url` differ and are each self-consistent, only `ats_job_id` is wrong on
-- one side. Merging those would fold two distinct job ads into one posting.
-- That defect is unrelated to source relabeling and is tracked separately
-- (#254); this migration must not touch it, so it is restricted to groups
-- that actually exhibit #249's shape: the same triple reached through
-- different source labels.
--
-- Locking, stated plainly rather than optimistically: this runs as one
-- statement, so every posting row a merge touches stays locked for the rest
-- of the run, not just for its own merge call -- Postgres row locks are
-- transaction-scoped, and batching the *scan* here would not change that.
-- On the live corpus (~5,500 duplicate postings) that is a one-time, brief
-- hold, not an unbounded one, so it is accepted rather than engineered away:
-- this repo's migrations run one file per transaction and have no chunked
-- commit convention to reach for. What IS bounded is the wait for those
-- locks -- `lock_timeout` below fails the migration fast if it collides with
-- a live crawl's write instead of blocking a deploy indefinitely. A corpus
-- large enough for the hold itself to matter is a reason to split this into
-- a chunked, multi-transaction script; nothing in the live corpus today asks
-- for that.
set local lock_timeout = '5s';

-- A function rather than an inline DO block so the same logic that runs once
-- here is also what the pgTAP test (job_hunter_backfill_ats_triple_dupes.sql)
-- exercises against seeded fixtures -- including the single-source anomaly
-- this must NOT touch. An inline block has no handle a test could call.
--
-- SECURITY INVOKER, deliberately, unlike job_hunter_merge_postings below it:
-- this never needs to be more privileged than whoever calls it, since it is
-- only ever called by the migration runner (the table owner) and by pgTAP
-- under the same connection. Definer would add an eighth entry to
-- job_hunter_store_functions.sql's "six security-definer exceptions, and no
-- more" guard for no reason -- job_hunter_merge_postings is already revoked
-- from every client role, so an unprivileged caller reaching this function
-- fails inside that call regardless of this function's own security mode.
create or replace function public.job_hunter_backfill_ats_triple_dupes()
returns table (groups_processed int, merged_pairs int)
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
begin
  for v_group in
    -- A posting already merged away keeps its own ats_provider/ats_board/
    -- ats_job_id "as they were" (job_hunter_merge_postings's header) --
    -- that is provenance, not a going-forward duplicate. Left in, it would
    -- make every group here look unresolved forever: the survivor and the
    -- row it already absorbed would keep re-qualifying as a "pair" on every
    -- run. Excluding it is also what makes a second run of this function
    -- see nothing left to do.
    select p.ats_provider, p.ats_board, p.ats_job_id,
           array_agg(p.id order by p.first_seen_at asc, p.id asc) as posting_ids
      from public.job_hunter_postings p
     where p.closed_at is null
       and coalesce(p.ats_provider, '') <> ''
       and coalesce(p.ats_board, '') <> ''
       and coalesce(p.ats_job_id, '') <> ''
       and not exists (
             select 1 from public.job_hunter_posting_merges m
              where m.duplicate_id = p.id)
     group by p.ats_provider, p.ats_board, p.ats_job_id
    having count(*) > 1
       and count(distinct p.source) > 1
  loop
    v_ids := v_group.posting_ids;
    v_survivor := v_ids[1];
    for v_i in 2 .. array_length(v_ids, 1) loop
      v_survivor := public.job_hunter_merge_postings(v_survivor, v_ids[v_i]);
      v_merged_pairs := v_merged_pairs + 1;
    end loop;
    v_groups_processed := v_groups_processed + 1;
  end loop;

  return query select v_groups_processed, v_merged_pairs;
end
$$;

comment on function public.job_hunter_backfill_ats_triple_dupes() is
  'One-time backfill for #249: merges open postings that share an '
  '(ats_provider, ats_board, ats_job_id) triple across more than one source '
  'label into one survivor via job_hunter_merge_postings. Deliberately '
  'excludes a triple shared by postings under a single source label -- see '
  '#254 for why. Callable repeatedly and idempotently: once a group is '
  'merged, job_hunter_resolve_posting collapses it to one row and a later '
  'call finds nothing left to do for it.';

-- Same posture as job_hunter_merge_postings, which this calls: not something
-- any client role should be able to invoke.
revoke all on function public.job_hunter_backfill_ats_triple_dupes()
  from public, anon, authenticated, service_role;

do $$
declare
  v_result record;
begin
  select * into v_result from public.job_hunter_backfill_ats_triple_dupes();
  raise notice 'job_hunter_backfill_ats_triple_dupes: merged % pair(s) across % ATS-triple group(s)',
    v_result.merged_pairs, v_result.groups_processed;
end $$;

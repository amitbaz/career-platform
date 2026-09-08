-- Batch the last per-job round trip left in discovery's hot path.
--
-- `collect_candidates` records, for every eligible job on a supported ATS
-- board, that the board produced a candidate. `record_ats_eligible_job` did
-- that with a select followed by an update, one job at a time: two serial
-- PostgREST round trips per eligible job, against a remote database, inside
-- the daily run. Run 34201733339 reported eligible=1358, almost all of them
-- on ATS boards -- roughly 2,700 round trips to increment a counter on a few
-- dozen rows (issue #151).
--
-- Everything around it in that function already batches: job upserts chunk at
-- 100, `upsert_ats_boards` collapses thousands of sightings to one call per
-- distinct board, `job_hunter_needs_evaluation` answers for the whole run at
-- once. This is the same collapse, one round trip for the whole run.
--
-- `security invoker` with an empty search_path, like every function before
-- it: row level security still decides which rows this can touch, so the
-- update reaches only the caller's own registry rows.

create or replace function public.job_hunter_record_ats_eligible_jobs(
  p_boards jsonb,
  p_now timestamptz
)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_updated int;
begin
  if p_boards is null or jsonb_typeof(p_boards) <> 'array' then
    raise exception 'p_boards must be a jsonb array, got %',
      coalesce(jsonb_typeof(p_boards), 'null');
  end if;

  -- One row per board, never per job: `update ... from` touches a target row
  -- once per statement, so two entries for the same board would silently
  -- lose one of them. The caller collapses its sightings before sending
  -- them, and `eligible_jobs` carries the count that collapse produced.
  with sightings as (
    select
      element->>'provider' as provider,
      element->>'board_identifier' as board_identifier,
      coalesce((element->>'eligible_jobs')::int, 0) as eligible_jobs
    from jsonb_array_elements(p_boards) as element
  )
  update public.job_hunter_ats_registry as registry
     set eligible_jobs_seen = registry.eligible_jobs_seen + sightings.eligible_jobs,
         last_eligible_at = p_now
    from sightings
   where registry.provider = sightings.provider
     and registry.board_identifier = sightings.board_identifier;

  get diagnostics v_updated = row_count;
  return v_updated;
end;
$$;

comment on function public.job_hunter_record_ats_eligible_jobs(jsonb, timestamptz) is
  'Record, in one round trip, that each named board surfaced the given number '
  'of eligible jobs this run: adds to eligible_jobs_seen and stamps '
  'last_eligible_at. Boards absent from the registry are left alone, as the '
  'per-job version did. Each board must appear at most once in p_boards; the '
  'caller collapses its per-job sightings first. Returns how many registry '
  'rows were updated.';

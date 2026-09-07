-- Batch entry points for discovery's hot path.
--
-- `collect_candidates` persists every job it discovers -- roughly 19,000 raw
-- and 13,000 unique on a normal day. One PostgREST round trip per job put
-- that work at about 70,000 sequential requests, which spent the whole
-- 60-minute GitHub Actions budget before evaluation started (issue #97).
--
-- These functions do not add persistence logic. `job_hunter_upsert_jobs`
-- calls the existing single-job `job_hunter_upsert_job` once per element,
-- inside one round trip instead of one per job, so identity resolution and
-- duplicate merging stay in exactly one place.
--
-- All three are `security invoker` with an empty search_path, like the six
-- functions in 202609060004: row level security still decides every row.

create or replace function public.job_hunter_upsert_jobs(p_jobs jsonb)
returns table (input_index int, id uuid, is_new boolean, description_changed boolean)
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_element jsonb;
  v_index int;
  v_result record;
begin
  if p_jobs is null or jsonb_typeof(p_jobs) <> 'array' then
    raise exception 'p_jobs must be a jsonb array, got %',
      coalesce(jsonb_typeof(p_jobs), 'null');
  end if;

  -- Ordered, one element at a time, deliberately. Two jobs in one batch can
  -- resolve to the same identity, and the second must merge into the first
  -- exactly as two sequential single-job calls would.
  for v_element, v_index in
    select value, (ordinality - 1)::int
      from jsonb_array_elements(p_jobs) with ordinality as t(value, ordinality)
     order by ordinality
  loop
    select * into v_result from public.job_hunter_upsert_job(v_element);
    input_index := v_index;
    id := v_result.id;
    is_new := v_result.is_new;
    description_changed := v_result.description_changed;
    return next;
  end loop;
end;
$$;

comment on function public.job_hunter_upsert_jobs(jsonb) is
  'Upsert an ordered array of jobs in one round trip. Returns one row per '
  'input element, tagged with its zero-based input_index so a caller can zip '
  'results back onto what it sent -- ids alone cannot, because two elements '
  'may resolve to the same job. No exception handling: a failure aborts the '
  'whole call, and the caller replays the batch one job at a time to isolate '
  'the bad element.';

create or replace function public.job_hunter_needs_evaluation(p_job_ids uuid[])
returns table (job_id uuid, needs boolean)
language sql
security invoker
set search_path = ''
as $$
  select
    j.id,
    case
      when e.evaluated_at is null then true
      when e.status = 'failed' then true
      -- Deliberately asymmetric: the job side is coalesced, the evaluation
      -- side is not. PostgresJobStore.needs_evaluation compares
      -- `evaluation[...] != (job_row.get(...) or "")`, so a null recorded at
      -- evaluation time counts as changed against an empty stored value.
      -- Reproduced, not corrected: correcting it here would quietly change
      -- which jobs get re-evaluated.
      when e.description_hash_at_eval is distinct from coalesce(j.description_hash, '') then true
      when e.content_confidence_at_eval is distinct from coalesce(j.content_confidence, '') then true
      else false
    end
  from public.job_hunter_jobs j
  left join lateral (
    select ev.status, ev.evaluated_at,
           ev.description_hash_at_eval, ev.content_confidence_at_eval
      from public.job_hunter_evaluations ev
     where ev.job_id = j.id
     order by ev.evaluated_at desc, ev.created_at desc, ev.id desc
     limit 1
  ) e on true
  where j.id = any(p_job_ids);
$$;

comment on function public.job_hunter_needs_evaluation(uuid[]) is
  'Bulk form of PostgresJobStore.needs_evaluation: two requests per job '
  'become one request per id array. An id the caller cannot read returns no '
  'row, and the caller treats a missing id as needing evaluation -- which is '
  'what the per-job method does with a job whose evaluations it cannot see.';

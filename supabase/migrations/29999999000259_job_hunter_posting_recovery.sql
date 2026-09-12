-- Recoverable unresolved-posting enrichment (issue #259).
--
-- Placeholder timestamp per AGENTS.md's migration-numbering rule -- issue
-- number, not a date -- renumbered to a real YYYYMMDDHHMMSS once the branch
-- is otherwise ready to merge, issued by whoever owns the board.
--
-- job_hunter_match_state_counts (29999999000243) already classifies a posting
-- with insufficient content_confidence as `unresolved`, correctly: missing
-- information produces no hard block. What was missing is the other half --
-- nothing ever tried to turn an unresolved posting into a resolved one. This
-- migration gives a posting a recovery schedule, on the same
-- posting-is-shared-state pattern job_hunter_posting_freshness.sql already
-- established, plus the two functions the recover_posting stage and its cron
-- tick need.
--
-- Design: docs/superpowers/specs/2026-09-12-posting-recovery-design.md

-- Columns --------------------------------------------------------------------

alter table public.job_hunter_postings
  add column recovery_attempts integer not null default 0,
  add column recovery_last_attempt_at timestamptz,
  add column recovery_last_outcome text not null default '',
  add column recovery_next_attempt_at timestamptz,
  add constraint job_hunter_postings_recovery_outcome_check check (
    recovery_last_outcome in ('', 'recovered', 'unresolved')
  );

comment on column public.job_hunter_postings.recovery_attempts is
  'How many recover_posting attempts have completed against this posting '
  '(#259). Observability only -- it never gates whether another attempt '
  'happens, so no retry count can turn into a permanent exclusion.';
comment on column public.job_hunter_postings.recovery_last_outcome is
  'What the last completed recover_posting attempt found: '''' (never '
  'attempted), recovered (content_confidence became sufficient), or '
  'unresolved (attempt completed, still insufficient). A failed or '
  'rate-limited fetch is not a posting state -- it is the queue''s own '
  'retry/backoff, and never reaches this column.';
comment on column public.job_hunter_postings.recovery_next_attempt_at is
  'When recover_posting is next due for this posting. Null once content is '
  'sufficient or recovery was never needed. Set to now() by '
  'job_hunter_posting_recovery_schedule on insert and whenever a merge '
  'changes the posting while it stays insufficient (#259 AC3), and to '
  'job_hunter_recovery_interval(age) by the stage after a completed attempt '
  'that is still unresolved.';

-- What the enqueuer scans: open, insufficient postings in due order.
create index job_hunter_postings_recovery_due_idx
  on public.job_hunter_postings (recovery_next_attempt_at)
  where closed_at is null and recovery_next_attempt_at is not null;


-- Timing configuration ---------------------------------------------------------
--
-- One row, the same shape job_hunter_ingestion_timing_config already uses:
-- the defaults are what runs, and the row exists so the cadence can be
-- retuned with one update and no code change (#259 AC2). The curve is
-- aggressive for a posting's first day, then doubles every
-- decay_doubling_period, capped at max_interval -- a posting that is never
-- going to resolve costs less and less over time rather than being hammered
-- forever, while a fresh one is retried often enough that a source crawled
-- again a few hours later still lands inside the aggressive window.

create table public.job_hunter_recovery_config (
  singleton boolean primary key default true check (singleton),
  aggressive_window interval not null default interval '24 hours'
    check (aggressive_window > interval '0'),
  aggressive_interval interval not null default interval '20 minutes'
    check (aggressive_interval > interval '0'),
  decay_doubling_period interval not null default interval '12 hours'
    check (decay_doubling_period > interval '0'),
  max_interval interval not null default interval '7 days'
    check (max_interval > interval '0')
);

comment on table public.job_hunter_recovery_config is
  'Configuration for job_hunter_recovery_interval (#259). One row; the '
  'defaults are what runs.';

insert into public.job_hunter_recovery_config default values;

alter table public.job_hunter_recovery_config enable row level security;
revoke all on table public.job_hunter_recovery_config
  from public, anon, authenticated, service_role;


-- job_hunter_recovery_interval ---------------------------------------------------
--
-- How long a posting waits before its next recovery attempt, from its age
-- alone: aggressive_interval for the first aggressive_window, then doubling
-- every decay_doubling_period after that, clamped to max_interval. Unlike
-- job_hunter_freshness_interval this reads a config row rather than closing
-- over fixed bounds, because AC2 asks for a cadence an operator can retune,
-- not only one that decays.

create or replace function public.job_hunter_recovery_interval(p_age interval)
returns interval
language sql
stable
security invoker
set search_path = ''
as $$
  select case
    when p_age < c.aggressive_window then c.aggressive_interval
    else least(
      c.max_interval,
      -- The exponent is capped before power() ever runs: an old enough
      -- posting's uncapped exponent (months of age / a 12-hour doubling
      -- period is easily in the hundreds) produces a value so large that
      -- converting it to an interval overflows before the surrounding
      -- least() gets a chance to clamp it down to max_interval. 30 doublings
      -- of any sane aggressive_interval is already many times larger than
      -- any sane max_interval, so the cap never changes which branch of
      -- least() wins -- it only keeps the arithmetic representable.
      c.aggressive_interval * power(
        2,
        least(
          30.0,
          extract(epoch from (p_age - c.aggressive_window))
            / extract(epoch from c.decay_doubling_period)
        )
      )
    )
  end
  from public.job_hunter_recovery_config c
 where c.singleton;
$$;

comment on function public.job_hunter_recovery_interval(interval) is
  'The recovery-retry interval for a posting of the given age: '
  'aggressive_interval for its first aggressive_window, doubling every '
  'decay_doubling_period after that, clamped to max_interval (#259).';


-- job_hunter_posting_recovery_schedule -------------------------------------------
--
-- The before-insert-or-update trigger that keeps recovery_next_attempt_at
-- current for every writer, present and future, in one place -- rather than
-- teaching job_hunter_merge_posting_batch and job_hunter_upsert_posting the
-- same rule twice (#259 decision 4):
--
--   * A new, insufficient posting is due immediately -- recovery starts at
--     once, not after the first scheduled tick.
--   * An insufficient posting whose description or last_seen_at just moved
--     (a richer source variant merged in, or the same fingerprint was
--     re-crawled) is due immediately too (AC3): whatever new evidence just
--     arrived is worth trying again on, right away, rather than waiting out
--     whatever the decaying interval last set.
--   * A posting whose content just became sufficient needs no more recovery
--     (AC7): its schedule is cleared.
--
-- Deliberately not a rule any stage or merge function tries to reproduce --
-- one BEFORE trigger covers every insert or update to this table, forever.

create or replace function public.job_hunter_posting_recovery_schedule()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  if tg_op = 'INSERT' then
    -- Only defaults an unset schedule -- a real writer (job_hunter_upsert_
    -- posting, job_hunter_merge_posting_batch) never mentions this column, so
    -- new.recovery_next_attempt_at is null and this is where a brand new
    -- insufficient posting gets its first due time. A caller that does supply
    -- one deliberately (a backfill, a fixture) is trusted rather than
    -- overwritten.
    if not public.job_hunter_content_confidence_sufficient(new.content_confidence)
       and new.recovery_next_attempt_at is null then
      new.recovery_next_attempt_at := now();
    end if;
    return new;
  end if;

  if not public.job_hunter_content_confidence_sufficient(new.content_confidence) then
    if new.description_hash is distinct from old.description_hash
       or new.last_seen_at is distinct from old.last_seen_at then
      new.recovery_next_attempt_at := now();
    end if;
  else
    new.recovery_next_attempt_at := null;
  end if;
  return new;
end;
$$;

comment on function public.job_hunter_posting_recovery_schedule() is
  'Keeps recovery_next_attempt_at current on every write to job_hunter_postings '
  '(#259): due at once on insert or on a change while still insufficient, '
  'cleared once content_confidence becomes sufficient.';

create trigger job_hunter_postings_recovery_schedule
  before insert or update on public.job_hunter_postings
  for each row execute function public.job_hunter_posting_recovery_schedule();


-- Stage queue plumbing -----------------------------------------------------------
--
-- A fifth queue-coupled stage alongside the four job_hunter_stage_queues.sql
-- (#183) created. `stage_queue.Stage`'s docstring calling those four "fixed
-- by epic #181" is updated in the same change -- a fifth was always
-- structurally possible, #181 just had not needed one yet.

select pgmq.create('job_hunter_recover_posting');

alter table public.job_hunter_stage_attempts
  drop constraint job_hunter_stage_attempts_stage_check,
  add constraint job_hunter_stage_attempts_stage_check check (stage in (
    'crawl_source', 'resolve_persist', 'extract_facets', 'recheck_freshness',
    'recover_posting'
  ));
alter table public.job_hunter_stage_dead_letters
  drop constraint job_hunter_stage_dead_letters_stage_check,
  add constraint job_hunter_stage_dead_letters_stage_check check (stage in (
    'crawl_source', 'resolve_persist', 'extract_facets', 'recheck_freshness',
    'recover_posting'
  ));

-- Re-created at its current (#184) signature -- job_hunter_source_registry.sql
-- already replaced the three-argument version this section used to restate,
-- adding p_schedule_key. Only the queue-name branch changes here.
create or replace function public.job_hunter_schedule_stage_enqueue(
  p_stage text,
  p_schedule text,
  p_payload jsonb default '{}'::jsonb,
  p_schedule_key text default null
)
returns bigint
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_queue_name text;
  v_job_name text;
  v_job_id bigint;
begin
  v_queue_name := case p_stage
    when 'crawl_source' then 'job_hunter_crawl_source'
    when 'resolve_persist' then 'job_hunter_resolve_persist'
    when 'extract_facets' then 'job_hunter_extract_facets'
    when 'recheck_freshness' then 'job_hunter_recheck_freshness'
    when 'recover_posting' then 'job_hunter_recover_posting'
    else null
  end;

  if v_queue_name is null then
    raise exception 'unknown job hunter stage: %', p_stage
      using errcode = '22023';
  end if;
  if jsonb_typeof(p_payload) <> 'object' then
    raise exception 'stage payload must be a JSON object'
      using errcode = '22023';
  end if;

  v_job_name := 'job-hunter-enqueue-' || replace(p_stage, '_', '-');
  if p_schedule_key is not null then
    if public.job_hunter_source_schedule_slug(p_schedule_key) = '' then
      raise exception 'schedule key % has no usable job-name form', p_schedule_key
        using errcode = '22023';
    end if;
    v_job_name := v_job_name || '-'
      || public.job_hunter_source_schedule_slug(p_schedule_key);
  end if;

  select cron.schedule(
    v_job_name,
    p_schedule,
    format('select pgmq.send(%L, %L::jsonb);', v_queue_name, p_payload::text)
  ) into v_job_id;
  return v_job_id;
end;
$$;

create or replace function public.job_hunter_stage_queue_metrics()
returns table (
  stage text,
  queue_depth bigint,
  visible_depth bigint,
  dead_letter_depth bigint
)
language sql
security invoker
set search_path = ''
as $$
  select s.stage,
         m.queue_length,
         m.queue_visible_length,
         (select count(*)
            from public.job_hunter_stage_dead_letters d
           where d.stage = s.stage)
    from (values
      ('crawl_source'::text, 'job_hunter_crawl_source'::text),
      ('resolve_persist', 'job_hunter_resolve_persist'),
      ('extract_facets', 'job_hunter_extract_facets'),
      ('recheck_freshness', 'job_hunter_recheck_freshness'),
      ('recover_posting', 'job_hunter_recover_posting')
    ) as s(stage, queue_name)
    cross join lateral pgmq.metrics(s.queue_name) m;
$$;

revoke all on function public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_stage_queue_metrics()
  from public, anon, authenticated, service_role;


-- Worker telemetry -----------------------------------------------------------------
--
-- recover_posting joins the three workers job_hunter_worker_runs.sql (#258)
-- already tracks, on its own Render cron schedule (render.yaml,
-- job-hunter-recover-posting, every five minutes -- the same cadence as the
-- enqueue tick above, since draining less often than postings are enqueued
-- would just let the queue build up between drains).

alter table public.job_hunter_worker_runs
  drop constraint job_hunter_worker_runs_worker_check,
  add constraint job_hunter_worker_runs_worker_check check (worker in (
    'crawl_source', 'extract_facets', 'recheck_freshness', 'recover_posting'
  ));
alter table public.job_hunter_worker_schedules
  drop constraint job_hunter_worker_schedules_worker_check,
  add constraint job_hunter_worker_schedules_worker_check check (worker in (
    'crawl_source', 'extract_facets', 'recheck_freshness', 'recover_posting'
  ));

insert into public.job_hunter_worker_schedules (worker, expected_interval_seconds)
values ('recover_posting', 5 * 60);


-- job_hunter_enqueue_due_recover_posting -----------------------------------------
--
-- What the cron tick runs. Sends one {posting_id} message per posting that is
-- due, open, insufficient, checkable, and not already waiting on the queue --
-- job_hunter_enqueue_due_freshness restated for this queue. Checkable means
-- there is something to fetch: a URL, a known ATS identity to fetch the
-- official description from directly, or (failing both) at least a company
-- name a later ticket's search tier could use -- see the design doc's scope
-- section for why that tier does not exist yet.
--
-- Enqueues and does nothing else, as #183 requires of anything cron runs.

create or replace function public.job_hunter_enqueue_due_recover_posting(p_limit integer)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_messages jsonb[];
begin
  if p_limit is null or p_limit <= 0 then
    raise exception 'p_limit must be positive, got %', p_limit
      using errcode = '22023';
  end if;

  with due as (
    select p.id
      from public.job_hunter_postings p
     where p.closed_at is null
       and p.recovery_next_attempt_at is not null
       and p.recovery_next_attempt_at <= now()
       and not public.job_hunter_content_confidence_sufficient(p.content_confidence)
       and (p.url <> ''
            or p.company <> ''
            or (coalesce(p.ats_provider, '') <> ''
                and coalesce(p.ats_board, '') <> ''
                and coalesce(p.ats_job_id, '') <> ''))
       and not exists (
             select 1 from pgmq.q_job_hunter_recover_posting q
              where q.message->>'posting_id' = p.id::text)
     order by p.recovery_next_attempt_at, p.id
     limit p_limit
     for update of p skip locked
  ),
  -- Leased a day out, exactly as freshness leases: the stage sets the real
  -- next attempt (via job_hunter_recovery_interval, or null via the trigger
  -- once sufficient) when it completes, so the lease only matters for a
  -- message that never completes, retried tomorrow rather than every tick.
  leased as (
    update public.job_hunter_postings p
       set recovery_next_attempt_at = now() + interval '1 day'
      from due
     where p.id = due.id
    returning p.id
  )
  select coalesce(
           array_agg(jsonb_build_object('posting_id', l.id)
                     order by p.recovery_next_attempt_at, l.id),
           '{}'::jsonb[])
    into v_messages
    from leased l
    join public.job_hunter_postings p on p.id = l.id;

  if cardinality(v_messages) > 0 then
    perform pgmq.send_batch('job_hunter_recover_posting', v_messages);
  end if;
  return cardinality(v_messages);
end;
$$;

comment on function public.job_hunter_enqueue_due_recover_posting(integer) is
  'Put up to p_limit due, open, insufficient, checkable postings on the '
  'recover_posting queue, each at most once, and lease them a day out. Run '
  'by pg_cron; enqueues and performs no stage work (#183, #259).';

revoke all on function public.job_hunter_enqueue_due_recover_posting(integer)
  from public, anon, authenticated, service_role;


-- job_hunter_recovery_backlog ------------------------------------------------------
--
-- AC8's other half: job_hunter_match_state_counts already reports how many
-- open postings are unresolved and why; this reports how long they have been
-- waiting and how their attempts have gone, for the Engine Lab analytics page
-- (#264) to read later. Counts and age buckets only, never row bodies --
-- matching job_hunter_match_state_counts's own shape -- so it stays cheap
-- even though it necessarily scans every open, insufficient posting once.
--
-- Revoked from anon/authenticated like the other whole-corpus aggregates
-- (job_hunter_worker_health, job_hunter_crawl_window_evidence): there is no
-- per-user dimension to this, and #264 has not built a staff-role mechanism
-- yet to grant it to.

create or replace function public.job_hunter_recovery_backlog(p_now timestamptz default now())
returns table (
  age_bucket text,
  recovery_last_outcome text,
  count integer
)
language sql
stable
security invoker
set search_path = ''
as $$
  select
    case
      when p_now - p.first_seen_at < interval '24 hours' then 'under_24h'
      when p_now - p.first_seen_at < interval '7 days' then 'under_7d'
      else 'over_7d'
    end as age_bucket,
    p.recovery_last_outcome,
    count(*)::integer as count
    from public.job_hunter_postings p
   where p.closed_at is null
     and not public.job_hunter_content_confidence_sufficient(p.content_confidence)
   group by 1, 2;
$$;

comment on function public.job_hunter_recovery_backlog(timestamptz) is
  'Every open, unresolved posting''s age bucket and last recovery outcome, '
  'counted (not returned as rows), for Engine Lab analytics (#259 AC8, #264).';

revoke all on function public.job_hunter_recovery_backlog(timestamptz)
  from public, anon, authenticated, service_role;


-- The tick ----------------------------------------------------------------------------
--
-- Every 5 minutes -- finer than freshness's 30, because AC2 asks for
-- attempts to be frequent in a posting's first day and the aggressive
-- interval default is 20 minutes: a coarser tick would routinely miss it.
-- The limit bounds one transaction, not throughput: the anti-join above means
-- a limit reached today simply leaves the rest due for the next tick.

select cron.schedule(
  'job-hunter-enqueue-recover-posting',
  '*/5 * * * *',
  'select public.job_hunter_enqueue_due_recover_posting(1000);'
);

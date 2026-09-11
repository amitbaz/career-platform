-- Every ingestion worker invocation, and crawl timing as evidence (issue #258).
--
-- The crawl ledger (#184) records what each crawl of each source produced.
-- It records nothing when a Render worker wakes, finds its queue empty and
-- exits, so the cost of the fifteen-minute drains cannot be measured, and a
-- worker that stopped running looks exactly like one with nothing to do.
-- This migration records every invocation, derives health from those
-- records, groups crawl yield by source, UTC hour and weekday, and turns
-- that evidence into a keep / reduce / insufficient_evidence verdict per
-- window that the per-source enqueue then acts on.
--
-- Everything here is shared machinery in the #183 sense: the engine's own
-- operational state, which no user reads. Row level security is on with no
-- policy, and every grant is revoked as well, so neither half is
-- load-bearing alone.

-- Worker runs ----------------------------------------------------------------

create table public.job_hunter_worker_runs (
  id uuid primary key default gen_random_uuid(),
  worker text not null check (worker in (
    'crawl_source', 'extract_facets', 'recheck_freshness'
  )),
  started_at timestamptz not null default now(),
  heartbeat_at timestamptz not null default now(),
  finished_at timestamptz,
  -- How long this run may go without a heartbeat before it counts as dead.
  -- Written by the worker from its own drain's visibility timeout: a batch
  -- that outlives it has already been handed to another worker, so a run
  -- silent for that long is dead or broken by the queue's own definition,
  -- not by a number somebody chose.
  stale_after_seconds integer not null check (stale_after_seconds > 0),
  claimed integer not null default 0 check (claimed >= 0),
  outcomes jsonb not null default '{}'::jsonb
    check (jsonb_typeof(outcomes) = 'object'),
  stop_reason text check (stop_reason in (
    'queue_empty', 'limit', 'time_budget', 'error'
  )),
  elapsed_ms integer check (elapsed_ms >= 0),
  -- Time from enqueue to claim, summed and maximised over the messages this
  -- run claimed. The mean is total / claimed.
  queue_delay_total_ms bigint not null default 0 check (queue_delay_total_ms >= 0),
  queue_delay_max_ms bigint not null default 0 check (queue_delay_max_ms >= 0),
  error text not null default '',
  -- A finished run always says why it stopped, and an unfinished one never
  -- does: a killed process is recognisable by its missing reason.
  constraint job_hunter_worker_runs_finish_carries_reason
    check ((finished_at is null) = (stop_reason is null))
);

comment on table public.job_hunter_worker_runs is
  'One row per invocation of an ingestion worker, written when it starts and '
  'updated after every batch and when it finishes (issue #258). A run that '
  'found its queue empty is a finished run with stop_reason queue_empty, not '
  'an absent row. A run whose process was killed stays unfinished, and '
  'job_hunter_worker_health reports it once its heartbeat is stale.';

create index job_hunter_worker_runs_recent_idx
  on public.job_hunter_worker_runs (worker, started_at desc);

-- Health reads each worker's latest finished run after every invocation, so
-- that read needs its own bounded path on a table that only ever grows.
create index job_hunter_worker_runs_finished_idx
  on public.job_hunter_worker_runs (worker, finished_at desc)
  where finished_at is not null;

-- Worker schedules -----------------------------------------------------------
--
-- What each worker's schedule promises, so that a worker that stopped being
-- invoked at all is a health failure rather than a quiet gap. The values are
-- the Render cron schedules in render.yaml; an integration test fails when
-- the two disagree. `expected_since` starts the clock, so a freshly deployed
-- worker is not reported missing before its first scheduled invocation.
create table public.job_hunter_worker_schedules (
  worker text primary key check (worker in (
    'crawl_source', 'extract_facets', 'recheck_freshness'
  )),
  expected_interval_seconds integer not null check (expected_interval_seconds > 0),
  expected_since timestamptz not null default now()
);

comment on table public.job_hunter_worker_schedules is
  'How often each ingestion worker is scheduled to run, mirroring render.yaml '
  '(issue #258). A worker with no run started within two intervals missed at '
  'least one whole invocation, which job_hunter_worker_health reports.';

insert into public.job_hunter_worker_schedules (worker, expected_interval_seconds)
values
  ('crawl_source', 15 * 60),
  ('extract_facets', 15 * 60),
  ('recheck_freshness', 6 * 60 * 60);

-- Timing configuration -------------------------------------------------------
--
-- One row. The defaults are the mechanism; the row exists so scheduling stays
-- configuration and can be overridden without a code change.
--
-- `significance` is a probability, not a count: a window is reduced only when
-- the source's own rate makes the window's silence less likely than this. It
-- is scale-free, so a source producing two postings a day and one producing
-- two hundred are both judged against themselves.
create table public.job_hunter_ingestion_timing_config (
  singleton boolean primary key default true check (singleton),
  -- At least one week: a complete rolling week always contains a weekend,
  -- and the design requires one before any window is judged.
  lookback_days integer not null default 7 check (lookback_days >= 7),
  significance double precision not null default 0.05
    check (significance > 0 and significance < 1),
  -- Whether a `reduce` verdict changes what is enqueued. With it off the
  -- evidence and the verdicts are still produced; only the enqueue ignores
  -- them.
  apply_reductions boolean not null default true
);

comment on table public.job_hunter_ingestion_timing_config is
  'Configuration for crawl-window evidence and reductions (issue #258). One '
  'row; the defaults are what runs.';

insert into public.job_hunter_ingestion_timing_config default values;

-- The crawl ledger learns when and why each crawl happened -------------------
--
-- `started_at` has so far defaulted to insert time, and the stage inserts
-- after the crawl, so it held the finish time. The stage now writes the real
-- start. `enqueued_at` is when pgmq received the message and `claimed_at` when
-- a worker claimed it, both on the database clock, so claimed_at minus
-- enqueued_at is the queue-to-worker delay. It stops at the claim rather than
-- at started_at because a batch is claimed at once and processed in turn:
-- time spent on earlier messages in the batch is not queue delay. `purpose`
-- tells a safety crawl in a reduced window from a scheduled one.
alter table public.job_hunter_source_crawls
  add column worker_run_id uuid
    references public.job_hunter_worker_runs (id) on delete set null,
  add column purpose text not null default 'scheduled'
    check (purpose in ('scheduled', 'safety')),
  add column enqueued_at timestamptz,
  add column claimed_at timestamptz;

create index job_hunter_source_crawls_worker_run_idx
  on public.job_hunter_source_crawls (worker_run_id);

comment on column public.job_hunter_source_crawls.purpose is
  'scheduled, or safety: a crawl kept in a window its evidence says to reduce, '
  'so a change in the source''s behaviour there is still observed (issue #258).';

alter table public.job_hunter_worker_runs enable row level security;
alter table public.job_hunter_worker_schedules enable row level security;
alter table public.job_hunter_ingestion_timing_config enable row level security;

revoke all on table public.job_hunter_worker_runs
  from public, anon, authenticated, service_role;
revoke all on table public.job_hunter_worker_schedules
  from public, anon, authenticated, service_role;
revoke all on table public.job_hunter_ingestion_timing_config
  from public, anon, authenticated, service_role;

-- Worker health --------------------------------------------------------------
--
-- One row per scheduled worker. `unfinished` outranks `missing`, since a
-- worker being killed mid-run still starts on time.
--
--   unfinished: a run in the last day whose heartbeat is older than its own
--               stale_after_seconds. The day is how long the failure stays
--               visible, not what detects it.
--   missing:    no run started within two expected intervals, so at least one
--               whole scheduled invocation did not happen.
create or replace function public.job_hunter_worker_health(
  p_now timestamptz default now()
)
returns table (
  worker text,
  status text,
  detail text,
  last_started_at timestamptz,
  last_finished_at timestamptz
)
language sql
stable
security invoker
set search_path = ''
as $$
  -- The clock runs from the later of the last run and expected_since, so a
  -- schedule installed or changed after the last run gets its full two
  -- intervals before its first run is overdue.
  select s.worker,
         case
           when dead.id is not null then 'unfinished'
           when p_now - greatest(latest.started_at, s.expected_since)
                > make_interval(secs => 2 * s.expected_interval_seconds)
             then 'missing'
           else 'ok'
         end,
         case
           when dead.id is not null then format(
             'run %s started %s and last heartbeat %s, over %s seconds ago',
             dead.id, dead.started_at, dead.heartbeat_at, dead.stale_after_seconds)
           when p_now - greatest(latest.started_at, s.expected_since)
                > make_interval(secs => 2 * s.expected_interval_seconds)
             then case
               when latest.started_at is null
                 or latest.started_at < s.expected_since then format(
                 'no run recorded since %s; expected one every %s seconds',
                 s.expected_since, s.expected_interval_seconds)
               else format(
                 'last run started %s; expected one every %s seconds',
                 latest.started_at, s.expected_interval_seconds)
             end
           else ''
         end,
         latest.started_at,
         finished.finished_at
    from public.job_hunter_worker_schedules s
    left join lateral (
      select r.started_at
        from public.job_hunter_worker_runs r
       where r.worker = s.worker and r.started_at <= p_now
       order by r.started_at desc
       limit 1
    ) latest on true
    left join lateral (
      select r.finished_at
        from public.job_hunter_worker_runs r
       where r.worker = s.worker and r.finished_at <= p_now
       order by r.finished_at desc
       limit 1
    ) finished on true
    left join lateral (
      select r.id, r.started_at, r.heartbeat_at, r.stale_after_seconds
        from public.job_hunter_worker_runs r
       where r.worker = s.worker
         and r.finished_at is null
         and r.started_at <= p_now
         and r.started_at > p_now - interval '1 day'
         and r.heartbeat_at + make_interval(secs => r.stale_after_seconds) < p_now
       order by r.started_at desc
       limit 1
    ) dead on true
   order by s.worker;
$$;

comment on function public.job_hunter_worker_health(timestamptz) is
  'ok, missing or unfinished for each scheduled ingestion worker, with the '
  'evidence in `detail` (issue #258). Every worker invocation reads it after '
  'finishing and exits non-zero when any worker is unhealthy.';

-- Worker evidence ------------------------------------------------------------
--
-- By worker, ISO weekday and UTC hour over the lookback: how often a worker
-- woke to nothing, what the invocations cost, and how long claimed work had
-- waited. An unfinished run is charged the time up to its last heartbeat.
create or replace function public.job_hunter_worker_run_evidence(
  p_now timestamptz default now()
)
returns table (
  worker text,
  iso_weekday integer,
  utc_hour integer,
  runs integer,
  empty_runs integer,
  empty_ratio double precision,
  compute_seconds double precision,
  claimed bigint,
  mean_queue_delay_seconds double precision,
  max_queue_delay_seconds double precision,
  failed_runs integer,
  unfinished_runs integer
)
language sql
stable
security invoker
set search_path = ''
as $$
  select r.worker,
         extract(isodow from r.started_at at time zone 'UTC')::integer,
         extract(hour from r.started_at at time zone 'UTC')::integer,
         count(*)::integer,
         count(*) filter (
           where r.stop_reason = 'queue_empty' and r.claimed = 0)::integer,
         (count(*) filter (
           where r.stop_reason = 'queue_empty' and r.claimed = 0))::double precision
           / count(*),
         sum(coalesce(
           r.elapsed_ms::double precision,
           extract(epoch from r.heartbeat_at - r.started_at) * 1000)) / 1000.0,
         sum(r.claimed)::bigint,
         sum(r.queue_delay_total_ms)::double precision
           / nullif(sum(r.claimed), 0) / 1000.0,
         max(r.queue_delay_max_ms)::double precision / 1000.0,
         count(*) filter (where r.stop_reason = 'error')::integer,
         count(*) filter (where r.finished_at is null)::integer
    from public.job_hunter_worker_runs r
    cross join public.job_hunter_ingestion_timing_config c
   where r.started_at > p_now - make_interval(days => c.lookback_days)
     and r.started_at <= p_now
   group by 1, 2, 3;
$$;

comment on function public.job_hunter_worker_run_evidence(timestamptz) is
  'Worker invocations over the lookback, by worker, ISO weekday and UTC hour: '
  'empty-queue ratio, compute time and queue-to-worker delay (issue #258).';

-- Crawl-window evidence ------------------------------------------------------
--
-- One row per enabled crawl target, ISO weekday and UTC hour -- all 168 of
-- them, so a window with no crawls is reported as such rather than missing.
--
-- Novelty is new_to_corpus + changed, the measure the band scheduler already
-- uses. Each successful crawl covers the hours since the previous successful
-- crawl of the same target, so a sparse safety crawl and a dense scheduled
-- one are comparable, and the target's rate is novelty per covered hour
-- across the lookback.
--
-- The verdict:
--   insufficient_evidence  the target's history does not yet span the whole
--                          lookback, or the window has no covered hours;
--   reduce                 the window produced no novelty and, at the
--                          target's own rate, a silence that long is less
--                          likely than `significance` -- P(0) = exp(-expected),
--                          compared as expected > -ln(significance) because
--                          Postgres raises on floating-point underflow;
--   keep                   otherwise.
--
-- Source-published-to-first-seen delay is not reported: no adapter captures a
-- publication time a source can be trusted for, so the column is null and
-- says why. tests/test_worker_runs.py fails when Job gains such a field.
create or replace function public.job_hunter_crawl_window_evidence(
  p_now timestamptz default now(),
  p_crawl_key text default null
)
returns table (
  crawl_key text,
  iso_weekday integer,
  utc_hour integer,
  crawls integer,
  scheduled_crawls integer,
  safety_crawls integer,
  successful_crawls integer,
  failures integer,
  rate_limited integer,
  new_to_corpus integer,
  changed integer,
  requests integer,
  elapsed_ms bigint,
  covered_hours double precision,
  novelty_per_crawl double precision,
  novelty_per_request double precision,
  novelty_per_compute_second double precision,
  mean_queue_delay_seconds double precision,
  published_to_first_seen_seconds double precision,
  published_to_first_seen_reason text,
  target_novelty_per_hour double precision,
  expected_novelty double precision,
  history_complete boolean,
  recommendation text
)
language sql
stable
security invoker
set search_path = ''
as $$
  with bounds as (
    select p_now - make_interval(days => c.lookback_days) as since,
           c.significance
      from public.job_hunter_ingestion_timing_config c
  ),
  targets as (
    select t.crawl_key
      from public.job_hunter_crawl_targets t
     where t.enabled
       and (p_crawl_key is null or t.crawl_key = p_crawl_key)
  ),
  -- Read from twice the lookback so the first crawl inside it still has a
  -- predecessor to measure its coverage from.
  successful as (
    select c.source_key,
           c.started_at,
           c.new_to_corpus + c.changed as novelty,
           extract(epoch from c.started_at - lag(c.started_at) over (
             partition by c.source_key order by c.started_at)) / 3600.0
             as covered_hours
      from public.job_hunter_source_crawls c
      cross join bounds b
     where c.outcome in ('fetched', 'not_modified')
       and c.started_at > b.since - (p_now - b.since)
       and c.started_at <= p_now
       and c.source_key in (select t.crawl_key from targets t)
  ),
  covered as (
    select s.source_key,
           extract(isodow from s.started_at at time zone 'UTC')::integer as iso_weekday,
           extract(hour from s.started_at at time zone 'UTC')::integer as utc_hour,
           sum(s.covered_hours) as hours
      from successful s
      cross join bounds b
     where s.started_at > b.since and s.covered_hours is not null
     group by 1, 2, 3
  ),
  target_rate as (
    select s.source_key,
           sum(s.novelty)::double precision / nullif(sum(s.covered_hours), 0)
             as per_hour
      from successful s
      cross join bounds b
     where s.started_at > b.since and s.covered_hours is not null
     group by 1
  ),
  history as (
    select t.crawl_key,
           exists (
             select 1
               from public.job_hunter_source_crawls c
              cross join bounds b
              where c.source_key = t.crawl_key and c.started_at <= b.since
           ) as complete
      from targets t
  ),
  per_window as (
    select c.source_key,
           extract(isodow from c.started_at at time zone 'UTC')::integer as iso_weekday,
           extract(hour from c.started_at at time zone 'UTC')::integer as utc_hour,
           count(*) as crawls,
           count(*) filter (where c.purpose = 'scheduled') as scheduled_crawls,
           count(*) filter (where c.purpose = 'safety') as safety_crawls,
           count(*) filter (
             where c.outcome in ('fetched', 'not_modified')) as successful_crawls,
           count(*) filter (where c.outcome = 'failed') as failures,
           count(*) filter (where c.outcome = 'rate_limited') as rate_limited,
           sum(c.new_to_corpus) as new_to_corpus,
           sum(c.changed) as changed,
           sum(c.requests) as requests,
           sum(c.elapsed_ms) as elapsed_ms,
           avg(extract(epoch from c.claimed_at - c.enqueued_at))
             filter (where c.claimed_at is not null and c.enqueued_at is not null)
             as mean_queue_delay_seconds
      from public.job_hunter_source_crawls c
      cross join bounds b
     where c.started_at > b.since
       and c.started_at <= p_now
       and c.source_key in (select t.crawl_key from targets t)
     group by 1, 2, 3
  ),
  grid as (
    select t.crawl_key, d.iso_weekday, h.utc_hour
      from targets t
      cross join generate_series(1, 7) as d(iso_weekday)
      cross join generate_series(0, 23) as h(utc_hour)
  ),
  measured as (
    select g.crawl_key,
           g.iso_weekday,
           g.utc_hour,
           coalesce(w.crawls, 0)::integer as crawls,
           coalesce(w.scheduled_crawls, 0)::integer as scheduled_crawls,
           coalesce(w.safety_crawls, 0)::integer as safety_crawls,
           coalesce(w.successful_crawls, 0)::integer as successful_crawls,
           coalesce(w.failures, 0)::integer as failures,
           coalesce(w.rate_limited, 0)::integer as rate_limited,
           coalesce(w.new_to_corpus, 0)::integer as new_to_corpus,
           coalesce(w.changed, 0)::integer as changed,
           coalesce(w.requests, 0)::integer as requests,
           coalesce(w.elapsed_ms, 0)::bigint as elapsed_ms,
           coalesce(cv.hours, 0)::double precision as covered_hours,
           w.mean_queue_delay_seconds::double precision as mean_queue_delay_seconds,
           r.per_hour as target_novelty_per_hour,
           coalesce(r.per_hour, 0) * coalesce(cv.hours, 0) as expected_novelty,
           h.complete as history_complete,
           b.significance
      from grid g
      cross join bounds b
      join history h on h.crawl_key = g.crawl_key
      left join per_window w
        on w.source_key = g.crawl_key
       and w.iso_weekday = g.iso_weekday
       and w.utc_hour = g.utc_hour
      left join covered cv
        on cv.source_key = g.crawl_key
       and cv.iso_weekday = g.iso_weekday
       and cv.utc_hour = g.utc_hour
      left join target_rate r on r.source_key = g.crawl_key
  )
  select m.crawl_key,
         m.iso_weekday,
         m.utc_hour,
         m.crawls,
         m.scheduled_crawls,
         m.safety_crawls,
         m.successful_crawls,
         m.failures,
         m.rate_limited,
         m.new_to_corpus,
         m.changed,
         m.requests,
         m.elapsed_ms,
         m.covered_hours,
         (m.new_to_corpus + m.changed)::double precision / nullif(m.crawls, 0),
         (m.new_to_corpus + m.changed)::double precision / nullif(m.requests, 0),
         (m.new_to_corpus + m.changed)::double precision
           / nullif(m.elapsed_ms / 1000.0, 0),
         m.mean_queue_delay_seconds,
         null::double precision,
         'no_trusted_source_timestamp'::text,
         m.target_novelty_per_hour,
         m.expected_novelty,
         m.history_complete,
         case
           when not m.history_complete or m.covered_hours = 0
             then 'insufficient_evidence'
           when m.new_to_corpus + m.changed = 0
            and m.expected_novelty > -ln(m.significance)
             then 'reduce'
           else 'keep'
         end
    from measured m;
$$;

comment on function public.job_hunter_crawl_window_evidence(timestamptz, text) is
  'Crawl yield and cost by crawl target, ISO weekday and UTC hour over the '
  'lookback, with a keep / reduce / insufficient_evidence verdict per window '
  '(issue #258). Recalculated on every read; nobody runs an analysis.';

-- The per-target enqueue -----------------------------------------------------
--
-- What each crawl target's cron entry now runs, in place of a bare pgmq.send.
-- In a window the evidence currently says to reduce, it enqueues only when
-- the target has had no safety crawl within p_safety_minutes -- the
-- next-slower band's interval -- and none is still queued, and labels that
-- message a safety crawl. So across a run of reduced windows every band is
-- probed at the next-slower cadence: an hourly target's quiet night gets one
-- safety crawl per six hours, a fifteen-minute target's one per hour.
--
-- Only safety crawls count. A scheduled crawl in an adjacent kept window says
-- nothing about a reduced one, and counting it could leave the reduced window
-- unprobed forever. A reduced window whose occurrence keeps falling inside
-- another window's safety interval is not starved either: with no crawl of
-- its own in the lookback it becomes insufficient_evidence, is crawled on
-- schedule at its next occurrence, and is judged again -- so every window is
-- observed at least every other week. Safety crawls feed the same evidence, so one that
-- finds something turns its window back to keep. Everywhere else, and when
-- apply_reductions is off, it enqueues a scheduled crawl exactly as before.
create or replace function public.job_hunter_enqueue_crawl(
  p_payload jsonb,
  p_safety_minutes integer
)
returns bigint
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_key text := p_payload ->> 'crawl_key';
  v_purpose text := 'scheduled';
  v_apply boolean;
  v_recommendation text;
begin
  if coalesce(v_key, '') = '' then
    raise exception 'crawl payload must carry a crawl_key'
      using errcode = '22023';
  end if;
  if coalesce(p_safety_minutes, 0) <= 0 then
    raise exception 'safety interval must be positive'
      using errcode = '22023';
  end if;

  select c.apply_reductions into v_apply
    from public.job_hunter_ingestion_timing_config c;

  if coalesce(v_apply, false) then
    select e.recommendation into v_recommendation
      from public.job_hunter_crawl_window_evidence(now(), v_key) e
     where e.iso_weekday = extract(isodow from now() at time zone 'UTC')::integer
       and e.utc_hour = extract(hour from now() at time zone 'UTC')::integer;

    if v_recommendation = 'reduce' then
      if exists (
           select 1 from public.job_hunter_source_crawls c
            where c.source_key = v_key
              and c.purpose = 'safety'
              and c.started_at > now() - make_interval(mins => p_safety_minutes))
         or exists (
           select 1 from pgmq.q_job_hunter_crawl_source q
            where q.message ->> 'crawl_key' = v_key
              and q.message ->> 'purpose' = 'safety') then
        return null;
      end if;
      v_purpose := 'safety';
    end if;
  end if;

  return (
    select pgmq.send(
      'job_hunter_crawl_source',
      p_payload || jsonb_build_object('purpose', v_purpose)
    )
  );
end;
$$;

comment on function public.job_hunter_enqueue_crawl(jsonb, integer) is
  'Enqueue one crawl target''s crawl, or, in a window its evidence says to '
  'reduce, at most one labelled safety crawl per p_safety_minutes (issue #258).';

-- The scheduler installs the enqueue above -----------------------------------
--
-- Unchanged from #184 except for the command each entry runs and the safety
-- interval it is given: the next-slower band, or the slowest band itself.
-- The band array and cron shapes must still match source_schedule.py; the
-- Python test reads whichever migration last defines this function.
create or replace function public.job_hunter_reschedule_sources()
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_bands int[] := array[15, 60, 360, 1440, 4320, 10080];
  v_source record;
  v_index int;
  v_minute int;
  v_hour int;
  v_schedule text;
  v_slug text;
  v_safety int;
  v_count int := 0;
begin
  for v_source in
    select jobname from cron.job
     where jobname like 'job-hunter-enqueue-crawl-source-%'
  loop
    perform cron.unschedule(v_source.jobname);
  end loop;

  for v_source in
    select s.crawl_key,
           coalesce((
             select count(*)
               from (
                 select c.outcome, c.new_to_corpus + c.changed as novelty
                   from public.job_hunter_source_crawls c
                  where c.source_key = s.crawl_key
                  order by c.started_at desc
                  limit 6
               ) recent
              where recent.outcome in ('rate_limited', 'failed')
                 or recent.novelty = 0
           ), 0) as demotions,
           coalesce((
             select count(*)
               from (
                 select c.outcome, c.new_to_corpus + c.changed as novelty
                   from public.job_hunter_source_crawls c
                  where c.source_key = s.crawl_key
                  order by c.started_at desc
                  limit 6
               ) recent
              where recent.outcome not in ('rate_limited', 'failed')
                and recent.novelty > 0
           ), 0) as promotions
      from public.job_hunter_crawl_targets s
     where s.enabled
  loop
    v_index := greatest(0, least(
      array_length(v_bands, 1) - 1,
      3 + v_source.demotions - v_source.promotions
    ));

    v_minute := abs(hashtext(v_source.crawl_key)) % 60;
    v_hour := abs(hashtext(v_source.crawl_key || ':hour')) % 24;

    v_schedule := case v_bands[v_index + 1]
      when 15 then format('%s-59/15 * * * *', v_minute % 15)
      when 60 then format('%s * * * *', v_minute)
      when 360 then format('%s %s-23/6 * * *', v_minute, v_hour % 6)
      when 1440 then format('%s %s * * *', v_minute, v_hour)
      when 4320 then format('%s %s 1-31/3 * *', v_minute, v_hour)
      else format('%s %s * * 4', v_minute, v_hour)
    end;

    v_safety := v_bands[least(v_index + 2, array_length(v_bands, 1))];

    v_slug := public.job_hunter_source_schedule_slug(v_source.crawl_key);
    if v_slug = '' then
      raise exception 'schedule key % has no usable job-name form', v_source.crawl_key
        using errcode = '22023';
    end if;

    perform cron.schedule(
      'job-hunter-enqueue-crawl-source-' || v_slug,
      v_schedule,
      format(
        'select public.job_hunter_enqueue_crawl(%L::jsonb, %s);',
        jsonb_build_object('crawl_key', v_source.crawl_key)::text,
        v_safety
      )
    );
    v_count := v_count + 1;
  end loop;

  return v_count;
end;
$$;

comment on function public.job_hunter_reschedule_sources() is
  'Install one pg_cron entry per enabled crawl target, banded on measured '
  'corpus novelty (issue #184), each running job_hunter_enqueue_crawl so a '
  'reduced crawl window keeps only its safety crawls (issue #258).';

revoke all on function public.job_hunter_worker_health(timestamptz)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_worker_run_evidence(timestamptz)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_crawl_window_evidence(timestamptz, text)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_enqueue_crawl(jsonb, integer)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_reschedule_sources()
  from public, anon, authenticated, service_role;

select public.job_hunter_reschedule_sources();

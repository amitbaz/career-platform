-- Worker-run telemetry and crawl-window evidence (issue #258).
--
-- Most scenarios below are dated in 2001 and pass that date as p_now, so the
-- rows other worktrees' test runs leave on the shared stack cannot fall in
-- their windows. The enqueue reads now(), so its scenario is built relative
-- to the transaction's own clock instead, on crawl keys nobody else uses.
begin;
select no_plan();

-- Shared machinery: nobody holding a user's session reaches any of it --------

select has_table('public', 'job_hunter_worker_runs', 'every worker invocation has a row');
select has_table('public', 'job_hunter_worker_schedules', 'each worker''s schedule is recorded');
select has_table('public', 'job_hunter_ingestion_timing_config', 'timing is configuration');

select is(
  (select array_agg(t.table_name || ' ' || r.role_name order by t.table_name, r.role_name)
     from (values ('job_hunter_worker_runs'), ('job_hunter_worker_schedules'),
                  ('job_hunter_ingestion_timing_config')) as t(table_name)
     cross join (values ('anon'), ('authenticated'), ('service_role')) as r(role_name)
    where has_table_privilege(r.role_name, 'public.' || t.table_name,
                              'select, insert, update, delete')),
  null,
  'no role a user can hold may read or write worker telemetry or its configuration');

select is(
  (select array_agg(p.proname::text || ' ' || r.role_name order by p.proname, r.role_name)
     from pg_proc p
     join pg_namespace n on n.oid = p.pronamespace
     cross join (values ('anon'), ('authenticated'), ('service_role')) as r(role_name)
    where n.nspname = 'public'
      and p.proname in ('job_hunter_worker_health', 'job_hunter_worker_run_evidence',
                        'job_hunter_crawl_window_evidence', 'job_hunter_enqueue_crawl')
      and has_function_privilege(r.role_name, p.oid, 'execute')),
  null,
  'nor execute the functions that read it');

select has_column('public', 'job_hunter_source_crawls', 'purpose',
  'a crawl says whether it was scheduled or a safety crawl');
select has_column('public', 'job_hunter_source_crawls', 'enqueued_at',
  'a crawl carries when its message was enqueued');
select has_column('public', 'job_hunter_source_crawls', 'worker_run_id',
  'a crawl names the worker run that performed it');

select throws_ok(
  $$ insert into public.job_hunter_worker_runs
       (worker, started_at, finished_at, stale_after_seconds)
     values ('crawl_source', '2001-01-01', '2001-01-01', 900) $$,
  '23514', null,
  'a finished run must say why it stopped');

select throws_ok(
  $$ insert into public.job_hunter_source_crawls (source_key, outcome, purpose)
     values ('purpose-check', 'fetched', 'whenever') $$,
  '23514', null,
  'a crawl purpose is scheduled or safety');

-- An empty invocation is evidence, not an absent row --------------------------

insert into public.job_hunter_worker_runs
  (worker, started_at, heartbeat_at, finished_at, stale_after_seconds, claimed,
   stop_reason, elapsed_ms)
values
  ('crawl_source', '2001-01-01 10:00Z', '2001-01-01 10:00:02Z', '2001-01-01 10:00:02Z',
   900, 0, 'queue_empty', 2000),
  ('crawl_source', '2001-01-01 10:15Z', '2001-01-01 10:20Z', '2001-01-01 10:20Z',
   900, 4, 'queue_empty', 300000);
update public.job_hunter_worker_runs
   set queue_delay_total_ms = 40000, queue_delay_max_ms = 20000
 where started_at = '2001-01-01 10:15Z' and worker = 'crawl_source';

select results_eq(
  $$ select runs, empty_runs, empty_ratio, compute_seconds, claimed,
            mean_queue_delay_seconds, max_queue_delay_seconds
       from public.job_hunter_worker_run_evidence('2001-01-02')
      where worker = 'crawl_source' and iso_weekday = 1 and utc_hour = 10 $$,
  $$ values (2, 1, 0.5::double precision, 302::double precision, 4::bigint,
             10::double precision, 20::double precision) $$,
  'a run that woke to an empty queue counts as an empty run, and one that claimed work does not');

-- Health ----------------------------------------------------------------------

update public.job_hunter_worker_schedules set expected_since = '2000-01-01';

insert into public.job_hunter_worker_runs
  (worker, started_at, heartbeat_at, finished_at, stale_after_seconds, stop_reason)
values
  ('crawl_source', '2002-01-01 11:50Z', '2002-01-01 11:51Z', '2002-01-01 11:51Z', 900, 'queue_empty'),
  ('extract_facets', '2002-01-01 11:50Z', '2002-01-01 11:51Z', '2002-01-01 11:51Z', 300, 'queue_empty'),
  ('recheck_freshness', '2002-01-01 06:00Z', '2002-01-01 06:10Z', '2002-01-01 06:10Z', 900, 'limit'),
  ('recover_posting', '2002-01-01 11:58Z', '2002-01-01 11:58:30Z', '2002-01-01 11:58:30Z', 300, 'queue_empty');

select results_eq(
  $$ select worker, status from public.job_hunter_worker_health('2002-01-01 12:00Z') $$,
  $$ values ('crawl_source', 'ok'), ('extract_facets', 'ok'), ('recheck_freshness', 'ok'),
            ('recover_posting', 'ok') $$,
  'every worker that ran on schedule is healthy');

-- A run in progress, heartbeating within its own stale window, is not a failure.
insert into public.job_hunter_worker_runs
  (worker, started_at, heartbeat_at, stale_after_seconds)
values ('crawl_source', '2002-01-01 11:55Z', '2002-01-01 11:58Z', 900);

select is(
  (select status from public.job_hunter_worker_health('2002-01-01 12:00Z')
    where worker = 'crawl_source'),
  'ok',
  'a run still heartbeating is in progress, not unfinished');

select is(
  (select status from public.job_hunter_worker_health('2002-01-01 12:30Z')
    where worker = 'crawl_source'),
  'unfinished',
  'a run silent for longer than its own stale window is reported unfinished');

select ok(
  (select detail like '%last heartbeat%' from public.job_hunter_worker_health('2002-01-01 12:30Z')
    where worker = 'crawl_source'),
  'and the report says which run and when it went quiet');

select is(
  (select status from public.job_hunter_worker_health('2002-01-01 12:45Z')
    where worker = 'extract_facets'),
  'missing',
  'a worker with no run started in two expected intervals is reported missing');

select is(
  (select status from public.job_hunter_worker_health('2002-01-01 17:00Z')
    where worker = 'recheck_freshness'),
  'ok',
  'a six-hourly worker is judged on its own interval, not the fastest one');

select is(
  (select status from public.job_hunter_worker_health('2002-01-01 12:00Z')
    where worker = 'recheck_freshness'),
  'ok',
  'the same worker, earlier in its interval, is healthy too');

update public.job_hunter_worker_schedules set expected_since = '2003-06-01'
 where worker = 'recheck_freshness';
select is(
  (select status from public.job_hunter_worker_health('2003-06-01 06:00Z')
    where worker = 'recheck_freshness'),
  'ok',
  'a worker that has not had time for its first run yet is not missing');
-- The 2002 run exists, but it predates the schedule, so it does not count.
select ok(
  (select status = 'missing' and detail like 'no run recorded%'
     from public.job_hunter_worker_health('2003-06-01 13:00Z')
    where worker = 'recheck_freshness'),
  'but one that never ran after two intervals is');

-- Crawl-window evidence -------------------------------------------------------
--
-- 'window-hourly' is crawled every hour for fourteen days before 2001-03-01,
-- finding four new postings each time, except in the 03:00 UTC hour where it
-- never finds anything. At its own rate a one-hour window should see about
-- 3.8 postings, and exp(-3.8) is well under 0.05, so 03:00 is reduced.

insert into public.job_hunter_crawl_targets (crawl_key) values
  ('window-hourly'), ('window-noon-only'), ('window-young')
  on conflict (crawl_key) do nothing;

insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, requests, elapsed_ms,
   started_at, finished_at, enqueued_at, claimed_at)
select 'window-hourly', 'fetched', 4,
       case when extract(hour from t at time zone 'UTC') = 3 then 0 else 4 end,
       -- Claimed ten seconds before the crawl started: the queue delay is
       -- enqueue to claim, and the batch's processing time is not part of it.
       0, 2, 4000, t, t, t - interval '70 seconds', t - interval '10 seconds'
  from generate_series(timestamptz '2001-02-15 00:00Z', timestamptz '2001-02-28 23:00Z',
                       interval '1 hour') t;

select is(
  (select count(*)::int from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-hourly')),
  168,
  'every weekday and UTC hour of a target gets a row, crawled or not');

select is(
  (select recommendation from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-hourly')
    where iso_weekday = 3 and utc_hour = 3),
  'reduce',
  'a window that stays silent where the target''s own rate predicts output is reduced');

select is(
  (select recommendation from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-hourly')
    where iso_weekday = 3 and utc_hour = 10),
  'keep',
  'a productive window is kept');

select results_eq(
  $$ select crawls, successful_crawls, new_to_corpus, requests, elapsed_ms,
            covered_hours, novelty_per_crawl, novelty_per_request,
            novelty_per_compute_second, mean_queue_delay_seconds
       from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-hourly')
      where iso_weekday = 3 and utc_hour = 10 $$,
  $$ values (1, 1, 4, 2, 4000::bigint, 1::double precision, 4::double precision,
             2::double precision, 1::double precision, 60::double precision) $$,
  'a window reports its yield per crawl, request and compute second, and its queue delay');

select results_eq(
  $$ select published_to_first_seen_seconds, published_to_first_seen_reason
       from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-hourly')
      where iso_weekday = 3 and utc_hour = 10 $$,
  $$ values (null::double precision, 'no_trusted_source_timestamp'::text) $$,
  'publication delay is withheld, and says why, while no source timestamp is trusted');

select is(
  (select count(*)::int from public.job_hunter_crawl_window_evidence('2001-02-20', 'window-hourly')
    where recommendation <> 'insufficient_evidence'),
  0,
  'before one complete rolling week of history, no window is judged');

-- A safety crawl in the reduced window that finds something turns it back.
insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at, finished_at, purpose)
values ('window-hourly', 'fetched', 2, 2, 0, '2001-02-28 03:30Z', '2001-02-28 03:30Z', 'safety');

select results_eq(
  $$ select safety_crawls, recommendation
       from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-hourly')
      where iso_weekday = 3 and utc_hour = 3 $$,
  $$ values (1, 'keep'::text) $$,
  'a safety crawl contributes evidence, and one that finds postings turns a reduced window back to keep');

-- A target only ever crawled at noon has nothing to say about 05:00.
insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at, finished_at)
select 'window-noon-only', 'fetched', 1, 1, 0, t, t
  from generate_series(timestamptz '2001-02-15 12:00Z', timestamptz '2001-02-28 12:00Z',
                       interval '1 day') t;

select is(
  (select recommendation from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-noon-only')
    where iso_weekday = 3 and utc_hour = 5),
  'insufficient_evidence',
  'a window with no crawls in the lookback is insufficient evidence, not a verdict');

-- A target younger than the lookback is judged nowhere, however silent.
insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at, finished_at)
select 'window-young', 'fetched', 1, 0, 0, t, t
  from generate_series(timestamptz '2001-02-26 00:00Z', timestamptz '2001-02-28 23:00Z',
                       interval '1 hour') t;

select is(
  (select count(*)::int from public.job_hunter_crawl_window_evidence('2001-03-01', 'window-young')
    where recommendation <> 'insufficient_evidence'),
  0,
  'a target without a complete week of history is not judged in any window');

-- The enqueue acts on the verdict ---------------------------------------------
--
-- 'enqueue-reduced' repeats the hourly pattern relative to now(): silent in
-- the current UTC hour, productive in every other. Each crawl sits half way
-- between its hour's matching minute and the end of that hour, so the crawl
-- exactly a week ago still falls inside the lookback and inside the current
-- weekday and hour.

insert into public.job_hunter_crawl_targets (crawl_key) values
  ('enqueue-reduced'), ('enqueue-unknown')
  on conflict (crawl_key) do nothing;

insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at, finished_at)
select 'enqueue-reduced', 'fetched', 4,
       case when n % 24 = 0 then 0 else 4 end, 0, t, t
  from generate_series(1, 336) n,
       lateral (select now() - make_interval(hours => n)
                  + (date_trunc('hour', now()) + interval '1 hour' - now()) / 2 as t) at;

select is(
  (select recommendation from public.job_hunter_crawl_window_evidence(now(), 'enqueue-reduced')
    where iso_weekday = extract(isodow from now() at time zone 'UTC')::int
      and utc_hour = extract(hour from now() at time zone 'UTC')::int),
  'reduce',
  'the enqueue scenario is reduced in the current window');

-- Each enqueue runs as its own statement, into a temp table: a row the
-- function inserts is invisible to the statement that called it.
--
-- The safety interval here is 360 minutes, what an hourly target is given.
-- The target crawled in the previous hour, well inside it, but that was a
-- scheduled crawl in a kept window and says nothing about this one. Counting
-- it is the defect that could leave a reduced window unprobed forever.
create temp table enqueued_safety as
  select public.job_hunter_enqueue_crawl('{"crawl_key": "enqueue-reduced"}'::jsonb, 360) as msg_id;
select is(
  (select q.message ->> 'purpose' from pgmq.q_job_hunter_crawl_source q
     join enqueued_safety e on e.msg_id = q.msg_id),
  'safety',
  'scheduled crawls in adjacent kept windows do not stand in for a reduced window''s safety crawl');

select is(
  public.job_hunter_enqueue_crawl('{"crawl_key": "enqueue-reduced"}'::jsonb, 360),
  null,
  'and not a second one while it is still queued');

-- Once a safety crawl has run -- here two hours ago, in an earlier reduced
-- hour of the same quiet stretch -- an hourly target is not probed again for
-- six hours. This is what makes reduce real for the hourly band and slower,
-- not only the fifteen-minute one.
select pgmq.delete('job_hunter_crawl_source', (select msg_id from enqueued_safety));
insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at, finished_at, purpose)
values ('enqueue-reduced', 'fetched', 0, 0, 0,
        now() - interval '2 hours', now() - interval '2 hours', 'safety');

select is(
  public.job_hunter_enqueue_crawl('{"crawl_key": "enqueue-reduced"}'::jsonb, 360),
  null,
  'an hourly target''s reduced hours get one safety crawl per six hours, not one per hour');

select isnt(
  public.job_hunter_enqueue_crawl('{"crawl_key": "enqueue-reduced"}'::jsonb, 60),
  null,
  'while a fifteen-minute target, whose safety interval is an hour, is probed again');

update public.job_hunter_ingestion_timing_config set apply_reductions = false;
create temp table enqueued_unreduced as
  select public.job_hunter_enqueue_crawl('{"crawl_key": "enqueue-reduced"}'::jsonb, 180) as msg_id;
select is(
  (select q.message ->> 'purpose' from pgmq.q_job_hunter_crawl_source q
     join enqueued_unreduced e on e.msg_id = q.msg_id),
  'scheduled',
  'with reductions switched off, the verdict is still computed but the enqueue ignores it');
update public.job_hunter_ingestion_timing_config set apply_reductions = true;

create temp table enqueued_unknown as
  select public.job_hunter_enqueue_crawl('{"crawl_key": "enqueue-unknown"}'::jsonb, 60) as msg_id;
select is(
  (select q.message from pgmq.q_job_hunter_crawl_source q
     join enqueued_unknown e on e.msg_id = q.msg_id),
  '{"crawl_key": "enqueue-unknown", "purpose": "scheduled"}'::jsonb,
  'a target without a verdict is enqueued as a scheduled crawl, as before');

select throws_ok(
  $$ select public.job_hunter_enqueue_crawl('{}'::jsonb, 60) $$,
  '22023', null,
  'a payload without a crawl key is refused');

-- The scheduler installs the enqueue, with the next-slower band as its safety interval.
insert into public.job_hunter_crawl_targets (crawl_key) values ('enqueue-productive')
  on conflict (crawl_key) do nothing;
insert into public.job_hunter_source_crawls
  (source_key, outcome, fetched, new_to_corpus, changed, started_at)
select 'enqueue-productive', 'fetched', 10, 5, 5, now() - make_interval(hours => n)
  from generate_series(1, 6) n;

select public.job_hunter_reschedule_sources();

select ok(
  (select command like 'select public.job_hunter_enqueue_crawl(%"crawl_key": "enqueue-productive"%, 60);'
     from cron.job
    where command like '%"crawl_key": "enqueue-productive"%'),
  'a fifteen-minute target''s cron entry runs the enqueue with the hourly band as its safety interval');

select * from finish();
rollback;

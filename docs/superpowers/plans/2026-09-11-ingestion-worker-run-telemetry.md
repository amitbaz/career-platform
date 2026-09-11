# Ingestion worker-run telemetry and crawl-window evidence (#258)

Design: `docs/superpowers/specs/2026-09-11-engine-ready-stack-design.md`, "Ingestion timing
measurement". This plan records the implementation decisions the design left to the ticket.

## What exists

- `job_hunter_source_crawls` records one row per crawl attempt, but `started_at` defaults to
  insert time, and the insert happens after the crawl, so today `started_at` is the finish
  time. Queue delay cannot be derived from it.
- The three Render cron workers (`crawl-source`, `extract-facets`, `recheck-freshness`) log a
  `stopped_because` and outcome counts, and write nothing when the queue is empty.
- `job_hunter_reschedule_sources()` bands each crawl target on novelty and installs one pg_cron
  entry whose command is `select pgmq.send(...)`.

## Decisions

1. **One row per worker invocation** (`job_hunter_worker_runs`), inserted before the drain
   starts and updated after every batch (`heartbeat_at`, `claimed`, `outcomes`) and at finish
   (`finished_at`, `stop_reason`, `elapsed_ms`, queue delay). `queue_empty` is a finished run
   with `stop_reason = 'queue_empty'`. An exception finishes the run with `stop_reason =
   'error'`; a killed process leaves it unfinished on purpose.
2. **Health is derived from definitions, not tuned numbers.**
   - *Unfinished:* a run whose last heartbeat is older than its own `stale_after_seconds`. That
     value is the drain's visibility timeout, written on the row by the worker: a batch that
     outlives it has already been handed to another worker, so a run silent for that long is
     dead or broken by the queue's own definition.
   - *Missing:* no run started within two expected intervals, meaning at least one whole
     scheduled invocation did not happen. Expected intervals live in
     `job_hunter_worker_schedules`, seeded from `render.yaml`; an integration test fails when
     the two disagree.
   - Unfinished runs stay a failure for 24 hours after they start. That is a display window,
     not a detection threshold.
   - `job_hunter_worker_health()` returns one row per worker. Every worker invocation reads it
     after finishing, logs each failure, and exits 1 if any worker is unhealthy, so a failure
     turns a Render cron run red rather than scrolling out of a log. Known gap: if every Render
     worker stops, nothing runs the check; Engine Lab (#264) reads the same function.
3. **Queue delay** comes from pgmq's `enqueued_at`, carried on `QueueMessage`. Each drain
   records the total and the maximum across its claimed messages. The crawl row also stores
   `enqueued_at`, `worker_run_id`, `purpose` and a real `started_at`.
4. **Source-published-to-first-seen delay is not reported.** No adapter captures a source
   publication timestamp (`Job` has no such field), so the evidence column is always null and
   carries the reason `no_trusted_source_timestamp`. A test fails when `Job` gains a
   publication-time field, so the column is wired up rather than left null.
5. **Crawl-window evidence** (`job_hunter_crawl_window_evidence`) is one row per enabled crawl
   target × ISO weekday × UTC hour (168 per target), over a rolling lookback (7 days by
   default). Novelty is `new_to_corpus + changed`, the measure the band scheduler already uses.
   Each successful crawl covers the hours since the previous successful crawl of the same
   target, so a sparse safety crawl and a dense scheduled one are comparable.
6. **The recommendation is a test of significance, not a threshold on counts.**
   - `insufficient_evidence` when the target's history does not yet span the whole lookback (a
     full rolling week always contains a weekend), or the window has no successful crawl in it.
   - `reduce` when the window produced no novelty, and the target's own rate (novelty per
     covered hour, across the lookback) makes that silence less likely than the configured
     significance: `exp(-rate × window_hours) < significance`, 0.05 by default.
   - `keep` otherwise.

   Scale-free, like the aggregator signal: a target with 2 new postings a day and one with 200
   are judged against their own rates. Only silent windows are reduced. A quiet window is kept.
7. **Reduction is enforced in the enqueue, with labelled safety crawls.** The per-target cron
   command becomes `select public.job_hunter_enqueue_crawl(<payload>, <safety minutes>)`. In a
   window currently recommended `reduce`, it enqueues only when the target has neither crawled
   nor had a message queued within the next-slower band's interval, and the message carries
   `purpose = 'safety'`. Safety crawls feed the same evidence, so one that finds novelty turns
   its window back to `keep`. `apply_reductions`, `significance` and `lookback_days` live in
   `job_hunter_ingestion_timing_config`, one row.
8. **Worker evidence** (`job_hunter_worker_run_evidence`) is worker × ISO weekday × UTC hour:
   runs, empty runs, empty ratio, compute seconds, claimed, mean and maximum queue delay, failed
   and unfinished runs.
9. All new tables are shared machinery: row level security with no policy, every grant revoked,
   invoker functions revoked from every user role.

## Steps (test first)

1. Migration `29999999025800_job_hunter_worker_runs.sql` (placeholder number) and pgTAP
   `job_hunter_worker_runs.sql`, plus the table and function lists in
   `job_hunter_isolation.sql` and `job_hunter_store_functions.sql`.
2. `QueueMessage.enqueued_at`, and the claim selects it.
3. `worker_runs.py`: the recorder and the health reader.
4. The drains take `on_batch` and record queue delay. The crawl stage accepts `purpose` and
   records `started_at`, `enqueued_at`, `worker_run_id` and `purpose`.
5. The CLI wraps each of the three drains in a recorded run and runs the health check.
6. `test_source_schedule.py` reads whichever migration last defines
   `job_hunter_reschedule_sources`.
7. Docs: `apps/job-hunter/AGENTS.md`, `CONTEXT.md`, `render.yaml` comments.

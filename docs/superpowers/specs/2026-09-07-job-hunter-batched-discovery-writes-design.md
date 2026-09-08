# Job Hunter: batching discovery's writes to Postgres

Design for issue #97. Follows #70 (the SQLite-to-Postgres store port, shipped in `b1b1d02`)
and the two index fixes that followed it, #93 (`b8604d4`) and #94 (`3005cc5`).

## The problem

The daily run stopped finishing. Run 34094159716 was cancelled by the workflow's
60-minute timeout after producing no output whatsoever for its last 59 minutes:

```
09:16:38 INFO job_hunter.pipeline: profile extraction: source=cache error=none
09:16:54 WARNING job_hunter.sources.base: wellfound rate limited: ...
10:15:51 ##[error]The operation was canceled.
```

The last line before the silence comes from source discovery. The next line the
pipeline would have written is the `discovery: raw=...` summary in `run_pipeline`.
Between those two points sits exactly one function, `collect_candidates`, and it held
the process for the entire hour.

Nothing failed. The run was doing the work it was asked to do, one network round trip
at a time.

## Why it is slow

`collect_candidates` persists as it goes, and every persist is an HTTP request to
PostgREST:

| Call site | Cost per job |
| --- | --- |
| `discovery.py:328` — `upsert_logical_job` over every raw job | 1 request |
| `discovery.py:345` — `upsert_logical_job` over every unique job | 1 request |
| `discovery.py:350` — `set_job_market` | 1 request |
| `discovery.py:356` — `needs_evaluation` | 2 requests |
| `ats_registry.py:50` — `upsert_ats_board`, for jobs on a supported ATS | 2 requests |

The last pre-migration run reported `raw=18839 unique=12819`. Those counts put a normal
day at roughly 70,000 sequential requests, all of them before the pipeline logs its
first discovery number. At 50 ms per round trip that is 58 minutes; at 100 ms it is
close to two hours. The observed behaviour — an hour of silence, no error — is what
that arithmetic predicts.

Under SQLite these were in-process function calls costing microseconds, which is why
the design was invisible until #70 moved the store behind the network. The indexes
added in #93 and #94 cut the server-side time of each call but not their number, so
they could not have fixed this.

The schema offers no way out: `job_hunter_upsert_job(p_job jsonb)` in
`202609060004_job_hunter_store_functions.sql` takes one job.

## The shape of the fix

Send batches. The per-job work is genuinely per-job, but it does not need a network
round trip wrapped around each unit of it — a Postgres function can loop over an array
server-side at no I/O cost at all.

Three principles constrain the design:

1. **Do not reimplement identity resolution.** `job_hunter_upsert_job` resolves a job's
   identity from canonical URL, ATS triple, normalized company/title/location, and
   fingerprint, merging every duplicate it finds. It is the most intricate SQL in the
   repository, ported line by line from `store.py:744-905`. The batch path calls it;
   it does not restate it.
2. **The request count must stop tracking the job count.** After this change the writes
   in discovery's batch phases (design steps 2 and 5-7 below: raw upsert, unique upsert,
   market attribution, evaluation-need lookup, board registration, terminal statuses) are
   bounded by the number of chunks, not the number of postings.

   This is scoped to those phases on purpose. The canonical-resolution tail
   (`discovery.py`'s final loop over `prefiltered`) is **not** batched — see
   [Out of scope](#out-of-scope) — and remains per-job: a job that reaches the resolve
   branch pays roughly six to eight PostgREST requests (`_harvest_ats_board_safely`,
   `upsert_logical_job`, `set_job_market`, `needs_evaluation`, and, once eligible,
   `record_ats_eligible_job`). **Superseded in part by #151:** eligibility recording is
   no longer among them -- it is collected during the loop and flushed once, through
   `record_ats_eligible_jobs`. The rest of the tail is still per-job. `max_canonical_resolutions_per_run` bounds only the jobs
   whose URL is *not* already a supported ATS URL; a prefiltered job that already carries
   one bypasses the shortlist gate entirely and always resolves. So a run where 1,000
   ATS-hosted jobs survive prefilter still spends 6,000-8,000 sequential requests in that
   tail. That is this branch's largest remaining scaling risk, and the next thing to
   batch if the daily run approaches its timeout again.
3. **One bad posting must not cost a run.** Today an exception from any of those calls
   propagates and kills the process. Batching must not make that worse, and should make
   it better.

## The SQL surface

A new migration, `202609070003_job_hunter_batch_discovery_writes.sql`, adds three
functions. Each is `security invoker` with `set search_path = ''`, matching the six
existing store functions, so row-level security continues to decide every row the
caller can see or touch.

### `job_hunter_upsert_jobs(p_jobs jsonb)`

```
returns table (input_index int, id uuid, is_new boolean, description_changed boolean)
```

Iterates the array with `jsonb_array_elements` and `with ordinality`, calling
`job_hunter_upsert_job` on each element and returning one row per input element,
tagged with its zero-based index in the input array. That index is what lets the caller
zip results back onto the Python list it sent; ids alone would not, because a batch may
contain two source copies of one logical job that resolve to the same id. (`position`
would be the obvious column name and is deliberately avoided — it is a SQL keyword.)

Order matters and is preserved. Two jobs in one chunk can resolve to the same identity,
in which case the second call merges into the first exactly as two sequential calls
would today.

There is no `exception` block. A failure aborts the chunk and raises to the caller (see
"Failure handling").

### `job_hunter_needs_evaluation(p_job_ids uuid[])`

```
returns table (job_id uuid, needs boolean)
```

Replaces the two-request-per-job pattern in `PostgresJobStore.needs_evaluation`. The
existing method reads a job's evaluations, then the job row, and compares the stored
description hash and content confidence against what was evaluated. This function does
the same comparison as one query over both tables for the whole id array.

Ids the caller does not own return no row at all rather than a `false`, because RLS
filters them before the function sees them. The Python wrapper treats a missing id as
"needs evaluation", matching what the per-job method does with a job it cannot read.

### `job_hunter_set_job_markets(p_rows jsonb)`

```
returns void
```

A single `update ... from jsonb_to_recordset(p_rows) as r(id uuid, market_id text)`.
PostgREST cannot express "set a different value on each of these rows" in one request,
which is the only reason this needs to be a function at all.

## The store methods

`PostgresJobStore` gains four methods. The existing single-job methods stay: the
Telegram webhook and the cover-letter workflow persist one job at a time and should
keep doing so.

- `upsert_logical_jobs(jobs) -> list[tuple[str, bool, bool]]` — chunks, calls
  `job_hunter_upsert_jobs`, returns results in input order.
- `needs_evaluation_bulk(job_ids) -> dict[str, bool]`
- `set_job_markets(pairs)` — skips the call entirely when no job was attributed.
- `upsert_ats_boards(references) -> int` — takes every ATS board sighting from
  the run and returns how many were newly admitted. It asks the registry once
  per **distinct** (provider, board) pair through the existing single-board
  method. A run sights a board once per job that references it, so thousands
  of sightings collapse to dozens of calls; that already breaks the tie to job
  count, and a fourth SQL function would buy a constant factor on dozens of
  calls at the cost of more SQL to maintain.

Chunk size is 100 jobs. The bound that matters is not payload size but time: each call to
`job_hunter_upsert_jobs` runs a whole chunk's identity resolutions inside one transaction,
against Supabase's default 8-second `statement_timeout` on the `authenticated` role and the
client's 25-second read timeout. A chunk that exceeds either comes back as a 500 or 504,
which is retryable, so an oversized chunk burns its retries and backoff before falling back
to replaying every job in it one at a time — turning a run that should be faster into one
that is worse than before batching existed. 100 keeps a chunk's server-side work well inside
the 8-second budget. A run makes two upsert passes, over raw jobs and unique jobs, so both
passes count toward the total: at 100 per chunk a normal run of raw=18,839 and unique=12,819
costs roughly 318 upsert calls, against the ~70,000-request-per-job baseline.

## Failure handling

`job_hunter_upsert_jobs` runs inside one transaction, so an error anywhere in a chunk
rolls back that whole chunk. Recovering row-by-row inside plpgsql would mean wrapping
each element in a `begin ... exception` block, and Postgres implements such a block as
a subtransaction — a savepoint per job, on every run, to insure against a case that may
never happen.

The recovery lives in Python instead. `upsert_logical_jobs` sends a chunk; if the call
raises, it replays that chunk one job at a time through the existing single-job RPC,
logs the job that fails with its source and URL, skips it, and carries on. Clean runs —
the overwhelmingly common case — pay nothing. A poison posting costs one chunk's worth
of extra calls and no longer takes the run down with it.

That fallback only makes sense for an isolated bad chunk. A poison posting is a property
of one row, so it fails one chunk and the next chunk succeeds; scattered bad postings
never trip anything further. But if the batch path itself is broken — the function
missing, the role's `statement_timeout` cutting every call, PostgREST unreachable — every
chunk fails, and replaying them all one job at a time would issue tens of thousands of
requests to produce a run slower than the unbatched code this replaces, while looking in
the log like a run of bad luck rather than an outage. `upsert_logical_jobs` counts
consecutive chunk failures, resetting the count on any success, and once three chunks in
a row fail it stops falling back and raises instead, naming the real problem. So isolated
bad postings are skipped and the run continues; a systemic failure aborts loudly instead
of silently degrading into something worse than the bug this branch fixes.

## Restructuring `collect_candidates`

The current loop interleaves network fetches with per-job writes. Separating those two
concerns is what makes batching possible, and the result reads more clearly than what
it replaces:

1. **Discover and attribute.** Source calls, ATS identity, content confidence, cheap
   market attribution. In memory, unchanged.
2. **Batch-upsert the raw jobs.** Provenance for every source copy, as today.
3. **Deduplicate.** `_dedupe`, unchanged.
4. **Network work on unique jobs.** `enrich_job` for a job with a URL and no
   description. This stays per-job: it is an outbound fetch to a job board, not a
   database call, and it is bounded by how many jobs lack descriptions.
5. **Batch-upsert the unique jobs** and collect the ids.
6. **Batch the remaining reads and writes.** `set_job_markets`, `needs_evaluation_bulk`,
   and `upsert_ats_boards` over the run's board sightings, which it collapses to the
   distinct boards.
7. **Prefilter and count.** Availability, prefilter, per-market and per-source stats. No
   I/O at all in this loop.

Two details survive the move and are worth naming, because losing either would be a
silent regression:

- ATS board harvesting must still see a job's **observed** market hint, computed before
  the full attribution in step 6 overwrites `job.market_id`. The reference and its hint
  are captured in step 4 and carried into the batch.
- A job whose `needs_evaluation` is false is recorded in `rediscovered_job_ids`, which
  `run_pipeline` uses to requeue pending deliveries. That list is built in step 7 from
  the bulk result and must keep the same membership.

## What does not change

- Identity, merging, and provenance: the same function decides all three.
- The order in which jobs are persisted.
- Every counter in `DiscoveryStats`, and therefore the `discovery:` log line and the
  per-market and per-source metrics that follow it.
- The single-job store methods and their callers.
- The workflow's 60-minute timeout. Raising it would hide this rather than fix it.

## Testing

- **pgTAP**, in the existing suite: ordinality lines up with input order for each of the
  three functions; a second user's ids return nothing (RLS holds); empty input is a
  no-op; two elements of one chunk that share an identity merge exactly as two separate
  calls do.
- **Python unit tests** against the fake Supabase client asserting *request counts*, not
  only results. The regression being prevented is "one request per job", so a test that
  only checks the returned data would not catch its return. A run of N jobs must issue
  a number of requests bounded by ceil(N/100) plus a constant.
- **Failure fallback**, unit level: a chunk whose call raises replays per job, the bad
  job is skipped and logged, and every other job in the chunk is persisted.
- **Integration**, against the local stack, in `tests/integration/`: a batch upsert
  round-trips through real PostgREST with a real token.
- Full `pytest -q` from `apps/job-hunter/.venv`, plus `pnpm db:test`.

## Rollout

The migration must be pushed to the hosted project (`supabase db push`) before or with
the merge: the deployed workflow runs against `main`, and the new store methods call
functions that will not exist until it is applied. No data migration and no backfill —
the change adds functions and rewrites a loop; no table, column, or stored row changes
shape.

## Out of scope

- The `Sync Gmail intelligence` step failing on every run with `Required environment
  variable 'GMAIL_CLIENT_ID' is not set`, masked by `continue-on-error`. Noticed while
  reading these logs, unrelated to this issue, deliberately left alone.
- Evaluation-phase and delivery-phase store calls. They run over the ~100 selected jobs,
  not the ~19,000 discovered ones, so they are not what spends the hour.
- The canonical-resolution loop at the end of `collect_candidates`. It stays per-job.
  (**Since updated by #151**, which took the eligibility write out of that loop and
  batched it; everything else below still holds.)
  Batching it means restructuring a loop that interleaves outbound page fetches, a
  resolver, re-attribution, and store writes whose results feed the next decision — a
  different problem from "the same write, N times". It is bounded for non-ATS URLs by
  `max_canonical_resolutions_per_run` but unbounded for jobs already on a supported ATS
  URL, and that is the cost principle 2 above names.
  `test_collect_candidates_resolver_tail_is_not_batched` records its per-job shape so a
  change to it is visible.

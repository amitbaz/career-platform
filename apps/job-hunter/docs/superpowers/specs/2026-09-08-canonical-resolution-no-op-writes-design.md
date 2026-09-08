# Stop re-writing a job whose URL is already canonical

Issue #160 (parent #114, measured by #120).

## Problem

`collect_candidates`' canonical-resolution tail is 73% of discovery time
(`canonical=2041.1s` of `total=2805.0s` in run 34242178934) while making only 81
network attempts in the whole run. The cost is not resolution; it is the writes
that follow it.

A job whose URL is already a supported ATS URL resolves locally:
`CanonicalResolver.resolve` returns `method="direct"` with the job's own URL and
the reference parsed from it, having decided nothing. The branch then re-applies
the identity the job already carries, re-harvests the board that the batched
`upsert_ats_boards` already registered, re-attributes the market to the same
value, re-upserts the row, re-writes its market, and calls `needs_evaluation`
again for an answer `needs_evaluation_bulk` produced earlier in the same run —
roughly five serial round trips per job, ~6,000 for the run.

1,140 of 1,221 resolutions took that path. Those jobs deliberately bypass the
`max_canonical_resolutions_per_run` shortlist because their resolution is free,
which leaves the expensive half of the branch — the writes — unbounded.

## Approach

Tell a job that resolved to something new from one that resolved to what it
already was, by comparing the job's persisted facts before and after resolution
instead of by inspecting the resolution's `method`.

`_resolution_fingerprint(job)` captures exactly what the writes that follow
persist: `url`, `canonical_url`, the ATS triple, `description`,
`content_confidence` (all carried by `_job_payload`, whose `fingerprint` is
itself derived from `url`), and `market_id` (written by `set_job_market`). The
branch snapshots it before resolving, applies every in-memory effect exactly as
today — identity fill, authoritative description fetch, market re-attribution —
and compares.

- Fingerprint unchanged: skip `upsert_logical_job`, `set_job_market` and
  `needs_evaluation` entirely. The job keeps the id the batched persist gave it
  and the `needs_evaluation_bulk` answer that put it in `prefiltered`, both of
  which are still correct because nothing about the job changed. Counted as
  `canonical_unchanged`.
- Fingerprint changed: write exactly as today, including adopting the
  survivor id `upsert_logical_job` returns when late canonicalization merges
  rows.

Comparing state rather than trusting `method="direct"` keeps the two cases apart
even when a `redirect`/`embedded`/`targeted_search` resolution happens to land
on the job's own URL, and it fails safe: any new field a future resolution step
touches must be added to the fingerprint or the branch keeps writing.

Availability is deliberately not in the fingerprint: it is not persisted by any
job write (no column, no payload key), and a resolution that closes a posting
takes the `set_job_status` branch above this one.

## Board registration

`_harvest_ats_board_safely` is redundant for these jobs, as #160 suspected.
`ats_board_reference` — the pure half of `harvest_ats_board` — already ran for
every unique job in `PHASE_ENRICH`, and `upsert_ats_boards` registered the
result in `PHASE_UNIQUE_PERSIST`. A job already on a supported ATS URL therefore
had its board written minutes earlier, from the same reference.

So the branch stops calling the store per job. It records the `(provider,
board)` pairs the batched phase already registered, collects a reference only
for a board not yet registered this run, and flushes them once after the loop
through `upsert_ats_boards` — the same batched write the rest of discovery uses,
which also collapses two jobs resolving onto one new board into a single
registration. `stats.ats_boards_discovered` now takes that flush's return value
instead of counting per-job harvests.

## Acceptance

- No store writes during canonical resolution for a job that was already
  canonical.
- A genuine resolution persists exactly as before, survivor id included.
- Resolution-phase round trips scale with jobs that resolved to something new.
- No board is registered twice in one run.
- Which jobs a run discovers, deduplicates, filters, ranks or delivers is
  unchanged.

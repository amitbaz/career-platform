# ATS identity attribution on discovered jobs

Issue: #60 — Populate ATS provider, board, and job id on discovered jobs.

## Problem

87% of stored jobs that carry a recognisable ATS URL (10,379 of 11,871 in the
`job-hunter-state` corpus of 2026-09-05) have an empty `ats_provider` and
`ats_board`. Most of them came from the Ashby, Lever and Greenhouse adapters
themselves, which were invoked with a known board identifier and received the
ATS job id in the API payload, yet dropped both on the floor.

The only code path that has ever populated those fields is canonical
resolution (`discovery.collect_candidates`, on `resolution.ats`), and that path
deliberately skips jobs whose URL is already a supported ATS URL — precisely
the jobs the adapters produce.

The cost is that ATS identity, the strongest dedup key the store has
(`JobStore._find_job_ids_by_ats`), is unusable for the large majority of ATS
jobs, so deduplication falls back to canonical URL and fallback identity.
Per-board reporting sees the same fraction of reality.

## Approach

One rule, applied in one helper, at two levels.

### `canonical.apply_ats_identity(job, fallback=None)`

Fills a job's empty `ats_provider` / `ats_board` / `ats_job_id` from the first
available reference:

1. the job's own URLs — `canonical_url`, then `url`, then `original_url`,
   parsed by the existing `parse_supported_ats_url()`;
2. the `fallback` reference the caller knows independently of the URL.

URL-derived identity is preferred over the caller's fallback so that identity
always agrees with what every other code path derives from the same URL
(`ats_registry.extract_ats_reference`, `discovery.candidate_ats_key`,
`JobStore._find_job_ids_by_ats`). Two records of one posting must not end up
with board identifiers that differ only in case or slug spelling, because that
would silently split the dedup key instead of joining it.

Already-populated fields are never overwritten, which is the same
strongest-wins rule the store applies on update (`_update_logical_job` uses
`job.ats_provider or row["ats_provider"]`).

### Adapters supply the fallback

`AshbySource`, `LeverSource` and `GreenhouseSource` each know their provider
and board identifier at construction time and receive the ATS job id in the
listing payload. Each now calls `apply_ats_identity()` with that reference.

The fallback matters where the posting URL is not parseable by
`parse_supported_ats_url()` — most visibly Greenhouse, which serves modern
boards from `job-boards.greenhouse.io` while the parser only recognises
`boards.greenhouse.io`. Those jobs get identity from the adapter's own
knowledge rather than from their URL.

This also covers the learned ATS registry scan, since `LearnedAtsSource`
discovers through these same three adapters.

### Discovery derives identity for every other source

`collect_candidates` calls `apply_ats_identity(job)` once per raw job, before
the first `upsert_logical_job` and before `_dedupe`. A job from any source
whose URL happens to be a supported ATS URL — `search:brave`, `hackernews`,
Gmail — therefore carries identity from its first persistence onward, and the
in-run dedup ATS key (`candidate_ats_key`) can actually match.

### Backfill of existing rows

`JobStore.backfill_ats_identity()` fills identity on stored rows that have a
recognisable ATS URL and no identity, using the same parser. `run_pipeline`
calls it once per run, non-fatally: the DB it operates on lives inside a
GitHub Actions artifact, so a migration that only runs on schema change would
need an artifact round trip to be observed, while a self-healing per-run pass
converges on the first run after deploy and is a no-op on every run after
that. The query is bounded by a `LIKE` filter on the three known ATS hosts, so
it does not scan rows that could never be attributed.

## Non-goals

- Changing dedup, ranking, or the shortlist. Populating identity makes the
  existing ATS dedup key usable; it does not change what that key does.
- Collapsing the location fanout duplicates of #61. Those postings have
  genuinely distinct ATS job ids and are a separate defect.
- Teaching `parse_supported_ats_url()` new hosts (`job-boards.greenhouse.io`).
  The adapter fallback covers the discovery case without changing what URLs
  the rest of the system considers canonical.

## Verification

- Each adapter populates provider, board and job id (`test_ats_adapters.py`).
- Greenhouse falls back to its own token and id when the posting URL is not a
  parseable ATS URL.
- URL-derived identity beats a conflicting fallback; populated fields survive
  (`test_canonical.py`).
- A non-ATS source with an ATS URL gets identity through discovery
  (`test_discovery.py`).
- Backfill fills legacy rows and leaves non-ATS rows alone (`test_store.py`).
- `pnpm job-hunter:test`.

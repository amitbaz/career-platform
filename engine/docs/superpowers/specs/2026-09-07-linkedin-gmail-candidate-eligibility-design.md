# LinkedIn Gmail Candidate Eligibility Design

## Problem

Gmail job candidates are staged independently, but discovery persists every source
copy into `job_hunter_jobs` before it ranks and selects a shortlist. The current
`job_hunter_unmaterialized_inbound_jobs()` RPC excludes a staged candidate as soon
as any logical job matches its Gmail source key, canonical URL, or normalized
company/title/location identity. A LinkedIn candidate that misses one shortlist
therefore cannot appear in the next run despite never reaching Gemini.

## Goal

Keep a fresh Gmail/LinkedIn candidate eligible until its matched logical job has a
current completed evaluation, is explicitly closed by deterministic discovery,
or the candidate has not been seen for 14 days. Re-emitting it must retain one
logical job and its accumulated provenance.

## Scope

- Replace the materialization-based Gmail lookup with an eligibility-based lookup.
- Persist deterministic prefilter and availability closure as terminal job status.
- Enforce a 14-day retention window from the inbound candidate's `last_seen_at`.
- Preserve the existing evaluation re-evaluation rules for failed evaluations and
  changed description/content-confidence evidence.
- Prove the two-run missed-shortlist scenario and the terminal cases through
  Python and pgTAP tests.

## Non-goals

- LinkedIn scraping, API integration, or metadata extraction changes.
- New candidate tables or a standalone retry queue.
- Changing the ranking, shortlist size, Gemini budget, or normal evaluation policy.
- Reopening a deterministically rejected or closed job simply because Gmail sees
  the same posting again.

## Design

### Eligibility query

Add a new `security invoker` RPC named
`public.job_hunter_eligible_inbound_jobs()` and replace the Python store method
with `PostgresJobStore.list_eligible_inbound_jobs()`. Remove the superseded
`job_hunter_unmaterialized_inbound_jobs()` RPC in the same migration so callers
cannot retain the one-shot semantics.

The RPC returns a candidate only when all of the following are true:

1. It belongs to the caller under RLS.
2. `last_seen_at` is within the previous 14 days.
3. No matching logical job is terminal (`status` is `rejected` or `closed`).
4. No matching logical job has a current successful evaluation: the latest
   evaluation status is not `failed`, and its description hash and content
   confidence equal the current job values.

Matching retains the three current, index-backed rules: Gmail source plus
candidate key, canonical URL when both URLs exist, and non-empty normalized
company/title/location identity. A matching job that needs evaluation remains
eligible, including one with no evaluation, a failed evaluation, or changed
evaluation evidence. The query must preserve the existing RLS-friendly
materialized candidate CTE and independently indexable match branches.

### Terminal discovery lifecycle

Use the existing `job_hunter_jobs.status` column; no new table or candidate-state
column is required. Add `PostgresJobStore.set_job_status(job_id, status)` and use
it only after discovery has determined a terminal result:

- `closed` when a fetched/canonical-resolved posting reports closure.
- `rejected` when deterministic prefilter or eligibility rejects it.

Status is intentionally terminal for this issue. Existing logical upsert behavior
continues to preserve it on later sightings, preventing an old rejected or closed
Gmail candidate from returning indefinitely. A future explicit reopen workflow is
outside this change.

### Data flow

```text
fresh Gmail candidate
  -> eligible-inbound RPC
  -> GmailStagedSource
  -> logical upsert and provenance (same logical job)
  -> prefilter / shortlist
     -> not selected: remains eligible next run
     -> deterministic reject or closure: terminal status, no retry
     -> selected and current evaluation saved: no retry
  -> after 14 days without a newer sighting: no retry
```

### Compatibility and failure behavior

The change is a new migration layered on the deployed schema; prior migrations
remain immutable. The RPC stays `security invoker` with the same caller-owned RLS
semantics. The existing `needs_evaluation()` method remains the authoritative
definition of a current evaluation; SQL mirrors its latest-evaluation,
failed-status, description-hash, and content-confidence rules.

The normal `upsert_logical_job()` path still resolves the repeated Gmail source
copy to the existing logical record, so a retry creates additional provenance only
when appropriate and never a second logical job. Existing cross-source behavior is
retained: a Gmail candidate can enrich an unevaluated matching public job, while a
current evaluated public job suppresses further Gmail work.

## Acceptance criteria

1. A materialized but unevaluated Gmail/LinkedIn candidate is returned in a later
   discovery run.
2. A two-run test proves a candidate can miss the first shortlist, be evaluated
   on the second run, and reuse one logical job ID.
3. A current successful evaluation suppresses later fresh evaluation work; failed
   or evidence-changed evaluation retains the existing retry behavior.
4. `rejected`, `closed`, and older-than-14-day candidates are excluded.
5. The eligibility RPC remains caller-scoped under RLS, and cross-source
   deduplication/provenance tests remain valid.

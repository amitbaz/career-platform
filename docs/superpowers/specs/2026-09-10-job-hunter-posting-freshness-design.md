# Posting freshness: re-check on a decaying schedule, close what is gone (#186)

Parent: #181. Blocked by #183 (merged) and, in practice, #184 (merged): this
work reuses #184's conditional-request support in `http.py` and its fix to
`job_hunter_schedule_stage_enqueue`'s job naming.

## Problem

Nothing learns whether a posting still exists. The corpus only grows, and an
advertisement the employer took down is still scored and delivered as though
it were live. A posting whose text the employer edited keeps the facets read
from the old text until something happens to re-crawl it.

## Decisions taken with the owner (2026-09-10)

1. **Consumer.** `recheck_freshness` is drained by its own process —
   `python -m job_hunter recheck-freshness` — run by its own scheduled
   workflow. It shares no process, schedule or queue with the daily run, so a
   slow re-check pass cannot delay a crawl or an extraction (acceptance
   criterion 5).
2. **What "changed" means.** A description is compared and updated only when
   the re-check reads the same channel the stored text came from. Today that
   is exactly one channel: an `official_ats` posting on Greenhouse, Lever or
   Ashby, re-read through the same board API and parsed by the same adapter
   that produced the stored text, so the two hashes are comparable. Every
   other posting is checked for existence only. Comparing an employer page's
   text against an aggregator's rendering of it would differ every time and
   buy a platform-key re-extraction on every check.
3. **Test seams.** The stage, exercised against the local stack with a fake
   HTTP session; and pgTAP for the schema: the interval, the enqueuer, the
   readers, and reopening.

## Design

### Schema (one migration)

Freshness is a property of the advertisement, so it lives on
`job_hunter_postings` rather than in a new shared table:

| column | meaning |
| --- | --- |
| `closed_at` | when a re-check established the posting is gone; null while open |
| `closed_reason` | why: `http_404`, `http_410`, `closure_phrase`, `absent_from_board`, `board_gone` |
| `freshness_checked_at` | when a re-check last completed |
| `freshness_next_check_at` | when it is next due; a newly discovered posting is first due six hours after it is found |
| `freshness_etag`, `freshness_last_modified` | the validators that make the next check conditional |

`closed_reason` exists because an empty result must carry its reason
(AGENTS.md rule 5): a posting that stopped being delivered has to say why.

**The interval** is `job_hunter_freshness_interval(age)`: a quarter of the
posting's age, clamped to between 6 hours and 7 days. A posting found an hour
ago is re-checked six hours on; one found eight days ago, two days on; one
older than four weeks, weekly. It is a function of age alone, with no
operator input.

**Enqueuing** is `job_hunter_enqueue_due_freshness(p_limit)`, run by `pg_cron`
every 30 minutes. It sends one `{posting_id}` message per posting that is due,
open, checkable (it has a URL or an ATS identity) and not merged away, and is
not already waiting on the queue. It leases each one by pushing
`freshness_next_check_at` a day out, so a message that dead-letters is retried
the next day rather than every tick. It only enqueues; the cron session
performs no stage work (#183).

### The stage (`recheck_freshness_stage.py`)

One message, one posting, one conditional request.

- **ATS channel** — a posting with a Greenhouse, Lever or Ashby identity. The
  board is fetched once per drain (a drain re-checking forty postings on one
  board costs one request) and parsed by the board's own adapter. Missing
  from the board: closed, `absent_from_board`. Board 404/410: closed,
  `board_gone`. Present, and the posting is `official_ats`: the description
  hash is compared, and a different hash updates the posting and enqueues
  `extract_facets` in the same transaction.
- **Page channel** — everything else, by its URL (the canonical URL only when
  it has none: canonicalising can drop a query parameter the page needs). 304:
  unchanged. 404 or 410: closed. 200: closed if the page states the posting is
  closed (`availability.detect_closure`), otherwise open. The description is
  never touched.
- **Neither is evidence of closure:** a timeout, a connection error or a 5xx
  is a transient stage failure and retries through the queue; a 429 releases
  the message without counting an attempt; a 401/403 (bot protection) is
  recorded as `unverified` and the posting stays open — the same rule
  `availability.py` has always applied.

Every completed check stamps `freshness_checked_at`, stores the validators and
sets the next check from the interval.

A change reuses the one existing notion of a changed posting: the posting's
`description_hash`. `extract_facets` compares it against the hash its facets
were read at, and `job_hunter_needs_evaluation` against the hash scoring saw,
so both re-run without any new mechanism.

### What a closed posting stops

- `job_hunter_pending_delivery_jobs` skips it, so it is never delivered.
- `extract_facets` does not read it, and nothing enqueues a read for it.
- The daily run drops it from its own crawl's candidates, its deferred-scoring
  queue and its rediscovered set before ranking
  (`PostgresJobStore.closed_job_ids`), so it is never scored on the user's key
  either. A closed job waiting on the deferred-scoring queue is removed from
  it rather than kept.

Nothing is deleted. A job row that already points at a closed posting still
reads it; the posting keeps its fingerprint, text and facets (acceptance
criterion 6).

### Reopening

The employer's own board listing a closed posting again reopens it:
`job_hunter_merge_posting_batch` clears `closed_at` when the listing it folds
in is `official_ats`, and `crawl_source`'s unchanged-hash short-circuit no
longer drops a closed posting, so an unchanged re-listing reaches the merge.

An aggregator's listing does not reopen (changed after code review). An
aggregator is routinely slower to drop an advertisement than the board it
copied it from; if its listing reopened what the board had dropped, the
posting would flip open on every daily crawl, be delivered from that crawl,
and be closed again by the next re-check. A job genuinely re-advertised on an
aggregator usually comes back under a new id, which is a new posting anyway.
A Gmail alert, which can arrive days after the advertisement came down, does
not reopen either.

## Out of scope, stated so nothing claims it

- **Gmail inbound candidates** (`job_hunter_eligible_inbound_jobs`) are not
  filtered by `closed_at`. A LinkedIn alert for a closed posting can still be
  materialised and scored by the daily run's Gmail path. LinkedIn is checked
  by the page channel only, where closure detection is weakest anyway.
- **Description updates for non-ATS postings** — decision 2.
- **A per-job ATS endpoint.** Ashby has none, and the board listing serves all
  three providers; reusing it keeps one parser per provider.

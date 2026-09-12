# Recoverable unresolved-posting enrichment (#259)

Parent: #181, sequenced as step B0 of epic #285 (`docs/adr/0002-modular-engine-and-supabase-as-backend.md`).
Runs against the current flat `job_hunter` package; its code moves along with the
restructure's later steps (C/D), unchanged, per #285's "Owner decisions recorded 2026-09-12".

## Problem

`job_hunter_match_state_counts` (added by #243, `29999999000243_job_hunter_match_every_open_posting.sql`)
already classifies every open posting as `ineligible`, `qualified` or `unresolved` — `unresolved`
meaning no facets yet (`no_facets`) or a content-confidence tier too thin to trust
(`low_content_confidence`: empty or `partial_unknown`). Missing information correctly produces no
hard block. What is missing is the other half: nothing ever *tries* to turn an unresolved posting
into a resolved one. A posting with only a thin aggregator snippet stays thin forever unless some
other user's crawl happens to discover a richer copy of the same advertisement.

## Scope

In scope: postings whose `content_confidence` is insufficient
(`job_hunter_content_confidence_sufficient` is false — empty or `partial_unknown`). For these,
attempt free, unauthenticated canonical resolution (direct ATS URL, HTTP redirect to one, or a
single embedded link on the posting's own page) and, when a supported ATS reference is found,
fetch the full official description. This reuses exactly the free tiers `canonical.py` already
implements for the legacy pipeline (`direct`, `redirect`, `embedded`), reimplemented as an explicit
user-free stage in the four-stage engine's shape (`stage_queue.py`, `pgmq`), because those tiers
need only the posting's own recorded URL and no credential.

Out of scope, stated so nothing claims it:

- **The paid/public targeted-search tier** (`canonical.py`'s `targeted_search`/`watch_target`
  methods). Those need a search backend and, in production, a per-user Brave key
  (`config.py: credentials.brave_search_api_key`) — the opposite of a user-free ingestion stage.
  A future ticket can fund a platform-level search budget the way `job_hunter_platform_ai_usage`
  funds shared extraction; until then, a posting that is not on a supported ATS host and carries
  no embedded link to one stays unresolved, correctly, rather than being scored against a
  per-user credential.
- **`no_facets` recovery.** A posting with sufficient content but no facets yet is not this
  ticket's problem — `extract_facets_stage.py` already owns reading a sufficiently-confident
  posting once. This ticket only feeds it a posting whose content just became sufficient.
- **Closing a posting.** `recheck_freshness` owns existence; this ticket never sets `closed_at`.

## Decisions

1. **A fifth queue-coupled stage**, `recover_posting`, following exactly the shape
   `recheck_freshness` established (issue #186): one message per posting, its own drain process,
   its own Render cron, its own worker-run record. `stage_queue.Stage`'s docstring calling the
   existing four "fixed by epic #181" is updated — a fifth was always structurally possible, #181
   just had not needed one yet.
2. **Scheduling lives on `job_hunter_postings`**, the same posting-is-shared-state pattern
   freshness uses, rather than a new table: `recovery_attempts`, `recovery_last_attempt_at`,
   `recovery_last_outcome`, `recovery_next_attempt_at`.
3. **The cadence is a function of age, configurable in one place.** A new singleton table,
   `job_hunter_recovery_config` (the same shape `job_hunter_ingestion_timing_config` already
   uses), holds the aggressive window, the aggressive interval, the post-window doubling period
   and the maximum interval. `job_hunter_recovery_interval(age)` reads it. Concretely, by
   default: every 20 minutes for the posting's first 24 hours, then doubling every 12 hours after
   that, capped at 7 days — so a posting that is never going to resolve costs less and less over
   time rather than being hammered forever, and an operator can retune the curve with one `update`
   and no code change (AC2).
4. **A changed source record reopens the schedule immediately (AC3), by trigger, not by touching
   every writer.** `job_hunter_merge_posting_batch` and `job_hunter_upsert_posting` already apply
   the "better description wins" ladder (`job_hunter_preferred_description`) whenever a richer
   source variant merges in (#182) — that machinery already satisfies "richer copies and source
   variants may contribute facts without duplicating the posting" (AC4) and is not touched here.
   What is missing is *noticing* that a variant merged but still left the posting insufficient, and
   trying again right away instead of waiting out the decaying interval. A `before insert or
   update` trigger on `job_hunter_postings` does this in one place, for every writer, present and
   future: if the row's content confidence is (still) insufficient and either its description hash
   or its `last_seen_at` moved, `recovery_next_attempt_at` is reset to `now()`. If confidence just
   became sufficient, the schedule is cleared — recovery is over for that posting (AC7). Re-editing
   the ~300-line merge functions to carry this one rule twice was rejected as the same mistake
   `job_hunter_preferred_description` was extracted to avoid.
5. **The stage never marks a posting permanently unresolved (AC9).** `recovery_attempts` is a
   counter for observability only; it never gates whether another attempt happens, and the interval
   function has a fixed ceiling rather than an unbounded one. A posting can fail recovery a
   thousand times and is still due again in at most 7 days, and `job_hunter_match_jobs` /
   `job_hunter_match_state_counts` read `content_confidence` alone, never `recovery_attempts` —
   there is no code path by which retry count can turn into a hard exclusion.
6. **Outcomes recorded per attempt (AC6):** `recovery_last_outcome` is one of `recovered` (content
   confidence became sufficient) or `unresolved` (attempt completed, still insufficient) — the two
   outcomes a completed attempt can reach. Failure and rate-limiting are not posting states; they
   are the underlying HTTP fetch raising `TransientStageFailure`/`QuotaExhausted`, which
   `stage_queue.StageRunner` already retries/backs off/releases exactly as `recheck_freshness`'s
   fetch does, so "remaining unknowns" after a failed message is simply "the posting's schedule is
   untouched and it is retried by the queue's own policy" — no separate bookkeeping needed for a
   state the queue already owns.
7. **Analytics reads what already exists plus one new function.** `job_hunter_match_state_counts`
   already reports unresolved counts and the `no_facets`/`low_content_confidence` reason split
   (AC8, first half). This ticket adds `job_hunter_recovery_backlog()`, an aggregate over open,
   insufficient postings by age bucket and `recovery_last_outcome` — ages, not full rows, matching
   `job_hunter_match_state_counts`'s own "counts, not rows" shape — for the Engine Lab analytics
   page (#264) to read later.

## Schema

One migration, `job_hunter_postings` gains:

| column | meaning |
| --- | --- |
| `recovery_attempts` | how many recovery attempts have completed against this posting |
| `recovery_last_attempt_at` | when the last one completed |
| `recovery_last_outcome` | `''` (never attempted), `recovered`, or `unresolved` |
| `recovery_next_attempt_at` | when the next attempt is due; `null` once sufficient or never needed |

Plus `job_hunter_recovery_config` (singleton, tunable) and:

- `job_hunter_recovery_interval(p_age interval)` — reads the config row.
- `job_hunter_posting_recovery_schedule()` — the before-insert-or-update trigger (decision 4).
- `job_hunter_enqueue_due_recover_posting(p_limit)` — the cron tick, `recheck_freshness`'s enqueuer
  restated for this queue: due, open (`closed_at is null`), checkable (has a URL, or an ATS
  identity, or a company name to build a query from later) and not already waiting on the queue.
- `job_hunter_recovery_backlog()` — the analytics read (decision 7).

## The stage (`recover_posting_stage.py`)

One message, one posting. Imports nothing user-scoped at module level, like every stage before it.

1. Already sufficient (a race with another writer): no fetch, recorded as `recovered` with the
   schedule already cleared by the trigger.
2. `parse_supported_ats_url(posting.url)` — free, no network (`direct`).
3. Else, if the posting has a URL: one conditional-free GET (`retry=False`, 429 raises
   `QuotaExhausted`, 5xx/timeout raises `TransientStageFailure`, matching
   `recheck_freshness_stage._fetch` exactly) —
   `parse_supported_ats_url(response.url)` (`redirect`), else the one distinct embedded ATS link on
   the page via `fetching.extract_job_page_links` (`embedded`, same "exactly one distinct posting"
   guard `canonical.py` already documents).
4. If a reference is found: `canonical.fetch_authoritative_description` (already fails open —
   returns `None` rather than raising). Non-empty text: update `description`, `description_hash`,
   `content_confidence = official_ats`, `canonical_url`, `ats_provider/board/job_id`; enqueue
   `extract_facets` in the same transaction (a posting can never be left with new text nothing
   reads); outcome `recovered`. Empty text: still worth keeping the identity fields (an ATS
   reference is real evidence even without new text yet), outcome `unresolved`.
5. No reference found anywhere: outcome `unresolved`, no column changes besides the bookkeeping
   ones (`recovery_attempts`, `recovery_last_attempt_at`, `recovery_last_outcome`, and
   `recovery_next_attempt_at` computed from `job_hunter_recovery_interval` — the trigger only fires
   on a description/last-seen change, which this path has neither of).

## Out of scope, stated so nothing claims it

- Targeted/paid search and company-watch-target resolution (decision, scope section above).
- Anything that closes a posting — `recheck_freshness`'s job, untouched.
- A UI for the analytics function — #264's job; this ticket only makes the data queryable.

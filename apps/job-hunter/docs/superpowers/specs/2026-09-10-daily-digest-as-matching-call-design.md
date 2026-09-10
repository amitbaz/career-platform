# The daily digest becomes a call to the matching operation

Issue #188 (parent #181, epic #114). Blocked by #187 (closed, merged via PR #235).

## The problem

`matching.match_jobs` (#187) is the one ranking/blocking/scoring implementation #181 promises
every caller shares. `pipeline.py`'s daily run does not call it. #187's own design doc explains
why at the time: the wrapper's contract (rank, then score up to `limit`) didn't fit the run's
orchestration — offer-limit tracking, the deferred-evaluation queue, on-demand facet extraction
— so the run kept its own ~150-line interleaved loop (`_evaluate_and_deliver_one_job` and its
callers) and was rewired only onto the SQL primitive (`store.match_jobs`) underneath the
wrapper, not the wrapper itself. That was the right call for #187's scope. #188 is where the
work #187 deferred gets done: fold the run's orchestration into a shape `matching.match_jobs`
can actually serve, so "one matching operation, every caller" stops being true only in SQL.

Two more requirements ride along: `job_hunter_posting_display_credit` (#184) has no reader yet,
and this is where it gets one; and cross-run delivery state currently living in `pipeline.py`
(the daily offer limit, quota deferral, rediscovery re-offer) has to survive the move without a
previously-delivered job reappearing.

## Why `matching.match_jobs` can't be called as-is

`store.match_jobs` (`job_hunter_match_jobs`) ranks **the requesting user's whole corpus** —
every `job_hunter_jobs` membership row, not just what today's crawl discovered. Calling
`matching.match_jobs` naively, once a day, with no further change, would re-score the entire
corpus every run: a job scored and delivered last week has no way to say "already sent" to the
wrapper, so it would cost a fresh model call again today, and every day after. That is not a
performance detail — it silently breaks the offer limit (nothing bounds re-delivery), the
"previously-delivered jobs do not reappear" acceptance criterion, and the daily spend on the
user's own Gemini key.

**So `matching.match_jobs` gains the one thing it is currently missing: it must know what this
user has already been told.** Concretely, for each ranked row, before deciding blocked or
scored:

1. **Delivered already → not returned at all.** `store.get_evaluation(job_id) is not None and
   store.has_delivery(job_id, "telegram_message")` — the exact predicate
   `_evaluate_and_deliver_one_job` checks today at its own top (`pipeline.py:1246`) — costs
   nothing and is skipped before any facet or blocker work. This is what makes AC
   "previously-delivered jobs do not reappear" hold for every caller, not just the digest.
2. **Evaluated, not yet delivered → reused, no model call.** A job scored on an earlier run
   but never delivered (withheld by the offer cap, dropped by a failed Telegram send, or
   deferred by AI quota before it could score at all — see below) gets its stored `Evaluation`
   back as a `MatchedJob` with a new field, `fresh=False`. This is the *retry* path:
   `job_hunter_pending_delivery_jobs` already expresses "evaluated, floor-passing, undelivered"
   in one SQL round trip, so the wrapper reads that first and treats every id it returns as
   reused, before touching the ranked/blocked rows for anything else.
3. **Neither → the existing path.** Blocked-by-facets or scored-by-model, exactly as today,
   `fresh=True`, counted against `limit`.

This is the "Option 1" seam from the earlier scoping discussion: the skip/reuse rule lives
*inside* `matching.match_jobs`, once, so a future on-demand caller (a dashboard, a search
endpoint) gets it for free and cannot forget it — which is also what makes AC4 ("a user asking
on demand and the schedule asking on their behalf return the same jobs for the same corpus,
profile, and delivery history") true by construction rather than by two callers independently
agreeing to filter the same way.

**`MatchedJob` gains `fresh: bool`.** `scored` (was `evaluation` the model's own answer, or a
facet block) stays what it already means. `fresh` is new and orthogonal: `False` for a row read
back from an earlier run's evaluation, `True` for anything decided this call. The caller uses
`fresh` to decide whether to persist (a reused row is already in the store) and whether to run
company-watch promotion (already run when the row was first decided); it uses `scored` for
nothing the caller does not already use it for today (whether a model call happened).

## What the caller — `run_pipeline` — does with the results

`pipeline.py`'s ~150-line interleaved loop
(`pending_evaluation_ids` walk + `selected` walk, both calling `_evaluate_and_deliver_job`)
is replaced by:

1. **A facet pre-pass, before calling `match_jobs`.** `matching.match_jobs` cannot extract a
   posting nobody has read — it only skips a row with `has_facets=False`, as it does today. A
   job discovered *today* must still be scorable *today* (AC: "what the user receives is
   unchanged from today"), so the run extracts facets for this run's shortlist
   (`selected`) before scoring, using the same bounded machinery `_extract_facets_for_run`
   already has, just moved earlier and narrowed to the shortlist alone (see "What is removed"
   below for why the pending-evaluation half of its input goes away). The trailing
   `drain_extract_facets_queue` backfill pass — the durable, cross-run mechanism that is how
   the corpus acquires facets at all outside of a user's own shortlist — is untouched and still
   runs after scoring, on whatever budget the pre-pass and scoring did not spend.
2. **One call: `matching.match_jobs(store, ai, policy, candidate_context,
   limit=settings.policy.max_jobs_per_run)`.** `limit` bounds model-scored attempts, exactly as
   #187 wrote it — it does not bound deliverable count. Today's loop already scores more jobs
   than `daily_offer_limit` when some rank above it score `skip`; the difference here is that
   `match_jobs` cannot itself stop early once enough *offers* have accumulated, because it does
   not know what an offer is. A run now typically spends nearer its full `max_jobs_per_run`
   budget instead of stopping once the offer cap is filled. Accepted deliberately — see the
   scoping discussion — rather than reintroducing a resumable/interruptible contract into the
   wrapper, which is the exact duplication #187 already rejected once.
3. **Walk the ordered results, applying floor and cap — the caller's job per the ticket.** For
   each `MatchedJob` in the order `match_jobs` returned them (already rank order):
   - `fresh=True`: `store.save_evaluation(...)`, run `promote_company` exactly as
     `_evaluate_and_deliver_one_job` does today (including following a mid-run merge to the
     surviving job id), then decide floor/offer-cap.
   - `fresh=False`: nothing to persist or promote — it was decided on an earlier run. Decide
     floor/offer-cap the same way.
   - **Floor:** `evaluation.total_score < match_score_floor` withholds it from the digest,
     counted as `withheld_by_score_floor` (offer decisions) or `skipped` (everything else) —
     unchanged from today.
   - **Offer cap:** only a `fresh=True` row whose decision is an offer
     (`_OFFER_DECISIONS`) consumes `daily_offer_limit`. A `fresh=False` row is a *retry* of an
     offer already budgeted on the run that first produced it, so it reaches the digest without
     touching the cap — this is exactly what `_requeue_pending_delivery` and the final
     `pending_delivery_job_ids` sweep do today, and is why AC "delivery failures do not affect
     what was matched" holds: a Telegram send that fails leaves the evaluation intact and
     undelivered, and the next run's `match_jobs` call hands it straight back at zero
     additional cost, uncounted against that day's cap.
   - Below the cap, once `daily_offer_limit` fresh offers have been produced, remaining `fresh`
     rows are left alone — not scored again, not discarded, exactly like today's "ranks again
     next run." (They were already scored this call, unlike today; the cost trade-off above is
     what pays for that.) Remaining `fresh=False` rows keep appearing every call regardless of
     the cap, same as `_requeue_pending_delivery` today.
4. **Digest build and delivery are untouched** — `select_deliverable_items`, `build_digest`,
   `mark_delivered`, the navigation-card path. They operate on `DigestItem`, not on
   `MatchedJob`, and nothing about what a `DigestItem` is changes except the new
   `display_credit` field (below).

## What is removed

- **The `job_evaluation` deferred-AI-work queue** (`store.enqueue_ai_work` /
  `list_pending_ai_work` / `complete_ai_work` for that work type, and `pending_evaluation_ids`
  in `run_pipeline`). Its job was retrying a job whose evaluation was deferred by AI quota,
  ahead of newly-selected candidates. Once `match_jobs` scans the user's whole corpus every
  call, a job with no evaluation yet is simply part of that scan next time there is budget —
  the queue's only remaining effect would be *priority* (retried before the day's newly-ranked
  candidates), and AC4 (on-demand and scheduled agree) argues against a hidden priority queue an
  on-demand caller would not see. `AIBudgetExceeded`/`AIQuotaPaused` from `evaluate_job` inside
  `matching.match_jobs` (unchanged, still per-user-key-paced) simply stops that call's scoring
  loop early, the same way running out of `limit` does; nothing is enqueued because nothing
  needs to be — the row is unresolved (no stored evaluation) and reappears in the next call's
  ranked results.
- **`_requeue_pending_delivery` and its two call sites** (the `rediscovered_job_ids` loop and
  the final `pending_delivery_job_ids` sweep). Both are subsumed by point 2 in "reused, no model
  call" above: `match_jobs` already re-derives hard blockers from *current* facets on every
  call, so a job blocked last week whose facets changed enough to clear today naturally comes
  back unblocked without a dedicated rediscovery path, and every evaluated-but-undelivered job
  is retried by construction, not by three separate mechanisms agreeing to do the same thing.
  `discovery.rediscovered_job_ids` itself (the crawl-time "still on the board" signal that bumps
  `last_seen`/keeps a posting from going stale) is untouched — only the pipeline-level re-offer
  built on top of it goes.
- **`queued_job_ids` / `pending_evaluation_id_set` bookkeeping** in `run_pipeline`. These
  existed to stop the old dual loop (pending-evaluation walk + shortlist walk) from double
  processing a job that appeared in both. A single pass over one ordered result list has no
  such overlap to guard against.

## `display_credit`

`MatchedJob` gains nothing for this — it is resolved once per digest item build, by
`posting_id` (which `MatchedJob` already carries), through a new store method wrapping
`job_hunter_posting_display_credit`:

```python
def posting_display_credit(self, posting_id: str) -> dict | None:
    ...
```

`DigestItem` gains `display_credit_text: str = ""` and `display_credit_url: str = ""` (empty
means no obligation — matches the SQL function's `null` for "source imposes nothing"; empty
string rather than `None` to keep the dataclass's existing all-`str`-defaults shape).
`telegram.py::_digest_line` appends `f" | {text}: {url}"` when both are non-empty, immediately
after the existing `url` line and before `hard_blockers` — never the advert body (already true;
`_digest_line` has never carried a description), never `badge_url` (per the migration comment
and the issue thread, Telegram's `sendMessage` cannot render one). Resolved from the posting,
never from the user: the new store method takes only `posting_id`, matching
`job_hunter_posting_display_credit`'s own signature having no user parameter to accept.

## Testing

- `test_matching.py`: the three new predicates (delivered → skipped entirely; evaluated,
  undelivered → reused with `fresh=False`, no call to the fake provider; neither → existing
  behavior, now asserting `fresh=True`), each store-backed.
- `test_pipeline.py`: the existing offer-limit/floor/rediscovery/merged-job/quota tests
  (`test_pipeline_delivers_at_most_the_daily_offer_limit`,
  `test_pipeline_withholds_offers_below_the_match_score_floor`,
  `test_a_rediscovered_job_is_backfilled_through_the_queue`,
  `test_pipeline_does_not_offer_a_job_merged_into_an_already_delivered_one`,
  `test_pipeline_ignores_stale_pending_evaluation_for_already_delivered_job`, and the rest
  listed in scoping) are the regression surface: same behavior, new mechanism. Each gets
  ported to assert against the new call path rather than deleted, since they are the
  acceptance criterion "what the user receives is unchanged from today" made concrete.
- New: a `display_credit` test asserting a digest line carries the source's required text and
  link and never a `badge_url` or the description, for a source row with a non-empty
  `display_credit`, and asserting no such text appears for a source with `{}`.
- New: an AC4 equivalence-style test — same corpus/profile/delivery-history through
  `matching.match_jobs` twice (simulating on-demand then scheduled) asserts identical ordered
  job ids, the second call spending no fresh provider calls on rows the first call already
  delivered.

## Migration

No new tables. `posting_display_credit` reads an existing function
(`job_hunter_posting_display_credit`, #184). One SQL change surfaced at implementation time,
not anticipated here: `job_hunter_match_jobs` (#187) never excluded a closed posting
(`job_hunter_postings.closed_at`, #186) or a membership row prefilter had already rejected
(`job_hunter_jobs.status in ('rejected', 'closed')`). Both were harmless while nothing called
the function for delivery — `run_pipeline` built its shortlist from `discovery.eligible`, which
never contained either. #188 makes this function the one thing deciding what reaches a user over
the *whole* corpus, so both gaps became reachable and are fixed in
`29999999000000_job_hunter_match_jobs_excludes_closed_postings.sql` (pgtap-covered).

## Deviations from this design, found during implementation

- **The reuse step does not call `job_hunter_pending_delivery_jobs`.** Instead `match_jobs`
  bulk-prefetches `store.delivered_job_ids` and `store.get_evaluations_bulk` once for the whole
  ranked set and classifies each row inline (delivered+evaluated → skip; evaluated,
  undelivered → reuse; neither → decide). Same three outcomes this doc describes, cheaper: one
  round trip per bulk read instead of a second SQL function whose own row shape would have had
  to be reconciled against `job_hunter_match_jobs`'s ranked order.
- **The offer cap bounds `fresh=False` (reused) rows too, not only `fresh=True` ones.** §3 above
  says only a fresh offer consumes `daily_offer_limit`. That was wrong once scoring stopped
  bounding itself by the cap: under #188, a cap-deferred candidate is still scored and stored the
  same run, so the "not yet delivered" backlog `match_jobs` hands back next call routinely holds
  *everything* the cap withheld today — not the small incidental backlog (a failed send, a
  rediscovery) the old design assumed. Left uncapped, that backlog would flood out in full on the
  very next run, defeating the cap's own pacing purpose. `pipeline.py` now counts `fresh` and
  reused offers alike against `delivered_offers`.
- **A facet-decided block is recomputed fresh every call, never reused from a stored
  evaluation.** `store.get_evaluations_bulk` only reuses a row with `evaluation.model` set (a
  genuine model answer); a stored block has no `model` and is never read back this way. Blocking
  is free (SQL-derived, no provider call) and must reflect the user's *current* profile — caching
  it would let a block outlive the floor/preference edit that produced it, with no way for the
  user to un-stick it short of the row reaching a fresh scoring pass again.
- **`MatchResult` carries two more fields than planned:** `parse_failure_job_ids` (the subset of
  `failed_job_ids` that failed to parse, `evaluation.EvaluationError`, feeding
  `RunSummary.scoring_parse_failures`) and `skipped_without_facets_job_ids` (rows left unresolved
  for lack of current facets, including a facets row a changed posting made stale — a staleness
  check via `store.jobs_needing_facets` this design didn't anticipate needing inside `match_jobs`
  itself, since a row reached from elsewhere in the corpus can carry facets the run's own facet
  pre-pass never touched).

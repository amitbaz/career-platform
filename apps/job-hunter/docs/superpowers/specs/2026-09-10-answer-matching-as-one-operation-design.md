# Answer matching as one on-demand operation over stored facets

Issue #187 (parent #181, epic #114). Blocked by #175, #177 (both closed).

## The problem

Matching is decided once a day, inside the crawl. `pipeline.py` ranks only the jobs
*this run* just discovered (`rank_jobs(eligible, ...)` at `pipeline.py:1667`), selects a
diverse subset with `select_diverse_candidates`, and scores that subset plus whatever was
deferred last time. There is no way to ask "what matches me" against the corpus the engine
already holds — a dashboard, an on-demand search, and a differently-scheduled digest would
each need to reinvent this, and #181's whole premise is that they must not: "one matching
operation serves every caller; there is no second ranking implementation."

## The shape of the change

**Ranking and hard blockers move into one SQL function, deterministic and free.** It reads
the whole corpus the requesting user is a member of — `job_hunter_jobs` (this user's rows,
RLS-scoped) joined to `job_hunter_postings`, `job_hunter_job_facets`, `job_hunter_companies`,
`job_hunter_search_profiles` and `job_hunter_search_profile_markets` — and returns every
candidate ordered by the same score `ranking.rank_jobs` computes today, with a
`hard_blockers` column populated wherever `hard_blockers.hard_blockers_from_facets` would
already have blocked it. **Only the model-scoring seam is per-provider-call**; ranking and
blocking cost one query.

**The ranking formula is ported term-for-term, not rewritten.** `profile_priority_score`'s
seven components and two penalties are free-text substring/keyword matching against
`normalize_text`-normalized title/description, plus a company-facets lookup — none of it
is a judgement call, so none of it needs the model. This codebase already carries SQL twins
of every normalization primitive the Python scorer uses:

| Python (`normalize.py` / `job_identity.py`) | SQL twin (already migrated) |
| --- | --- |
| `normalize_text` | `job_hunter_normalize_text` |
| `normalize_company_name` | `job_hunter_normalize_company` (also `job_hunter_postings.normalized_company`, generated) |
| `normalize_job_title` / `normalize_location` | `job_hunter_normalize_tokens` |
| `locations_compatible` | `job_hunter_locations_compatible` |

So each `ranking.py` component gets a same-named SQL sibling, unit-tested independently in
pgtap exactly the way the Python originals are unit-tested in `test_ranking.py`, then
composed by one `job_hunter_profile_priority_score` function that mirrors
`profile_priority_score`'s own composition line for line:

| `ranking.py` | New SQL function |
| --- | --- |
| `_role_seniority_fit` | `job_hunter_role_seniority_fit(title, preferred_roles[], preferred_seniority[])` |
| `_signal_coverage` | `job_hunter_signal_coverage(title, description, must_have[], nice_to_have[])` |
| `_market_location_fit` (+ `_profile_location_fit` fallback) | `job_hunter_market_location_fit(location, description, remote, market_id, ...)` |
| `source_quality` | `job_hunter_source_quality(source, url, specialist_board_hosts[])` |
| `market_priority_bonus` | `job_hunter_market_priority_bonus(market_id, market position)` — a join + `count(*) filter`, no function needed |
| `company_fit` | `job_hunter_company_fit(company_identity, preferences...)` — reads `job_hunter_companies` directly |
| `_avoid_signal_penalty` | `job_hunter_avoid_signal_penalty(title, location, description, avoid_signals[])` |
| `_backend_transition_penalty` | `job_hunter_backend_transition_penalty(title, description, frontend_signals[], backend_heavy_signals[])` |
| `profile_priority_score` (composition) | `job_hunter_profile_priority_score(...)` |

`CandidatePreferences` (`preferred_roles`, `must_have_signals`, etc.) is not a table —
it is extracted once from the CV and cached content-addressed in
`job_hunter_candidate_context_cache` (`candidate_context.py`). The operation's Python
wrapper reads it the same way `get_candidate_context` already does and passes the six
arrays into the SQL function as ordinary `text[]` parameters. Nothing about that cache
changes.

**Hard blockers are the existing per-user comparison, ported the same way.**
`hard_blockers.hard_blockers_from_facets` is already written for exactly this: it compares
`job_hunter_job_facets` columns against per-user thresholds
(`BlockingThresholds.for_job`), and nothing in it is a judgement call. `job_hunter_hard_blockers(facets row, thresholds)` is a direct port — compensation-vs-floor,
remote-policy-vs-required, relocation-policy-vs-allowed — reusing the same fail-open rule
(an `unknown`/undisclosed facet never blocks). What does **not** move: `prefilter.py`'s
free-text employment-type/sponsorship/language checks and `market_policy.attribute_market`.
Both already run once, at discovery time, over the freshly-crawled listing — their output
(`job.market_id`, and whether the row exists as a stored `job_hunter_jobs` membership at
all) is what the new operation reads as a *stored fact*, not something it recomputes. The
ticket's own scope names this: "location eligibility, remote policy, seniority,
compensation" is `hard_blockers.py`'s vocabulary, not `market_eligibility.py`'s.

**One SQL entry point, `job_hunter_match_jobs`.** `language sql, security invoker, set
search_path = ''`, scoped by `(select auth.uid())` like every other reader in this file —
following the house style in `job_hunter_eligible_inbound_jobs`
(`20260909150000_job_hunter_read_posting_facts.sql`). Parameters: the six candidate-preference
arrays, `p_limit`. Returns `(job_id, posting_id, score int, hard_blockers text[])` ordered
the same way `rank_jobs` orders — `score desc, company, title, job_id` — for every
membership row this user holds, blocked or not: the caller decides what to do with a
blocked row (skip it at zero cost) rather than the function silently dropping rows a
caller might want to audit.

**The Python operation, `matching.match_jobs(store, ai, user_id, policy, context, limit)`
in a new `src/job_hunter/matching.py`.** Calls `job_hunter_match_jobs` once, then for the
first `limit` **non-blocked** rows calls `evaluate_job` exactly as
`_evaluate_and_deliver_one_job` does today, and for every blocked row (whether inside or
outside the limit) builds `hard_blockers.blocked_evaluation` — no provider call, matching
AC3 ("a blocked posting costs nothing"). `ai` is the caller's own `AIProvider`, built from
that user's credentials exactly as it is built today; nothing about credential/ledger
selection changes (`ai/usage.py` is untouched). This is the one operation an on-demand
caller (a future dashboard or search endpoint) uses directly.

**`pipeline.py`'s ranking and hard-blocker call sites are rewired onto the same SQL
function, `store.match_jobs`** — not onto `matching.match_jobs` itself. The wrapper's
contract (rank, then score up to `limit`) does not fit the daily run's orchestration:
offer-limit tracking, the deferred-evaluation queue, `select_diverse_candidates`'s
per-source diversity cap, and on-demand facet extraction for a posting nobody has read
(`_facets_for_scoring`) all have to interleave with scoring in ways the wrapper does not
express. Duplicating that orchestration inside `matching.match_jobs` to make the pipeline
call it, or stripping it out of the pipeline to fit the wrapper, was judged a larger and
riskier change than reusing the ranking/blocking primitive both callers actually share.
`rank_jobs`/`_facet_decided_blockers` (the Python originals) remain as pipeline.py's
fallback for exactly two cases: a user with no search profile yet (`store.match_jobs`
has nothing to rank against), and the SQL call failing outright — both already the
run's existing resilience pattern (compare the `company_facets` bulk-read fallback a few
lines above the same call site). One further wrinkle the wrinkle above does not cover: a
posting first read (or re-read) *during* this run cannot yet be in the SQL ranking's own
snapshot (`sql_match_by_job_id` is built before the evaluation loop extracts facets), so
`_evaluate_and_deliver_one_job` also falls back to `_facet_decided_blockers` for any job
that needed a fresh facets read this run, using the facets that read just produced rather
than the stale (or absent) snapshot. This is what AC1 asks for in the shape the daily run
can actually take: one ranking/blocking implementation, with the pre-#187 Python path kept
only as the fallback for what the SQL call cannot yet see.

## What is deliberately out of scope

- Any UI. #181 is explicit: "This spec makes an on-demand search answerable; it does not
  build a dashboard." No dashboard or search endpoint is added here — only the operation a
  future one would call, and the daily digest's own call site.
- `market_eligibility.py` / `prefilter.py`'s free-text checks. They stay at discovery time,
  unchanged, exactly as `hard_blockers.py`'s own design doc drew that line for the model
  call they replace.
- `select_diverse_candidates`'s per-source diversity cap. The ticket's `limit` bounds how
  many are *scored*, not source mix; `max_jobs_per_run`/`source_minimum_per_run`/
  `source_max_share` stay a pipeline-level concern layered on top of the ranked list the
  operation returns, unchanged from today.
- Company-preference field-name reconciliation between `CompanyPreferences` (Python) and
  `job_hunter_search_profiles.preferred_company_sizes` (SQL): confirmed identical in
  intent (`size_band` vocabulary) during implementation, not re-litigated here.

## Testing

Primary seam is the SQL function, per #181's own testing decision ("Third seam: the
matching operation... asserted on the ranking returned for a given user and filters, and
on how many provider calls it cost"). Each small SQL helper gets a pgtap case mirroring its
Python counterpart's `test_ranking.py`/`test_hard_blockers.py` cases; `job_hunter_match_jobs`
gets pgtap cases for ordering, hard-blocker flagging, and RLS (a second user's rows never
appear). The Python wrapper gets a store-backed test with the fake AI provider (the pattern
`test_pipeline.py` already uses) asserting: blocked rows never reach the fake provider,
scoring stops at `limit`, and — the acceptance criterion that actually proves the port —
an equivalence test running the same fixture corpus and profile through both
`matching.match_jobs` and today's `rank_jobs` → `hard_blockers_from_facets` path and
asserting the same job ids in the same order.

## Migration

New functions only; no existing table or column changes.
`supabase/tests/pgtap/job_hunter_store_functions.sql` pins the exact function set and must
be updated to include the new functions. Written under a placeholder filename
(`29999999000000_job_hunter_match_jobs.sql`) per AGENTS.md's numbering rule while the branch
was in flight; renamed to `20260910140000_job_hunter_match_jobs.sql` at PR time, after
checking `main` and the other in-flight worktree for the highest timestamp either carried.

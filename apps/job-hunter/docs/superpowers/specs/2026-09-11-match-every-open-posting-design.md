# Match every open posting without crawl-time membership or a title gate

Issue #243 (parent #246, epic #114). Design context:
`docs/superpowers/specs/2026-09-11-engine-ready-stack-design.md`.

## The problem

`job_hunter_match_jobs` (`supabase/migrations/20260910170000_...folds_variant_groups.sql`)
ranks `job_hunter_jobs j join job_hunter_postings p` — it can only return a posting this
user already holds a per-user membership row for. That row is created unconditionally by
`upsert_logical_jobs` at crawl time (`discovery.py:941,979`), then `prefilter_job`
(`prefilter.py:20-88`) sets `status='rejected'` on it when `is_software_engineering_title`
(`prefilter.py:7-18`) fails. So today a posting is invisible to matching for two
compounding reasons: nobody has crawled it *as this user* yet, or the engineering-title
gate rejected it before matching ever ran. Neither is a fact about fit — the first is an
accident of scheduling, the second drops every non-engineering profession by construction.

`job_hunter_jobs.status` carries no reason code, so an existing `rejected` row cannot be
told apart from "keyword the user configured" versus "not an engineering title" after the
fact. The clean fix is for matching to stop reading `status` for eligibility at all: a
membership row's `status` becomes bookkeeping for the legacy daily-digest path
(`pipeline.py`, retiring under #189), never an input to whether matching considers a
posting.

## What does not change

- `discovery.py`, `prefilter.py` and the legacy daily pipeline are untouched. They still
  write `job_hunter_jobs` rows and set `status`; #189 deletes that path once this issue and
  #259 exist (per the parent design doc). This ticket does not delete or rewire them.
- `job_hunter_job_facets` is already keyed on `posting_id`, not `job_id` (#175): objective
  facts were already posting-scoped before this ticket. Company facets
  (`job_hunter_companies`) are also already posting-scoped. Neither needs a schema change.
- `matching.match_jobs`'s existing seams — delivered/reused/fresh, `limit`-bounded scoring,
  per-row failure isolation (#145), quota-exhaustion stop (#188) — are kept. This ticket
  changes what rows the SQL ranking considers and what it hands back per row, not the
  Python scoring loop's shape.
- Persisting a fresh evaluation stays the caller's job (`MatchedJob.fresh`), unchanged.

## The three-way classification

Every open posting gets one state per user, computed fresh at read time rather than stored:

- **`ineligible`** — a hard constraint is proven violated by a **confirmed** fact. Never by
  an absent one.
- **`unresolved`** — no `job_hunter_job_facets` row yet, or the posting's
  `content_confidence` is insufficient (`job_hunter_content_confidence_sufficient`, already
  the same tier check the current hard-blocker computation guards on). This is exactly
  #259's territory: a posting stays `unresolved` until enrichment or a source-variant
  contributes enough evidence to try `job_hunter_hard_blockers` on, or it closes.
- **`qualified`** — enough evidence exists (facets present, sufficient content confidence),
  and no hard constraint fired.

## Hard constraints: what's real today and what's deferred

The issue names six. Three have existing, confirmed data behind them and are implemented as
real SQL predicates in this ticket. Three name facts this codebase does not yet capture
anywhere, and are deliberately **not** faked with a placeholder that could silently never
fire and be mistaken for "checked":

| # | Constraint | Status | Data source |
| - | --- | --- | --- |
| 1 | Stated salary below an explicit minimum | **Implemented** (already existed) | `job_hunter_job_facets.compensation_*` vs `SearchPolicy.salary_floor_eur` / market `gross_base_floor` |
| 2 | Required work mode conflicting with confirmed requirement | **Implemented** (already existed) | `job_hunter_job_facets.remote_policy`/`relocation_policy` vs market `remote_policy='required'`/`relocation_policy` |
| 3 | Known hiring or location ineligibility | **Implemented, new** | `job_hunter_job_facets.hiring_regions` vs the regions the user's configured market locations resolve to (`hiring_scope.py`'s alias table, ported to SQL — see below) |
| 4 | Work outside CV-driven office and knowledge work | **Deferred** | No facet captures this today. Needs a new objective fact from enrichment (facets.py), which is outside this ticket's boundary (matching, not extraction). Tracked as a follow-up; the classification has no predicate for it, so it never fires — consistent with "unknown facts cannot create a hard exclusion" rather than a gap disguised as a check. |
| 5 | Clearly incompatible seniority | **Deferred** | `job_hunter_job_facets.seniority` exists, but no *confirmed* (as opposed to soft-preferred) user seniority level exists anywhere — `CandidatePreferences.preferred_seniority` is explicitly a ranking preference, not a stated requirement. Introducing confirmed-fact capture is onboarding's scope (#78/#109), not this ticket's. |
| 6 | Explicit user exclusion | **Deferred to #263** | #263 ("soft swipe learning and explicit reversible rules") is the ticket that creates rule data. There is nothing to read yet. |

This is why the PR description calls these two out explicitly rather than leaving them
silently unimplemented: rule 6 in root `AGENTS.md` — prefer no claim to an unenforced one.

### Porting hiring-region eligibility to SQL

`hiring_scope.py::regions_for_locations` maps configured place names (market locations) to
one of four region tokens (`north_america`, `europe`, `middle_east`, `asia_pacific`) via a
static alias table — this is the *only* half of that module matching needs; the prose
scope-cue extraction (`determine_hiring_scope`) already ran once at enrichment time and its
answer is stored on `job_hunter_job_facets.hiring_regions`. `job_hunter_regions_for_locations
(text[]) returns text[]` ports the same alias table as a SQL function, using the same
`job_hunter_normalize_text` + `\m...\M` word-boundary idiom `job_hunter_salary_floor_for_job`
already uses for a keyed lookup. The hard-constraint check becomes: if
`facets.hiring_regions` is non-empty (an explicit statement) and shares no region with
`job_hunter_regions_for_locations(market.locations)`, the posting is ineligible under that
market — the same fail-open reading `HiringScope.permits` already documents (an empty
`hiring_regions` or empty resolved market regions never blocks).

## Matching without a prior membership row

`job_hunter_match_jobs` is rewritten to drive from `job_hunter_postings`, not
`job_hunter_jobs`:

```
job_hunter_postings p
  left join job_hunter_jobs j on j.posting_id = p.id and j.user_id = (select auth.uid())
  left join job_hunter_job_facets f on f.posting_id = p.id
  left join job_hunter_companies c on c.identity = p.normalized_company
where p.closed_at is null
```

`j.status` is no longer read for filtering — see "The problem" above for why an existing
`rejected` row must not keep suppressing a posting once the gate that wrote it is gone.

**Market attribution without a stored `market_id`.** Today a row's market comes from
`j.market_id`, attributed once at crawl time. A posting with no membership row has no
attribution to read, and the work-mode and salary-floor constraints are market-scoped
(`job_hunter_search_profile_markets.remote_policy`/`relocation_policy`/`gross_base_floor`).
Rather than re-implement `market_policy.attribute_market`'s full attribution walk in SQL,
matching evaluates the posting against **every one of the user's configured markets** via a
`left join lateral` that behaves two ways:

- a row **with** an existing membership gets exactly its stored market (`mk.market_id =
  j.market_id`) — byte-identical to today's single-market join, so every existing
  membership-based test keeps its current SQL result;
- a row **without** membership gets one candidate row per configured market (or one
  synthetic all-null row when the user has configured none, via `left join ... on true`,
  which preserves the outer row exactly the way `left join` already does elsewhere in this
  function).

Each candidate is scored and hard-blocked exactly as today, then reduced to one row per
posting **before** the existing variant-group fold: prefer a market under which the row is
not hard-blocked (has_facets and no blockers) over a higher score under a blocking market,
tie-broken by score — the same `row_number() over (partition by ... order by (eligible)
desc, score desc, ...)` idiom the variant-group fold already uses one level up. A posting is
`ineligible` only if it is hard-blocked under **every** market it was evaluated against
(there is only one candidate row per posting after this reduction, so "ineligible" is simply
"the winning candidate is still blocked").

A user with zero configured markets gets the single synthetic no-market candidate, and the
work-mode hard constraint does not fire for it (fails open) — a deliberate difference from
the existing "market_currency is null" fallback inside `job_hunter_hard_blockers`'s
salary-floor branch, which this ticket does not touch for already-attributed rows.

**Bounding the work.** Returning the whole ranked, classified corpus on every call is what
AC10 ("corpus growth cannot create an unbounded synchronous request") rules out — today's
function returns every one of the caller's own (small, crawl-filtered) membership rows, but
once the driving table is every open posting in the shared corpus that stops being small.
`job_hunter_match_jobs` gains a `p_limit integer` parameter and returns at most that many
**qualified** rows, ranked. Reporting on the other two states is a separate, cheap aggregate
query — see next section — not a second copy of the full ranked set.

## Reporting counts and reasons (rule 5: an empty result must carry its reason)

A new function, `job_hunter_match_state_counts(<same preference/policy args>)`, runs the
same classification (posting × market, reduced per posting, no `p_limit`) and returns
`(state text, reason text, count integer)` grouped rows — cheap relative to full-row
materialisation because it never touches the model-scoring seam and Postgres aggregates
without transferring row bodies. `matching.match_jobs` calls it once per invocation and
carries the totals on `MatchResult` (`state_counts: dict[str, int]`,
`ineligible_reasons`/`unresolved_reasons` breakdowns), so a short or empty result always
has a "here's why" a caller can render, per AC9.

## Membership as an output, not a precondition

`job_hunter_jobs` keeps its current shape (`user_id, posting_id, market_id, status,
first_seen_at, last_seen_at` — see `20260909210000_job_hunter_job_membership.sql:423-444`).
A new, narrow SECURITY DEFINER function, `job_hunter_ensure_job_membership(p_posting_id
uuid, p_market_id text default '')`, does exactly the insert-or-touch
`job_hunter_upsert_job` already does at lines 940-953 of that migration, without the
identity-resolution machinery `upsert_job` needs for a freshly-discovered listing — matching
already knows the exact `posting_id` from its own ranking, so there is nothing to resolve.

`matching.match_jobs`'s row loop is patched, not rewritten: `row["job_id"]` is `None` for a
posting with no prior membership. The delivered/existing-evaluation bulk lookups and the
staleness check are built only from rows that already have a `job_id` (a job with no history
cannot be stale or already delivered — there is nothing to compare against). The two places
the loop currently calls `store.get_job(job_id)` (the blocked branch and the fresh-scoring
branch) first call `store.ensure_membership(posting_id, market_id)` when `job_id is None`,
which returns the just-created id and lets the rest of the existing code run unmodified.
This is the literal reading of AC8: a row earns its membership row only when matching
actually decides to act on it (block or score it), never merely for appearing in the ranked
set — an `unresolved` or a losing/never-scored row never writes one.

## What `MatchedJob`/`MatchResult` gain

Additive only, so every existing assertion on the current fields keeps passing:

- `MatchedJob` gains nothing (a `MatchedJob` is already only ever an `ineligible`/`qualified`
  decision — the SQL surfaces `unresolved` separately, mirroring the current
  `skipped_without_facets_job_ids` field).
- `MatchResult` gains `skipped_without_facets_posting_ids: list[str]` (posting ids always
  exist; the existing `skipped_without_facets_job_ids` stays job-id-keyed for backward
  compatibility and is empty for a posting that never had a membership row) and
  `state_counts: dict[str, int]` from the new counts function.

## Tests

**pgtap** (`supabase/tests/pgtap/`):
- `job_hunter_match_jobs.sql` (new cases): an open posting with no `job_hunter_jobs` row for
  the caller is returned when qualified; a posting hard-blocked under every configured
  market is absent from the bounded ranked call and counted `ineligible` by the counts
  function; a posting with no facets row counts `unresolved`; RLS — a second user's
  membership or lack thereof never changes what the first user sees, and matching a
  single-market membership row (the existing behavior) is bit-for-bit unchanged.
- `job_hunter_regions_for_locations.sql` (new): a representative alias from each of the four
  regions resolves; an unrecognised location resolves to no region (fails open); acronym
  case rules mirror `regions_for_locations`'s "configuration, not prose" reading (bare `US`
  matches, case-insensitively, because these are typed place names).
- `job_hunter_store_functions.sql` — pin the two new functions
  (`job_hunter_ensure_job_membership`, `job_hunter_match_state_counts`) and
  `job_hunter_regions_for_locations` in the tracked set; `job_hunter_shared_writes.sql` if
  `ensure_job_membership`'s definer status needs pinning there too (it never becomes
  reachable from `authenticated` on tables it wasn't already able to write, since it does
  exactly what `upsert_job` already does — confirm during implementation).

**pytest** (`apps/job-hunter/tests/test_matching.py`):
- New: `test_match_jobs_considers_a_posting_with_no_prior_membership` — seed a posting and
  facets, no `job_hunter_jobs` row, assert the result includes it and a membership row now
  exists (this is the literal AC: "an open posting becomes invisible solely because no
  membership row exists" must fail this test if it regresses).
- New: `test_match_jobs_never_creates_membership_for_an_unresolved_posting` — a
  facetless/no-membership posting, assert no `job_hunter_jobs` row is written.
- New: `test_match_jobs_reports_state_counts_on_a_short_result` — an ineligible and an
  unresolved posting alongside zero qualified ones, assert `state_counts` names both.
- Existing tests are kept as-is (they all pre-create membership); this is what proves the
  change is additive rather than a rewrite of documented behaviour.

## Out of scope

- The ready pool (#260), daily stack selection (#261), why-lines (#262) and swipe/exclusion
  learning (#263) — separate tickets per the parent design's ticket boundaries.
- #259's recoverable enrichment machinery — this ticket only produces the `unresolved`
  classification; it does not schedule or perform recovery.
- #79's non-engineering persona fixture does not exist yet (confirmed: no code or fixture
  references it anywhere in the repo). This ticket's AC ("cannot pass through an alternate
  profession-specific path") is satisfied structurally — there is exactly one classification
  path and it carries no profession branch — rather than by a persona-specific test, since
  the persona itself is #79's own unclaimed scope.
- Deleting `prefilter.py`'s title gate, `discovery.py`'s membership-creation call sites, or
  any of `SearchPolicy`'s now-unread `engineering_title_*` fields. That is #189's job, after
  this ticket and #259 both exist, per the parent design doc's own sequencing.

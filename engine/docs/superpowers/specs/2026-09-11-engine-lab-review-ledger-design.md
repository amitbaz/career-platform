# Engine Lab measurement ledger (issue #257)

Status: implementation plan for a claimed ticket. Parent design:
`docs/superpowers/specs/2026-09-11-engine-ready-stack-design.md`.

## Superseded (2026-09-11): no review page, no login

The original design paired this ledger with a bespoke Flask review page, complete with its own
Supabase Auth login (an emailed one-time code, then — after the code turned out to have no
visible number in a fresh local stack — an emailed sign-in link with a client-side callback), an
owner-bootstrap security-definer function, and invite/claim RPCs for collaborators. All of that
went through two rounds of real security and UX review (see PR #282's history) and worked end to
end. The owner then tried it and called the login flow "useless" for a tool one person and
occasional collaborators use, and is instead evaluating an off-the-shelf internal tool (Retool or
similar, tracked as #283) to browse and judge cards.

That removed an entire identity layer this design used to depend on: a `job_hunter_engine_lab_collaborators`
table, `job_hunter_engine_lab_bootstrap_owner`/`_invite`/`_claim_invite` security-definer
functions, RLS keyed off a reviewer's own `auth.uid()`, and the whole Flask `engine_lab_web.py`
module (login/code/callback/invite/summary routes) plus `ENGINE_LAB_SESSION_SECRET`/
`ENGINE_LAB_OWNER_EMAIL`. None of it applies once the consuming tool brings its own login and its
own trusted database credential instead of a per-reviewer Supabase Auth session — so it was all
deleted rather than kept around unused. What is left, and what the rest of this document
describes, is the ledger itself: two tables and the pure card-selection/judgement logic in
`engine_lab.py`, with no identity scheme at all. Whatever eventually calls into this module
connects with its own trusted credential and supplies `reviewer_id` as a free-form string.

## What #257 does and does not include

Included: durably recording an impression before a card is shown, recording two independent
judgements per card, concealing the card's cohort until a judgement exists, carrying the versions
needed to reproduce what was evaluated, and a daily summary that reports intended and audit
counts separately — all as library functions and a schema, for whatever tool ends up calling them.

Not included, because the sibling tickets that would produce them do not exist yet: matching
every open posting (#243), the ready pool (#260), daily stack selection (#261), grounded why
lines (#262), or the internal tool itself (#283). Card selection here is a thin, clearly-versioned
read over what `match_jobs` (#187) already computes and persists — real `Evaluation` rows and
facet-derived hard blocks — never a second ranking implementation. When #262 lands, only
`explanation_version` and the why-line construction change; nothing else in this design depends
on how the why line is produced.

## Schema

One migration, `supabase/migrations/29999999000000_job_hunter_engine_lab.sql` — a deliberately
non-timestamp placeholder per root `AGENTS.md`; renumber to the real `YYYYMMDDHHMMSS` at PR time.

```sql
create table public.job_hunter_engine_lab_impressions (
  id                    uuid primary key default gen_random_uuid(),
  reviewer_id           text not null,
  posting_id            uuid not null references public.job_hunter_postings(id),
  cohort                text not null check (cohort in
                          ('intended', 'audit_hard_excluded', 'audit_unresolved', 'audit_below_threshold')),
  profile_version       text not null,
  posting_version       text not null,
  matching_version      text not null,
  explanation_version   text not null,
  configuration_version text not null,
  shown_at              timestamptz not null default now()
);
-- RLS is on, with no policy at all for `authenticated`/`anon` -- only a trusted connection
-- (service_role, or the postgres role migrations run as) can reach this table, the same way any
-- internal tool would connect to Postgres directly rather than through a per-user session.
-- No update, no delete — an impression is a fact about what was shown, immutable once written;
-- that immutability is what makes "durable before render" checkable at all.

create table public.job_hunter_engine_lab_judgements (
  id                 uuid primary key default gen_random_uuid(),
  impression_id      uuid not null unique references public.job_hunter_engine_lab_impressions(id),
  reviewer_id        text not null,
  worth_applying     boolean not null,
  why_line_judgement text not null check (why_line_judgement in ('helpful', 'flawed')),
  problem_reason     text,
  judged_at          timestamptz not null default now()
);
-- Same RLS shape as impressions. `unique (impression_id)` is the "exactly one judgement per
-- impression" rule; there is no update policy, so re-judging means a fresh impression, not an
-- edit to history.
```

`reviewer_id` is a plain string, not a foreign key to any identity table — there is no
`auth.users` row it has to resolve to. `worth_applying` and `why_line_judgement` are separate
not-null columns — never a single combined verdict — which is what "stored independently" in the
acceptance criteria means, and what stops the why-line quality question silently inheriting the
match verdict.

Cohort concealment: the column exists and is written at impression time (the selection logic
already knows which bucket it picked), but nothing in `engine_lab.py` returns it until
`reveal_cohort` is called, which the caller should only do after a judgement has actually been
recorded — concealment is a contract on when the caller reads the column, not something the
schema itself can enforce, since there is no page here to control that timing on the caller's
behalf any more.

Every column above is `not null` with no default that would make an omission silently pass — a
posting- or config-version field can't be left out, and neither can `worth_applying` or
`why_line_judgement`. pgTAP asserts this directly (see Testing).

## Versions

Reproducibility, not an incrementing registry — each "version" is the content that decided the
row, so it changes exactly when the thing it names changes and never needs a bump list:

- `posting_version` = the posting's own `description_hash` (already stored, already the value
  that decides whether a posting needs re-evaluation — see `postgres_store.needs_evaluation`).
- `matching_version` = a constant naming the code path, `"match_jobs-evaluations-v1"` for cards
  drawn from a persisted `Evaluation`, `"hard-blockers-v1"` for the audit-cohort's
  facet-only classification. Bumped by hand when the selection logic underneath changes.
- `explanation_version` = `"why-line-stub-v1"` — see below; becomes whatever #262 assigns once
  grounded why lines exist.
- `profile_version` = the same cache key `candidate_context.get_candidate_context` already uses
  (`profile_hash` of the CV text, the configured AI model, `CANDIDATE_CONTEXT_SCHEMA_VERSION`) —
  the exact input that decided `CandidatePreferences`, which is what `store.match_jobs`'s ranking
  actually reads. Read from the cache only; a cold cache (no `CandidateContext` extracted yet)
  is reported as an `unavailable` result with that reason, never as a trigger to extract one —
  card selection makes no AI call, ever.
- `configuration_version` = `sha256` of the threshold fields on the caller's `SearchPolicy` that
  the ranking and hard-blocker check actually use (`salary_floor_eur`, `thresholds`,
  `match_score_floor`, `blocked_title_keywords`), serialised with sorted keys. This is the knob
  side of the profile; `profile_version` above is the content side.

## Card selection

All in `apps/job-hunter/src/job_hunter/engine_lab.py`, the entire boundary for eligibility,
ranking, selection and explanation logic — whatever eventually calls this module (Retool or
otherwise) brings no logic of its own, only a trusted database credential and a `reviewer_id`.

No AI call happens on selection. The one source below is `store.match_jobs` — a single cheap
SQL round trip (confirmed by reading `matching.py`) that already returns, per posting,
`hard_blockers` (the SQL port of `hard_blockers_from_facets`) and `has_facets`, at zero AI cost —
plus `get_evaluations_bulk` for the rationale text of a posting that already has a persisted
`Evaluation`. Both are reads `matching.match_jobs` already performs on every call; #257 adds no
second ranking implementation, it reads the first one's own output.

- **`intended`**: `hard_blockers` empty, `has_facets` true, `score >= match_score_floor`.
- **`audit_hard_excluded`**: `hard_blockers` non-empty — the exact mechanism `match_jobs` itself
  uses to exclude a row.
- **`audit_unresolved`**: `has_facets` false — approximated, because #259 has not given
  "unresolved" a stored status yet; the module names this approximation in a comment so #259 has
  one call site to fix instead of a scattered assumption.
- **`audit_below_threshold`**: `hard_blockers` empty, `has_facets` true, `score < match_score_floor`.

One candidate is chosen per call, not a fixed daily batch: a weighted random pick over
whichever of the four buckets has a non-empty candidate list this call (70% `intended`, 10% each
audit bucket when available, renormalised over whatever is actually non-empty). A fixed slot
order (e.g. "the third card of the day is always the audit sample") would let a reviewer learn
the concealment by counting; a per-call random pick does not leak position as a signal. The
seven-day validation's exact 7-intended/1-each-audit daily shape is #265's acceptance criterion,
not #257's — this module only has to be able to produce a card from each bucket, not enforce
their ratio.

Writing the impression row before returning the card is what makes "durable before render" true
rather than asserted: `select_next_card` cannot return a `ReviewCard` without a database round
trip that already succeeded.

**Why line**, until #262: a short deterministic sentence built only from already-stored fields —
`Evaluation.rationale` when the card has one (a real model verdict), or "Excluded: <the hard
blocker(s)>" for `audit_hard_excluded`, or "Not enough information yet to evaluate this posting"
for `audit_unresolved`. Never invented text, never a template that fabricates a reason not
already in `rationale`/`hard_blockers` — this satisfies D10's "no why line invents or misstates a
fact" using only what already exists, and `explanation_version` names it clearly as a stub so a
review of the 90%-helpful threshold is not confused by it.

## Daily summary

`engine_lab.daily_summary(client, day)` groups `job_hunter_engine_lab_impressions` joined to
`job_hunter_engine_lab_judgements` by `cohort`, for one UTC day, reporting per cohort:
impression count, judged count, `worth_applying` rate, `helpful` rate. It reports the four
cohort names explicitly (`intended`, `audit_hard_excluded`, `audit_unresolved`,
`audit_below_threshold`) even when a cohort has zero rows that day — a cohort absent from the
grouped query result is exactly the "missing cohort" the acceptance criteria ask to be named,
and the zero-row case is what a naive `group by` silently drops, so the function starts from the
four known cohort names and left-joins counts onto them rather than the other way round.

## Testing — what fails if a field is silently omittable

- **pgTAP** (`supabase/tests/pgtap/job_hunter_engine_lab.sql`), the repo's established idiom for
  "this must fail" (`job_hunter_platform_ai_usage.sql` uses the same `throws_ok` shape): for each
  `not null` column on both ledger tables, `throws_ok($$ insert ... $$, '23502', ...)` with that
  one column omitted. For the cohort and why-line-judgement enums, `throws_ok` with an
  out-of-set value. For "one judgement per impression", a second insert on the same
  `impression_id` must raise a unique violation. For access, both `anon` and an ordinary
  `authenticated` session are asserted to read and write nothing on either table — there is no
  identity scheme here for RLS to key off, so the only correct answer for those roles is "no
  access at all," not "your own rows."
- **pytest, module-level** (`apps/job-hunter/tests/test_engine_lab.py`), pure unit tests against
  fakes: `select_next_card` writes an impression row before constructing the returned card
  (asserted against the fake client's own call log, not the return value, so a code path that
  builds a card without writing first is caught); bucket classification and the audit-vs-intended
  score-floor boundary; `record_judgement` rejects an invalid why-line verdict before writing;
  `daily_summary` names a cohort with zero rows rather than omitting it.

## Out of scope, filed as follow-ups if found

Anything about ranking quality, real why-line grounding, or the Analytics page (#264) is a
different ticket. What tool actually calls into this ledger, and how, is #283's to decide.
Work found outside #257 while building this becomes a new issue assigned to the owner.

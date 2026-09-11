# Engine Lab review page and measurement ledger (issue #257)

Status: implementation plan for a claimed ticket. Parent design:
`docs/superpowers/specs/2026-09-11-engine-ready-stack-design.md`.

## What #257 does and does not include

Included: a private review page that shows one card at a time, records a durable impression
before the card is rendered, records two independent judgements per card, conceals the card's
cohort until the judgement is committed, carries the versions needed to reproduce what was
evaluated, and a daily summary that reports intended and audit counts separately.

Not included, because the sibling tickets that would produce them do not exist yet: matching
every open posting (#243), the ready pool (#260), daily stack selection (#261), grounded why
lines (#262). Card selection here is a thin, clearly-versioned read over what `match_jobs`
(#187) already computes and persists — real `Evaluation` rows and facet-derived hard blocks —
never a second ranking implementation. When #262 lands, only `explanation_version` and the
why-line renderer change; nothing else in this design depends on how the why line is produced.

## Where this lives

`apps/job-hunter/AGENTS.md`: "matching is one operation (`match_jobs`, #187); ... Matching/
delivery stays on Vercel + GitHub Actions." Job Hunter already deploys as one Flask app on
Vercel (`apps/job-hunter/vercel.json` → `main.py` → `telegram_webhook.create_app()`), which is
the request-response surface this repository already uses for the engine. Engine Lab is a new
set of routes on that same Flask app, in a new `engine_lab_web.py` registered from
`create_app()`, not a page in `apps/relay`. Relay is being phased out to one screen (Profile;
CV/cover letter/keys) per root `AGENTS.md`, and Engine Lab is not a product feature — it is the
engine's own instrumentation, so it belongs with the engine.

This also means Engine Lab never needs Relay's Supabase Auth (browser-side signup/session). It
authenticates its own reviewers server-side, the same way the webhook already authenticates
Job Hunter's own trusted processes.

## Identity: who is "the owner and explicitly invited collaborators"

No owner/collaborator concept exists anywhere in this codebase today (confirmed: no `owner_id`
column, no allowlist table, no `is_owner()` function). This design introduces exactly one new
table, `job_hunter_engine_lab_collaborators`, and reuses the JWT-minting pattern
`AccessTokenMinter` already established for `JOB_HUNTER_USER_ID` (`supabase_auth.py`) rather
than inventing browser-facing auth (password login, magic links, Supabase Auth signup).

- `invited_by` runs `python -m job_hunter engine-lab-invite --email you@example.com`. This
  generates a fresh `user_id` (a `uuid4`, not necessarily a real `auth.users` row — the same way
  `JOB_HUNTER_USER_ID` is just a UUID recognised by RLS, not evidence of a signup flow) and a
  32-byte random token, stores the token's SHA-256 hash, and prints the raw token once. Sharing
  that token out of band with a collaborator *is* the explicit invitation the acceptance
  criteria ask for.
- A reviewer visits `/engine-lab/login`, submits the token. Flask hashes it, looks up the
  collaborator (a `job_hunter_runner`-claimed read — see below), and on a match sets a signed
  Flask session cookie (`app.secret_key` from a new `ENGINE_LAB_SESSION_SECRET` env var) holding
  the collaborator's `user_id`, nothing else. The raw token is never stored, never logged, and
  never leaves the login request.
- Every later request re-reads `job_hunter_engine_lab_collaborators` for that `user_id` and
  refuses (redirects to `/engine-lab/login`) if the row is missing or `revoked_at` is set — so
  revoking access is one `update ... set revoked_at = now()` away from taking effect on the next
  request, not just at next login.
- The reviewer's browser never sees a Supabase JWT. Flask mints one server-side, per request,
  scoped to that reviewer's `user_id`, using the existing `SUPABASE_SIGNING_KEY_B64` (already a
  required env var for this deployment) and an extended `AccessTokenMinter` that can carry one
  extra boolean claim: `engine_lab_reviewer: true`. That claim, plus `sub = <reviewer uuid>`, is
  what row-level security keys on for the two ledger tables.
- Reading the collaborators table to validate a login token needs a caller trusted before we
  know who they are. `job_hunter_platform_ai_usage`'s RLS already solved this: gate `select` to
  the existing `job_hunter_runner` claim (any token minted by a process holding the signing
  key), not to a specific `sub`. Flask mints a `job_hunter_runner` token as `JOB_HUNTER_USER_ID`
  for this one lookup, exactly as `telegram_webhook._build_supabase_client` already does.
  `insert`/`update` on the collaborators table (inviting, revoking) requires a second claim,
  `engine_lab_admin: true`, set only by the CLI invite command — so a compromised webhook
  process could read the collaborator list but could not invite or revoke.

`AccessTokenMinter` changes minimally: one new constructor parameter,
`extra_claims: dict[str, Any] | None = None`, merged into the minted payload. Default is
`None` → identical behaviour to today, so the webhook's own token minting is untouched.

## Schema

One migration, `supabase/migrations/29999999000000_job_hunter_engine_lab.sql` — a deliberately
non-timestamp placeholder per root `AGENTS.md`; renumber to the real `YYYYMMDDHHMMSS` at PR
time.

```sql
create table public.job_hunter_engine_lab_collaborators (
  user_id     uuid primary key,
  email       text not null unique,
  display_name text not null default '',
  token_hash  text not null,
  invited_at  timestamptz not null default now(),
  invited_by  text not null,
  revoked_at  timestamptz
);
-- RLS: select to job_hunter_runner claim; insert/update to engine_lab_admin claim. No delete
-- policy — revoke by setting revoked_at, never by removing the row a token_hash could collide
-- into.

create table public.job_hunter_engine_lab_impressions (
  id                    uuid primary key default gen_random_uuid(),
  reviewer_id           uuid not null references public.job_hunter_engine_lab_collaborators(user_id),
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
-- RLS: insert to (engine_lab_reviewer claim and auth.uid() = reviewer_id) or job_hunter_runner
-- (the daily-summary job reads across reviewers). No update, no delete — an impression is a
-- fact about what was shown, immutable once written; that immutability is what makes "durable
-- before render" checkable at all.

create table public.job_hunter_engine_lab_judgements (
  id                 uuid primary key default gen_random_uuid(),
  impression_id      uuid not null unique references public.job_hunter_engine_lab_impressions(id),
  reviewer_id        uuid not null references public.job_hunter_engine_lab_collaborators(user_id),
  worth_applying     boolean not null,
  why_line_judgement text not null check (why_line_judgement in ('helpful', 'flawed')),
  problem_reason     text,
  judged_at          timestamptz not null default now()
);
-- RLS: same shape as impressions. `unique (impression_id)` is the "exactly one judgement per
-- impression" rule; there is no update policy, so re-judging means a fresh impression, not an
-- edit to history.
```

`worth_applying` and `why_line_judgement` are separate not-null columns — never a single
combined verdict — which is what "stored independently" in the acceptance criteria means, and
what stops the why-line quality question silently inheriting the match verdict.

Cohort concealment: the column exists and is written at impression time (server already knows
which bucket it picked), but the review page's render never includes it, and
`/engine-lab/judge` only echoes it back in the response *after* the judgement insert succeeds.

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

## Card selection (engine-owned, not page logic)

All in `apps/job-hunter/src/job_hunter/engine_lab.py`, called by the Flask routes and by
nothing else — the acceptance criterion "no eligibility, ranking, selection or explanation logic
in the page" means this module is the entire boundary; `engine_lab_web.py` only turns a
`ReviewCard` into HTML and a POST into a call.

No AI call happens on a page load. The one source below is `store.match_jobs` — a single cheap
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

One candidate is chosen per request, not a fixed daily batch: a weighted random pick over
whichever of the four buckets has a non-empty candidate list this call (70% `intended`, 10% each
audit bucket when available, renormalised over whatever is actually non-empty). A fixed slot
order (e.g. "the third card of the day is always the audit sample") would let a reviewer learn
the concealment by counting; a per-request random pick does not leak position as a signal. The
seven-day validation's exact 7-intended/1-each-audit daily shape is #265's acceptance criterion,
not #257's — this module only has to be able to produce a card from each bucket, not enforce
their ratio.

Writing the impression row before returning the card is what makes "durable before render" true
rather than asserted: the function that returns a `ReviewCard` cannot return one without a
database round trip that already succeeded.

**Why line**, until #262: a short deterministic sentence built only from already-stored fields —
`Evaluation.rationale` when the card has one (a real model verdict), or "Excluded: <the hard
blocker(s)>" for `audit_hard_excluded`, or "Not enough information yet to evaluate this posting"
for `audit_unresolved`. Never invented text, never a template that fabricates a reason not
already in `rationale`/`hard_blockers` — this satisfies D10's "no why line invents or misstates a
fact" using only what already exists, and `explanation_version` names it clearly as a stub so a
review of the 90%-helpful threshold is not confused by it.

## Daily summary

`engine_lab.daily_summary(store, day)` groups `job_hunter_engine_lab_impressions` joined to
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
  `impression_id` must raise a unique violation. For RLS, `authenticate_as` a non-collaborator
  and assert both ledger tables refuse insert and select.
- **pytest** (`apps/job-hunter/tests/test_engine_lab.py`): `select_review_cards` writes an
  impression row before constructing the returned card (assert the row exists via a second,
  independent read, not via the return value, so a code path that builds a card without writing
  first is caught); a card is never missing any of the five version fields (a
  dataclass with no defaults on those fields already makes "forgot to set one" a
  `TypeError`, and a test constructs one without each field in turn to confirm it); cohort is
  absent from the HTTP response body until judgement, present after;
  `record_judgement` rejects a second call for the same `impression_id`; `daily_summary` names a
  cohort with zero rows rather than omitting it.

## Out of scope, filed as follow-ups if found

Anything about ranking quality, real why-line grounding, or the Analytics page (#264) is a
different ticket. Work found outside #257 while building this becomes a new issue assigned to
the owner, per `docs/agents/roles.md`.

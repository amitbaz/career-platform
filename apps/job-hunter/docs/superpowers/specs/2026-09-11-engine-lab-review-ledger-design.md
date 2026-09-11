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

This also means Engine Lab never needs Relay at all — but it does use real Supabase Auth,
directly, server-side (see below), not a platform-minted token.

## Identity: who is "the owner and explicitly invited collaborators"

**Revised after owner review** (2026-09-11): the first version of this design minted its own
JWTs with a custom `token_hash`-based invite link, mirroring `AccessTokenMinter`'s
trusted-process pattern. The owner rejected that in review — a bespoke token to generate, copy
and track for every person, and no answer to "who is the owner, based on what" beyond "whoever
ran the CLI first." This version uses real Supabase Auth instead: no tokens, no CLI, no custom
JWT claim.

- **Login is GoTrue's own emailed one-time code** — `POST /auth/v1/otp` then `POST /auth/v1/verify`
  (`type: "email"`), called directly from Flask via the existing `HttpClient`, never through a
  browser SDK or Relay. A reviewer gets a real `auth.users` row and a real Supabase-issued
  `access_token`/`refresh_token` — this module never mints a token of its own for a reviewer.
- **`ENGINE_LAB_OWNER_EMAIL`** (a new env var) is the one fact that decides who the owner is.
  Nothing in the database knows this value. Right after a code verifies, Flask compares the
  verified email against it and, only on a match, calls
  `job_hunter_engine_lab_bootstrap_owner(p_user_id, p_email)` using a *runner-claimed* client
  (`subject_store_client`, the same `AccessTokenMinter` identity every other trusted Job Hunter
  process uses) — never the reviewer's own session client.
  **Security fix (PR #282 review round 3):** the first cut of this function checked only that
  the caller's own `auth.jwt() ->> 'email'` matched `p_email`, which is a self-consistency check,
  not an identity check — any Supabase Auth user (self-registered via `create_user: true` on
  `/auth/v1/otp`) could call this RPC directly over PostgREST with their own uid/email and
  permanently claim ownership before the real owner ever logged in, since the database never
  compared against `ENGINE_LAB_OWNER_EMAIL` itself. The fix moves the gate into the database: the
  function now requires the caller's JWT to carry `job_hunter_runner: true`, a claim only
  `AccessTokenMinter` (run server-side, with a private signing key) can produce — an ordinary
  reviewer session never has it, so the RPC is unreachable to anyone but Flask, regardless of
  what `p_email` is passed. Flask's own `ENGINE_LAB_OWNER_EMAIL` check still runs first (so a
  non-owner login never triggers the call at all), but the database no longer trusts that check
  alone.
- **Collaborators are invited by email, from the page itself, by the owner** — a plain HTML form
  (`/engine-lab/invite`) that calls `job_hunter_engine_lab_invite(p_email)` (owner-only,
  self-checked). This creates a row with no `user_id` yet, because the invitee has no
  `auth.users` row until they sign in. When they do, `job_hunter_engine_lab_claim_invite()`
  backfills their real `user_id` by matching their own verified email — never a caller-supplied
  one, so nobody can claim someone else's invite.
- **Revocation** is `revoked_at` on the collaborators row (owner-only path, not yet a page
  action — filed as a small follow-up if wanted). Every route re-reads the caller's own
  collaborator row on each request, so a revoke takes effect on the very next request, not just
  at next login.
- **Session state**: Flask's own signed cookie (`ENGINE_LAB_SESSION_SECRET`) holds the reviewer's
  `access_token`/`refresh_token`/`expires_at` between requests, refreshing via
  `/auth/v1/token?grant_type=refresh_token` when close to expiry. The token is used as-is for
  that reviewer's own PostgREST calls — no re-minting.

Nothing in `supabase_auth.py` changes. `AccessTokenMinter` is unmodified and is used for exactly
one thing here: `subject_store_client`, which is a completely separate concept from reviewer
login — it answers "whose corpus and profile is being matched" (always `JOB_HUNTER_USER_ID`,
the same identity every other Job Hunter process already acts as), never "who is signed into
Engine Lab."

## Schema

One migration, `supabase/migrations/29999999000000_job_hunter_engine_lab.sql` — a deliberately
non-timestamp placeholder per root `AGENTS.md`; renumber to the real `YYYYMMDDHHMMSS` at PR
time.

```sql
create table public.job_hunter_engine_lab_collaborators (
  email      text primary key,
  user_id    uuid unique references auth.users(id),
  is_owner   boolean not null default false,
  invited_at timestamptz not null default now(),
  invited_by uuid references public.job_hunter_engine_lab_collaborators(user_id),
  revoked_at timestamptz
);
-- Keyed by email, not user_id: an invitee has no auth.users row (and hence no user_id) until
-- their first sign-in. No insert/update/delete policy for `authenticated` at all — every write
-- goes through one of three security-definer functions:
--   job_hunter_engine_lab_bootstrap_owner(p_user_id, p_email)  -- claims owner, once, callable
--                                                       only by a caller carrying the
--                                                       job_hunter_runner claim (see Identity)
--   job_hunter_engine_lab_invite(p_email)           -- owner-only, adds a pending row
--   job_hunter_engine_lab_claim_invite()            -- backfills the caller's own user_id by
--                                                       their own verified email
-- Two more security-definer helpers, job_hunter_engine_lab_caller_is_owner() and
-- job_hunter_engine_lab_is_active_collaborator(uuid), let RLS policies (here and on the two
-- tables below) check membership without a recursive self-join.

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
-- RLS: insert requires reviewer_id = auth.uid() and an active collaborator row for that uid;
-- select allows a reviewer their own rows, or the owner across everyone (for the daily
-- summary). No update, no delete — an impression is a fact about what was shown, immutable
-- once written; that immutability is what makes "durable before render" checkable at all.

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
- **pytest, module-level** (`apps/job-hunter/tests/test_engine_lab.py`): `select_next_card`
  writes an impression row before constructing the returned card (asserted against the fake
  client's own call log, not the return value, so a code path that builds a card without
  writing first is caught); bucket classification and the audit-vs-intended score-floor boundary;
  `record_judgement` rejects an invalid why-line verdict before writing; `daily_summary` names a
  cohort with zero rows rather than omitting it; the owner-bootstrap/invite/claim functions are
  called with the right RPC name and arguments, and never called at all when this module's own
  precondition (verified email matches `ENGINE_LAB_OWNER_EMAIL`) fails.
- **pytest, HTTP layer** (`apps/job-hunter/tests/test_engine_lab_web.py`), a Flask test client
  with `engine_lab` faked at the module boundary: cohort is absent from `GET /engine-lab/review`'s
  body and present after `POST /engine-lab/judge`; a `javascript:`-scheme posting URL is never
  rendered as a link; `/engine-lab/invite` and `/engine-lab/summary` refuse a non-owner; an
  unauthenticated request redirects to login; a wrong code and an uninvited email are both
  refused without creating a session.

## The page itself is deliberately minimal

`engine_lab_web.py` renders plain HTML strings from Flask — no template engine, no client-side
JavaScript, a full page reload on every action. That is a real, visible trade-off, made
knowingly: `apps/job-hunter` already deploys as one small Flask app on Vercel for the Telegram
webhook, and reusing it for a handful of internal pages costs no new app, hosting or build
pipeline for a tool a few people use. It is not a statement about what internal tooling should
look like going forward.

The owner's direction (2026-09-11): once there is more than one internal page like this, they
want a separate, modern internal web app — for the owner's own use running the business, not
the mobile app end users see — with room for more pages beyond review (analytics, ingestion
health, and whatever else #264 and later engine work need a place to live). That is a real
initiative, not a decision to make inside this ticket; filed as a follow-up issue rather than
started here.

## Out of scope, filed as follow-ups if found

Anything about ranking quality, real why-line grounding, or the Analytics page (#264) is a
different ticket. Work found outside #257 while building this becomes a new issue assigned to
the owner, per `docs/agents/roles.md`.

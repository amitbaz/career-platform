# Engine-ready stack design (issue #246)

Status: approved shaping decisions; implementation is split into the tickets at the end of this
document.

## Purpose

The stack is the first complete slice of the engine: shared posting facts become a ranked,
explained set of opportunities for one user, and the user's decisions become learning signals.
The mobile app and internal tools consume that slice. They do not decide eligibility, ranking,
selection, explanations or replenishment themselves.

This design sets both the behavior and the evidence required before the engine may be called
ready. It serves the competitive position in `docs/research/beating-both.md`: win on match
quality and the learning loop, not on showing the largest possible job board. Employer outcomes
remain the stronger signal later through #80; this slice starts with measured human judgements
and swipes without confusing them with outcomes.

## Scope

This slice includes:

- matching against every open posting in the shared corpus;
- profession-neutral eligibility, ranking and explanations;
- recoverable handling of incomplete posting information;
- a background per-user ready pool;
- a fresh daily stack with reserve replenishment;
- soft swipe learning and explicit reversible rules;
- private review and analytics surfaces;
- automatic ingestion-efficiency and freshness measurement; and
- a seven-day engine-ready validation.

It does not include the product's full onboarding, general CV rewriting, per-job application
drafting, the coach, submitting, pricing or tier policy. A rough-CV input contract is included
because matching cannot be evaluated without deciding how missing user evidence behaves.

## Terms and states

### Posting readiness is not a binary matchable flag

An open posting has one of three engine states for a user:

- **Ineligible:** known facts prove a hard constraint is violated.
- **Qualified:** the engine has enough evidence to compare and rank it.
- **Unresolved:** available evidence is insufficient. It is not rejected and remains recoverable
  until it closes.

`Unresolved` is deliberately not called `unmatchable`. Missing information cannot prove that a
good match does not exist.

Hard constraints are limited to:

- work outside CV-driven office and knowledge work;
- known hiring or location ineligibility;
- a required work mode that conflicts with the user's confirmed requirement;
- clearly incompatible seniority;
- stated salary below an explicit user minimum; and
- an explicit user exclusion.

Titles, industry, company size and nonessential skills are preferences unless the user explicitly
turns one into a rule. An adjacent title is allowed when the actual work is a strong match; its
why line must explain the connection.

### Missing CV evidence is unknown, not negative evidence

A rough CV must not block the first useful experience. The engine consumes a minimum set of
confirmed facts extracted from the CV and a small number of direct questions. Missing evidence
lowers confidence or creates an unknown; it does not become proof of poor fit.

The user's explicit career goal overrides the titles already present on the CV. CV evidence still
sets confidence and makes stretch visible. General CV polishing can happen later, and job-specific
tailoring happens only after the user chooses to prepare an application.

## Engine shape

The flow is:

1. Ingestion maintains one shared corpus and shared objective facts per posting.
2. Incomplete postings enter recoverable enrichment, with aggressive attempts during their first
   24 hours, less frequent attempts later, and immediate reconsideration when a source changes.
   Richer copies and source variants may contribute facts. The posting remains unresolved until
   enough evidence exists or it closes.
3. Matching reads every open posting. A per-user row created during crawling is not a prerequisite
   for consideration.
4. Known hard constraints exclude; unresolved evidence remains visible to recovery and audit;
   qualified postings are ranked against confirmed facts, goals and preferences.
5. Bounded background work prepares a per-user ready pool, including grounded why lines. It never
   performs objective extraction separately for each user.
6. The first open of the user's day selects today's stack and reserve from that ready pool.
7. Decisions update tomorrow's ranking. Explicit rules also invalidate matching reserve cards
   immediately today.

Canonical page retrieval and other network enrichment are background posting work. Neither an
API request nor opening the stack waits on live crawling, live page retrieval or live AI work.

## Daily stack behavior

The product starts with a configurable daily preparation capacity of 20. This is the number of
right swipes the system is prepared to turn into applications, not a limit on cards the user is
allowed to inspect. The value is engine-owned configuration, not a hard-coded product promise and
not connected to pricing yet.

At first open:

- the engine returns the best available cards plus a reserve;
- a left swipe may be replaced from the reserve while capacity remains;
- a configurable review pause prevents the interaction becoming an endless job board, but the
  user may continue;
- opportunities not selected today remain eligible for a later day rather than appearing as a
  blocked list;
- new opportunities discovered after today's selection normally compete tomorrow;
- a closed posting disappears immediately; and
- a quality threshold may produce a short stack and must never be bypassed to fill a quota.

The experience target is the first card within 1 second and the complete initial stack within
2 seconds. The ready pool is what makes this possible.

The engine response always carries one of these explicit statuses:

- `ready`: cards are available;
- `short`: fewer cards passed the quality bar, with the reason breakdown;
- `processing`: useful work is in progress, with freshness and next-action information; or
- `unavailable`: the request cannot currently be served, with the cause.

An empty card array without one of these reasons is invalid.

## Why lines

Each card leads with one short reason this posting is worth the user's attention. It must be
grounded in confirmed user evidence and shared posting facts. Missing information is stated as
unknown and never filled by invention.

A stretch card names its main stretch immediately. Match quality and explanation quality are
measured separately: a good match can have a poor explanation, and a persuasive explanation
cannot rescue a poor match.

## Feedback

An ordinary left or right swipe is a soft preference signal for future ranking. It never creates
a permanent exclusion by itself.

An optional explicit rejection reason creates a visible, reversible rule. That rule applies
immediately to today's reserve and to future ranking. Preference signals remain separate from
employer outcomes: an internal judgement, a product swipe and an employer response are different
events even when all three later teach the engine.

## Engine interface

Surfaces need three small capabilities and contain no engine decisions:

1. **Open today's stack:** returns cards, preparation capacity remaining, reserve availability,
   pool freshness, status and any shortfall reason.
2. **Record a decision:** records the impression, user action, optional reason, why-line judgement
   when internal, and all relevant versions before returning a replacement if one is available.
3. **Read analytics:** returns authorised aggregate measurements for the internal owner surfaces.

The exact transport and schemas belong to the implementation tickets. These behavioral boundaries
do not.

## Private Engine Lab

Engine Lab is private to the owner and explicitly invited collaborators. It has two surfaces:

**Update (2026-09-11):** #257's implementation of Review shipped a bespoke Flask page with its
own Supabase Auth login, then dropped it — the owner found a one-off login flow not worth
maintaining for a single-owner tool and is instead evaluating an off-the-shelf internal tool
(Retool or similar, tracked as #283) to serve as the actual surface. What #257 built and kept is
the measurement ledger only (two tables, no identity scheme — see its own design doc); "the
review page" below describes the product behavior any surface serving it must have, not a page
that exists in this repository. Analytics (#264) has not been built yet and may or may not follow
the same path.

### Review

The review page shows the card facts, why line and source link, then records:

- the impression before the card is shown;
- `worth applying` or `not worth applying`;
- `why helpful` or `why flawed`;
- an optional problem or rejection reason; and
- profile, matching, explanation and posting versions.

`Worth applying` is internal evaluation language. The product action remains `prepare it`. They
may both teach ranking, but they must never be mixed in measurement.

### Analytics

The analytics page shows:

- engine-ready progress and each threshold independently;
- hard-error and blind-audit findings;
- the funnel from open corpus through ineligible, unresolved, qualified, ready pool and shown;
- exclusion, rejection and short-stack reasons;
- ready-pool freshness and response latency;
- trends by day, source, profession and market;
- active configuration and matching/explanation versions; and
- ingestion worker health and time-of-week yield.

The page reads engine-provided events and aggregates. It does not reproduce matching logic.

## Ingestion timing measurement

The existing per-source crawl ledger already records outcome, new-to-corpus count, changed count,
request count and elapsed time. It does not durably record a Render worker invocation that wakes,
finds an empty queue and exits. Every worker invocation must therefore write a run record from
start through finish, including `queue_empty`, claimed message count, result counts and elapsed
time. An unfinished or missing expected run is a visible health failure, not a quiet gap.

Engine Lab groups these records by source, UTC hour and weekday and reports:

- empty-worker ratio and approximate compute time;
- new and changed postings per crawl, request and compute second;
- queue-to-worker delay;
- source-published-to-first-seen delay where the source timestamp is trustworthy; and
- failures, rate limits and stale telemetry.

After one complete rolling week, including a weekend, the page produces `keep`, `reduce`, or
`insufficient evidence` for each source/time window and shows the evidence. The system never uses
one worldwide night or weekend blackout. When a quiet window is reduced, labelled safety crawls
continue there so the system can detect behavior changes instead of making the original assumption
permanent. Scheduling remains configurable and the recommendation is recalculated continuously;
the owner is not responsible for remembering to run an analysis.

## Engine-ready validation

Validation lasts seven consecutive days and reviews ten cards per day:

- seven intended recommendations;
- one hard-excluded posting;
- one unresolved posting; and
- one posting immediately below the recommendation threshold.

The reviewer does not see which of the last three groups a card came from until after judging it.
Recommendation and audit metrics are reported separately. The seven-day total is 49 intended
recommendations and 21 blind-audit cards. If the engine cannot produce seven intended cards on a
day without lowering its quality bar, it records a volume failure rather than padding the stack.

The `Engine ready` bar is:

- at least 80% of intended recommendations are judged worth applying to (at least 40 of 49);
- zero hard eligibility mistakes;
- zero invented or incorrect why-line claims;
- at least 90% of why lines are judged specific and helpful;
- no clearly good match is discovered in the blind-audit groups;
- first-card latency is at most 1 second and complete-initial-stack latency at most 2 seconds; and
- every short or empty result carries its measured reason.

Any failed threshold is shown independently and blocks the milestone. This one-week sample is an
initial release gate, not statistical proof that the miss rate is near zero. Blind audit continues
after the milestone so later regressions can fail loudly.

## Ticket boundaries

Existing corpus prerequisites:

- #249 — persist one ATS job once, whichever source discovered it (complete);
- #254 — stop ATS job identifiers crossing between distinct postings; and
- #79 — exercise the engine with a non-engineering persona.

Implementation is split into independently reviewable tickets for:

1. #257 — Engine Lab review and its measurement ledger.
2. #258 — ingestion worker-run telemetry and time-aware scheduling evidence.
3. #259 — recoverable unresolved-posting enrichment.
4. #243: matching every open posting without crawl-time membership or an engineering-title gate.
5. #260 — the bounded per-user ready pool.
6. #261 — daily stack selection, configurable capacity and reserve replenishment.
7. #262 — grounded why lines and stretch disclosure.
8. #263 — soft swipe learning and explicit reversible rules.
9. #264 — private Engine Lab analytics.
10. #265 — the seven-day engine-ready validation.

#189 remains an ingestion cleanup ticket. #243 removes the missing crawl-time membership handoff;
the unresolved-posting ticket owns background canonical/content recovery. Once both exist, #189
can delete the monolithic code without silently deleting either responsibility.

Each implementation ticket gets its own implementation plan when claimed. This shaping record is
not a single cross-system implementation plan.

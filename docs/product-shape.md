# Product shape

Last updated: 2026-09-09.

The founder's direction: one cross-platform app is the product, Relay is a proof of concept to
be rewritten and loses its name, and Telegram demotes to a notification channel. This document
says what that app is, what survives the rewrite, and what it costs.

Recommendations with reasoning, not options. **Founder's call** marks the few that need his
taste.

Read [research/beating-both.md](research/beating-both.md) alongside this. Its conclusions
sharpen the spine below. #80 is the join between inbox outcomes and `opportunities`. The
coach rebuild's first feature is the interview-invitation trigger, which builds preparation
from the application actually sent.

## The finding that decides the shape

**There are two application trackers in this repository and neither knows the other exists.**

- **`opportunities` + `opportunity_events`** — the coach side. A real lifecycle:
  `considering → applied → interviewing → offer → rejected | withdrawn | closed`, with an
  event log and the transitions expressed as RPCs (`create_opportunity`,
  `transition_opportunity`, `schedule_opportunity_interview`).
- **`job_hunter_application_events`** — the engine side. Derived from the user's inbox:
  `job_id`, event type, confidence, company, role title, `source` defaulting to `gmail`.

`apps/job-hunter/src` contains **no reference to `opportunities` at all** — two comments use
the English word, and that is the entire connection. So the engine reads your mail, works out
that you applied or were rejected, and writes it to a table the product's own tracker cannot
see. The user would have two lists of the same applications.

**And the bridge the founder wants already exists in the schema.**
`practice_plan_opportunities` links a practice plan to a *specific opportunity* with a
`primary`/`supporting` relevance. The connective tissue between "find me the job" and "prepare
me for this interview" was modelled weeks ago and has never been fed by the engine.

## What the app is

**The opportunity is the object the product is built around.** Not jobs, not practice
sessions — the opportunity, because it is the only thing in the system with a life that spans
both halves.

One spine, five moments:

1. **A match arrives.** Gili found it, scored it, and says why.
2. **You decide.** Interested becomes an opportunity; not interested teaches the ranking.
3. **You apply.** The pack is prepared here; the engine's inbox detection moves the status
   rather than writing to a parallel log.
4. **You prepare.** An interview is scheduled on the opportunity, and a practice plan attaches
   to it — grounded in your own evidence, for this company and this role.
5. **You track.** The event log already exists and is already an audit trail.

Everything the product does is one of those five things against one object. That is the app,
and it is why one app rather than two: today the engine ends where the user's anxiety begins.

**Recommendation: `opportunities` becomes the single spine, and `job_hunter_application_events`
becomes a source of transitions into it rather than a parallel tracker.** Two trackers is not a
gap to be bridged later; it is the thing that must be reconciled first, because every surface
built on either one deepens the split.

## What survives the rewrite

**Relay's code should not survive. Relay's domain model mostly should.** These are different
questions and answering them together would throw away the most considered thinking in the
repository.

The founder's concern — that Relay was built for his own needs as a software engineer and
does not support a broader product — was checked against the schema and the source rather than
argued. **He is right about the product and wrong about the object model**, and the two need
separating.

**Profession-neutral in structure and in data. Keep as the model:** `profiles` (role and
seniority are free text), `source_documents`, `profile_evidence`, `career_stories`,
`career_story_evidence`, `coach_observations`, `observation_evidence`, `interview_sessions`,
`interview_questions`, `question_evaluations`, `session_evaluations`, `competencies` — whose
names are free text with a generated `normalized_name` and no vocabulary constraint. The
evaluation dimensions are entirely neutral: correctness, depth, clarity, structure, practical
experience, trade-off awareness, communication, confidence, relevance. Blueprint tables carry
counts and status only; there is no tech-interview stage enum anywhere. Plus the **opportunity
lifecycle and its event log**, and **`practice_plan_opportunities`** — the bridge, already
correct and profession-neutral.

**Engineering in data only — reseedable with an edit, not a migration:** the tech-lead round
content, the backend topic areas, the seniority calibration (whose own source says it is
"calibrated to a frontend-leaning Senior Full Stack Engineer"), and the profile-extraction
prompt hard-coded to "Extract a concise software-engineering profile".

**Engineering in structure — these three need a migration or a real rewrite, and they are the
substance of the founder's point:**

1. **`hands_on_checkpoints.code text not null`.** The column is named `code`, not `artifact` or
   `work_product`. The generator is a single hard-coded React exercise whose own comment says
   it is "intentionally React-specific for now", defaults the role to "frontend engineer", and
   grades with a prompt opening "You are a senior frontend interviewer". A hands-on checkpoint
   for any other profession is not a variation on this; it is a different thing.
2. **The readiness dimensions.** Seven axes, four of them engineering — frontend, backend,
   system-design, coding, ai-engineering — and every competency-name rule is a stack regex
   (`react|css|a11y`, `sql|node|queue`). Evidence that matches nothing falls through to a
   category rule and otherwise returns null. The drop is deliberate, documented and surfaced as
   a count, and that reasoning is sound: a wrong assignment silently corrupts a score where a
   dropped one can be investigated. **The problem is not that it is silent — it is that for a
   non-engineer the vocabulary matches almost nothing**, so the readiness picture that comes
   back is coarse, mostly routed through the category fallback, and labels a nurse's practical
   answer "coding".
3. **`architecture` and `system-design` as members of the question-category enum.**

**So what was built for one software engineer is the coaching content and the readiness
vocabulary, not the object model underneath them.** The evidence model and the opportunity
spine transfer as they stand; the coach's vocabulary needs generalising before a second
profession can use it.

**Do not keep:** the view structure and the front-end itself. `relay-shell.tsx` is a 62 KB
component with a 91 KB test file beside it; that is a monolith, and rebuilding around the
opportunity spine is cheaper than unpicking it.

**Do not keep the name.** See below.

**Rebuild the coach's product surface from new specifications**, as the founder has already
said, and generalise the three structural items above as part of that work rather than
before it. The tables are worth keeping as a model; the experience they were built for was a
concept, not a product.

## Where Telegram sits

**Notification and a single decision. Nothing else.**

The minimum it must still do: deliver the match with its reasons, take one tap for interested
or not interested, and deep-link into the app for everything after that. Deciding properly,
preparing and tracking all move in.

That means `job_hunter_telegram_navigation_sessions` — a navigation state machine living
inside the bot — largely goes. A bot that holds navigation state is a bot pretending to be an
app, and once there is an app it is duplicated surface with none of the affordances.

**Keep Telegram.** It is how a job reaches someone who is not in the app, and speed to the user
is the axis the competitor sells on. It stops being where the product happens.

## The name

**Nothing replaces Relay.** If the app is the product, it takes the product's name: Gili. The
web app is Gili, the cross-platform app is Gili, and the bot is Gili sending you a message.

The only naming question left is directory naming inside the repository, which is a rename
ticket rather than a brand decision.

## What this does to the launch preconditions — the honest part

**#201 lists "a web posting surface that can display a posting with its attribution" as one
line among nine. If the answer is a cross-platform app, that line is no longer one of nine —
it is larger than the other eight together.** It is a full front-end, an authenticated
multi-user experience, an opportunity tracker, an application flow, and eventually the coach,
across two or three platforms. It competes with the engine tickets for every workspace, and
the engine is the part that is nearly finished.

**Recommendation: one codebase, mobile as the product, web as the first release channel.**

**Mobile is the product.** The daily moment this is built around — a notification, "here is
what I found", one tap to decide — is a phone moment, and that is where people are. An earlier
version of this document recommended web on iteration speed, which was build convenience
dressed as product reasoning.

**But web-first is not a detour, because it is the same app through the channel with no
gatekeeper.** Expo treats `web` as a first-class bundler target beside iOS and Android, so one
React codebase produces all three — and the existing investment is Next.js and React, so the
skills transfer rather than being spent. The choice is not which surface to build; it is which
target to release first. (The stack decision belongs to the board; it is raised here only
because whether web-first is a detour depends on it. Platform coverage is per-module, and
anything resting on a mobile-only capability will have no web equivalent.)

**Two product reasons — not convenience — for releasing web first:**

1. **Applying is a desktop activity and the product cannot change that.** Automatic submission
   is deliberately forbidden, so the user takes the pack and completes the employer's own form
   — Greenhouse, Workday, Lever, iCIMS. Those forms are miserable on a phone. A mobile-only
   first release walks the user to the moment of highest intent and hands them a task their
   device is bad at. **Decide on mobile, apply on a laptop** is a behavioural observation
   rather than a compromise, and the pack must be reachable from a desktop whatever ships
   first.
2. **Match quality is unproven and every fix on web is instant, where every fix on mobile is a
   review cycle.** While the claim everything rests on is still being validated, a release
   cadence measured in days is a real tax.

**Scope the first release to the spine's first three moments** — a match arrives, you decide,
you apply and track — with the coach explicitly out of it. Beyond launch pressure, the coach's
readiness vocabulary and hands-on checkpoint are engineering-shaped in structure, so shipping
it early would either ship an engineer-only experience or force the generalisation under time
pressure.

**Founder's call:** whether to release web first at all, which turns largely on the Play Store
gate below. Register as an organization and that gate disappears, which weakens the argument
considerably.

### The store lead times, which belong on the launch list

**Google Play requires new *personal* developer accounts created on or after 13 November 2023
to run a closed test with at least 12 testers, opted in continuously for 14 days, before
applying for production access.** Opting out and back in restarts the count. **Organization
accounts registered with a legal business entity are exempt entirely.**

For a pre-launch product with no users, finding twelve real testers and keeping them opted in
for a fortnight is plausibly the longest single item before launch — and it is avoidable for
the price of registering as a business, which is happening anyway. **Register the Play account
as an organization from the start.**

Apple is the smaller item — a developer account at $99 a year, review, and TestFlight for beta
— but it is still a release cycle rather than a push. Both belong with the lead-time
preconditions rather than being discovered in launch week.

## Sequence

1. Reconcile the two trackers — `opportunities` as the spine.
2. Write `docs/voice.md` against these surfaces, before the front-end is built.
3. Register the Play developer account as an organization, and the Apple account, before
   either is on the critical path.
4. Build one codebase around the spine's first three moments, releasing web first and mobile
   as the store accounts clear.
5. Demote Telegram to notification plus one decision.
6. Rebuild the coach from new specifications, attached to the opportunity, generalising the
   readiness vocabulary and the hands-on checkpoint as part of that work.

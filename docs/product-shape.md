# Product shape

Last updated: 2026-09-09.

The founder's direction: one cross-platform app is the product, Relay is a proof of concept to
be rewritten and loses its name, and Telegram demotes to a notification channel. This document
says what that app is, what survives the rewrite, and what it costs.

Recommendations with reasoning, not options. **Founder's call** marks the few that need his
taste.

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

**Keep as the model, whether or not a line of code survives:**

- The **opportunity lifecycle and its event log**, including the RPCs — they encode real
  transitions, not CRUD.
- **`practice_plan_opportunities`** — the bridge, already correct.
- The **interview coaching domain**: competencies, blueprints, session and question
  evaluations, hands-on checkpoints, `profile_evidence`, `career_stories` and
  `career_story_evidence`, `coach_observations`. Grounding preparation in the user's own
  recorded evidence is the differentiator the founder named and it is already modelled.

**Do not keep:** the view structure and the front-end itself. `relay-shell.tsx` is a 62 KB
component with a 91 KB test file beside it; that is a monolith, and rebuilding around the
opportunity spine is cheaper than unpicking it.

**Do not keep the name.** See below.

**Rebuild the coach's product surface from new specifications**, as the founder has already
said. The tables are worth keeping as a model; the experience they were built for was a
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

**Recommendation: launch on one narrow surface, and make it the web app.**

Scope it to the spine's first three moments — a match arrives, you decide, you apply and track
— with the coach explicitly out of the first release. Reasons, in order of weight:

1. **Match quality is still unproven and everything rests on it.** Building three surfaces
   before a single user has validated the matches multiplies the cost of being wrong. The
   narrow surface is what produces the evidence.
2. **The engine is nearly done; the app is not started.** The shortest path to a paying user is
   to finish one and build the minimum of the other.
3. **The coach needs new specifications from the ground up**, which the founder has already
   decided. Putting it in the first release means specifying it under launch pressure, which is
   how the considered part of the repository would get rushed.
4. Cross-platform after there is evidence people want the daily thing. A web app that people
   return to daily is the proof that an installed app is worth building.

**Founder's call:** whether the first release is web-only or waits for cross-platform. The
trade is speed to evidence against completeness of the first impression, and it is a taste
judgement about what he is willing to show people.

## Sequence

1. Reconcile the two trackers — `opportunities` as the spine.
2. Write `docs/voice.md` against these surfaces, before the front-end is built.
3. Build the web app around the spine's first three moments.
4. Demote Telegram to notification plus one decision.
5. Rebuild the coach from new specifications, attached to the opportunity.

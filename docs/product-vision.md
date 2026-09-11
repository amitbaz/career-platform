# Product vision

Last updated: 2026-09-11. This is the record of the owner's product direction after the Caddie
and Prep Room teardowns. The challenges below were discussed with the owner and closed one at a
time on 2026-09-10 and 2026-09-11. The Decisions section is what is settled; anything parked or
provisional is marked where it appears.

When this document and an older one disagree, this one wins.

## The idea, as the owner stated it

Finding a job and getting ready for interviews is exhausting. The product makes the whole
process, end to end, a lot easier. It is not another job hunter and not another generic
interview coach; the value is that one product carries the user from finding the role to
being ready for the interview.

1. **The engine is the product.** Ingestion, enrichment and matching must be strong, smart and
   efficient before any interface is built. A well-shaped engine is what gives freedom to build
   any interface, or several, later.
2. **The main interface is a mobile app.** Not the web, not a bot. There is no Telegram bot in
   the plan at this point.
3. **Matching borrows from dating apps, without copying them.** Matches are not a daily batch
   arriving in the evening. They are a stack of cards (exact UI to be shaped later) where the
   user makes instant decisions: apply, don't apply, don't show me posts like this, stop
   suggesting these. A card shows only what the user needs to decide instantly.
4. **The match is one-sided.** Unlike dating, the employer does not have to like the user
   back. The engine proposes a job because it believes the job fits; the user accepts or not.
5. **Accepted jobs go into a bucket.** Roughly capped per day (about 20 was the first
   figure). The user does not have to fill it. In the bucket the user reviews each job in
   detail and decides whether to apply.
6. **Applying comes with every tool** needed to make the application quick and easy.
7. **The coach is the second main area of the app.** An AI coach that knows the user like a
   personal coach and keeps improving them for interviews — continuously, including before any
   interview invitation has arrived. How it connects to the matching side in the interface is
   open; both share the same engine and database.
8. **An application tracker.** Mechanism open. Gmail access is a Google restricted scope
   (verification plus an annual CASA assessment past 100 users), so it may start manual.

## Challenges under discussion

Raised 2026-09-10 from a product perspective, not a technical one. Discussed and closed in
order.

| # | Challenge | Status |
| --- | --- | --- |
| 1 | A swipe rewards shallow judgement, and match quality is the only edge. How many cards a day, and what is on a card? | closed — D1 |
| 2 | The bucket asks for the same decision twice, and 20 tailored applications a day is not humanly possible. Should a right swipe mean "prepare it for me"? | closed — D2 |
| 3 | Four swipe meanings break the quick decision; the stack must visibly respond to "never like this" | closed — D4 |
| 4 | A coach that trains in parallel is Prep Room's game; the link between the two areas may be the product | closed — D5 |
| 5 | Applying is the highest-intent moment and happens on the worst device | closed — D8 (provisional) |
| 6 | A manual tracker defers the one thing no competitor can copy — the outcome loop | closed — D9 |
| 7 | Who the product is for is unstated; the cards depend on it | closed — D6, D7 (draft) |
| 8 | Raised while closing 2: a user who applies to many jobs and hears back from few will conclude the product does not work | closed — D3 |

## Decisions

### D1. The stack: a fresh daily selection with configurable preparation capacity (2026-09-11)

**Decided.** The engine starts with capacity to prepare twenty applications a day, but the user
is not punished for rejecting cards.

- **Twenty is configurable capacity, not a card-view limit or a quota.** The value belongs to
  engine configuration and is not tied to pricing yet. A left swipe may be replaced from a ready
  reserve while preparation capacity remains. The experience can pause after a configurable
  number of reviews so it does not become an endless job board, but the user may continue.
- **The stack is never padded.** On a day with seven real matches the stack holds seven and says
  so ("only 7 worth your time today"). Padding with mediocre cards destroys the feeling the
  product is built on — *these are actually a match for me* — and it does so within days.
- **Skipped days do not pile up.** A user returning after three days sees the best twenty
  available now, not a backlog. Postings that closed in the meantime are gone.
- **A new day feels fresh.** The first open selects from a background ready pool. Opportunities
  not selected today can compete later; excellent jobs discovered after today's selection
  normally arrive tomorrow instead of appearing as a blocked list.
- **The card leads with why.** One line that immediately tells the user why this job is worth
  considering. Then the facts known about the posting: role, company, location and work mode,
  seniority, and salary when stated. An unknown is shown as unknown ("salary not stated"),
  because that is information the user values too.

**Why:** the owner's picture of the product is twenty real matches, decided in thirty seconds,
in place of hours of searching and reading. The relief is the product. It survives only if
every card deserves its place, so match quality — not volume — sets the count.

**What it asks of the engine:** a bounded per-user ready pool, selected into a daily stack on first
open; first card within one second and the complete initial stack within two; configurable
preparation capacity and review pause; reserve replenishment; a quality bar that can leave the
stack short; a *why* line generated per user and posting; card facts from shared enrichment;
closed postings removed immediately; every short or empty result carrying its reason.

### D2. A right swipe means "prepare it for me"; the product never submits (2026-09-11)

**Decided.**

- **A right swipe starts the application.** Drafting begins the moment the user swipes: the
  CV tailored to this posting, a short cover note, and the employer's screening questions
  answered.
- **The bucket holds prepared applications awaiting review**, not a to-do list. Per
  application the user approves, edits or drops. Every field names its source; a gap is
  stated, never invented. This is a different decision from the swipe ("is this application
  right?", not "am I interested?"), so nothing is decided twice.
- **The product never submits.** The user always presses the final send, on the employer's own
  form. No automated submission to any job board or applicant tracking system.
- **Closed postings leave the bucket automatically.**
- **The claim is fifteen tailored applications in the time one used to take** — honestly one to
  three minutes of review each, against two to three hours by hand. Not "fifteen in five
  minutes", which only auto-submission or a generic one-click application could deliver.

**Why:** twenty seconds per application is achievable only by auto-submitting or by sending the
same generic application everywhere. Generic mass applications draw responses around 1–2%, so
the user would feel productive for two weeks and then conclude the product does not work.
Auto-submission carries risks the product cannot undo: most employer and applicant-tracking
terms forbid automated submission (and a block hits every user at once); a wrong automatic
answer about right to work, sponsorship or salary is a false statement in the user's name;
recruiters flag bot applications; and GDPR exposure grows. Neither competitor auto-submits:
Caddie says "No auto-submit, ever"; Prep Room says "Nothing sends unseen". LinkedIn Easy Apply
is not a counter-example — the employer opted into LinkedIn's own form and the user presses
send.

**Honest note:** Prep Room already has a card with a "Draft it" action feeding a review queue
with per-field provenance, so this flow alone is not our edge. The difference has to come from
the continuous stack, drafting that starts on the swipe with no extra step, and the outcome
loop (challenge 6).

**What it asks of the engine:** a drafting operation triggered per right swipe, metered per
user (packs are the expensive per-user unit); the posting's screening questions (known from
its applicant tracking system); per-field provenance on every drafted answer; bucket entries
retired when freshness closes the posting.

### D3. The product explains silence and acts on it (2026-09-11)

**Decided.** The failure to design against is not "few interviews" — it is silence with no
explanation. Thirty applications and two replies, unexplained, reads as "the product does not
work"; the same numbers, explained and acted on, read as progress. Four layers:

1. **Honest expectations before the first swipe.** Most applications get no reply, even good
   ones; that is the market. The product shows what normal looks like for the user's field and
   market — only from numbers we have measured, never invented ones.
2. **Progress shown as steps, not only interviews.** Applied, replied, interview, offer. A reply
   is a win before any interview exists. Once measured: "your Strong matches get replies three
   times as often as your Good ones" — the match-quality proof shown to the user who produced
   it.
3. **Silence and rejection visibly change the stack.** "Three rejections from senior fintech
   roles — shifting toward mid-size product companies." Outcomes become information that shapes
   tomorrow's cards, and the user can see that happen.
4. **Diagnosis, handed to the coach.** When replies stay low, the product says why it thinks so
   — a CV thin on what these postings keep asking for, too many stretch roles, applying days
   after a posting went up — and the coach works on exactly that.

**Why:** a product that makes applying fast invites volume, and volume meets a market where
most applications go unanswered. Without an explanation the user blames the product, which
turns the best feature into the proof it does not work. Explained silence is also the one
thing neither competitor can offer, because neither knows what happened after the user applied.

**Consequences:**

- **The outcome loop is load-bearing, not "later".** "Applied" comes free from our own flow
  (D2), but replies, interviews and rejections do not. All four layers need them, so challenge 6
  decides whether this decision can be delivered at all.
- **This is the link between the two halves of the app** (challenge 4): the coach's continuous
  training is driven by what the market is telling this user.

**What it asks of the engine:** outcome events attached to each application; response rate by
match band, per user and per field and market; ranking that learns from outcomes, not only from
swipes; a diagnosis that names a cause from the user's own data.

### D4. Two gestures, reasons one tap deeper, and learning the user can see (2026-09-11)

**Decided.**

- **Two gestures on a card.** Right: prepare it for me (D2). Left: not this one — a soft signal
  the engine learns a little from, without ruling anything out.
- **Reasons are optional and one tap deeper.** After a left swipe a short row of reasons may
  appear, drawn from the card's own facts: the company, the location, too junior, the salary,
  the industry. The user can tap one or keep swiping.
- **Only an explicit reason creates a rule.** A single left swipe never hides anything for good.
  An explicit rule applies to matching reserve cards immediately today as well as future ranking.
- **Every rule is visible when it is learned** ("fewer on-site roles in Tel Aviv"), and the next
  cards visibly change.
- **A "what I've learned about you" list** shows every rule and lets the user undo any of them.
- **No third "not now" gesture for now.** The bucket already is "later". Add one only if real use
  shows the need.

**Why:** an instant decision can carry two meanings, not four. "Don't show me posts like this"
is ambiguous without a reason — a user rejecting a company can be misread as rejecting a role
type — and a wrong inference is invisible, because the user never sees what the engine hides.
Visible, undoable rules are the guard against learning wrong silently.

**What it asks of the engine:** preference signals at two strengths (soft swipe, explicit
reason) kept apart from outcomes (D3) — a swipe is an opinion, an employer's answer is a fact;
rules derived only from explicit reasons, each one inspectable and reversible; ranking that
applies soft signals as weights, never as hard exclusions.

### D5. The coach never trains in the abstract (2026-09-11)

**Decided.** The coach is the second main area of the app, and every session is anchored to
something real in this user's search. Voice is first-class: the user speaks answers, not only
types them. Shape to be refined; the direction is settled.

**Four kinds of practice:**

1. **For the jobs the user chose.** After a right swipe, a short session rehearsing what the
   draft claims for that employer: "Three minutes? Let's rehearse the story behind the payments
   migration your draft mentions — they will ask."
2. **For the diagnosis (D3).** When the market signals a weakness, the coach works on it: "Replies
   are low on roles asking for stakeholder management — let's build that story."
3. **For a specific interview.** When an invitation lands: the company, the role, the date, and
   every claim the user made in that application.
4. **For the career narrative, independent of any one job.** Tell me about yourself; what brings
   you here; two problems you solved at your last company; the biggest thing you designed or
   owned; a conflict; a failure. Built from the user's CV and career evidence into a set of the
   user's own stories, rehearsed until they are strong, and reused across every interview.

**The two halves feed each other.** The stack decides what the coach trains; the coach makes the
stack's applications succeed. A story rehearsed well becomes stronger material for the next
drafted application; a claim made in an application focuses the next practice.

**Outcomes change the drafted CVs.** What the market answers — invited, not invited, rejected —
changes how the next CVs are drafted: which experience leads, which claims are emphasised.

**Two guards on that:**

- **Tailoring never invents.** Learning from outcomes may reorder, re-emphasise and re-word what
  the user actually did. It never adds a skill or a claim (the tailoring rules in the Caddie
  teardown, "What to take", item 4).
- **One user's outcomes are a small sample.** Thirty applications cannot prove which CV version
  works; chasing that noise makes CVs worse, not better. Per-user adjustments are presented as
  what seems to be working, never as proven, until the evidence supports it. Aggregated across
  users — later — the signal becomes real.

**The coach is profession-neutral.** "An architecture you designed" is the engineer's version of a
universal question — the biggest thing you designed or owned. A nurse's version is a difficult
case. The question types are universal; the content comes from the user's own career.

**Why:** a coach with nothing at stake is ignored within a week, and a generic question bank is
Prep Room's game, where volume wins and we lose. Anchoring every session to the user's own
search is the thing only a product holding both halves can do.

**Nothing is kept from Relay** (owner, 2026-09-11). Relay was an early idea the product has
outgrown; neither its code nor its data model is a base for the coach. The coach is designed
fresh from this document. This overrides `product-shape.md`'s recommendation to keep Relay's
domain model.

**What it asks of the engine and the domain:** a story set per user, built from CV and career
evidence; drafted claims linked to the stories behind them; outcome events feeding CV drafting;
a profession-neutral question vocabulary.

### D6. Who it is for: CV-driven office work, designed first for experienced people in active search (2026-09-11)

**Decided.**

- **The market is CV-driven office and knowledge-work roles, every profession within that** —
  marketing, finance, HR, sales, operations, product, design, legal, customer success,
  engineering, and more — across the four launch markets. Not shift work, retail or trades,
  where hiring is a short form or a walk-in and tailoring and story coaching add little.
- **Designed first for experienced, mid-career people in active search.** Their pain is highest,
  D1 to D3 were built around them, their hiring is CV-driven, and they can pay.
- **The same person moves between two modes** — *active search* and *keep watching* (D7) — so
  the product serves both without being two products.

**The segments behind this, and what each needs:**

| | Out of work, active | Employed, quietly looking |
| --- | --- | --- |
| Urgency | very high | low: "only if something great comes up" |
| Time | plenty | very little |
| Wants | many good matches, fast applying | a few excellent matches, no noise |
| Silence (D3) | hurts most | barely noticed |
| Privacy | not a concern | critical: the employer must not know |
| Stays | until hired, typically two to six months | for years |

- **Graduates and early career:** thin CVs, few stories, high volume, little money; the coach is
  worth most to them and matching has least to work with.
- **Senior and executive:** many roles are never posted, so the corpus covers less of their
  market; they would pay the most and we can help them least.
- **Career changers:** where our matching is strongest — "right in substance, wrong on paper"
  (Caddie teardown, "What to take", item 1) — and where title matching fails them everywhere
  else.

**Why:** every piece of the flow — a tailored CV (D2), screening questions, a market that goes
silent (D3), story-driven interviews (D5) — assumes CV-driven hiring. Depth in that market beats
breadth across all work; Prep Room's board mixing retail roles with HR internships is breadth
without curation.

**Consequences:**

- **The engine must stop filtering to software-engineering titles.** Today `prefilter.py`
  (`is_software_engineering_title`) drops every non-engineering posting before matching sees it.
  "Office work" means that filter goes entirely.
- **A non-engineer must test the engine early.** The only real user today is a software engineer,
  so an engine tuned on his feedback will look right for engineers and quietly fail a marketing
  manager. #79 (a Hebrew non-tech persona) is the existing ticket for this.
- **A rough CV is evidence, not an entrance exam.** Missing CV evidence is unknown, not negative.
  The first useful stack needs only a minimum factual profile confirmed by the user. An explicit
  career goal overrides previous job titles; the CV still determines confidence and makes stretch
  visible. General polishing happens later, and tailoring happens per chosen job.

### D7. Two modes, two tiers, and nothing lost between them (2026-09-11)

**Decided: the shape.** Prices, allowances and the two questions at the end are deliberately
parked (owner, 2026-09-11).

**Mode is what the user is doing; tier is what they pay.** The two are separate.

| | Free | Paid |
| --- | --- | --- |
| **Keep watching** | The engine watches the market and notifies only on an excellent match — rare, a few a week at most. Discreet. The user's profile, stories and learned rules stay warm. | Optional, to be decided: companies the user names to watch closely, instant alerts. |
| **Active search** | A genuinely useful taste: a smaller daily stack with the *why*, a small number of prepared applications, a sample of the coach. Enough to prove the matches are good. | The full product: the stack up to twenty (D1), preparation on every right swipe (D2), the full coach with voice and interview preparation (D5), the diagnosis and outcome insights (D3). |

**The principle underneath:** watching is shared work — one crawl and one extraction serve every
user — so it is nearly free to give away. Scoring depth, drafting applications and coaching are
per-user work with a real cost each time. So free gives the watching, and paid does the work.
The cost to watch is the number of *active* free users, not the total free base; a user keeping
watch costs almost nothing.

**Moving between states:**

- **Active to hired: "I found a job".** The most valuable moment in the product. Active search
  pauses, paid ends, and the user drops to *keep watching* for free. Nothing is deleted. The
  product asks which job it was — the outcome that proves (or disproves) match quality, the
  moment for a testimonial, and the moment a friend who is searching now hears about us.
- **Active user going quiet without saying so.** The product notices, asks "still searching?",
  and moves to *keep watching* rather than billing someone who stopped. Charging a user who
  stopped using the product is the fastest way to lose them for good.
- **Keep watching to active: "I'm looking again".** One tap. The profile, stories, learned rules
  and outcome history come back; the first stack arrives the same day. The product asks for an
  updated CV, because years may have passed.
- **Paid lapses.** The user drops to the free version of whichever mode they are in. Nothing is
  lost.

**Rule across all of it: the user's data is never lost between states.** The free tier is the
memory of a customer — where people live between searches, and where they come back from.

**Parked for the owner** (not now, 2026-09-11):

1. Whether *keep watching* has a paid version at all — companies the user names to watch
   closely, instant alerts — or is free only, so the only thing anyone pays for is active search.
2. What the free *active* taste includes exactly, and what tips a user into paying. The working
   guess is prepared applications, because that is where the work is.
3. Allowances and prices.

### D8. Submitting is decided per posting: phone when the form is short, laptop when it is long (2026-09-11) — provisional

**Decided as the direction; provisional until the owner has used it.** The owner wants to feel
the flow in real use before the details are fixed.

- **The phone is the main interface** for deciding, reviewing and practising. A small desktop
  helper exists only for the one step phones are bad at.
- **The product says, before the user commits, how hard the apply step is.** The engine knows
  each posting's applicant tracking system, so a card or bucket item can say "two-minute apply,
  fine on your phone" or "long form, best on a laptop".
- **Short forms** (Greenhouse, Lever, Ashby: one page, a CV upload, a few questions) get
  assisted filling on the phone: the employer's form opens in the app, fields are filled from the
  reviewed application, the user checks them and presses send.
- **Long forms** (Workday, iCIMS, Taleo: an account, the CV re-typed into fields, several pages)
  go to a **submit session** on a laptop: one sitting, all the prepared applications, a small
  desktop helper (a web page or a browser extension) filling each form from its application. The
  user presses send every time.
- **D2 holds throughout:** assisted filling is never submission. Every field is visible and the
  user sends.

**Why:** by D2 the user always sends on the employer's own form. Long application-system forms
are miserable on a phone, which is exactly where a user who swiped, reviewed and meant to apply
gives up — the funnel breaks at its highest-intent point. Deciding per posting keeps the phone
experience for everything it is good at and is only possible because the engine already knows
each posting's application system.

**To validate:** the owner uses it for real — the phone flow on short forms, a submit session on
long ones — before the details (which systems count as short, extension or web page) are fixed.

**What it asks of the engine:** each posting's applicant tracking system and an effort class for
its form; the prepared application stored as structured answers mapped to that form's own
questions, not only as a document.

### D9. Outcomes arrive in four layers, and the inbox is asked for at the moment of value (2026-09-11)

**Decided.** The tracker is not manual-first. D3 depends on knowing what happened after the
user applied, so outcomes are captured in layers, each catching what the one above misses:

1. **Applied — free.** It happens in the product's own flow (D2, D8).
2. **Inbox connection, Gmail first.** Automatic and accurate. The engine already classifies mail
   as applied, recruiter contact, interview, technical, offer and rejected. Google's restricted
   scope works without verification for the first 100 users (who see an "unverified app"
   screen); verification and the annual CASA assessment stay parked until the engine is strong
   enough to approach that limit (owner, 2026-09-10).
3. **A personal forwarding address.** Works with any provider, Outlook included, with no Google
   verification. On a phone, forwarding one recruiter email from the share menu takes two taps.
4. **One-tap check-ins** — "heard back from Monzo? nothing / reply / interview / rejected" — for
   users who connect nothing, and to confirm silence when it matters.

**Silence is inferred from time.** No reply after a few weeks is recorded as no response — the
most common outcome of all — and confirmed by a check-in where it matters.

**The inbox is asked for at the moment of value, not at sign-up.** Reading someone's email is
the largest trust request the product makes. Asked at onboarding, many refuse because they
cannot yet see why; asked after the first applications — "want me to track replies to these
twelve automatically?" — the value is obvious. **The product shows exactly what it read:** only
job-related mail, visible in the app.

**Why:** a manual tracker is abandoned within weeks, and without outcomes the product is blind
to the market's answer — exactly as blind as both competitors. The outcome loop is the one thing
neither of them can copy without rebuilding.

**What it asks of the engine:** outcome events from four sources (own flow, inbox, forwarded
mail, check-ins) joined to the application they belong to; silence inferred on a clock; a record
of every message read, shown to the user.

### D10. Engine readiness is earned in a private, blind seven-day review (2026-09-11)

**Decided.** Engine quality is measured in Engine Lab before a product surface depends on it.
For seven consecutive days, the owner reviews ten cards a day: seven intended recommendations
and one blind sample each from hard-excluded, unresolved and just-below-threshold postings.

The engine is ready only when at least 80% of the 49 intended recommendations are worth applying
to; there are no hard eligibility mistakes; no why line invents or misstates a fact; at least 90%
of why lines are specific and helpful; no clearly good match appears in the 21 blind-audit cards;
the latency targets in D1 hold; and every short or empty result explains itself. Each threshold is
reported separately. One week is an initial gate, not statistical proof of a near-zero miss rate,
so blind audit continues afterward.

**The measurement is built, not remembered.** The private Review page records impressions and
judgements; the private Analytics page shows the funnel, reasons, versions, freshness, latency and
ingestion health. Ingestion records every worker invocation, including an empty queue, and learns
source/time-of-week yield without a global night or weekend blackout. Quiet windows retain safety
crawls so changed behavior can be detected.

**What it asks of the engine:** a versioned measurement ledger; blind samples from all decision
states; owner-only aggregate analytics; durable worker-run telemetry; and a mechanism that makes
stale or missing measurement visibly fail. The full shaping record is
`docs/superpowers/specs/2026-09-11-engine-ready-stack-design.md`.

## What the engine must do

Collected from the "What it asks of the engine" line of each decision. This is the bridge from
product to engine work; engine tickets are checked against it.

**Ingestion and enrichment (shared, per posting):**

- Every CV-driven office posting, in every profession, across the four markets — no
  engineering-only title filter anywhere (D6).
- Card facts from shared enrichment: role, company, location and work mode, seniority, salary
  when stated, unknowns kept as unknown (D1).
- Recover incomplete postings in the background; insufficient evidence remains `unresolved`,
  never becomes a permanent `unmatchable` decision, and is reconsidered until closure (D10).
- Each posting's applicant tracking system, its screening questions, and an effort class for its
  form (D2, D8).
- Closed postings detected and removed from every stack and bucket (D1, D2).
- Every ingestion worker invocation recorded, including an empty queue; source yield visible by
  UTC hour and weekday, with safety crawls preserving evidence in quieter windows (D10).

**Matching (per user):**

- Every open posting considered for each user's match decision; no crawl-time per-user membership
  row and no engineering-title filter determines what matching is allowed to see (D6, D10).
- Known hard violations are `ineligible`, sufficient evidence is `qualified`, and insufficient
  evidence is `unresolved`; unknown facts are not negative evidence (D6, D10).
- A bounded ready pool per user, selected into a fresh daily stack with configurable preparation
  capacity, reserve replenishment and explicit shortfall reasons (D1).
- A grounded *why* line per user and posting, with the main stretch disclosed immediately (D1,
  D10).
- Preference signals at two strengths — a soft left swipe, an explicit reason — applied as
  weights, with rules only from explicit reasons, each inspectable, reversible and applied to
  today's reserve immediately (D4).
- Outcomes kept apart from preferences, and ranking that learns from both (D3, D4).
- A private, versioned review and analytics interface that enforces the engine-ready thresholds
  and continues blind recall auditing after the initial seven-day gate (D10).

**Per-user work (metered):**

- Drafting triggered by a right swipe: tailored CV, cover note, screening answers, every field
  naming its source, never inventing (D2, D5).
- Prepared applications stored as structured answers mapped to the form's own questions (D8).
- Drafting that learns from outcomes by reordering and re-emphasising, never adding (D5).

**Outcomes and learning:**

- Outcome events from four sources joined to their application; silence inferred on a clock
  (D9).
- Response rate by match band, per user and per field and market; a diagnosis naming a cause
  from the user's own data (D3).

**Coach (shares the engine and database):**

- A story set per user from CV and career evidence; drafted claims linked to the stories behind
  them; a profession-neutral question vocabulary (D5).

## Launch lead times

Carried over from the deleted `product-shape.md`; still true.

- **Register the Google Play developer account as an organization.** New personal accounts
  created on or after 13 November 2023 must run a closed test with at least 12 testers, opted in
  continuously for 14 days, before applying for production access. Organization accounts
  registered with a legal business entity are exempt. For a pre-launch product with no users
  this is plausibly the longest single item before launch, and registering as a business avoids
  it.
- **Apple:** a developer account at $99 a year, review, and TestFlight for beta — a release cycle
  rather than a push.
- Both belong with the launch preconditions (#201), not in launch week.

## Documents corrected on 2026-09-11

- **Deleted:** `docs/product-shape.md`, superseded by this document. Its launch lead times are
  above.
- **Corrected to match this document:** root `AGENTS.md` (product direction, the owner's
  standing rules, and "dated records are not current truth"), `CONTEXT.md` (the product's
  vocabulary), root `README.md`, `docs/positioning.md` (pricing marked as input to a parked
  decision; Telegram removed), `docs/research/*` (links, and the assumption that Relay's model
  survives), and the opening of `apps/job-hunter/README.md` and `apps/job-hunter/AGENTS.md`.
- **Marked legacy:** the Relay documents and `apps/job-hunter/docs/telegram-job-navigator.md`.
- **Left as they are:** `docs/adr/0001-the-engine-is-the-product.md` (still right),
  `docs/monorepo-migration.md` (a finished runbook), `docs/marketing-and-brand.md` (brand work
  these decisions do not touch).
- **Dated specs, plans and task reports** stay where they are; root `AGENTS.md` now says they
  are records of past decisions, not current truth.
- **Still to do:** the full rewrite of `apps/job-hunter/README.md` and `apps/job-hunter/AGENTS.md`,
  when #189 removes the daily run they mostly describe.

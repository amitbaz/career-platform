# Positioning

Last updated: 2026-09-11.

Written as recommendations with the reasoning behind them, not as open questions. Where a
decision genuinely needs the founder's taste it is marked **founder's call**; everything else
is a recommendation to act on.

Read [marketing-and-brand.md](marketing-and-brand.md) first for the name, the promise and the
visual direction. This document covers what we claim, how the product sounds, and what we
charge.

Read [research/beating-both.md](research/beating-both.md) alongside this. It sets out how we
beat both known competitors, and several of its conclusions revise this document. **The
competitor pricing below is stale.** As of 2026-09-10 caddie.careers charges £9.99 a
*month*, not £6.99 a week ([research/caddie-careers-teardown.md](research/caddie-careers-teardown.md)),
so the argument that €7.99 a week "matches rather than undercuts" them no longer holds.

**Read [product-vision.md](product-vision.md) first; where the two disagree, it wins.** It
changes this document in three places. The main interface is a mobile app, with no Telegram bot.
There are two customer modes — active search and keep watching — so the premise below that
every customer leaves when hired holds only for active search (D6, D7). And the shape of the
free and paid tiers is decided there, with prices and allowances parked (D7).

## The proof gap, which constrains everything below

Two columns. Anything in the right-hand column that reaches a landing page unlabelled is a
claim we cannot support, and claims are expensive to walk back once someone has paid.

| What we can prove today | What we believe and intend to prove |
| --- | --- |
| Objective facts about a posting are extracted once and shared, at a measured €0.00137 per posting. Cost scales with postings, not users. | Continuous ingestion delivers a match sooner than the competitor's nightly evening batch. Architecturally true; **no end-to-end number exists.** |
| Roughly 14,000 postings seen per run, 209 newly discovered. | Match quality is better than title-matching or than the competitor's five-dimension model. **Currently an assertion.** |
| Gross margin of about 91% at a hundred subscribers; break-even between one and two. | Interview preparation grounded in the user's own evidence is a differentiator. **Not built; concept only.** |
| A learned ATS registry, per-key quota ledgers, row-level security, and a corpus that persists between runs. | Unlimited watching — that the market can be crawled once for everybody. **Extraction is shared today; discovery is not.** #203 and #204 are what make it true. |

**The uncomfortable summary: the only thing we can claim today that the competitor cannot is
our cost structure, and nobody buys a cost structure.** Every customer-facing differentiator
is currently in the right-hand column.

**What settles each one.** The latency claim is settled by #189, which should carry
time-from-posting-appears to pack-delivered as an acceptance condition rather than discovering
it afterwards. The match-quality claim is settled by #80 and nothing else; until that runs,
match quality is a hypothesis and the repository's own rule — *measure quality claims, do not
assert them* — applies to marketing exactly as it applies to code.

**Recommendation: do not launch on a speed claim or a quality claim.** Launch on the
experience, which is real the day it ships, and add each claim as its experiment lands. A
product that says "here is what I found for you today, and why" is honest on day one. A
product that says "faster and better matches" is writing a cheque #189 and #80 have not yet
signed.

## The voice is the differentiator we can ship

The name does not have to explain the product. Duolingo means duo plus lingo; Google, Spotify
and Uber mean nothing at all and read as inevitable now, because the product filled the word.
Gili starts ahead of that baseline — it already means "my joy" in the founder's language — so
the name is free to be a name, and the explaining is done by how the product speaks.

**The competitor sends a scored list. Gili sends a note.** "Here is what I found today, here
is why I thought this one was worth your morning, here is the one I nearly didn't send you."
That is not a feature they can add in a sprint; it is an identity, and it is the only
differentiator on this list that does not depend on an unfinished experiment.

It also gives the interview half a natural home later: the same someone who found the role
prepares you for it. No other framing connects those two products.

### What it costs, concretely

**Now: one document, roughly half a day.** `docs/voice.md`, one page: who Gili is in three
sentences, five rules, and about a dozen worked before-and-after examples drawn from real
strings. Plus one line in AGENTS.md saying user-facing text is written to it.

**Later: every user-facing string in three codebases.** The voice has to hold on every card
and *why* line, in push notifications, the app's copy, transactional email, and **every error
message** — which is the hard case, because error strings are written by whoever hit the
error, are scattered across the tree, and are precisely where a persona dies. Retrofitting
means finding all of them across `job-hunter` and `relay` and the future app, rewriting them,
and living with the ones that are missed.

**Recommendation: write `docs/voice.md` before the app's first screens are built**, since they
will otherwise establish a voice by accident.

## Pricing

> **Status (2026-09-11): input to a parked decision, not a recommendation.** The tier *shape* is
> decided in [product-vision.md](product-vision.md), D7: the mode (active search or keep
> watching) is separate from the tier (what the user pays); free gives the watching and paid does
> the work; nothing is lost moving between states. Prices and allowances are parked by the
> owner. The analysis below was written assuming every customer leaves when hired, which D6
> found true only for active search — people employed and quietly looking stay for years. Its
> free-tier table also predates the stack (D1): "once daily" delivery and "five scored matches a
> day" describe the retired digest.

### The customer does not stay, and that changes everything

**This product succeeds by making itself unnecessary.** Someone subscribes while they are
looking, finds a job, and leaves. That is structural, not a retention failure to be solved, and
every economic figure written before this section assumed a subscriber base that persists.
It does not.

**So the metric is lifetime value across one search, not monthly revenue.** How long a search
runs, from published 2026 figures: the median duration of unemployment is about 11 weeks and
the average about 24; most white-collar professionals take three to six months. **Tech job
seekers average 9.7 months — the longest of any industry by a wide margin**, which matters
because that is the audience the current corpus actually serves. Take four months as the
planning figure and treat tech as materially longer.

**This reverses the billing-rhythm recommendation below.** That recommendation argued for
monthly because "the promise is a standing relationship". The promise is not a standing
relationship, and the earlier reasoning was built on a premise that does not hold.

| | LTV over a 4-month search | Maximum sustainable acquisition cost |
| --- | --- | --- |
| Monthly at €14.99 | **€60** | €20 |
| Weekly at €7.99 | **€139** | €46 |

At a six-month search — nearer the tech average — weekly reaches €208 of lifetime value and
about €69 of permitted acquisition cost.

**The decisive argument is not that weekly feels urgent — it is that a monthly price cannot
fund customer acquisition at all.** With no long tail of loyal subscribers, the business is
acquisition, forever, against a lifetime value capped by the nature of the product. A €20
ceiling rules out paid channels before anyone has designed a campaign; €46 makes them marginal
but possible. That bound should be known before any distribution idea is proposed.

**Weekly is also the more honest offer.** When someone lands a job they cancel, and they lose
at most a week rather than a month. A rhythm that stops when the need stops is a better deal
for the customer *and* worth more to us, which is rare enough to say out loud rather than treat
as a trick.

### What theirs signals

£6.99 a week (about €8.15), metered on *scans* and *packs*, billed weekly. Free tier is 3 scans
and 3 packs a week; Member is 12 scans and 15 packs; Ultra is £25 a week (about €29.15).

**Their weekly price annualises to roughly £363, about €423 — a figure they never show
anywhere.** That is a positioning observation rather than a pricing one: weekly billing hides
the annual number, which is most of why it works.

Weekly billing is a deliberate signal: this is a sprint, not a subscription, cancel when you
land a job. That is well judged for a temporary need and lowers the commitment to start, and
hiding the annual figure noted above is most of what makes it work.

### The weakness in their model, and the half of it we have actually fixed

**They meter scans because scanning costs them per user.** A free-tier job seeker gets three
scans a week and has to decide when to spend one. That is the opposite of relief — it makes
the user ration the thing that is supposed to remove their anxiety.

**Half of that constraint is removed in our architecture today, and half is not.** The
distinction matters enough to state precisely, because an earlier version of this document got
it wrong:

- **Extraction is shared, and that is built.** A posting's objective facts are read once and
  reused by every user, at a measured €0.00137 per posting. AI cost is bounded by postings
  rather than by users. This was #175's achievement and it holds.
- **Discovery is still per-user, and that is not built.** All three discovery tables —
  `job_hunter_ats_registry`, `job_hunter_company_watch` and `job_hunter_gmail_sync_state` —
  carry `user_id`, and the pipeline builds its source list from that per-user state under
  row-level security. **N users means N crawls of substantially the same market.** What N users no longer means is N copies of each advertisement: since #178 a
  job row is one user's membership of a shared posting, so the Nth user costs a narrow row
  rather than the description, the identity columns and the fetch metadata again. The crawl
  is what still duplicates.

**The consequence for pricing is direct.** A licensed feed's quota is a *platform* quota —
Adzuna's is 2,500 calls a month on the key — so if each user's crawl spends from it, the
ceiling divides by user count in exactly the way theirs does. On today's code we have the
weakness we would be criticising.

**So "Gili watches everything, always" is a promise about the architecture we are building,
not the one we have.** The strategic insight stands and the competitor still cannot easily
copy it: their metering is a symptom of per-user scanning, and ours does not have to be. But
the tense is future, and **#203 (share learned ATS boards across users) and #204 (share
automatic company watches against the company entity) are what make it present.** Until they
land it belongs in the believe column, and it should not appear on a landing page.

### Recommendation

**Weekly billing at €7.99. Unlimited watching and scoring. Application packs metered.**

- Weekly rather than monthly, for the reasons in "The customer does not stay" above: the
  rhythm follows from the customer leaving when the product works, and it roughly doubles
  lifetime value over the same search.
- Unlimited watching as the headline, because it is true once discovery is shared, it is
  differentiated, and it is the direct product of the architecture.
- Packs metered, because they are the genuinely per-user expensive action and a cap there is
  honest rather than artificial.

€7.99 is a natural consumer price point and lands within pennies of theirs — £6.99 a week is
about €8.15 — so it **matches rather than undercuts**. Fixed cost is about €43 a month and
break-even is one to two subscribers, so there is room almost immediately, but with 91% gross
margin price is not where the contest is: undercutting a funded competitor competes on the one
axis where we have no advantage to defend, and signals inferiority. **Founder's call:** the
exact number, and whether to sit slightly under, at, or above theirs.

### The free tier

**A permanent free tier is required** — someone must be able to stay on it indefinitely. That
is a harder design than a trial, because it has to be worth using forever without cannibalising
the paid tier, and because a free tier built to frustrate is worse than none: it teaches people
the product does not work.

**The constraint is requests per day, not money.** A free user's AI costs about €2.70 a month
at the top of the range and usually far less. Irrelevant. What a free user consumes is
**quota** — the same 500-requests-per-day Gemini allowance — and free users are always the
majority, so a badly-shaped free tier does not cost money, it eats the ceiling and paying
customers hit a wall.

#### The shape

**Free gives the watching. Paid does the work.** That is not a marketing line; it is the cost
structure stated as a product. Watching is shared across all users and near-free once discovery
is shared; scoring depth and application packs are genuinely per-user and genuinely expensive.

| | Our free | Our paid | Their free |
| --- | --- | --- | --- |
| Watching the market | unlimited, shared sources | unlimited, plus custom watched companies | 3 scans a week |
| Scored matches | **5 a day**, with reasons | **50 a day** | only what a scan returns |
| Application packs | **1 a month** | **20 a month** | 3 a week |
| Delivery | once daily | on arrival | on scan |

**Their free tier is three scans and three packs a week.** Set beside five scored matches a
day, the contrast makes the argument without needing to be claimed: theirs is a taste that runs
out, ours is a product someone can live on.

Five scored matches a day with reasons is a genuinely useful product. It is a curated
shortlist, it arrives every morning, and a patient job seeker could run their whole search on
it. That is the test the tier has to pass.

#### What the free tier is actually for

**It is where people live between job searches, not primarily a conversion funnel.** Because
the customer leaves when they find a job, the free tier is the only thing that keeps the
relationship alive afterwards: someone who found work through us stays on free, dormant, with a
warm profile, and returns in two or three years when they are looking again. That same person
is also the one who tells a friend who is searching *now* — which matters more than usual for a
product whose entire business is acquisition.

**So the free tier is the memory of a customer and the referral surface.** Two consequences:
there must be an explicit "I found a job" action that pauses scoring and keeps the profile, and
that moment is the most valuable event in the product — the referral moment, the testimonial
moment, and the outcome data that would settle the match-quality question.

#### What makes someone pay

Not "more of the same". **The upgrade buys the moment after you find something.**

You are on the free tier, five matches arrive, one of them is a role you actually want. Now you
have to write a tailored CV and covering letter for it tonight. Free found it; paid does that
work with you, and does it nineteen more times this month. The second reason is depth: fifty
scored matches surfaces the roles that do not obviously match your title, which is the whole
argument against title matching and the thing a five-item shortlist cannot show you.

#### The arithmetic

Per scored posting: **€0.00167**. Extraction, paid once per posting for everybody: €0.00137.

**On free Gemini, with one scoring key at 500 requests a day, the tier is impossible:**

| Paying users | Free users | Requests/day |
| --- | --- | --- |
| 0 | 100 | 500 |
| 2 | 80 | 500 |
| 5 | 50 | 500 |
| 10 | 0 | 500 |

Ten paying customers and no free users exhausts the key. **So the free tier makes a paid Gemini
tier a hard prerequisite, not merely a consequence of retiring bring-your-own-key.**

**On paid Gemini the request ceiling stops binding and money takes over, and money is
comfortable:** 100 free users cost **€25 a month**, 1,000 cost **€251**. A paying user's own AI
is **€2.51 a month** against about €34.72 of monthly-equivalent revenue.

**The real limit is the ratio of *active* free users to paying ones**, and "active" is
load-bearing. A dormant alumnus is not being scored, so they consume no requests and cost
essentially nothing. Only free users actively searching count against this ratio, which is a
far smaller number than the total free base.

At weekly billing — €7.99, about €34.72 a month equivalent:

| Active free per paying user | AI cost per paying user | Gross margin |
| --- | --- | --- |
| 10 | €5.02 | 86% |
| 20 | €7.53 | 78% |
| 50 | €15.06 | 57% |
| 100 | €27.60 | 21% |

**The weekly rhythm fixes the free tier's economics as well as acquisition.** At the monthly
price these went negative at fifty free users per payer; at weekly, fifty *active* free users
still returns 56%. The daily score count remains the dial if conversion disappoints, but there
is far more room than the earlier arithmetic suggested — and that arithmetic was wrong in its
denominator, counting dormant users as though they were being scored.

#### Launch shape differs from intended shape, and the difference is #203 and #204

Unlimited watching is only cheap once the crawl is shared. Until #203 and #204 land, each user
triggers their own crawl, and a licensed feed's quota is a **platform** quota: Adzuna's 2,500
calls a month is **83 calls a day across all users combined**. A free tier offering per-user
crawling would exhaust that at a handful of users.

**So at launch the free tier watches the shared default source set — one crawl serving
everybody — and custom watched companies are paid.** That is not a growth tactic; it is an
honest statement of what is actually shared today. When #203 and #204 land, custom watches
become cheap and can move down to free.

#### Bring-your-own-key survives, but not as the free tier

A user who brings their own Gemini key brings their own 500 requests a day and costs nothing
but hosting — genuinely free, forever, and invisible to our ceiling. That makes it tempting as
*the* free tier, and it should not be, for the reason already decided: asking a non-technical
user to obtain an API key is a barrier most of the intended audience will not clear, and it
splits the product into two experiences.

**Recommendation: keep it as an option on the free tier, not as the tier.** "Bring your own key
and get paid-tier scoring depth at no charge." It self-selects for exactly the users who can do
it, removes them from the quota entirely, gives technical early adopters a real reason to stay,
and the code already exists. It is a relief valve, not a product.

> **Currency note.** Our own prices, costs and margins are stated in euros. The competitor's
> prices are quoted in sterling as they publish them, with a euro equivalent in brackets.
> Converted at £1 = €1.1662 and $1 = €0.8594, the mid-market rates on 9 September 2026. These
> figures move with the rate; re-derive rather than re-quote them after any material change.

#### The tier we should not build

**Do not launch a free tier that meters scans — but argue it from where we are going, not
from where we are.** The reasoning is that scan-metering is a permanent tax in their
architecture and a temporary one in ours: once #203 and #204 land, crawling is shared and a
scan cap would be an artificial limit rather than a real cost. Building the pricing around a
cap we intend to remove would mean re-teaching customers later.

**The honest caveat, which is a launch-sequencing constraint rather than a pricing one:** on
today's code discovery is per-user, so an unlimited-watching promise made before #203 and #204
land would be sold against a cost we are still paying per user — and against a licensed
feed's platform quota, that is the binding limit before the AI quota is. **Unlimited watching
should not be advertised until discovery is shared.** That makes #203 and #204 pricing
prerequisites, not just efficiency work.

## What this means for the roadmap

Nothing here asks for a ticket that does not exist. It changes emphasis:

- **#183 through #186** are the differentiation work, not plumbing — but they earn the speed
  claim only once #189 measures it.
- **#80** moves from "nice to have" to the experiment that unlocks the second half of the
  positioning.
- **#203 and #204** are pricing prerequisites, not efficiency work: they are what turns
  unlimited watching from an intention into a claim, and they gate advertising it.
- **The app's first screens** should not be built before `docs/voice.md` exists.

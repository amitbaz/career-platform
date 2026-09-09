# Positioning

Last updated: 2026-09-09.

Written as recommendations with the reasoning behind them, not as open questions. Where a
decision genuinely needs the founder's taste it is marked **founder's call**; everything else
is a recommendation to act on.

Read [marketing-and-brand.md](marketing-and-brand.md) first for the name, the promise and the
visual direction. This document covers what we claim, how the product sounds, and what we
charge.

## The proof gap, which constrains everything below

Two columns. Anything in the right-hand column that reaches a landing page unlabelled is a
claim we cannot support, and claims are expensive to walk back once someone has paid.

| What we can prove today | What we believe and intend to prove |
| --- | --- |
| Objective facts about a posting are extracted once and shared, at a measured $0.00159 per posting. Cost scales with postings, not users. | Continuous ingestion delivers a match sooner than the competitor's nightly evening batch. Architecturally true; **no end-to-end number exists.** |
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

**Later: every user-facing string in three codebases.** The voice has to hold in the digest,
the Telegram bot's replies, the web app's copy, transactional email, and **every error
message** — which is the hard case, because error strings are written by whoever hit the
error, are scattered across the tree, and are precisely where a persona dies. Retrofitting
means finding all of them across `job-hunter` and `relay` and the future app, rewriting them,
and living with the ones that are missed.

**Recommendation: write `docs/voice.md` before the web posting surface is built**, since that
surface is on the launch-precondition list and will otherwise establish a voice by accident.

## Pricing

### What theirs signals

£6.99 a week, metered on *scans* and *packs*, billed weekly. Free tier is 3 scans and 3 packs
a week; Member is 12 scans and 15 packs; Ultra is £25 a week.

Weekly billing is a deliberate signal: this is a sprint, not a subscription, cancel when you
land a job. That is well judged for a temporary need and lowers the commitment to start. It
also annualises to about £363, which is a large number nobody is shown.

### The weakness in their model, and the half of it we have actually fixed

**They meter scans because scanning costs them per user.** A free-tier job seeker gets three
scans a week and has to decide when to spend one. That is the opposite of relief — it makes
the user ration the thing that is supposed to remove their anxiety.

**Half of that constraint is removed in our architecture today, and half is not.** The
distinction matters enough to state precisely, because an earlier version of this document got
it wrong:

- **Extraction is shared, and that is built.** A posting's objective facts are read once and
  reused by every user, at a measured $0.00159 per posting. AI cost is bounded by postings
  rather than by users. This was #175's achievement and it holds.
- **Discovery is still per-user, and that is not built.** All four discovery tables —
  `job_hunter_ats_registry`, `job_hunter_company_watch`, `job_hunter_search_api_usage` and
  `job_hunter_gmail_sync_state` — carry `user_id`, and the pipeline builds its source list from
  that per-user state under row-level security. **N users means N crawls of substantially the
  same market**, and `job_hunter_jobs` still produces N job rows per posting.

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

**One monthly price. Unlimited watching and scoring. Application packs metered.**

- Monthly rather than weekly, because the promise is "I am watching so you don't have to",
  which is a standing relationship, not a sprint. It also reads as a normal subscription
  rather than a countdown.
- Unlimited watching as the headline, because it is true, it is differentiated, and it is the
  direct product of the architecture.
- Packs metered, because they are the genuinely per-user expensive action and a cap there is
  honest rather than artificial.

**Starting price: £12–15 a month, founder's call on the exact number.** The reasoning: fixed
cost is about $50 a month and break-even is one to two subscribers, so the floor is
fixed-cost-over-subscribers rather than marginal cost, and there is room almost immediately.
It sits at roughly half their annualised rate while reading as an ordinary subscription rather
than as a discount — **we should not compete on being cheaper**, because with 91% gross margin
price is not where the contest is, and undercutting a funded competitor signals inferiority.
The number needs validating against willingness to pay; the structure does not.

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
- **The web posting surface**, already on the launch-precondition list, should not be built
  before `docs/voice.md` exists.

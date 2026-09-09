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
| A learned ATS registry, per-key quota ledgers, row-level security, and a corpus that persists between runs. | |

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

### The weakness in their model, and why we do not share it

**They meter scans because scanning costs them per user.** A free-tier job seeker gets three
scans a week and has to decide when to spend one. That is the opposite of relief — it makes
the user ration the thing that is supposed to remove their anxiety.

**Our architecture removes that constraint.** Objective extraction happens once per posting
and is shared, so watching the market is close to free for us however many users are watching.
The per-user cost is only the subjective scoring and the application packs.

**So we can offer what they structurally cannot: unlimited watching.** "Gili watches
everything, always" is a promise their unit economics do not permit without rebuilding, and it
falls directly out of the #118 and #174–#179 work. This is the strongest strategic finding in
this document: their metering is a symptom of per-user scanning, and ours does not have to be.

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

**Do not launch a free tier that meters scans.** Copying their free tier would import the
weakness we do not have. A free tier should cap *packs* and *scoring depth* and leave watching
untouched.

## What this means for the roadmap

Nothing here asks for a ticket that does not exist. It changes emphasis:

- **#183 through #186** are the differentiation work, not plumbing — but they earn the speed
  claim only once #189 measures it.
- **#80** moves from "nice to have" to the experiment that unlocks the second half of the
  positioning.
- **The web posting surface**, already on the launch-precondition list, should not be built
  before `docs/voice.md` exists.

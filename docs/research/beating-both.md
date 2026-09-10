# Beating Prep Room and Caddie

Last updated: 2026-09-10.

This draws conclusions from [preproom-teardown.md](preproom-teardown.md) and
[caddie-careers-teardown.md](caddie-careers-teardown.md). Read those for the evidence. This
document is the argument. Recommendations are stated with their reasoning. **Founder's call**
marks what needs the owner.

## The answer in one paragraph

Both competitors stop before the job seeker's outcome is known. Caddie's product ends at "sent,
by you". Prep Room drafts applications and prepares interviews, but has no way to know whether
an employer answered or an interview landed — the user has to come back and tell it. **We
read the inbox.** The engine already classifies mail as `APPLIED`, `RECRUITER_CONTACT`,
`INTERVIEW`, `TECHNICAL`, `OFFER` and `REJECTED`
(`apps/job-hunter/src/job_hunter/gmail_classifier.py:89`). The coach's opportunity log already
has `interview_scheduled` (`supabase/migrations/202608300002_opportunities.sql:59`). The two are
not connected. **Closing the loop from the employer's answer back into matching,
applications and preparation is the one thing neither competitor can do without rebuilding
— and #80 is the ticket that connects them.**

## Eight insights

### 1. Learn from outcomes, not proxies

Caddie learns from skips, saves and edits. Prep Room shows no learning from behaviour at all.
Both are learning from what the user *does*. Neither learns from what *employers* do.

- **For each user:** rank tomorrow's matches toward roles like the ones that answered, and
  away from the shape of the ones that rejected. A skip is an opinion; a rejection is a fact.
- **Across users, later and aggregated:** which posting facets respond to which kinds of
  profile. It is the only network effect available in this category. It gets stronger with
  every user and cannot be copied by adding content.
- **As a product feature:** "your response rate on Strong matches, against Good ones". That
  is #80's measurement shown to the user, and it is the proof insight 5 needs.

**The cost, which is on no list today:** `gmail.readonly` is a Google *restricted* scope.
Past 100 users it requires restricted-scope verification and an annual security assessment
(CASA) by a Google-approved assessor. That is weeks of lead time and real money every year
([Google: restricted scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification)).
**Parked by the founder on 2026-09-10:** not yet. Revisit it once the engine is strong enough
that inbox-driven outcomes are about to reach users beyond the 100-user test cap, and add it to
the launch prerequisites (#201) then, not before. A forwarding address and an Outlook connector
are the fallbacks.

### 2. The interview invitation is the most valuable moment, and both miss it

When an `INTERVIEW` email lands, we are the only product that holds all of these at once:

- the posting;
- the application we helped write, including the claims the user made to this employer;
- the user's evidence and career stories;
- the date.

So the coach can open with: "They will ask about the three claims in your cover letter. Here
is the story behind each, and here is where it is thin." Prep Room needs the user to paste
the job description again and knows nothing of what they sent. Caddie has no coach.

**This is wiring, not invention.** The pieces are the inbox classifier, `opportunities` with
its `interview_scheduled` event, and `practice_plan_opportunities`, the bridge that
[product-shape.md](../product-shape.md) found already modelled. **Recommendation: make the
interview-invitation trigger the coach rebuild's first feature**, ahead of a question bank.

### 3. Evidence is the shared currency between applying and preparing

Both competitors can trace a line to "from your resume" and no further. Our evidence model
(`profile_evidence`, `career_stories`, `coach_observations`) lets one item of evidence back a
CV bullet, a screening answer, and the story the coach rehearses.

Then it compounds in both directions:

- a story told well in practice becomes stronger material for the next cover letter;
- a claim made in an application focuses the next practice round.

Neither half can do this on its own. It is the concrete meaning of "the same someone who
found the role prepares you for it" ([positioning.md](../positioning.md)).

### 4. Speed only counts if the application is ready when the alert lands

Caddie's own statistics argue for our architecture: recruiters shortlist from the first
applicants in, and "fast-but-generic gets filtered" ([home](https://caddie.careers/)). Yet
they deliver a morning batch. Prep Room shows freshness ("checked 3 minutes ago") but pushes
nothing.

A continuous engine that alerts in five minutes but leaves the user an evening of writing is
not fast. **For Strong matches, draft the application before notifying.** It is metered —
packs are the expensive per-user unit, as both competitors' caps confirm. The measure is
time from posting seen to application ready. That is the condition positioning.md wants #189
to carry, extended from delivery to *ready*.

### 5. Honesty rules are table stakes; published proof is not

Both competitors already promise no auto-submit, nothing invented, provenance and auditable
changes. Meet every one of them (the merged list is in the Caddie teardown, "What to take",
item 4) and do not market them. They are the entry fee.

**What nobody does is publish measured outcomes.**

- Caddie cites industry statistics about other people.
- Prep Room says it refuses a "confidence score out of 100", then leads its resume check
  with one.

The first product to say "Strong matches got a response X% of the time, against 1–2% for the
market" owns the trust argument. Because of insight 1, we are the only one of the three that
can measure it. This is positioning.md's rule — measure quality claims, do not assert them —
turned from a constraint into the headline.

### 6. Do not compete on volume; build from what only we hold

Prep Room sells 22,000+ questions, 13,000 search pages and 300K+ roles; Caddie sells 10k
roles a day. Matching that is expensive and proves nothing.

What we hold that they do not is **structured facets for every posting, across four markets,
extracted once and shared.** Two uses:

- **Questions generated from the user's own claims plus this posting**, which a static bank
  cannot match for relevance.
- **Search pages from aggregated facets**: "what Monzo asks of senior product managers,
  across 14 postings this year". That is unique content and a search acquisition channel,
  which the €46 acquisition-cost ceiling in positioning.md demands. **Counsel first:** Adzuna
  and Reed attribution and licensing terms may restrict republishing anything derived from
  their feeds.

### 7. Local is the uncontested ground

Caddie covers the UK and Europe for four job families. Prep Room is US-shaped. **Neither shows
anything for Israel.**

Our engine already encodes local rules in `market_eligibility.py`: required language, salary
floors, sponsorship, and the Israel on-site constraint. That is the edge in the EU, UK and
Israel:

- multilingual postings;
- work authorisation;
- local salary norms;
- local boards.

In the US we meet Prep Room head-on, where the wedge has to be insights 1, 2 and 4 rather
than locality.

### 8. The bundle is the price anchor

As fetched on 2026-09-10:

- Caddie is £9.99 a month (€11.65) for applications.
- Prep Room is $20 a month (€17.19) for prep and a job board.
- **Both together are about €29 a month.**

Positioning.md's €7.99 a week is about €34.72 a month. That is above both combined, and its
"matches rather than undercuts" argument no longer holds.

**Founder's call:** price as the one product that does both halves and closes the loop —
somewhere around the combined anchor — or keep weekly billing and reprice. The free-tier
comparison still favours us: Caddie's free tier is an unscored list plus one fit a week,
against positioning.md's five scored matches a day.

## What not to do

- **Do not build a content library, contests or an editor extension.** That is Prep Room's
  game, and volume proves nothing.
- **Do not chase role counts.** Fewer, better matches, proven by #80, is the claim.
- **Do not ship warm introductions before counsel.** Of everything either competitor offers,
  that is the most exposed feature under GDPR.
- **Do not headline a raw 0–100 score.** Show bands with reasons, and keep the number for
  measurement.
- **Do not copy either set of no-refund terms** without counsel checking them against all
  four markets.

## Threat ranking

1. **Prep Room is the threat.** It is broad, polished, shipping quickly, and closest to our
   whole vision. It lacks delivery and the outcome loop. If it adds push notifications and an
   inbox connection, our lead narrows to the evidence model and locality. **So the loop has
   to ship first.**
2. **Caddie is a reference, not a threat** outside UK and EU tech. It is a solo, cheap,
   narrow product, and the best source of application-rigour ideas we have found.

## What this changes in the queue

| Item | Change |
| --- | --- |
| #80 (record applied jobs in the shared opportunities lifecycle) | **Becomes the strategic ticket.** It is the join between inbox outcomes and opportunities, and insights 1, 2 and 5 all rest on it |
| #189 | Its latency measure should run to *application ready*, not just delivered |
| #201 launch prerequisites | **Parked (founder, 2026-09-10).** Gmail restricted-scope verification and CASA go on #201 once the engine is strong enough, not now |
| Coach rebuild ([product-shape.md](../product-shape.md), step 6) | The first feature is the interview-invitation trigger |
| [positioning.md](../positioning.md), pricing | Reopen against the €29 bundle anchor (founder's call); correct the stale Caddie figures regardless |
| Matching | Adopt Caddie's disagreement signal and drop the title prefilter (Caddie teardown, "What to take", item 1) |

## Founder's calls

1. Pricing: the combined anchor, or reprice weekly.
2. Whether to accept the CASA cost and timeline for inbox access, or launch with forwarding.
   **Deferred (2026-09-10)** until the engine is strong enough; see insight 1.
3. Whether to open free accounts on both competitors to test their output quality with a real
   CV. This means accepting their terms.

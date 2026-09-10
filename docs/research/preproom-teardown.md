# Prep Room (preproom.ai) teardown

Last updated: 2026-09-10.

The owner found this competitor on 2026-09-10 and asked for a precise account of what it does,
how well, and which parts should be our starting point. Every claim below cites the page it
came from. The cited pages were fetched on 2026-09-10 without an account, so anything behind
sign-in is described only as far as their own public demos show it. Where a claim is an
inference rather than something they state, it is marked **inference**.

Read [positioning.md](../positioning.md) and [product-shape.md](../product-shape.md) first.
This document is written against both.

## The short version

**Prep Room is a better-built product than caddie.careers, and the closer competitor.** It
covers the whole funnel we planned — resume, find the role, apply, prepare, negotiate — in one
account for $20 a month. Its interview half is well past what we have shipped. Its job-search
half is a large, shallow board with no delivery.

**Adopt as our starting point:** their interaction design for match cards, the application
review queue, resume tailoring, and single-question practice. It is careful, honest and
specific, and most of it maps onto data we already produce. Also adopt their
profession-neutral round taxonomy as the seed for generalising our coach vocabulary.

**Do not adopt:** their job-search model — a pull board of 300K+ roles that you visit — or
their no-refund billing. Our continuous, curated push and the per-user evidence model behind
the coach are the two things they do not have.

## Who they are

- Operated by Bose Ventures LLC, "From California". Legal pages last updated September 6,
  2026 ([legal](https://preproom.ai/terms)).
- Founder and CEO is Shayani Bose, per LinkedIn
  ([profile](https://www.linkedin.com/in/shayanibose/)). A web search tried to link Bose
  Ventures LLC to Bose Corporation's venture arm. Nothing supports that, and the shared
  surname is probably the explanation. Treat funding as **unknown**.
- Stack, from their own list of service providers: Anthropic and OpenAI for generation,
  Supabase (US), Vercel, Stripe, Resend, and Cloudflare ([privacy](https://preproom.ai/privacy),
  section 4). That is our stack apart from the model vendor.
- Traction is **unknown**. No Reddit, Product Hunt or review-site presence turned up. The VS
  Code extension is at version 0.1.6, last updated 21 August 2026, with no ratings
  ([marketplace](https://marketplace.visualstudio.com/items?itemName=PrepRoom.prep-room-coding)).
  **Inference:** a recent launch, and heavy on content rather than users so far.

## What they do, surface by surface

Their navigation is Resources, Resume, JD, Your Jobs, Upskill, Contests, Salary negotiation,
Interview Simulator ([how it works](https://preproom.ai/how-it-works),
[upskill demo nav](https://preproom.ai/upskill)). Paths blocked in robots.txt show the
signed-in app: `/interview`, `/simulator`, `/practice`, `/predicted`, `/tailored`,
`/negotiate`, `/progress`, `/history`, `/loop`, `/import-resume`
([robots.txt](https://preproom.ai/robots.txt)).

### 1. Resources: the question bank

- They claim "22,000+ questions & answers" ([about](https://preproom.ai/about)). The sitemap
  lists 13,065 URLs: 7,846 `/answer/*` question pages, 5,138 `/code/*` coding pages, 59
  round-type hubs and 11 essays ([sitemap](https://preproom.ai/sitemap.xml)). Each question is
  its own indexable page. **That is a programmatic-SEO acquisition engine**, not just a
  library.
- **The round types are profession-neutral.** The hubs include clinical reasoning, patient
  communication, classroom management, teaching demo, curriculum design, med-legal, standards
  and codes, emergency procedure, revenue management, vendor negotiation, objection handling,
  CRM cockpit, case interview, Fermi estimation, product sense, metric diagnosis, SQL, system
  design, ML system design, on-call incident, and "AI-ready" ([sitemap](https://preproom.ai/sitemap.xml)
  under `/resources/*`). This is exactly the generalisation
  [product-shape.md](../product-shape.md) says our readiness vocabulary lacks.
- **Every question page carries a spec.** An example is
  [air-001](https://preproom.ai/answer/air-001):
  - the answer shape ("Explanation"), a target length (160 words), usual seniority ("Entry to
    director") and think time ("30 seconds, if you want it");
  - linked concept guides;
  - a four-beat structure (Definition, Mechanism, Where it breaks, In practice);
  - a sealed model answer, follow-ups after you finish, and a coach note sealed until you stop.

  Their reason for sealing is stated: "Nothing is marked until you stop, because underlining
  a word while you are still speaking changes the next one."
- Concept guides are short tutorials linked from questions. Levels are Entry, Mid and Senior,
  split into "Interview concepts" and "Code Room concepts"
  ([concepts](https://preproom.ai/resources/concepts),
  [STAR and CARL](https://preproom.ai/learn/star-carl-story-structure)).

### 2. Practice: voice, code, system design

- Answers are spoken with a live transcript, and critique arrives after the answer. They also
  claim "a throughline tracked across sessions"
  ([essay, gaps 6 and 8](https://preproom.ai/insights/why-the-leetcode-model-is-breaking)).
- A critique names what worked, then gives one concrete thing to sharpen and says it out loud:
  "Name the rule you would write down. Say it out loud next time."
  ([likely questions demo](https://preproom.ai/likely-questions)).
- Coding rooms run real code against hidden tests in Python, JavaScript, TypeScript, C++,
  Java and Rust. The same bank lives in a VS Code extension graded on their servers
  ([vscode](https://preproom.ai/vscode)). SQL questions exist too
  ([cod-d001](https://preproom.ai/code/cod-d001)).
- System design happens "on a real whiteboard" ([pricing](https://preproom.ai/pricing)).
- A weekly Saturday contest: five problems, open for 24 hours Pacific
  ([contests](https://preproom.ai/contests)).
- Their anti-gamification stance is explicit. Levels replace streaks: "Nothing here punishes
  you for missing a Tuesday". Test results replace "a confidence score out of 100"
  ([about](https://preproom.ai/about)).

### 3. JD: likely questions from a posting

- You paste a posting or a Greenhouse, Lever or Ashby link. It returns questions ranked by
  likelihood, "the reason each one made the set, in the posting's own words", and marks the
  ones written from this posting, as against the generic set
  ([likely questions](https://preproom.ai/likely-questions)).
- **The first read needs no account.** Keeping the set, the model answers and voice practice
  need a free account (same page). This is their top-of-funnel hook.
- Questions are also "matched against the postings we hold, so you can see how many of them
  one is worth before you spend an hour on it" ([about](https://preproom.ai/about)).

### 4. Resume check and tailoring

This is a seven-step pipeline against one optional posting
([resume check](https://preproom.ai/resume-check)):

1. An ATS score out of 100, with missing keywords.
2. Sections reordered, each labelled moved up, kept or cut.
3. Bullets rewritten. "Nothing invented. The same facts, in the order this posting reads
   for." "Every number came off your own resume. Where one was missing, it asks." "It cannot
   add a skill you do not have."
4. A resume file with a template.
5. A cover letter.
6. Practice questions generated from the resume.
7. A plan.

A signed-out upload is read and not kept. A signed-in resume is deleted 30 days after last
use ([privacy](https://preproom.ai/privacy), section 3).

### 5. Your Jobs: the board and drafted applications

- They claim "300K+ open roles, ranked against your resume". The live sample mixes a
  London SDR role, a US retail sales associate and a French HR internship
  ([jobs](https://preproom.ai/jobs)). **Inference:** breadth across every profession and
  country, not curation.
- Matches stay hidden until you upload a resume. After that a card shows:
  - title, company, location and pay;
  - freshness ("checked 3 minutes ago", "Listed 92 days");
  - one grounded sentence: "Payments and idempotency run through four of your last five
    years. This posting asks for both by name.";
  - a gap sentence: "Kubernetes is central here and yours is thinner than they ask for.";
  - a band (Strong or Good), and actions (Draft it, Open).

  A header shows "41 of 312 roles you clear" (same page).
- **The review queue is the best thing on the site.** Per application it lists every field
  with its provenance: "from your resume", "you answered, Jul 12". Status reads "1 flag to
  read", "ready, no flags" or "needs one answer". A gap is stated, not papered over: "The
  posting asks for 8. Answered from your resume, not rounded up." Its promise is "Nothing
  sends unseen." (same page). The paid plan drafts 100 applications a month
  ([pricing](https://preproom.ai/pricing)).
- A Chrome extension reads the posting on the page you have open, on demand, and "never sends
  a request to a job board on your behalf" ([privacy](https://preproom.ai/privacy),
  section 2). No store listing turned up.
- **Their job sources are not disclosed anywhere public.**

### 6. Upskill

The resume goes in, then the postings you match, then your gaps "ranked by how many of those
postings name each one", each with a learning path. It "rereads itself" when the resume
changes ([upskill](https://preproom.ai/upskill)).

### 7. Salary negotiation

"From offer letter to rehearsal" ([pricing](https://preproom.ai/pricing)). It is behind
sign-in (`/negotiate` is blocked in [robots.txt](https://preproom.ai/robots.txt)), so there are
no details.

## Business model

- **Free:** "It does not expire". It includes every question readable, concept guides, some
  spoken rounds a month, coding rooms, critique, a resume check against a posting, and a
  starter allowance of AI applications. **Paid:** $20 a month, or $15 a month billed $180 a
  year ([pricing](https://preproom.ai/pricing)). At $1 = €0.8594 (the rate
  [positioning.md](../positioning.md) uses) that is €17.19 a month, or €154.69 up front for a
  year.
- The terms still mention "Standard and Pro" tiers and "top-up packs", which the pricing page
  does not show. So pricing is in flux, or the terms are stale.
- **All sales are final.** There are no refunds, pro-rating or credit for unused features
  ([terms](https://preproom.ai/terms), purchaser section 3).
- **Their answer to structural churn is annual prepay; ours is weekly billing.** Someone
  whose four-month search ends pays $180 up front and gets nothing back. Our positioning puts
  weekly billing at €139 of value over a four-month search. Their annual plan collects about
  €155 before the search ends, and their monthly plan collects €69. That makes annual prepay
  a stronger revenue answer than ours and a worse deal for the customer.
  **Founder's call**, and **counsel should check** a no-refund policy against consumer
  cancellation rights in each of our launch markets (EU, US, UK, Israel) before anything like it is considered.
- Acquisition runs on free tools that need no account (likely questions, resume check),
  13,000 SEO pages, essays, the contest, and the VS Code extension. Their privacy policy
  mentions Google Ads conversion tracking ([privacy](https://preproom.ai/privacy), section 4).

## Legal posture worth borrowing

- **ADMT disclosure.** They state that processing is fully automated and that scores are
  "Advisory Feedback". They argue GDPR Article 22 does not apply because scores are never
  shared with employers ([privacy](https://preproom.ai/privacy), section 1). Whether that
  argument holds is **for counsel**. The disclosure pattern itself is worth copying.
- **Resume retention is tied to use.** Signed-out uploads are not kept. Signed-in resumes are
  deleted 30 days after last use, extended by use. Pasted job descriptions are kept until
  deleted. There is one-click account deletion, and GPC is honoured
  ([privacy](https://preproom.ai/privacy), sections 2 and 3).
- Opt-in cookie consent applies in the EU, EEA and UK; opt-out elsewhere (same page).

## Where they are weak, or say one thing and do another

- **There is no delivery.** Nothing on the site describes a push, a digest or a notification.
  The board is somewhere you go. [positioning.md](../positioning.md) builds the product on
  "here is what I found for you today, and why". They do not have that.
- **The board is breadth, not quality.** "300K+ roles" with retail and internship listings
  beside staff engineering roles is a volume claim. Nothing public shows how ranking works
  or how good it is. Our #80 experiment is still the only route to a
  quality claim for either product.
- **They contradict their own stance on scores.** The about page rejects "a confidence score
  out of 100", yet the resume check leads with "ATS score 72 of 100"
  ([about](https://preproom.ai/about), [resume check](https://preproom.ai/resume-check)).
- **The coach is keyed to the resume and the posting, not to accumulated evidence.**
  "Questions selected for your resume and target role" ([pricing](https://preproom.ai/pricing))
  and "a throughline across sessions" are the only statements about memory. There is no sign
  of career stories, competency evidence or coach observations as persistent objects.
  **Inference, unverified behind sign-in.**
- It is US-first: USD pricing, "From California", and US salary bands in the demos. The US is
  one of our launch markets too, so there we meet them head-on. No EU, UK or Israeli focus is
  stated.
- Content volume is not quality. 7,846 generated question pages will vary, and nothing
  public shows how they are reviewed.

## What we already have that maps onto theirs

| Their feature | Our equivalent today | Gap |
| --- | --- | --- |
| Match card with a grounded reason and a gap note | Evaluation returns `strengths`, `gaps`, `rationale` and per-requirement `candidate_support` (`apps/job-hunter/src/job_hunter/evaluation.py:81`) | Data exists; there is no surface that renders it their way |
| Freshness ("checked 3 minutes ago") | Continuous per-source crawl and re-check (#184, #186) | Not surfaced |
| Drafted application with per-field provenance | Cover-letter text and PDF only (`pdf.py:35`) | No field-level answers, no provenance, no review queue |
| Resume tailoring against a posting | None | Whole feature |
| Likely questions from a posting | The coach plans rounds from an opportunity's job description and gaps (`apps/relay/src/lib/coach.ts:989`, `lib/interview-planner.ts:471`) | Internal only; no "why this made the set" |
| Voice practice | Relay has a transcribe route on Gemini (`apps/relay/src/app/api/transcribe/route.ts`) | The POC is being rewritten ([product-shape.md](../product-shape.md)) |
| Profession-neutral round types | Readiness vocabulary is engineering-shaped ([product-shape.md](../product-shape.md), structural items 2 and 3) | Needs generalising; their taxonomy is a ready-made seed |
| Upskill from matched postings | Facets are extracted once per posting and shared (#175, #178) | Not built, but cheaper for us than for them |
| Career stories, evidence, observations, practice plans | `career_stories`, `profile_evidence`, `coach_observations`, `practice_plans`, `practice_plan_opportunities` (migrations 202608290005–202608310001) | **Our advantage.** Nothing equivalent is visible on their side |

## What to take as our starting point

In priority order. Each item names the ticket or document it feeds, rather than proposing new
scope.

1. **Match card format.** One grounded sentence, one gap sentence, a band rather than a raw
   number, and freshness. We already compute all of it. Build the posting surface
   ([product-shape.md](../product-shape.md) moments 1 and 2) to this spec, in our voice.
2. **Application review queue with per-field provenance.** Every answer names where it came
   from: CV, profile, or an answer the user gave on a date. It flags gaps instead of rounding
   up, and nothing leaves unseen. This is the spec for application packs (moment 3), and it
   matches our rule against automatic submission.
3. **Resume tailoring rules, word for word:** nothing invented; reorder for this posting;
   every number comes from the source; ask when one is missing; never add a skill. Adopt
   these as the acceptance criteria for any CV-tailoring work. They are also the
   hallucination guardrails we would want anyway.
4. **The single-question practice spec**, for the coach rebuild (product-shape sequence step
   6): answer shape with named beats, target length, think time, and a model answer and
   coach note sealed until you stop. Critique is one strength plus one concrete thing to
   say next time.
5. **Their round taxonomy as the seed vocabulary** when generalising `competencies`, the
   readiness dimensions and the question-category enum. It already covers healthcare,
   teaching, sales, operations and marketing.
6. **Upskill ranked by how many matched postings name a gap.** We already hold shared facets
   for every posting, so this is one query over data we pay for once, where they pay per
   read. It fits after the matching work (#235).
7. **Free, account-free first reads as acquisition.** The positioning doc caps sustainable
   acquisition cost at €46. Free tools that rank in search are how a product like this
   acquires users at that price. "Likely questions from this posting, no account" is cheap
   for us to offer.
8. **The legal patterns:** an ADMT disclosure, resume retention tied to use, and deleting
   signed-out uploads. Hand these to counsel as a template, not as a conclusion.

## What stays ours, and should be sharpened because of them

- **Delivery.** A curated note each morning with reasons, where they offer a board you visit.
  Their existence makes [docs/voice.md](../positioning.md#the-voice-is-the-differentiator-we-can-ship)
  more urgent, not less.
- **The coach remembers.** Stories, evidence and observations build up across the search, and
  preparation for a specific opportunity draws on them. They prepare you from your resume;
  we prepare you from everything the coach has learned. That is the product-shape spine's
  moment 4, and it is the claim to prove.
- **Quality over volume.** They sell 300K roles. We should never compete on that number, only
  on #80's evidence that fewer, better matches get more responses.
- **Four launch markets: EU, US, UK and Israel.** Their product is US-shaped. In the US we
  compete with them directly. In the EU, UK and Israel the edge is local: sources, languages,
  salary norms and work-authorisation rules they show no sign of handling.

## Open questions and how to settle them

- **How good is their critique and matching, behind sign-in?** Only a free account would
  answer that. Signing up means accepting their terms, so it is the **owner's decision**.
  If yes, test with the owner's real CV and one real posting, and record the output against
  ours.
- **Where do their 300K roles come from?** Not disclosed. It only matters as a signal of their costs
  and legal exposure, not as something we need to solve.
- **What are the free allowances in numbers?** Not published.
- **Are they funded?** Unknown.

## Sources

- <https://preproom.ai/>
- <https://preproom.ai/pricing>
- <https://preproom.ai/about>
- <https://preproom.ai/how-it-works>
- <https://preproom.ai/jobs>
- <https://preproom.ai/likely-questions>
- <https://preproom.ai/resume-check>
- <https://preproom.ai/upskill>
- <https://preproom.ai/contests>
- <https://preproom.ai/vscode>
- <https://preproom.ai/resources/concepts>
- <https://preproom.ai/resources/behavioral>
- <https://preproom.ai/answer/air-001>
- <https://preproom.ai/code/cod-d001>
- <https://preproom.ai/learn/star-carl-story-structure>
- <https://preproom.ai/insights/why-the-leetcode-model-is-breaking>
- <https://preproom.ai/terms> (the same page serves the privacy and purchaser terms)
- <https://preproom.ai/sitemap.xml>
- <https://preproom.ai/robots.txt>
- <https://marketplace.visualstudio.com/items?itemName=PrepRoom.prep-room-coding>
- <https://www.linkedin.com/in/shayanibose/>

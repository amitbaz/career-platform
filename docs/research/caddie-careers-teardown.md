# caddie.careers teardown

Last updated: 2026-09-10.

This is the companion to [preproom-teardown.md](preproom-teardown.md), written the same way
and for the same purpose: what caddie.careers does, how well, and which parts should be our
starting point. Every claim cites the page it came from. The cited pages were fetched on
2026-09-10 without an account. The public site has three pages (home, how-it-works and the
manifesto, per the [sitemap](https://caddie.careers/sitemap.xml)) plus privacy and terms.
Everything else is behind sign-in at `app.caddie.careers`. **Inference** marks what they do
not state outright.

## The short version

**Caddie is a narrow, deep application engine built by one person.** It does one job — find
roles for experienced product, engineering, design and marketing people in the UK and
Europe, and write the application — and it does the application half more rigorously than
anyone we have looked at. It has no interview preparation, a daily batch cadence, and one
founder whose company was incorporated in November 2025.

**Adopt as our starting point:**

- their scoring cascade, including "the two reads disagree? That is signal";
- pulling each employer's real screening questions at draft time;
- ATS-specific formatting;
- auditable tailoring, where every change shows the original and the reason, and missing
  facts become placeholders;
- the loop that learns from skips, saves and edits;
- the warm-intro step, subject to counsel.

**Do not adopt:** the audience limits (tech and marketing only, UK and Europe only), the
batch cadence, email-only deletion and indefinite retention, or the pricing — which has
changed since we last looked, and which invalidates part of
[positioning.md](../positioning.md). See "Pricing, and what changed".

## Who they are

- Operated by Viewery Ltd, company 16837989, registered in England and Wales
  ([privacy](https://caddie.careers/privacy), section 1). Incorporated 6 November 2025. One
  officer, director Dmitrii Meltcov, London. The registered business activities are software
  development, IT services and data processing
  ([Companies House](https://find-and-update.company-information.service.gov.uk/company/16837989)).
- The founder is Dmitry Meltsov, a product leader. His note says he spent nine months on a
  B2B voice AI company that ran out of funding, then built Caddie for his own job search
  ([manifesto](https://caddie.careers/manifesto)). He has said publicly that he is winding
  down Viewery's original product, "a voice-native intelligence layer for estate agency"
  ([RocketReach summary](https://rocketreach.co/dmitry-meltsov-email_55799888), secondary
  source). **Inference:** bootstrapped and solo, with Caddie running on the company left
  over from the previous venture.
- Their public page markup leaks the build. The pricing block carries a developer comment
  saying the numbers are "pinned to `engine.quota.plan_matrix()` by
  `tests/test_plan_matrix.py`" and that "pytest is the gate every lane runs before merging"
  ([home](https://caddie.careers/), page source). **Inference:** a Python engine built with
  parallel AI-agent lanes — the same way this repository is built.
- Model access goes through OpenRouter "and the model providers it routes to". The hosting
  provider is not named ([privacy](https://caddie.careers/privacy), section 6).

## What they do, hole by hole

Their how-it-works page is structured as eight golf "holes" plus a learning step between
rounds ([how it works](https://caddie.careers/how-it-works)).

### 1. Scout the course: sourcing

- They claim "~67 sources and the career pages you watch, swept daily. ~10k live roles",
  with a daily sweep at 06:00. Named boards include We Work Remotely, Landing.jobs,
  GermanTechJobs and Mind the Product, "…46 more boards + your watched career pages"
  ([how it works](https://caddie.careers/how-it-works)).
- Adzuna appears as the source badge on every sample match ([home](https://caddie.careers/)).
  Our earlier finding still holds: their "licensed Reed feed" is a free jobseeker key
  (memory: competitor sourcing).
- "Country-specific boards for the UK and the EU markets are already included"
  ([home](https://caddie.careers/)).

### 2. The score cascade

This is their strongest idea ([how it works](https://caddie.careers/how-it-works)):

1. **A location gate** — a hard filter on accepted countries, cities and work modes, applied
   before scoring.
2. **A machine read** — "the filters' view": keyword coverage against the posting, shown as
   "kw 9/12".
3. **A recruiter read** — an AI score against a rubric with weights the user sets at
   onboarding (their example is skills 30, domain 30, remote 30, stage 10, plus flags such as
   "0→1 remit", "b2b saas", "founder-friendly").

"The two reads disagree? That is signal. Wrong in substance gets skipped; wrong on paper gets
a pack that closes the vocabulary gap." In other words, a role you fit but whose keywords you
miss is exactly the role where tailoring pays.

The home page frames the recruiter read as five dimensions — industry, function, business
model, company stage and positioning (senior IC, lead, founder, contractor) — weighted by
the user. It marks which dimensions carried a match: "The two rows marked ◀ are what
actually got this PM the interview — neither is in the job title"
([home](https://caddie.careers/)).

Scores run 0–100 with bands (Strong, Good) and reasons: "an 81 and a 92 are different roles"
([home](https://caddie.careers/)).

### 3. Pull the real brief

At draft time the full job description is "fetched live from the source". The ATS is named,
and "the employer's real screening questions" are pulled — their example includes "Do you
have the right to work in the UK?" ([how it works](https://caddie.careers/how-it-works)).

**Inference:** Greenhouse, Ashby and Lever expose application questions through their public
job-board APIs, which is how this is done without scraping forms.

### 4. Find the angle

Company research (careers page, press, founder blog) is "distilled to the one angle that
leads your whole pack" — for example, "took a payments product 0→1 in a regulated market"
([how it works](https://caddie.careers/how-it-works)).

### 5. Beat the parser

The ATS that the application enters "sets the format rules before a word is written"
([how it works](https://caddie.careers/how-it-works)). Their examples:

- a single-column layout for Ashby, with tables dropped;
- MM/YYYY dates for Workday;
- the job description's exact phrasing kept ("payments product", not a synonym);
- spelling variants covered ("re-platforming" and "replatforming").

The home page lists per-ATS rules for Greenhouse, Ashby, Lever, Workday, iCIMS and Taleo,
with a parse score each, claiming that "six systems cover ~90% of tech roles across the UK &
Europe" ([home](https://caddie.careers/)).

### 6. Build the pack

The pack is a CV, a cover letter and screening answers, all led by the angle. "Every change
shows its original and its reason", and the reason quotes the posting: "why: the JD asks for
'experience launching payment products under regulation'. Quoted at hole 3."
([how it works](https://caddie.careers/how-it-works)).

### 7. You take the swing

You review line by line — keep, edit or revert to the original ("change 3 of 7") — and send
it yourself "from your own account, on the employer's own form". "No auto-submit, ever."
([how it works](https://caddie.careers/how-it-works)). The terms say the same: "It is a
drafting and research aid, not an application service"
([terms](https://caddie.careers/terms), section 2).

### 8. Warm the follow-up

"The right people at the company, found. A short, grounded intro, drafted." The sample is a
58-word note to a named Head of Product, with a Talent Partner as the alternative
([how it works](https://caddie.careers/how-it-works)). How they find those people is not
stated. Their privacy policy covers the user's data and says nothing about third parties'
personal data ([privacy](https://caddie.careers/privacy)). **Counsel should look at this
before we build anything like it.**

### Between rounds: Caddie learns

"Every skip, save and edit re-ranks tomorrow's round, and teaches the drafts your voice."
Their examples: "you rewrote the summary line → future CVs start from your wording";
"'synergy', struck twice → never drafted again"; "because yesterday you skipped 3 web3
roles, saved 2 payments roles, and edited 1 pack"
([how it works](https://caddie.careers/how-it-works)). The founder says of his own use: "By
now it writes like me." ([manifesto](https://caddie.careers/manifesto)).

### House rules

These four rules are printed on both main pages ([how it works](https://caddie.careers/how-it-works)):

1. **No auto-submit.**
2. **Never invents a fact.** "Where something's missing it leaves a visible placeholder, not
   a fabrication."
3. **Auditable by design.** "Every tailored change shows its original text and the reason;
   the job ad is quoted verbatim."
4. **You take every swing.**

### Delivery

"Scored overnight and waiting by morning": the sources are swept each evening, new roles are
scored 0–100 overnight, and the top of the list is ready in the morning. Members also get a
morning email of their best fits ([home](https://caddie.careers/)).

## Pricing, and what changed

As fetched on 2026-09-10 ([home](https://caddie.careers/), pricing section):

| | Free | Member | Ultra |
| --- | --- | --- | --- |
| Price | £0, forever, no card | **£9.99 a month**, invite-only beta, price locked on request | **£24.99 a month**, "Ask us" |
| Roles | every role from ~67 sources, newest first | whole list scored, live daily scans | scored live twice a day |
| Metered | "your fit of the week" by email, Mondays | 15 live scans and 30 packs a month, 100 custom sources, "the strongest letter model on every pack" | 60 scans and 100 packs a month |

Every new account starts with 7 days of Member (7 packs and 4 live scans) with no card. The
first scan covers the last 7 days. Their terms add that fees already charged "are
non-refundable except where required by law" ([terms](https://caddie.careers/terms),
section 7).

**This is not what we recorded.** On 2026-09-09 we recorded £6.99 a week for Member (12 scans
and 15 packs a week) and £25 a week for Ultra. [positioning.md](../positioning.md) ("What
theirs signals") built its argument on that weekly rhythm. The developer comment in their
markup mentions "the way the July weekly plan did" drift, so weekly billing existed and has
been replaced by monthly. **When it changed is unknown.** It may be recent, or our note may
have come from a stale page.

What it means, converted at £1 = €1.1662 (the rate positioning.md uses):

- Member is now **€11.65 a month**, against the €7.99 **a week** (about €34.72 a month)
  positioning.md recommends. That recommendation argued it "matches rather than undercuts"
  them. **It is now about three times their price.**
- The competitor that sold weekly billing as sprint-shaped has moved to monthly. That is a
  data point against the weekly thesis, though the churn argument in positioning.md does not
  depend on them.
- Across both competitors the monthly anchors are now £9.99 (Caddie) and $20 (Prep Room,
  about €17.19).

**Founder's call:** whether positioning.md's pricing section needs reopening. It should at
least have its competitor figures corrected.

## Legal posture

- UK GDPR, with performance of contract (Article 6(1)(b)) as the legal basis
  ([privacy](https://caddie.careers/privacy), section 4). There is no automated-decision
  disclosure, unlike Prep Room.
- "We don't run any advertising or analytics trackers on the Service." The only cookie is a
  session cookie (same page, section 2). That is a cleaner posture than Prep Room's.
- They state they do not train on user data (same page, section 3).
- Retention lasts "as long as your account is active", and deletion is by emailing the
  founder (same page, section 7). That is weaker than Prep Room's 30-day resume window and
  in-app deletion.
- The acceptable-use terms forbid users from fetching postings "in a way that ignores a
  board's own access rules" ([terms](https://caddie.careers/terms), section 6). That puts
  the scraping burden on the user for pasted links.
- The warm-intro feature processes third parties' personal data, and nothing in their
  privacy policy covers it. That is an exposure on their side and a warning for ours.

## Where they are weak

- **The audience is narrow by design:** "experienced product, engineering, design and
  marketing people", in the UK or Europe ([home](https://caddie.careers/)). There is no US or
  Israel coverage and nothing for other professions. Of our four launch markets they compete
  in two.
- **The cadence is a batch.** Evening sweep, overnight scoring, morning list; twice a day on
  Ultra. Our continuous engine (#184, #186) beats this on the axis they market on: "the
  window closes fast".
- **There is no interview preparation at all.** The funnel ends at "sent, by you" and the
  warm intro.
- **The sources are thin.** About 67 sources and 10k live roles a day, leaning on Adzuna and
  a free Reed key.
- **It is a one-person company**, running paid membership as an invite-only beta. The prices
  and plan shapes are still moving, as the weekly-to-monthly change shows.
- **Keyword coverage is part of the score.** "kw 9/12" is useful for the pack but crude as a
  fit signal. Their own cascade treats it as the machine's view, not the truth, which is the
  right instinct.

## What we already have that maps onto theirs

| Their feature | Our equivalent today | Gap |
| --- | --- | --- |
| Location gate | `market_eligibility.py` rejects only explicit contradictions: disallowed language, salary below floor, no sponsorship, non-permanent, Israel onsite. `hard_blockers.py` works from facets | Comparable, and more careful about unknowns |
| Machine read (keyword coverage) | `ranking.py`: `_title_fit`, `_signal_coverage`, `_strength_evidence` | We have a deterministic read. **`prefilter.py:7` is `is_software_engineering_title` — a title filter, the exact thing they market against** |
| Recruiter read against user weights | Evaluation scores six dimensions with must-have and preferred requirements and `candidate_support` (`evaluation.py:81`) | Our dimensions are fixed. No user-set weights, no onboarding rubric |
| "The reads disagree" as signal | Not used | New, and cheap: we already have both reads |
| Screening questions from the ATS | None in `apps/job-hunter` | New. The ATS registry already knows which ATS each board runs on |
| Company angle | `company_facets.py` holds per-company facts | Facts exist; no angle distilled per application |
| ATS-specific formatting | None | New |
| Pack with an audit trail | Cover letter only (`cover_letter.py`, `pdf.py`) | No CV tailoring, no screening answers, no diff view |
| Learning from skip, save and edit | None; "not interested teaches the ranking" is planned in [product-shape.md](../product-shape.md) moment 2 | New |
| Warm intro | None | New, and counsel first |
| Interview preparation | Relay's adaptive coach and evidence model | **Ours.** They have nothing |

## What to take as our starting point

In priority order, merged with the Prep Room list rather than duplicating it:

1. **The cascade, with disagreement as a signal.** We already run a gate, a deterministic
   read and an AI evaluation. Add the rule: when the AI read is high and the keyword read is
   low, the role is "right in substance, wrong on paper", so rank it and flag it for
   tailoring. Also replace the title prefilter, since it rejects exactly the roles this rule
   rescues. Feeds the matching work (#235) and #80's measurement.
2. **User-weighted rubric at onboarding.** Let the user weight the dimensions we already
   score. This does not break the project's rule against operator-tuned thresholds:
   weights are the user's stated preference, and detection must still work with none set.
3. **Screening questions pulled from the ATS at draft time.** We know each posting's ATS, so
   this is one fetch per draft. It turns an application pack from a cover letter into the
   whole form.
4. **Tailoring rules**, merging both competitors:
   - nothing invented, with a visible placeholder where a fact is missing (Caddie);
   - every change shows the original, the reason, and the posting quoted verbatim (Caddie);
   - every field names its source (Prep Room);
   - reorder for the posting, and never add a skill (Prep Room);
   - format for the target ATS (Caddie).
5. **The learning loop.** Skips and saves re-rank; edits teach the drafts the user's
   wording, and struck words are never drafted again. We already store evaluations per user,
   so this starts as a feedback table and a prompt input.
6. **The company angle** — one sentence distilled from the company facts we already hold,
   leading the pack and the cover letter.
7. **Time saved per role as a product metric.** They estimate "~1.5–3 h saved" per role. We
   could measure it rather than assert it, which fits the positioning doc's rule against
   unproven claims.
8. **Warm intro** — last, and only after counsel. It is the most GDPR-exposed feature either
   competitor has.

## What stays ours, and should be sharpened because of them

- **Continuous, not nightly.** They sell speed on a batch; ours can be real once #189
  measures time from posting to delivery.
- **All four launch markets, all professions.** They cover two markets and four job
  families.
- **The interview half, grounded in the user's evidence.** Neither competitor connects the
  application to preparation for that specific interview.
- **Shared extraction.** Their per-user scans are metered because each one costs them. Ours
  is cheaper per user once #203 and #204 land.

## Open questions

- **When did they move from weekly to monthly, and why?** Asking them is the owner's call.
  The Wayback Machine did not answer our automated query.
- **How are warm-intro contacts found, and on what legal basis?** Not disclosed.
- **What is their traction?** Unknown. Paid membership is invite-only, which suggests a
  small user base.
- **How good are the packs in practice?** Only an account would tell, which is the owner's
  decision, as with Prep Room.

## Sources

- <https://caddie.careers/>, including the pricing section and its page-source comment
- <https://caddie.careers/how-it-works>
- <https://caddie.careers/manifesto>
- <https://caddie.careers/privacy>
- <https://caddie.careers/terms>
- <https://caddie.careers/sitemap.xml>
- <https://caddie.careers/robots.txt>
- <https://find-and-update.company-information.service.gov.uk/company/16837989>
- <https://find-and-update.company-information.service.gov.uk/company/16837989/officers>
- <https://www.linkedin.com/in/dmitrymeltsov/>
- <https://rocketreach.co/dmitry-meltsov-email_55799888> (secondary)

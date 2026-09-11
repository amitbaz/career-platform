# CONTEXT

Vocabulary for the career platform. Use these terms in issues, specs, tests and code;
avoid the synonyms listed against them, which have caused real confusion here.

See [ADR-0001](docs/adr/0001-the-engine-is-the-product.md) for how the first four fit together.

## The engine

**Engine** — the search-and-match core: ingestion, enrichment and matching. The product.
Not "the bot", which names one surface of it.

**Ingestion** — discovering job postings from sources and persisting them as normalized
rows. Shared across all users. Prefer this to "scraping", which describes only some of
the sources; several are public ATS APIs.

**Enrichment** — deriving structured facts from a persisted posting. Splits in two, and
the distinction is load-bearing:

- **Objective extraction** — facts about the posting itself, identical for every user,
  computed once per job and cached. Shared, platform-funded.
- **Subjective scoring** — how well a posting fits one person. Per-user, funded by that
  user's own provider credentials.

"Evaluation" refers to the older combined operation that did both at once. Use it only
when talking about the pre-split behaviour — the operation itself was removed by #126.
Several code and schema names survive it (`evaluation.py`, `job_hunter_evaluations`, the
`job_evaluation` provider purpose): they name the artefact subjective scoring persists,
which is still an `Evaluation`, not the combined call.

**Facet** — one structured objective fact stored on a posting: hiring-eligible regions,
remote policy, seniority, compensation, a stated requirement and its depth. Facets are
what make filtering possible without reading a description.

**Company facet** — one structured objective fact about an *employer* rather than an
advertisement: its industry, its business model, its stage, its approximate size, its
headquarters region. Stored once per company in `job_hunter_companies`, keyed on
`normalize_company_name` — the suffix-stripping normalization, so "Acme Ltd" and "Acme"
are one employer — and shared by every user (#198). A posting facet amortises over one
posting; a company facet amortises over every role that employer publishes, which is why
it is the cheapest cache the engine has. Refreshed on a long interval, deliberately not
by a posting's description hash: a company does not stop being a marketplace because it
edited a job advert.

**Unknown** — the value every facet, posting or company, carries when nothing was
established. It is never "no". A company nobody has read scores neutrally on the company
dimensions and is neither promoted nor suppressed, and a missing fact must never block a
posting from being scored.

**Matching** — scoring enriched jobs against a user's profile and filters to produce
ranked results. Per-user, fast, answerable on demand. One ranking consumes the posting's
facets and the company's together; there is no separate company-matching path.

**Surface** — anything that consumes the engine and presents results: the mobile app (the
main interface), its desktop helper for long application forms, and push notifications. The
Telegram bot and the scheduled daily digest are legacy surfaces being retired. A surface never
contains matching logic.

**Stage** — one bounded unit of the shared ingestion/enrichment pipeline. The four
stages are `crawl_source`, `resolve_persist`, `extract_facets`, and
`recheck_freshness`. A stage consumes only its own durable **stage queue** and may
enqueue at most the next stage; the queues are the coupling between stages. Queue
payloads and operational state carry no user identity.

**Transient / permanent / quota failure** — the three stage-failure classes.
Transient work retries with increasing backoff and eventually dead-letters;
permanent work dead-letters immediately; quota exhaustion delays the message without
increasing its attempt count. A claimed message that is never acknowledged is not a
failure classification: its visibility timeout expires and another worker may claim it.

## Jobs and their lifecycle

**Posting** — one job advertisement in the world, held once in `job_hunter_postings` and
shared by everyone who discovers it. Identified by its fingerprint, which is computed
from the advertisement and never from the user who found it. It carries what the
advertisement says and how it was fetched; where two users hold different text for it,
the higher content confidence wins.

**Closed** — a posting a freshness re-check found gone: its page answered 404 or 410 or
states it is closed, or its ATS board no longer lists it (#186). A closed posting is never
delivered and never read for facets, but keeps its row, text and facets for anything
already pointing at it. Its employer's own ATS board listing it again reopens it; an
aggregator still listing it does not. Failing to reach a page is not evidence of closure. Prefer this to "expired", which implies a timer, and to
"deleted", which it is not.

**Job** — one user's membership of a posting, in `job_hunter_jobs`, pointing at it through
`posting_id`. It says nothing about the advertisement and carries only what is that user's:
which market it was attributed to, where it sits in their funnel, when they first and last
saw it, and every per-user artefact hanging off it. Uniqueness is one row per user per
posting (#178). Objective facts belong on the posting and subjective ones on the job; the
facets moved there in #175 and the rest followed through #177 and #178. Both words were
used interchangeably before #118 — they are two rows now, and a second user interested in
the same advertisement costs one membership row rather than a copy of it.

What the advertisement says is read from the posting (#177, #178): the `Job` the pipeline
is handed is composed from the posting plus the user's membership row, whether the work
already done is still current — its evaluation, its facets — is decided from the posting's
description hash, and identity resolution matches against postings. Matching moved with
the columns: #177 kept `url` and the identity predicates on the job row because a merged
job row was the only row that had seen every posting behind it, and #176 ended that by
making the merge itself posting-level, so the surviving posting now carries the resolved
link and the folded identity. Match on the posting; decide currency from the posting.

**Merge** — collapsing two postings that are the same advertisement into one survivor.
The fingerprint prefers the ATS triple (`ats_provider`/`ats_board`/`ats_job_id`) over
`(source, source_job_id)` when it is present and trustworthy (#249) — trustworthy meaning
`source_job_id` is either absent or agrees with `ats_job_id`, the property every known ATS
adapter guarantees at the moment it reads a listing. That collapses a board crawled
directly and the same board rediscovered through a company watch relabel at ingestion,
before a decision is ever needed: two sightings of one triple are one fingerprint, so it
is deduplication, not merge. What the trust condition holds back is an aggregator's own
numbering (`source_job_id` naming the aggregator's id, not the ATS job's) and a listing
whose `ats_job_id` disagrees with its own `source_job_id` (#254) — both still hash
source-scoped and so are still two postings until something resolves them; merging them
is a decision about the world, so it is made once and recorded once, in
`job_hunter_posting_merges` (#176). Every affected user's job row is re-pointed at the
survivor, whether or not that user merged anything, and a caller holding a merged-away
posting id resolves to the survivor through `job_hunter_resolve_posting`. The merged-away
posting keeps its row so its fingerprint stays claimed: without it the next crawl of that
source would insert a fresh competing posting and the decision would have to be made
again every day. Its facets are discarded rather than moved, because facts read from one
advertisement's text must not be stamped on another's as permanently current. Do not
confuse this with deduplication, which is resolving a discovered record against the
posting that shares its fingerprint — that has one answer and no decision in it.

**Variant group** — a set of postings on one ATS board that advertise the same position
in different places, e.g. one role an employer publishes as a separate ATS job per
office. It is a fact about the world, decided once at ingestion and stored on each
posting (`job_hunter_postings.variant_group_id`), by ATS board, normalized title and
description similarity (`job_hunter_assign_variant_groups`, #61). Matching folds a
group down to the caller's single best-ranked row, carrying every open location in the
group the caller holds a row for — but nothing about the postings themselves changes.
Do not confuse this with Merge: a merge is one advertisement seen twice that collapses
to one surviving row with the loser's facets discarded; a variant group is several
different advertisements for the same position that all keep their own row, their own
apply URL, their own location and their own facets, linked only for presentation and
scoring. Grouping never merges, redirects or discards a posting.

**Source** — one origin of postings. A feed, a public ATS board, a targeted search
backend, a watched company, or staged email.

**Raw / unique / eligible / selected** — the discovery funnel, in order. Raw is
everything a source produced; unique is after deduplication; eligible is what survived
the non-AI filters; selected is what was chosen for scoring. All four are reported per
source every run.

**Newly discovered** — the rows a run inserted, as against the ones it re-saw. Counted
from what the upsert actually inserted, never estimated from an assumed posting lifetime;
it is the figure capacity planning is sized against. It counts rows rather than unique
jobs, and can exceed unique, because deduplication and the store resolve identity by
different rules and the cost being sized is paid per row.

**Yield** — selected divided by raw, for a source. The value half of a source's
scorecard; elapsed time and request count are the cost half.

**Digest** — legacy: the set of offers the daily run delivers to one user. The product
replaces it with the stack (see "The product" below).

**Offer** — legacy: a job the daily run delivered to a user. Not every scored job becomes
one; a delivery cap and a match-score floor both sit between scoring and delivery. In the
product's language an offer is the employer's job offer — an outcome — and a posting shown to
a user is a card.

**Deferred** — a candidate that ranked well enough but was not reached this run. Deferred
candidates stay eligible for later runs. They are not rejected, and the two must not be
conflated in logs or counters.

## Users and configuration

**Search profile** — a user's database-backed search configuration: markets, role
families, thresholds, salary floors, language and remote preferences, and delivery
policy. Replaced the old repository-held YAML configuration.

**Market** — a geography-and-policy bundle within a search profile, carrying its own
locations, currency, salary floor, and remote, relocation and sponsorship rules.

**Bring-your-own-key** — each user supplies their own AI provider credentials and funds
their own subjective scoring. Shared objective extraction is the deliberate exception and
runs on a platform-owned key.

**Platform key** — the deployment's own provider credential, which funds shared objective
extraction and nothing else (`PLATFORM_GEMINI_API_KEY`). It has its own allowance and its
own global ledger, held apart from every per-user ledger, so platform-funded and
user-funded consumption are read separately. A user's key is never a fallback for it: when
the platform allowance is exhausted, or no platform key is configured, extraction pauses
for the run and the postings it did not read are enriched by a later one.

**Call class** — which of the two an AI call is, declared per call: `SHARED_EXTRACTION`
(objective, reusable, platform-funded) or `USER_SUBJECTIVE` (a judgement about one person,
funded by their key). The class selects the credential and the quota; nothing infers
either from the call site or from configuration.

## The product

What the user sees and does, as decided in [docs/product-vision.md](docs/product-vision.md).
The D-numbers point at the decision each term comes from.

**Stack** — the user's current match cards: up to twenty a day, refreshed continuously. A
ceiling, never a quota; a thin day gives a short stack (D1). Prefer this to "digest" or
"feed".

**Card** — one posting in the stack, showing only what an instant decision needs: the why
line, then the posting's known facts, with unknowns shown as unknown (D1).

**Why line** — the one line on a card that tells this user why this posting is worth their
time. Per user and posting (D1).

**Swipe** — the user's decision on a card. Right: prepare it for me. Left: not this one, a
soft signal. An optional reason after a left swipe can create a rule (D4).

**Rule** — a learned preference, created only by an explicit reason, shown to the user when it
is learned and reversible. A left swipe alone never creates one (D4).

**Prepared application** — what a right swipe produces: the CV tailored to the posting, a
short cover note and the screening answers, every field naming its source (D2). "Application
pack" and "cover letter" name older, narrower artefacts.

**Bucket** — the user's prepared applications waiting for review: approve, edit or drop. Not a
to-do list (D2).

**Submit session** — one sitting at a laptop in which a desktop helper fills long employer forms
from prepared applications; the user sends each one (D8).

**Outcome** — what happened after the user applied: a reply, an interview, a rejection, an
offer, or silence inferred from time (D9). Not a preference: a swipe is the user's opinion, an
outcome is the employer's answer (D3, D4).

**Active search / keep watching** — the user's mode. Active search is the stack and applying;
keep watching is rare alerts on excellent matches only. Separate from the tier, which is what
the user pays (D7).

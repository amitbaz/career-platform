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
when talking about the pre-split behaviour.

**Facet** — one structured objective fact stored on a job: hiring-eligible regions,
remote policy, seniority, compensation, a stated requirement and its depth. Facets are
what make filtering possible without reading a description.

**Matching** — scoring enriched jobs against a user's profile and filters to produce
ranked results. Per-user, fast, answerable on demand.

**Surface** — anything that consumes the engine and presents results: Telegram, the
scheduled daily run, a future application. A surface never contains matching logic.

## Jobs and their lifecycle

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

**Digest** — the set of offers delivered to one user in one run.

**Offer** — a job delivered to a user. Not every scored job becomes one; a delivery cap
and a match-score floor both sit between scoring and delivery.

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

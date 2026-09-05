# Job Hunter Bot v2 Discovery Quality Design

**Date:** 2026-08-30

## 1. Purpose

Improve the quality of jobs surfaced by `amitbaz/job-hunter-bot` by fixing the discovery and candidate-selection bottlenecks before changing the Gemini model.

The v1 bot is reliable end-to-end, but its discovery surface is narrower than the manual/daily workflow used in the Looking for a new job project. It also evaluates jobs source-by-source and stops after a fixed Gemini budget, so noisy early sources can consume the budget before higher-signal jobs are considered.

v2 changes the pipeline from:

```text
source -> evaluate immediately -> next source
```

into:

```text
all sources -> normalize/enrich/dedupe -> deterministic ranking -> top candidates -> Gemini
```

The optimization objective remains interview-quality opportunities, not raw job count.

## 2. Scope

### In scope

- Broader public-web and public-job-board discovery.
- More role-title/query families aligned with the candidate's actual career direction.
- Stable public source adapters where practical.
- Web-search discovery for sources without a reliable public API.
- Direct company/ATS discovery through configured Ashby, Lever, and Greenhouse boards.
- Global candidate collection before Gemini evaluation.
- Deterministic pre-Gemini ranking so high-signal jobs receive the limited evaluation budget first.
- Source-quality metadata and discovery metrics.
- Tests proving source order no longer controls which jobs reach Gemini.
- Backward-compatible SQLite/job evaluation/delivery behavior.

### Out of scope

- Changing the default Gemini model.
- Paid search APIs.
- Authenticated scraping of LinkedIn or other sites.
- Browser automation.
- Automated employer-form submission.
- Google Drive integration for CV/template secrets.
- Redesigning Telegram delivery or PDF generation.

The existing v1 safety boundary remains unchanged.

## 3. Problem statement

The current bot has three discovery-quality limitations.

### 3.1 Narrow source coverage

The default source set is currently Remotive, Arbeitnow, DuckDuckGo search, plus explicitly configured ATS boards. The default ATS lists are empty, so the bot misses many startup, scale-up, direct-employer, and community-posted roles.

The manual daily workflow searches a much broader surface, including startup boards, remote boards, company careers pages, VC/accelerator portfolios, Hacker News hiring threads, and multiple ATS ecosystems.

### 3.2 Static search strategy

The current configuration contains a small set of fixed search strings centered on exact titles such as Senior Product Engineer and Senior Frontend Engineer. Strong roles with adjacent titles can be missed even when the actual work is an excellent fit.

Examples of useful adjacent titles include:

- Staff Product Engineer
- Senior Software Engineer, Product
- Senior Software Engineer, Frontend
- Product-Focused Full Stack Engineer
- Founding Product Engineer
- Frontend Platform Engineer
- Staff Frontend Engineer
- AI Product Engineer
- Senior Engineer, Developer Experience / UI Platform when product-facing

### 3.3 Evaluation-budget starvation

`max_jobs_per_run` limits Gemini evaluations. In v1, the pipeline walks sources sequentially and evaluates qualifying jobs immediately. This means source ordering affects outcome quality.

A lower-signal source can consume the evaluation budget before a later high-signal ATS/company source is reached.

v2 must make the Gemini budget global rather than source-local.

## 4. Design principles

1. **Discovery breadth before model upgrades.** First ensure good jobs enter the candidate pool.
2. **Structured sources before fragile scraping.** Prefer stable public JSON/RSS/ATS endpoints when available.
3. **Search as discovery, not evidence.** Search-engine results only provide URLs; the bot must fetch the actual job page before evaluation.
4. **Global prioritization.** Gather all candidates before spending Gemini calls.
5. **Deterministic cheap ranking first.** Use titles, skills, source quality, remote/location evidence, and product/ownership signals to order candidates.
6. **Fail open by source.** One source failure never aborts the whole run.
7. **No authenticated scraping.** Public pages/endpoints only.
8. **Preserve persistence.** Existing SQLite data, evaluation caching, material generation, Telegram retry behavior, and artifact restore continue to work.

## 5. Target discovery surface

v2 uses a tiered discovery strategy.

### Tier 1: stable structured/public feeds

Keep existing sources and add adapters only where a stable public endpoint is practical.

Required baseline sources:

- Remotive
- Arbeitnow
- Remote OK public jobs feed/API if accessible without authentication
- We Work Remotely public RSS/feed if available and stable
- Hacker News `Who is hiring?` monthly thread discovery through a public HN/Algolia endpoint or equivalent public API

Each adapter must normalize records into the existing `Job` model and fail independently.

### Tier 2: direct ATS boards

Use existing Ashby, Lever, and Greenhouse adapters, but make them useful by allowing curated board seeds in configuration.

The configuration should support company board identifiers grouped by ATS.

Example shape:

```yaml
ats:
  ashby:
    - company-a
    - company-b
  lever:
    - company-c
  greenhouse:
    - company-d
```

The repository should contain only public board identifiers, never private credentials.

### Tier 3: targeted public-web discovery

For sources that do not expose a stable unauthenticated API suitable for ingestion, use web search queries to discover original postings.

Target domains/query families should include, where publicly indexable:

- Wellfound
- Y Combinator Work at a Startup
- Jobgether
- Welcome to the Jungle
- Landing.jobs
- direct company career pages
- Ashby
- Lever
- Greenhouse
- remote-only job boards
- startup/scale-up career pages
- venture/accelerator portfolio career pages
- engineering-community hiring pages

Search-engine discovery remains best effort. Source-specific failure or markup changes must not stop the run.

### LinkedIn

LinkedIn may be found through ordinary public web search, but v2 must not depend on authenticated LinkedIn access or scraping. Prefer the original employer/ATS URL when both are available.

## 6. Query families

Replace the small static query set with configuration-driven query families.

### Role families

At minimum:

- senior product engineer
- staff product engineer
- senior frontend engineer
- staff frontend engineer
- frontend technical lead
- senior software engineer frontend
- senior software engineer product
- product-focused full stack engineer
- full-stack product engineer
- founding product engineer
- ai product engineer
- frontend platform engineer

### Skill/context families

Combine role queries with high-signal candidate strengths:

- React
- TypeScript
- Next.js
- frontend architecture
- design systems
- product ownership
- B2B SaaS
- platform
- GraphQL
- monorepo
- AI-assisted engineering
- agentic development

### Geography families

Prioritize:

- remote Europe
- remote EU
- remote EMEA
- remote Germany
- Berlin remote/hybrid only when the posting explicitly allows remote work
- Israel remote where relevant

The query generator should not create an uncontrolled Cartesian explosion. Configuration should define a finite list of high-value query templates, with a hard cap per run.

Example query templates:

```text
"Staff Product Engineer" React TypeScript remote Europe
"Senior Software Engineer" frontend React remote EU
"Founding Product Engineer" TypeScript remote Europe
site:jobs.ashbyhq.com "Product Engineer" React remote
site:jobs.lever.co "Staff Frontend Engineer" TypeScript remote
site:boards.greenhouse.io "Senior Software Engineer" React remote Europe
```

## 7. Candidate collection architecture

Introduce a discovery phase that returns a complete candidate pool before evaluation.

```text
build sources
   |
   v
source.discover() for every source
   |
   v
enrich missing job pages
   |
   v
normalize + SQLite upsert/dedupe
   |
   v
prefilter hard blockers
   |
   v
rank all unevaluated candidates
   |
   v
select top max_jobs_per_run
   |
   v
Gemini evaluation
   |
   v
existing material + Telegram flow
```

Pending Telegram retries remain independent and must still run even when no new candidate is selected for Gemini.

## 8. Discovery candidate model

Keep the persisted `Job` schema backward compatible. Introduce an in-memory candidate wrapper rather than adding discovery-only fields to the SQLite schema unless implementation proves persistence is necessary.

Suggested shape:

```python
@dataclass(slots=True)
class DiscoveryCandidate:
    job_id: int
    job: Job
    source_quality: int
    prefilter_reason: str
    priority_score: int
```

Exact naming may change during implementation, but ranking metadata should remain separate from Gemini evaluation scores.

## 9. Deterministic pre-Gemini ranking

The pre-ranker exists only to decide which jobs deserve Gemini budget. It must not replace the final fit evaluation.

Use a transparent additive score with bounded weights.

Recommended signals:

### Title fit: 0-40

Highest scores for direct role families such as:

- Senior/Staff Product Engineer
- Senior/Staff Frontend Engineer
- Senior Software Engineer with explicit frontend/product scope
- Full-stack Product Engineer when frontend/product emphasis is clear

Lower but positive scores for adjacent titles.

### Candidate-strength evidence: 0-25

Count weighted evidence in title/description for:

- React
- TypeScript
- Next.js
- frontend architecture
- design systems
- GraphQL
- product ownership
- B2B SaaS
- monorepo/platform
- agentic/AI-assisted development

Use capped scoring so keyword repetition cannot dominate.

### Career-direction evidence: 0-15

Reward signals such as:

- end-to-end ownership
- product engineering
- cross-stack responsibility
- architecture/system design
- early-stage/startup ownership
- platform/core/shared systems

### Location/remote confidence: 0-10

Reward explicit EU/Europe/EMEA/Germany-compatible remote evidence. Unknown remote geography receives less credit than explicit compatibility.

### Source quality: 0-10

Recommended ordering:

1. original employer/ATS posting
2. structured specialist/startup/remote board
3. community hiring thread
4. general web-search result

The ranking function must be deterministic, unit-tested, and independent of source iteration order.

## 10. Source precedence and cross-source duplicates

The same posting may be discovered from multiple sources.

v2 should prefer richer/original records when duplicates can be recognized.

Existing fingerprints based on source + source job ID can prevent cross-source deduplication when two boards represent the same job differently. v2 should improve in-run duplicate collapse using canonical URL and normalized company/title identity before Gemini selection.

Do not perform a risky destructive migration of existing fingerprints.

Recommended approach:

- preserve existing SQLite fingerprint behavior;
- add an in-run candidate dedupe key using canonical URL first;
- when URLs differ, fallback to normalized company + title + location;
- prefer candidates with fuller descriptions and original employer/ATS URLs.

## 11. Enrichment strategy

Search-derived records often contain only a title and URL.

Before ranking/evaluation:

- fetch the original page;
- prefer JSON-LD `JobPosting` extraction;
- fill company, title, description, location, and remote hints;
- skip expensive re-fetching when a source already supplies a complete description;
- keep enrichment failure non-fatal.

A candidate without enough job content after enrichment may be persisted but should rank low or be excluded from Gemini selection.

## 12. Pipeline refactor

`run_pipeline()` remains the top-level orchestration entrypoint, but discovery/evaluation selection should be split into focused helpers/modules.

Recommended boundaries:

- `sources/`: source adapters only.
- `discovery.py`: collect source results, enrich, upsert, collapse in-run duplicates.
- `ranking.py`: deterministic pre-Gemini priority scoring and top-N selection.
- `pipeline.py`: orchestrate discovery, ranking, evaluation, material generation, retries, delivery.

Do not move unrelated existing behavior.

## 13. Configuration changes

Extend `SearchPolicy` and `config/search.yml` with explicit discovery controls.

Suggested configuration:

```yaml
max_jobs_per_run: 25
max_search_queries_per_run: 30

role_families:
  - senior product engineer
  - staff product engineer
  - senior frontend engineer
  - staff frontend engineer
  - senior software engineer frontend
  - senior software engineer product
  - founding product engineer
  - full-stack product engineer
  - ai product engineer

search_query_templates:
  - '"{role}" React TypeScript remote Europe'
  - '"{role}" remote EU product engineering'

search_domains:
  - jobs.ashbyhq.com
  - jobs.lever.co
  - boards.greenhouse.io

ats:
  ashby: []
  lever: []
  greenhouse: []
```

Implementation may keep an explicit `search_queries` override for backward compatibility.

## 14. Evaluation budget semantics

`max_jobs_per_run` must mean:

> maximum number of new/retryable jobs sent to Gemini after global ranking.

It must no longer mean the first N qualifying jobs encountered during source iteration.

Already-evaluated unchanged jobs do not consume the budget.

Pending Telegram retries do not consume the budget.

Failed/retryable evaluations may consume the budget when selected again.

## 15. Observability

Add concise run-level INFO logging so discovery quality can be measured.

At minimum log:

- jobs discovered per source;
- total raw discoveries;
- unique in-run candidates;
- candidates rejected by deterministic prefilter;
- candidates eligible for ranking;
- candidates selected for Gemini;
- source distribution of selected candidates;
- existing run summary.

Do not log CV text, cover-letter template text, API keys, Telegram credentials, or full job descriptions.

A suggested log form:

```text
discovery: raw=742 unique=318 eligible=61 selected=25
selected sources: ashby=7 greenhouse=5 web=4 remotive=3 hn=3 remoteok=3
```

## 16. Failure handling

- Each source failure is isolated and logged.
- Search-engine failure does not block structured sources.
- One malformed source record is skipped without aborting the adapter.
- Enrichment failure keeps the job persisted when possible but does not crash the run.
- Ranking must tolerate missing company/location/description fields.
- If all discovery sources fail, pending Telegram retries still execute.
- Existing Gemini, PDF, Telegram, and SQLite failure behavior remains unchanged unless required by the pipeline refactor.

## 17. Testing strategy

All external calls remain mocked in tests.

Required new coverage:

### Source adapters

- Remote OK normalization.
- We Work Remotely feed normalization if implemented as a direct adapter.
- Hacker News hiring-thread parsing/normalization.
- Source failure isolation.

### Query generation

- role templates generate deterministic queries;
- query cap is enforced;
- explicit legacy `search_queries` remain supported if retained.

### Ranking

- direct product/frontend roles outrank generic React roles;
- explicit Europe-compatible remote outranks unknown geography;
- original ATS/source records receive source-quality preference;
- keyword repetition does not produce unbounded scores;
- ranking order is stable.

### Global budget

Create multiple fake sources where an early source returns many mediocre candidates and a later source returns a very strong candidate.

Assert that the strong later candidate is selected within `max_jobs_per_run` regardless of source order.

### Cross-source duplicates

Assert two records for the same canonical URL are evaluated once and the richer/original record wins.

### Regression

Existing tests for:

- SQLite dedupe/re-evaluation;
- Gemini evaluation validation;
- cover-letter generation;
- PDF rendering;
- Telegram delivery retry;
- scheduled execution;

must continue to pass.

## 18. Acceptance criteria

v2 discovery quality is complete when:

1. The bot gathers candidates from all configured sources before selecting jobs for Gemini.
2. `max_jobs_per_run` is applied after deterministic global ranking.
3. A high-priority job from a later source cannot be starved solely because an earlier source produced many mediocre jobs.
4. Default discovery includes at least two additional high-value public discovery channels beyond v1, plus broader web-search query families.
5. ATS board seeding is straightforward through `config/search.yml`.
6. Adjacent career-fit titles are included in discovery/query strategy.
7. Duplicate postings found through multiple sources are collapsed in-run before Gemini evaluation.
8. Search results are enriched from the actual posting before evaluation when possible.
9. Run logs expose discovery funnel counts and selected-source distribution.
10. Existing SQLite state artifacts remain usable without reset.
11. Existing Telegram delivery retry behavior remains intact.
12. No Gemini model upgrade is required for this iteration.
13. `pytest -q` passes in CI.

## 19. Success measurement

After deployment, compare bot results against the manual daily workflow for at least several runs.

Track:

- overlap of surfaced jobs;
- number of genuinely attractive roles found only by the bot;
- number found only by the manual workflow;
- percentage of Gemini-selected jobs that are plausible applications;
- source distribution of strong matches.

Only after discovery coverage is materially closer should the project consider an A/B test of Gemini model quality using the same fixed set of jobs.

## 20. Non-goal reminder

This v2 is a discovery-quality release, not a general rewrite.

Do not expand scope into application submission, browser automation, paid APIs, model upgrades, a hosted service, or a new user interface.
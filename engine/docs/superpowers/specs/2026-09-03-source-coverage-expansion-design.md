# Source Coverage Expansion Design

Date: 2026-09-03
Status: Ready for review
Branch: `design/source-coverage-expansion`

## Problem

The market-driven search feature correctly broadened geographic policy, but most newly configured market-specific `source_domains` are not real ingestion sources. They are currently used primarily as domains inside generic Brave/DuckDuckGo search queries.

In production this has produced almost no incremental jobs because:

- DuckDuckGo returns zero usable targeted-search results on GitHub runners.
- Brave is intentionally scarce (250 searches/month) and cannot carry daily job ingestion alone.
- Existing direct ATS adapters for Ashby, Lever, and Greenhouse are only used for statically configured board IDs, and those lists are currently empty.
- Strong market-specific sites such as DevJobs, Wellfound, JobStreet, GotFriends, Built In, and Glints expose useful job inventories but are not directly ingested.

The result is that most volume still comes from broad legacy feeds such as Arbeitnow, Hacker News, Jobicy, RemoteOK, Himalayas, Remotive, WeWorkRemotely, and YC. Those feeds remain useful, but they do not provide enough targeted coverage for Israel, London, Singapore, NYC/SF, and high-quality European startup roles.

## Goal

Turn source coverage into a first-class discovery system so each configured market is backed by real job ingestion, not just generic web-search queries.

Success means:

1. Every priority market has at least one direct, market-relevant ingestion path.
2. Ashby/Lever/Greenhouse coverage grows automatically from jobs the bot already sees.
3. Brave is used primarily to discover new companies/ATS boards rather than repeatedly search for individual jobs.
4. DuckDuckGo is no longer relied on as the primary fallback for market-specific discovery.
5. Source telemetry makes it possible to measure which sources actually produce eligible and delivered jobs.

## Design Principles

- Prefer direct public feeds/pages over generic search engines.
- Reuse existing source adapters and source conventions.
- Keep the broad legacy feeds; this is additive coverage, not a rewrite.
- Persist learned source coverage so one discovery can create recurring value.
- Separate source discovery from job ingestion.
- Fail open per source: one broken site must not block the run.
- Do not add browser automation to the daily workflow unless a source proves impossible to ingest otherwise and is valuable enough to justify it.
- Do not change market priorities, salary floors, role targeting, Gemini model/quotas, or application logic in this feature.

## Source Model

The system will distinguish three source classes.

### 1. Direct job sources

A direct source returns jobs from a stable public feed, API, or server-rendered listing page. These sources participate in `collect_candidates()` like existing sources.

Initial direct-source targets:

| Market | Source | Initial treatment |
| --- | --- | --- |
| Israel | DevJobs | Direct adapter. Public server-rendered job inventory exposes frontend/full-stack filters, work mode, experience, company, location, and skills. |
| Israel | GotFriends | Direct adapter if list pagination can be consumed reliably with normal HTTP; otherwise use as a seed/discovery source while keeping individual job-page parsing available. |
| Germany/EU, London, US, Israel remote | Wellfound | Direct adapter for role/location pages. Public pages expose role, company, salary, remote scope, experience, and sometimes sponsorship/relocation metadata. |
| US NYC/SF | Built In | Direct adapter for relevant location/role searches if pagination is stable with normal HTTP. Individual job pages expose rich descriptions and sponsorship signals. |
| Singapore | JobStreet | Direct adapter for Singapore frontend/software-engineering search pages if pagination is stable with normal HTTP. |
| Singapore | Glints | Direct adapter if normal HTTP exposes stable listing pagination and job detail URLs. |

Adapters should only be promoted to production after a focused fixture/probe proves that normal `HttpClient` requests return deterministic, parseable listings. If a site requires unstable client-side behavior, it stays in the discovery/seed class rather than being represented as a working direct source.

### 2. Learned ATS registry

The repository already has working direct adapters for:

- Ashby
- Lever
- Greenhouse

The missing piece is automatic board discovery.

Add a persistent `ats_registry` table, separate from `company_watch` because the two concepts have different semantics:

- `company_watch` means a company has been promoted because it is strategically interesting.
- `ats_registry` means the bot knows how to scan a public ATS board, regardless of company quality.

Suggested fields:

```text
provider            ashby | lever | greenhouse
board_identifier    provider-specific board/site/token
company_name        best known display name, optional
market_hint         market that first produced the board, optional
first_seen_at
last_seen_at
last_checked_at
last_success_at
last_job_count
consecutive_failures
active
```

Unique key: `(provider, board_identifier)`.

### ATS harvesting

Harvest ATS references from every raw job before prefiltering whenever possible. Reuse `parse_supported_ats_url()` for URLs from:

- `job.url`
- `job.original_url`
- `job.canonical_url`
- already populated `job.ats_provider` / `job.ats_board`

This happens before relevance rejection because even an irrelevant backend/mobile role can reveal a company board that later contains an excellent frontend role.

When canonical resolution later discovers a supported ATS URL, it also upserts the board into the registry.

### ATS scanning

At source-build time, load active learned boards and instantiate the existing `AshbySource`, `LeverSource`, and `GreenhouseSource` adapters dynamically.

Do not scan an unbounded number of boards forever. Introduce a configurable per-run cap, initially `max_learned_ats_boards_per_run: 75`.

Board priority:

1. boards that recently produced eligible jobs;
2. boards associated with under-covered priority markets;
3. boards successfully checked recently;
4. round-robin among the remaining active boards.

Persist `last_checked_at`, success/failure state, and last job count so coverage naturally rotates instead of repeatedly scanning the same first N boards.

After repeated failures, temporarily deactivate/pause a board rather than deleting it. A later rediscovery can reactivate it.

## Search Engines Become Source Discovery, Not Primary Ingestion

Brave remains useful, but its role changes.

Current behavior largely asks Brave/DuckDuckGo for individual job pages. The new behavior should spend scarce Brave calls on queries that are likely to reveal reusable sources, especially:

- company career pages;
- `jobs.ashbyhq.com/<board>`;
- `jobs.lever.co/<site>`;
- `boards.greenhouse.io/<token>`;
- high-value companies in target markets.

Any supported ATS URL returned by Brave is harvested into `ats_registry`. Jobs returned directly by Brave may still enter discovery, but recurring value comes from learning the board.

The existing 250/month persisted Brave budget remains unchanged.

### DuckDuckGo

Disable DuckDuckGo as the fallback for the 30 market-specific discovery queries. Production evidence shows successful HTTP requests but zero parsed search hits, so counting these as useful market discovery adds latency without coverage.

DuckDuckGo may remain temporarily available for unrelated best-effort canonical resolution until that path is separately evaluated; this design does not depend on it for market coverage.

## Market Coverage Mapping

`source_domains` should stop implying that every listed domain is an active ingestion source.

Replace that ambiguity with explicit source configuration, conceptually:

```yaml
markets:
  - id: israel_remote
    direct_sources:
      - devjobs
      - gotfriends
      - wellfound
    discovery_domains:
      - jobs.techaviv.com
      - startup.jobs

  - id: london
    direct_sources:
      - wellfound
    discovery_domains:
      - workvisajobs.co.uk
      - builtin.com

  - id: singapore
    direct_sources:
      - jobstreet_sg
      - glints_sg
    discovery_domains:
      - nodeflair.com
      - mycareersfuture.gov.sg

  - id: us_nyc_sf
    direct_sources:
      - builtin
      - wellfound
```

Exact config shape can follow current dataclass conventions, but the semantic distinction is required:

- `direct_sources` means code actually ingests jobs from that source.
- `discovery_domains` means the domain may be searched/used to discover companies or boards; it must never be counted as source coverage by itself.

### Dynamic sources

TechAviv and NodeFlair currently expose useful public job portals, but their crawled HTML does not expose stable job rows as clearly as DevJobs/Wellfound/JobStreet. Treat them as discovery/seed inputs initially. During implementation, a focused HTTP probe may promote them to direct adapters if a stable public endpoint or server-rendered listing payload is found.

The same rule applies to MyCareersFuture and WorkVisaJobs: no source is labeled direct until the implementation proves deterministic ingestion with the bot's normal HTTP stack.

## Data Flow

A daily run becomes:

```text
legacy direct feeds
+ market direct adapters
+ learned ATS boards
+ limited Brave source discovery
        |
        v
raw jobs
        |
        +--> harvest ATS board identities --> ats_registry
        |
        v
existing dedupe / enrichment / market attribution / prefilter / Gemini evaluation
```

The learned registry creates a positive feedback loop:

```text
job seen once
  -> identify employer ATS board
  -> persist board
  -> scan entire board on future runs
  -> discover roles that no original feed/search query exposed
```

## Source Attribution

Jobs should retain the source that actually yielded the listing (`devjobs`, `wellfound`, `ashby`, etc.).

If a job appears through multiple sources, existing dedupe/provenance behavior remains responsible for preserving source copies while selecting a richer representative.

Learned ATS scans must use the existing bounded source labels (`ashby`, `lever`, `greenhouse`) rather than embedding company names into metric labels.

## Telemetry

Add source-quality telemetry beyond raw contribution.

Per source, log at minimum:

```text
raw
unique
eligible
selected
high_priority
possible_match
skip
blocked
delivered
```

For the ATS registry, log:

```text
ats_registry_total
ats_boards_discovered
ats_boards_scanned
ats_boards_successful
ats_boards_failed
ats_jobs_raw
```

For direct market adapters, log source failures separately so zero results can be distinguished from a parser/network failure.

This is necessary because the project priority is discovery quality: we must be able to answer whether DevJobs, Wellfound, JobStreet, a learned ATS registry, or another source is actually improving outcomes.

## Rollout Strategy

Implement incrementally rather than integrating every named website at once.

### Phase 1: Foundation + highest-confidence coverage

- persistent ATS registry and harvesting;
- dynamic Ashby/Lever/Greenhouse scans;
- DevJobs direct adapter;
- Wellfound direct adapter;
- disable DuckDuckGo market-query fallback;
- source-quality telemetry.

This immediately improves Israel plus Europe/London/US startup coverage and turns existing jobs into recurring ATS sources.

### Phase 2: Market-specific expansion

Add adapters after HTTP feasibility tests, in this priority order:

1. JobStreet Singapore;
2. Built In;
3. GotFriends;
4. Glints;
5. TechAviv if a stable endpoint is discovered;
6. NodeFlair / MyCareersFuture / WorkVisaJobs where technically reliable.

Each adapter ships independently with fixtures and source-contribution telemetry. A source that cannot be reliably ingested remains a discovery seed rather than blocking the broader feature.

## Testing

### ATS registry

- harvesting supported ATS URLs from raw jobs;
- deduplicating board identifiers;
- harvesting before prefilter rejection;
- canonical resolution adding newly discovered boards;
- persistence across runs;
- board scheduling/rotation and cap behavior;
- failure pausing/reactivation.

### Direct adapters

For every new direct source:

- parse a saved representative listing fixture;
- pagination behavior;
- title/company/location/url/description extraction;
- remote/salary/work-mode fields when available;
- malformed/missing fields fail safely;
- HTTP failure returns no jobs without failing the run.

### Pipeline

- new sources participate in existing dedupe;
- source provenance is preserved;
- market attribution still happens after ingestion;
- no changes to salary/language/sponsorship policy;
- source telemetry reflects actual eligible/delivered jobs.

## Success Criteria

The feature is validated by production runs, not merely green unit tests.

After rollout:

1. `search_results=0` from generic search is no longer the determining factor for market coverage.
2. Israel receives jobs directly from at least DevJobs and/or learned Israeli ATS boards.
3. London/EU/US receive additional startup jobs from Wellfound and learned ATS boards.
4. Singapore receives a real direct source after Phase 2 begins, starting with JobStreet if feasibility holds.
5. `ats_registry_total` grows over time from jobs already encountered.
6. At least one learned ATS board produces a job that was not present in the original source feed.
7. Per-source eligible/delivered metrics make it possible to tune or remove low-value sources based on evidence.

## Out of Scope

- Changing market priority percentages.
- Changing salary floors.
- Relaxing or tightening pre-Gemini relevance filters.
- Candidate-context extraction/retry changes.
- Gemini model or quota changes.
- Supabase migration.
- Application tracking or interview-prep changes.

Those can be handled as independent follow-up work after the top-of-funnel source coverage is real.
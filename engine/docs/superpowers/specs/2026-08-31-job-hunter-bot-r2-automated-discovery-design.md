# Job Hunter Bot R2 — Automated Discovery Design

Date: 2026-08-31
Status: Proposed for review
Repository: `amitbaz/job-hunter-bot`
Branch: `feat/r2-automated-discovery`

## 1. Purpose

R2 improves the standalone Job Hunter Bot's automated discovery coverage and job identity quality without introducing Supabase migration or user-driven ingestion.

The release has four goals:

1. Add a self-expanding company watchlist that learns relevant employers from strong matches and checks them directly on future runs.
2. Resolve discovered jobs aggressively but non-blockingly to canonical employer/ATS postings before final deduplication and scoring.
3. Preserve cross-source provenance so the same role found through Gmail, specialist boards, search, and ATS sources becomes one job with multiple source records rather than duplicated jobs.
4. Add a small curated set of higher-value specialist discovery sources while keeping the source layer fail-open and maintainable.

R2 remains SQLite-first. It must not depend on Relay or Supabase and must not block the existing Gmail intelligence flow implemented in R1.

## 2. Scope

### In scope

- SQLite-backed self-expanding company watchlist.
- Strict automatic company promotion only after a strong relevant evaluation.
- Manual watchlist entries supported alongside automatically learned entries.
- Direct re-checking of active watched companies on future runs.
- Structured ATS preference when an employer uses a supported ATS.
- Careers-page fallback when no structured ATS endpoint is known.
- Aggressive best-effort canonical employer/ATS posting resolution.
- Original source URL preservation alongside canonical URL.
- Cross-source provenance tracking.
- Improved deduplication across Gmail, search, specialist sources, aggregators, and ATS boards.
- A curated specialist-source expansion using a hybrid direct-adapter + targeted-search strategy.
- Source and watch health metrics.
- Independent fail-open behavior for all external discovery paths.
- Tests for watch promotion, canonical resolution, provenance, dedupe, source health, and end-to-end multi-source collapse.

### Out of scope

- Supabase/Postgres migration.
- Relay integration.
- User-driven/Telegram job-URL ingestion.
- Automated LinkedIn login or browser automation.
- LinkedIn credentials or cookies.
- Automated job application submission.
- Application-form automation, CAPTCHA, 2FA, work-authorization attestations, salary commitments, or demographic answers.
- Learning-to-rank from application outcomes.
- Real-time event infrastructure.
- Building a large long-tail catalog of fragile scrapers.

The deferred user-driven ingestion idea is intentionally not R3 in the immediate roadmap; it may be revisited later after the automated flow is stronger.

## 3. Architectural decision

R2 uses a hybrid discovery architecture.

Direct adapters are used only where a source exposes a stable, high-value public surface. Targeted web/search discovery is used for sources that are valuable but not stable enough to justify a dedicated scraper. All discovered jobs are normalized and then pass through a shared canonical-resolution and identity layer before the existing profession gate, ranking, Gemini evaluation, cover-letter generation, and Telegram delivery flow.

```text
Existing public sources
Gmail-staged jobs
Curated specialist sources
Company watchlist
Targeted search
        |
        v
Candidate normalization
        |
        v
Canonical employer/ATS resolution
        |
        v
Cross-source identity + provenance + dedupe
        |
        v
Existing profession gate / prefilter / ranking
        |
        v
Gemini evaluation
        |
        +--> normal delivery flow
        |
        +--> if strong match: learn/update company watch entry
```

Company watching is downstream of proven relevance. Random companies discovered once do not enter the persistent watchlist.

Canonical resolution is best-effort and non-blocking. Failure to resolve an employer posting never causes a discovered job to be dropped.

## 4. R2 relationship to R1

R1 remains intact and continues to run before normal discovery.

Gmail-derived job candidates remain a normal discovery input. R2 does not introduce a separate scoring or delivery path for Gmail jobs.

R2 improves Gmail-discovered jobs in the same way as every other source:

- canonical resolution;
- cross-source provenance;
- stronger deduplication;
- possible company-watch promotion after strong evaluation.

R1 application lifecycle events and review-needed behavior are not redesigned in R2.

## 5. Self-expanding company watchlist

### 5.1 Automatic promotion policy

Automatic promotion is deliberately strict.

A company may be promoted automatically only when all of the following are true:

- the job has completed normal Gemini evaluation;
- the evaluation has no hard blockers;
- `evaluation.decision` is `package_match` or `high_priority`;
- `evaluation.total_score` is at least the configured `policy.thresholds.package` value.

This uses the existing evaluation policy as the source of truth. With the current configuration, `package_match` begins at 75 and `high_priority` begins at 85, but R2 must not introduce a separate hard-coded promotion score.

Passing the profession gate alone is not sufficient.

`possible_match`, `skip`, `blocked`, failed evaluations, and unevaluated jobs must not auto-promote a company.

### 5.2 Conceptual schema

Add a persistent company-watch model roughly equivalent to:

```text
company_watch
- id                        primary key
- company_name
- normalized_company_name   unique logical company identity
- careers_url               nullable
- ats_provider              nullable
- ats_identifier            nullable
- discovered_from_job_id    nullable
- promotion_source          manual | automatic
- confidence
- active
- paused_until              nullable
- first_seen_at
- last_verified_at          nullable
- last_successful_check_at  nullable
- consecutive_failures
- created_at
- updated_at
```

The exact SQLite schema may differ, but the required behavior may not.

### 5.3 Manual and automatic entries

R2 supports both:

- manual watch entries configured explicitly by the user; and
- automatically learned entries created from strong evaluated jobs.

Manual entries must never be deleted or permanently disabled automatically.

Automatic entries may be temporarily paused after repeated failures but must not be silently deleted.

### 5.4 Watch target preference

For each watched company, prefer the most structured reliable endpoint available:

1. supported ATS provider + identifier;
2. employer careers/jobs endpoint;
3. generic careers page as fallback.

If a later discovery reveals a better endpoint than the current watch target, the watch entry may be upgraded.

An upgrade must be confidence-based and must not replace a known working structured endpoint with a weaker guess.

### 5.5 Company identity

Company deduplication must use a normalized company identity rather than raw display-name equality only.

Normalization should safely handle simple presentational differences such as:

- casing;
- repeated whitespace;
- punctuation differences;
- common legal suffixes where removal is unambiguous.

Fuzzy similarity alone must not merge clearly distinct companies.

### 5.6 Daily checks

Active, non-paused watched companies are checked on normal scheduled runs.

Jobs discovered through a watched company enter the same normal discovery pipeline as every other source.

The watchlist is a discovery input, not a bypass around ranking or Gemini evaluation.

## 6. Watch health policy

Each watch entry tracks check health.

On a successful check:

- update `last_successful_check_at`;
- reset `consecutive_failures` to zero;
- clear `paused_until`;
- refresh `last_verified_at` when the endpoint itself is verified.

On a failed check:

- increment `consecutive_failures`;
- log the failure without aborting the full run.

The deterministic R2 backoff policy is:

- failures 1 and 2: keep checking on the next normal run;
- failure 3: set `paused_until` to 24 hours after the failed check;
- after the pause expires, allow one normal retry;
- if that retry fails, pause for another 24 hours;
- any successful check clears the failure count and pause state.

This keeps the mechanism simple while preventing a broken endpoint from being hammered on every run.

Manual entries remain preserved through all failure states.

A later known-good endpoint may update and reactivate an unhealthy entry.

## 7. Canonical posting resolution

### 7.1 Goal

When the same job appears through LinkedIn alerts, Gmail, an aggregator, a specialist board, search results, and an employer ATS, R2 should identify the employer/ATS posting as the canonical representation whenever confidence is high.

The original source remains part of provenance and is never discarded.

### 7.2 Resolver order

For each job candidate, attempt resolution in this order:

1. employer/ATS URL already present in source data;
2. HTTP redirects or canonical location reached from the discovered URL;
3. structured employer/job links embedded in the source page;
4. known company ATS/watch metadata plus source job/company/title data;
5. targeted search for the original employer posting using company + role title + supported ATS/careers domains.

The resolver stops once a sufficiently strong canonical match is found.

### 7.3 Confidence and fallback

Canonical replacement requires high confidence.

Signals may include:

- exact or near-exact normalized title;
- exact or normalized company identity;
- compatible location;
- matching source/ATS job identifier;
- redirect chain to employer-owned or supported ATS domain;
- structured metadata linking the source to the employer posting.

If confidence is insufficient:

- retain the original URL as the job's usable URL;
- mark canonical resolution as unresolved/low-confidence for observability;
- continue normal dedupe/scoring using the strongest available identity.

Resolution failure must never reject an otherwise valid job.

## 8. Job URL and provenance model

R2 must preserve where each job was found.

Conceptually, a normalized job has:

```text
jobs
- existing job fields
- canonical_url            nullable/best-known canonical identity
```

and one or more provenance records:

```text
job_sources
- id
- job_id
- source
- source_job_id            nullable
- source_url
- first_seen_at
- last_seen_at
```

The concrete schema must include an idempotent unique source-record identity derived from source + source job ID where available, otherwise source + canonicalized source URL.

A job may therefore preserve provenance such as:

```text
linkedin_email
wellfound
yc
greenhouse
```

while remaining one logical job.

## 9. Cross-source deduplication

Final job identity uses the strongest available evidence in this order:

1. exact canonical URL;
2. ATS provider + ATS job identifier;
3. exact source-independent employer job identifier when trustworthy;
4. normalized employer + normalized role title + compatible location;
5. weaker fuzzy similarity only as a candidate signal requiring stronger corroboration.

Fuzzy similarity alone must never automatically merge jobs.

### 9.1 Late canonical discovery

If a source copy is stored first and a canonical ATS version is discovered later, the bot should enrich/merge the existing logical job instead of creating a second job.

Existing evaluation, material, delivery, and application-event associations must remain attached to the surviving logical job.

### 9.2 Provenance preservation during merge

When duplicate jobs are collapsed:

- preserve all source/provenance records;
- prefer the richer job description;
- prefer employer/ATS canonical URL over aggregator URL;
- preserve the best known company/location/remote metadata;
- do not discard lifecycle/application history.

Merge behavior must be deterministic so rerunning discovery cannot flip the surviving logical record back and forth.

## 10. Specialist-source strategy

R2 uses a curated small set, not a broad scraper catalog.

### 10.1 Initial specialist set

The implementation must cover these three categories:

- YC / Work at a Startup;
- Wellfound;
- one Europe-focused source, with Welcome to the Jungle as the preferred first choice.

For each source, implementation begins by verifying its current public integration surface.

If a stable public structured or semi-structured surface exists, use a dedicated adapter.

If it does not, use targeted public search for that source domain instead.

A source must not be dropped from R2 merely because a direct adapter is inappropriate; targeted-search fallback satisfies the source requirement.

### 10.2 Search-driven specialist discovery

Use targeted search for:

- VC portfolio job boards;
- smaller startup job boards;
- niche European engineering boards;
- the R2 specialist sources when a stable public adapter is unavailable.

Search-driven discovery may generate domain-specific queries such as:

```text
site:<source-domain> "senior frontend engineer"
site:<source-domain> "product engineer" React TypeScript
```

These results still pass through canonical resolution and normal ranking/evaluation.

### 10.3 No authenticated scraping

R2 must not log into LinkedIn, Wellfound, Welcome to the Jungle, or any other source for scraping purposes.

Only public/authorized surfaces may be used.

## 11. Source health and observability

Each source path reports compact health/contribution metrics.

Examples:

```text
source=yc discovered=18 eligible=6 errors=0
source=wellfound discovered=12 eligible=4 errors=1
source=company_watch discovered=21 eligible=8 errors=2
canonical_resolved=31
canonical_unresolved=9
cross_source_duplicates=14
companies_promoted=2
watch_checks=17
watch_paused=1
```

The final metric names may follow existing logging conventions, but logs must make it possible to answer:

- which sources produced jobs;
- which sources failed;
- how many jobs resolved canonically;
- how many duplicate source copies collapsed;
- how many companies were promoted;
- which watch entries are unhealthy or paused.

Logs must not expose private Gmail body content, secrets, CV text, or other unnecessary personal data.

## 12. Fail-open behavior

All R2 discovery paths fail independently.

A failure in any of these must not abort the whole job-hunter run:

- one specialist source;
- one company-watch endpoint;
- one canonical-resolution attempt;
- one targeted search query;
- one employer careers page.

Existing behavior for Gmail and public sources remains fail-open.

Canonical-resolution errors specifically degrade to the original source URL rather than dropping the job.

## 13. Ranking and scoring boundaries

R2 does not create source-specific Gemini scoring rules.

All jobs, regardless of origin, enter the existing profession gate, prefilter, ranking, shortlist budget, Gemini evaluation, and Telegram delivery policy.

Source quality may remain an input to existing ranking, but a specialist/watch source must not automatically outrank a better-matching job solely because of origin.

Company promotion happens only after the strict promotion criteria in section 5.1 are satisfied.

## 14. Manual watch configuration

R2 supports explicit manual seeds so important companies can always be checked.

Manual entries should use the existing YAML configuration surface unless the implementation plan finds a strong compatibility reason not to.

Manual seeds may specify:

- company display name;
- careers URL;
- supported ATS provider + identifier when known.

At runtime they are normalized into the same `company_watch` persistence model used by automatic entries.

Repeated processing of the same manual config must be idempotent.

## 15. Testing strategy

### 15.1 Watchlist unit tests

Verify:

- automatic promotion only after `package_match` or `high_priority` with no blockers and score at least the configured package threshold;
- profession-gate-only jobs do not promote companies;
- possible/weak/skip/blocked jobs do not promote companies;
- duplicate normalized company names do not create duplicate watch entries;
- manual entries are preserved;
- known ATS endpoints are preferred over generic careers URLs;
- later higher-confidence endpoints upgrade watch metadata;
- failures increment health counters;
- the third consecutive failure triggers a 24-hour pause;
- successful checks reset failure and pause state;
- repaired entries can become active again.

### 15.2 Canonical resolver tests

Verify:

- direct ATS URL passthrough;
- redirect-based resolution;
- embedded canonical/employer link extraction;
- company/title ATS matching;
- targeted-search matching;
- confidence gating;
- low-confidence fallback to original URL;
- resolver exceptions fail open;
- wrong-company or incompatible-title results are rejected.

### 15.3 Provenance and dedupe tests

Verify:

- the same canonical URL from multiple sources becomes one logical job;
- ATS provider + job ID collapses duplicates even when source URLs differ;
- company/title/location fallback works only when sufficiently exact;
- fuzzy similarity alone does not merge jobs;
- all source records remain attached after merge;
- late canonical discovery enriches the existing job;
- existing evaluations/materials/application events remain associated with the surviving job;
- reruns are idempotent and do not flip merge winners.

### 15.4 Specialist-source tests

For each dedicated adapter actually selected:

- fixture happy path;
- pagination where applicable;
- empty result;
- malformed response;
- network/API failure;
- source-specific IDs/URLs preserved correctly.

For every specialist source covered through targeted search, test domain-specific query generation and result normalization without assuming every search result is a valid job.

### 15.5 End-to-end discovery tests

Use fixtures representing the same role discovered through combinations such as:

```text
Gmail LinkedIn alert
+ specialist board
+ employer ATS
```

Expected outcome:

- one logical job;
- canonical employer/ATS URL when confidently resolved;
- multiple provenance records;
- one evaluation path;
- one normal delivery path;
- company promotion only after a qualifying strong score.

### 15.6 Failure-isolation tests

Verify that one failed source/watch/resolution attempt does not prevent successful candidates from other sources from reaching the existing pipeline.

## 16. Migration and SQLite compatibility

R2 remains SQLite-first.

New tables or columns must use backward-compatible initialization/migration behavior so existing production state can be restored and upgraded in place.

R2 should avoid leaking raw SQLite assumptions into source/canonical/watch business logic where practical. Persistence operations remain behind the existing store/repository boundary so a later Supabase migration can replace persistence incrementally.

No Supabase client or schema is introduced in R2.

## 17. Operational safety

R2 preserves all existing safety boundaries:

- no automated applications;
- no logged-in browser automation;
- no CAPTCHA/2FA handling;
- no legal/work-authorization attestations;
- no email mutation;
- no LinkedIn credential/cookie storage.

Public website access must remain respectful and bounded. Failed or rate-limited sources should fail open/back off rather than be hammered repeatedly.

## 18. Success criteria

R2 is complete when:

1. A qualifying strong evaluated job can automatically create or update a company watch entry using the existing evaluation thresholds.
2. Weak or merely profession-relevant jobs do not pollute the watchlist.
3. Active watched companies are checked automatically on later runs without manual intervention.
4. Three consecutive watch failures trigger a 24-hour pause, while later success restores normal checking.
5. Supported ATS endpoints are preferred over generic careers pages when confidently known.
6. Discovered jobs retain original source provenance and the best known canonical employer/ATS URL.
7. Jobs arriving through different sources collapse into one logical job when strong identity evidence exists.
8. Late canonical discovery enriches an existing job rather than duplicating it.
9. YC, Wellfound, and one Europe-focused specialist source are covered through stable public adapters or targeted-search fallback without authenticated scraping.
10. Canonical resolution is aggressive but never blocks or discards a valid unresolved job.
11. Source/watch failures remain isolated and the daily job hunt continues.
12. Logs expose source contribution, canonical-resolution, duplicate-collapse, and watch-health metrics.
13. Existing Gmail intelligence, evaluation, cover-letter, Telegram, and SQLite state behavior continues to work.
14. No Supabase dependency or user-driven ingestion is introduced.

## 19. Immediate roadmap after R2

After R2, the next release should continue prioritizing automation over manual ingestion.

Likely future candidates include:

- application-outcome analytics and feedback loops;
- additional automation around application tracking/interview handoff;
- a separately designed incremental Supabase migration slice when it no longer competes with discovery improvements.

User-driven Telegram URL ingestion remains deferred until it becomes a higher priority.

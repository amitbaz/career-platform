# Market-Driven Search Strategy Design

Date: 2026-09-02
Status: Proposed for review
Repository: `amitbaz/job-hunter-bot`
Branch: `feat/market-driven-search-strategy`

## 1. Purpose

The Job Hunter Bot's current AI model and feature set are considered good enough for the present stage. The immediate quality problem is upstream: the bot's search strategy is too narrow relative to the user's actual geographic options, relocation preferences, work authorization, language constraints, role-transition goals, and desired source coverage.

This design replaces the current mostly global/Europe-oriented search policy with a market-driven discovery model. The goal is to improve the quality and variety of discovered jobs without increasing Gemini usage in an uncontrolled way.

The design has six goals:

1. Represent geographic markets explicitly and in priority order in `config/search.yml`.
2. Give each market its own salary, language, remote/relocation, sponsorship, and source-search rules.
3. Expand country-specific and startup-specific discovery coverage, especially for Israel, London, Singapore, New York City, and San Francisco.
4. Broaden role discovery into frontend-heavy Full-Stack transition opportunities without pretending the candidate already has backend seniority.
5. Keep Gemini 3.5 Flash-Lite as the fixed evaluation model and spend its requests only after conservative deterministic filtering and ranking.
6. Add per-market observability so query-budget shares and source coverage can be tuned from real results.

This is a search-quality redesign, not a new application workflow.

## 2. Relationship to the existing R2 discovery architecture

This design extends, rather than replaces, the existing R2 automated-discovery architecture.

R2 remains responsible for:

- existing public source adapters;
- company watch discovery;
- targeted search;
- canonical employer/ATS resolution;
- cross-source provenance;
- deduplication;
- profession gating;
- ranking;
- Gemini evaluation;
- cover-letter generation;
- Telegram delivery.

The new market layer sits around discovery, pre-ranking, and evaluation policy:

```text
Ordered market profiles from config/search.yml
        |
        +--> market-specific query allocation
        +--> market-specific source domains
        +--> market-specific role/query variants
        |
        v
Existing public feeds + targeted search + company watch
        |
        v
Canonical resolution + provenance + cross-source dedupe
        |
        v
Market attribution
        |
        v
Conservative deterministic eligibility checks
        |
        v
Market-aware ranking / shortlist budget
        |
        v
Gemini 3.5 Flash-Lite evaluation with market policy
        |
        v
Existing delivery / cover-letter / watch-promotion flow
```

The design must preserve R2's fail-open and canonicalization behavior.

## 3. Scope

### In scope

- Ordered market profiles in `config/search.yml`.
- Market-specific search-query budget allocation.
- Deterministic query rotation so finite daily query slots cover more role/source combinations over time.
- Market-specific gross base salary floors.
- Market-specific allowed working languages.
- Market-specific remote, hybrid, onsite, relocation, and sponsorship handling.
- Country-specific and startup-specific targeted-search domains.
- Primary market attribution for each discovered logical job.
- Conservative pre-Gemini market eligibility checks.
- Frontend-heavy Full-Stack and mid-level transition-role discovery.
- Per-market search/discovery/evaluation/delivery metrics.
- Backward-compatible migration away from the single global `salary_floor_eur` and global remote-only evaluator assumptions.
- Tests for market config, allocation, attribution, eligibility, role broadening, and observability.

### Out of scope

- Changing the Gemini model.
- Supabase/Postgres migration.
- Replacing SQLite as the bot's current persistence layer.
- Automated job application submission.
- Browser automation or authenticated scraping of job boards.
- LinkedIn login/cookie automation.
- Building a large catalog of fragile direct scrapers.
- A dashboard for search analytics; logs/structured run metrics are sufficient for this iteration.
- Learning-to-rank from application outcomes.
- Contractor/freelance/part-time search support.

## 4. Candidate facts and policy assumptions

The search strategy must encode the following facts and preferences explicitly rather than relying on the model to infer them from free-form profile text.

### 4.1 Work authorization and geography

- The candidate has EU work authorization through Hungarian citizenship.
- The candidate also has Israeli citizenship and may be employed by Israeli companies.
- Germany, especially Berlin, is the preferred home base.
- Remote work is strongly preferred across Germany and Europe.
- Israel is a high-priority market only for remote roles; relocation back to Israel is not desired.
- London is a desired relocation market. Hybrid is acceptable and onsite may be considered for a sufficiently strong role, but visa sponsorship is required.
- Singapore is a desired relocation market. Visa sponsorship is required.
- In the United States, only New York City and San Francisco are active relocation targets. Visa sponsorship is required.
- Amsterdam, Paris, and Barcelona are secondary, selective relocation possibilities rather than primary search markets.

### 4.2 Language

Language requirements are market-specific:

- Germany / EU: English only.
- Israel: Hebrew or English.
- London: English only.
- Singapore: English only.
- New York City / San Francisco: English only.
- Amsterdam / Paris / Barcelona: English only.

A disallowed language is a blocker only when the posting explicitly requires it. A disallowed language described as optional or "nice to have" must not reject the job.

### 4.3 Employment type

The search should target permanent full-time employment.

Explicit freelance, contractor, internship, part-time, temporary, or fixed-term-only roles are out of scope for this search strategy.

### 4.4 Time-zone overlap

Time-zone overlap requirements are informational, not blockers.

A job requiring overlap with Tel Aviv, London, Singapore, EST, PST, or any other working hours should remain eligible unless another explicit rule blocks it. The requirement should be surfaced to the user as a note or warning so the user can decide personally.

## 5. Market priority model

### 5.1 Ordered list is the source of truth

Markets are configured as an ordered list in `config/search.yml`.

The order itself represents priority. Do not add a second independent numeric priority field that can contradict list order.

Initial order:

1. `germany_eu`
2. `israel_remote`
3. `london`
4. `singapore`
5. `us_nyc_sf`
6. `secondary_eu_relocation`

The order influences tie-breaking, query-budget allocation fallback, and ambiguous market attribution.

### 5.2 Initial query-share tuning values

The initial recommended shares are:

```yaml
markets:
  - id: germany_eu
    query_share: 0.35
  - id: israel_remote
    query_share: 0.25
  - id: london
    query_share: 0.17
  - id: singapore
    query_share: 0.10
  - id: us_nyc_sf
    query_share: 0.10
  - id: secondary_eu_relocation
    query_share: 0.03
```

These shares are initial tuning values, not permanent product rules.

With the current `max_search_queries_per_run: 30`, they should produce approximately:

- Germany / EU: 10 queries;
- Israel: 8 queries;
- London: 5 queries;
- Singapore: 3 queries;
- NYC / SF: 3 queries;
- secondary EU relocation: 1 query.

Exact rounding may differ by one query, but allocation must satisfy these invariants:

1. Every enabled market receives at least one query when the total query budget is at least the number of enabled markets.
2. Total allocated queries never exceed `max_search_queries_per_run`.
3. Remaining slots after minimum allocation are distributed by `query_share` using deterministic rounding.
4. Ties are resolved by market list order.

This preserves guaranteed coverage while still reflecting priority.

## 6. Conceptual market configuration

A market profile owns both discovery and evaluation policy.

Conceptually:

```yaml
markets:
  - id: germany_eu
    query_share: 0.35
    locations:
      - Berlin
      - Germany
      - Europe
    allowed_languages:
      - English
    salary:
      currency: EUR
      gross_base_floor: 90000
    remote_policy: preferred
    relocation_policy: selective
    sponsorship_policy: not_required
    source_domains: []
    query_templates: []
```

The final implementation schema may be normalized differently, but the following concepts must remain first-class per market:

- stable market ID;
- query share;
- locations / remote scope;
- allowed required languages;
- currency and gross base salary floor;
- remote/hybrid/onsite policy;
- relocation policy;
- sponsorship policy;
- market-specific source domains;
- market-specific query templates or query dimensions.

Market policy must be available downstream after discovery, not discarded after query generation.

## 7. Market-specific rules

### 7.1 Germany / EU

Priority: highest.

Salary floor:

- EUR 90,000 gross base annually.

Language:

- English required-language roles are valid.
- Roles explicitly requiring German, French, Dutch, Spanish, or another non-English language are blocked.

Work mode:

- Remote is preferred.
- Berlin hybrid roles are valid.
- Strong Germany-based hybrid/onsite roles may remain eligible because relocation within Germany is acceptable, but remote roles should rank higher.
- EU-wide remote roles that permit working from Germany are valid.
- Ordinary local-only roles elsewhere in Europe should not dominate this market; Amsterdam, Paris, and Barcelona selective relocation are handled separately.

Sponsorship:

- Not required for EU employment.

### 7.2 Israel remote

Priority: second.

Salary floor:

- ILS 420,000 gross base annually.
- Equivalent monthly floor: ILS 35,000 gross base.

Language:

- Hebrew or English.

Work mode:

- Remote roles are valid.
- A posting that says "remote" without clarifying whether the employee may live abroad should remain eligible with `international_remote_eligibility = unknown`.
- Explicit international/anywhere remote is a positive signal.
- Explicit requirement to be physically based in Israel is a blocker.
- Hybrid or onsite roles in Israel are blockers because relocation to Israel is not desired.

Employment mechanism:

- Do not require contractor, EOR, or German-entity language in the posting.
- Employment/payroll feasibility is not a search-stage blocker when the posting does not address it.

### 7.3 London

Priority: third.

Salary floor:

- GBP 90,000 gross base annually.

Language:

- English only.

Work mode:

- Remote, hybrid, and strong onsite roles may all be considered.
- Hybrid is explicitly acceptable.
- Country-limited UK remote remains relevant because relocation to London is acceptable.

Sponsorship:

- Required for actual employment.
- Explicit sponsorship available: strong positive.
- Explicit no sponsorship / no visa support: hard blocker.
- Sponsorship not mentioned: keep and mark `sponsorship = unknown`.

### 7.4 Singapore

Priority: fourth.

Salary floor:

- SGD 120,000 gross base annually.
- Equivalent monthly floor: SGD 10,000 gross base.

Language:

- English only.

Work mode:

- Remote, hybrid, or onsite may be considered because relocation is acceptable for a strong opportunity.
- Singapore-limited remote remains relevant if sponsorship/relocation can be resolved.

Sponsorship:

- Required.
- Explicit no sponsorship: blocker.
- Not mentioned: keep as unknown.

### 7.5 United States: NYC / SF

Priority: fifth.

Locations:

- New York City only.
- San Francisco / Bay Area only.

Salary floors:

- NYC: USD 180,000 gross base annually.
- San Francisco / Bay Area: USD 200,000 gross base annually.

Language:

- English only.

Work mode:

- Remote, hybrid, and onsite may be considered only when the role is meaningfully associated with NYC or SF/Bay Area relocation.
- Generic "US remote only" roles should not automatically receive equal priority. Keep them only when the posting/company/location information makes NYC/SF relocation plausible; otherwise down-rank or leave outside the active market.

Sponsorship:

- Required.
- Explicit no sponsorship: blocker.
- Not mentioned: keep as unknown.

### 7.6 Secondary EU relocation

Priority: sixth and deliberately small.

Locations and salary floors:

- Amsterdam: EUR 90,000 gross base annually.
- Paris: EUR 80,000 gross base annually.
- Barcelona: EUR 70,000 gross base annually.

Language:

- English only.

Work mode:

- Hybrid or onsite may be considered because relocation is possible, but this market exists only for unusually strong opportunities.
- These locations should receive less query budget and a lower ranking preference than Germany/EU remote.

Sponsorship:

- Not required due EU citizenship.

## 8. Role strategy

### 8.1 Core role families

Primary role families remain:

- Senior Frontend Engineer;
- Staff Frontend Engineer;
- Frontend Technical Lead / hands-on Frontend Lead;
- Senior Product Engineer;
- Product Engineer;
- Frontend Architect / hands-on Software Architect.

People-management-heavy roles remain blocked.

Engineering Manager remains blocked.

A "Tech Lead" title is valid only when the role remains materially hands-on and is not primarily people management, hiring, performance management, or organization planning.

### 8.2 Full-Stack expansion

The search should deliberately broaden into Full-Stack opportunities, but the candidate's backend experience must be represented conservatively.

The candidate is not currently a backend engineer and should not be evaluated as senior in backend-specific skills.

Acceptable target patterns:

- approximately 70% frontend / 30% backend;
- React/Next.js/TypeScript frontend with Node.js/TypeScript backend exposure;
- REST or GraphQL API work;
- PostgreSQL/Supabase or similar product-database work;
- product-engineering roles where frontend remains a major responsibility;
- backend expectations that can realistically be learned while working.

Potentially acceptable:

- approximately 60% frontend / 40% backend when the backend surface is product-oriented, learnable, and not infrastructure-heavy.

Usually unsuitable:

- 50/50 or backend-dominant roles;
- roles requiring multiple years of deep backend ownership;
- distributed-systems-heavy backend roles;
- Java/Go/Python backend-specialist roles where frontend is secondary;
- infrastructure/platform/backend architecture roles disguised as "full-stack".

Ambiguous Full-Stack roles should not be rejected solely by brittle keyword heuristics. Cheap deterministic ranking may penalize obvious backend-heavy signals, while Gemini should judge nuanced responsibility balance for shortlisted candidates.

### 8.3 Transition roles

Mid-level Full-Stack roles may be included when all of the following are true:

- frontend overlap is strong;
- backend expectations are realistically learnable;
- the role is permanent full-time;
- compensation meets the market-specific salary floor;
- the role offers a credible path toward Full-Stack capability.

These are a lower-priority lane than Senior/Staff Frontend opportunities, not a replacement for them.

## 9. Source strategy

### 9.1 Architecture

Use a hybrid source strategy consistent with R2:

1. retain existing global/public source adapters;
2. expand market-specific coverage primarily through targeted public search;
3. add a dedicated direct adapter only when a source has a stable public surface and produces enough value to justify maintenance.

Do not build a large scraper catalog in this iteration.

### 9.2 Existing global coverage

Continue using the current global/public sources, including the currently implemented remote/public feeds and YC/Hacker News/search paths.

These sources remain market-agnostic inputs. Market attribution occurs after normalization/canonicalization.

### 9.3 Candidate market-specific discovery sources

The initial source research identified the following high-value candidates.

Germany / EU:

- Welcome to the Jungle;
- Wellfound;
- Berlin-focused startup job sources;
- relevant VC portfolio job boards such as European venture portfolios;
- YC Europe / company career pages / supported ATS domains.

Israel:

- TechAviv Jobs;
- DevJobs Israel;
- GotFriends;
- YC Tel Aviv / Israeli startup listings;
- Startup.jobs Israel/Tel Aviv;
- Raz Technologies as a secondary aggregation/discovery source rather than a core dependency;
- direct company careers and ATS pages.

London:

- Welcome to the Jungle;
- WorkVisaJobs UK for sponsorship-oriented discovery;
- Built In;
- Wellfound;
- YC London;
- direct company careers and ATS pages.

Singapore:

- NodeFlair;
- JobStreet Singapore;
- Glints;
- MyCareersFuture;
- Built In where relevant;
- direct company careers and ATS pages.

NYC / San Francisco:

- Built In;
- Wellfound;
- Y Combinator / Work at a Startup;
- Startup.jobs;
- direct company careers and ATS pages.

The implementation plan must verify each candidate source's current public/authorized integration surface before choosing direct adapter vs targeted-search coverage. A source may be dropped or replaced if it no longer provides a usable public surface.

### 9.4 Targeted-search preference

Targeted search is preferred when:

- the source is publicly indexable;
- stable structured API access is unavailable;
- a direct scraper would be brittle;
- source-specific queries can reliably surface relevant job pages.

Example query dimensions may include:

```text
site:<domain> "senior frontend engineer" <market term>
site:<domain> "product engineer" React TypeScript <market term>
site:<domain> "full stack engineer" React Node <market term>
site:<domain> "frontend engineer" visa sponsorship London
```

Do not require every query to mention every preference. Query rotation should spread dimensions over time.

## 10. Query generation and rotation

### 10.1 Finite budget

`max_search_queries_per_run` remains the global cap for public search queries.

Market expansion must not bypass this cap.

### 10.2 Per-market allocation

Generate a deterministic number of slots for each enabled market using the allocation rules in section 5.2.

Then fill each market's slots from combinations of:

- role family;
- source domain;
- location / remote scope;
- sponsorship term where relevant;
- frontend-heavy Full-Stack variant;
- general ATS/careers search.

### 10.3 Deterministic rotation

Do not add persistent query-rotation database state in the first version.

Instead, rotate combinations deterministically using a stable day/run seed, for example local date plus market ID. The exact algorithm belongs in the implementation plan, but it must guarantee:

- stable/reproducible query order for a given run seed;
- variation across days;
- no duplicate query strings within a run;
- each market receives its allocated slots;
- high-priority role/source combinations appear more frequently than exploratory combinations.

This allows broad multi-day coverage without adding another persistence subsystem.

## 11. Market attribution

A discovered logical job may be compatible with more than one market, but it must have one primary market for policy evaluation and observability.

Determine the primary market using this ordered evidence:

1. explicit job location match;
2. explicit remote-country/region scope;
3. explicit sponsorship or relocation language;
4. source/query market context as supporting evidence;
5. if still ambiguous, first compatible market in configured market order.

Examples:

- A London hybrid job discovered from both Wellfound and a generic ATS search maps to `london`.
- A UK-company job explicitly allowing remote work from Germany maps to `germany_eu`, not automatically `london`.
- An Israeli remote role with no international-remote statement maps to `israel_remote` with international remote eligibility unknown.
- A fully remote Europe role discovered by an Israeli-focused query should still map to `germany_eu` if its actual scope is Europe/Germany.

Source/query origin alone must never override stronger job-location evidence.

The primary market ID should be preserved with the job's current discovery/evaluation context so downstream salary, language, remote, and sponsorship policy can use the correct market.

## 12. Conservative pre-Gemini eligibility policy

The pre-Gemini layer exists to save requests on explicit incompatibilities, not to make nuanced career decisions.

### 12.1 Hard reject before Gemini when explicit

Reject when the posting explicitly establishes any of the following:

- wrong profession / already-blocked profession family;
- people-management-heavy blocked role;
- permanent full-time requirement is not met;
- required working language is disallowed for the attributed market;
- disclosed gross base salary maximum is below that market's floor;
- sponsorship is explicitly unavailable in a market where sponsorship is required;
- Israel role is explicitly onsite/hybrid or requires physical residence in Israel;
- another market-specific work-mode rule is explicitly incompatible.

### 12.2 Keep unknowns

Do not reject when the posting simply omits information.

Keep and mark appropriately when:

- salary is not disclosed;
- sponsorship is not mentioned;
- Israeli international-remote eligibility is unclear;
- relocation details are unclear;
- time-zone overlap is required;
- work authorization wording is ambiguous.

Unknown must remain distinct from yes/no.

### 12.3 Ranking penalties instead of blockers

Use deterministic ranking penalties, not hard rejection, for:

- ambiguous backend-heavy Full-Stack roles;
- secondary relocation markets;
- remote/time-zone inconvenience;
- generic US-remote roles with weak NYC/SF relevance;
- Germany roles that are less remote than preferred but still feasible.

Gemini receives the strongest ambiguous candidates after this stage.

## 13. Market-aware Gemini evaluation

Gemini 3.5 Flash-Lite remains the fixed model.

The evaluator must stop using one global instruction that says every non-remote or relocation-required role is a blocker.

Instead, the evaluation prompt receives the attributed market's policy, including:

- gross base salary floor and currency;
- allowed required languages;
- remote/hybrid/onsite acceptance;
- relocation allowance;
- sponsorship requirement and current known status;
- market-specific unknown handling;
- candidate backend-transition context for Full-Stack roles.

Gemini should not infer a different market policy from prose when structured market policy is available.

The evaluator must continue to treat disclosed salary maximum below the relevant floor as a blocker, while missing salary remains unknown rather than blocked.

## 14. Gemini usage control

Expanding discovery does not mean evaluating every discovered job.

The existing bounded shortlist remains the main protection:

```text
more source/search coverage
        -> more raw candidates
        -> canonicalize / dedupe
        -> deterministic market eligibility
        -> rank
        -> select at most max_jobs_per_run fresh candidates
        -> Gemini evaluation
```

`max_jobs_per_run` continues to cap newly selected jobs sent to Gemini in a normal run, subject to existing pending-work/quota behavior.

High-scoring jobs may still trigger a separate Gemini cover-letter request as today.

The design does not increase Gemini limits merely because more discovery sources are added.

## 15. Observability and tuning

Add compact per-market metrics to normal run logging.

At minimum make it possible to answer, per market:

- query slots allocated;
- queries actually executed;
- raw jobs discovered;
- unique jobs after dedupe where attribution is known;
- deterministic eligibility rejections;
- ranked eligible jobs;
- selected jobs sent to Gemini;
- package/high-priority/possible/skip/blocked outcomes;
- delivered jobs.

Useful log shapes may resemble:

```text
market=germany_eu queries=10 raw=84 eligible=19 selected=11 delivered=4
market=israel_remote queries=8 raw=61 eligible=14 selected=8 delivered=3
market=london queries=5 raw=39 eligible=7 selected=5 delivered=2
```

Also preserve existing source-level observability. Market metrics supplement source metrics; they do not replace them.

This creates the feedback loop needed to tune `query_share` based on real output rather than intuition.

No analytics dashboard is required in this iteration.

## 16. Configuration migration and compatibility

The current config contains global fields such as:

- `salary_floor_eur`;
- `role_families`;
- `search_query_templates`;
- `search_domains`;
- `specialist_search_domains`;
- `specialist_query_templates`.

The implementation should migrate toward market-owned search configuration without breaking existing config loading abruptly.

The implementation plan must choose a clear compatibility strategy. Preferred direction:

1. add the new `markets` schema;
2. make market-driven query generation authoritative when `markets` exists;
3. preserve old fields temporarily only as a backward-compatible fallback for tests/local configs;
4. remove or deprecate redundant global search fields once no production path depends on them.

There must not be two active, independently maintained search strategies producing contradictory behavior.

The single global `salary_floor_eur` must no longer control market-specific evaluation when a market profile is attributed.

## 17. Failure behavior

The market layer remains fail-open where uncertainty is informational and fail-closed only for explicit incompatibility.

Failures must not abort the whole run when:

- one market-specific targeted query fails;
- one market source is unavailable;
- one job cannot be attributed with high confidence;
- sponsorship/remote eligibility is unknown;
- one query-rotation combination produces no results.

If market attribution is truly unresolved, the implementation should preserve the job for conservative fallback ranking/evaluation rather than silently discard it, unless another explicit prefilter blocker applies.

## 18. Testing strategy

### 18.1 Config tests

Verify:

- ordered markets parse deterministically;
- invalid duplicate market IDs fail config validation;
- query shares are non-negative;
- enabled markets receive valid salary/currency/language/work-mode policy;
- city-specific salary floors are represented correctly for NYC/SF and secondary EU cities;
- backward-compatible legacy config behavior is explicit and tested.

### 18.2 Query allocation tests

Verify:

- the 30-query initial budget produces the intended approximate 10/8/5/3/3/1 split;
- every enabled market receives at least one slot when possible;
- total allocation never exceeds the global cap;
- deterministic rounding resolves ties by market order;
- changing the global cap scales allocation without hard-coded per-market counts.

### 18.3 Rotation tests

Verify:

- same seed produces same query set/order;
- different dates rotate combinations;
- duplicate queries are removed;
- each market receives its allocated number of unique queries when enough combinations exist;
- specialist/source-domain coverage rotates across days.

### 18.4 Attribution tests

Verify examples including:

- London hybrid -> `london`;
- UK company, remote Germany/EU -> `germany_eu`;
- Israeli remote with international status omitted -> `israel_remote`;
- Singapore onsite with sponsorship unknown -> `singapore`;
- NYC role -> NYC salary policy;
- SF role -> SF salary policy;
- Amsterdam/Paris/Barcelona -> `secondary_eu_relocation` with city-specific floor;
- ambiguous source-origin conflict resolved by actual job-location evidence.

### 18.5 Eligibility tests

Verify:

- Berlin job explicitly requiring German is rejected;
- Berlin job with German only as nice-to-have survives;
- Israeli Hebrew job survives;
- Israeli onsite job is rejected;
- Israeli remote job with international eligibility omitted survives as unknown;
- London no-sponsorship job is rejected;
- London sponsorship omitted survives as unknown;
- Singapore sponsorship omitted survives;
- NYC/SF explicit no-sponsorship is rejected;
- time-zone overlap never hard-rejects by itself;
- missing salary survives;
- disclosed maximum below the correct market floor rejects.

### 18.6 Full-Stack tests

Verify:

- clearly frontend-heavy React/Node Full-Stack role survives prefilter;
- mid-level frontend-heavy transition role can survive when salary meets floor;
- explicitly backend-dominant Full-Stack role is rejected or heavily down-ranked according to deterministic evidence;
- ambiguous FE/BE balance reaches Gemini rather than being over-filtered;
- Engineering Manager remains blocked;
- hands-on Architect remains in scope.

### 18.7 Pipeline tests

Verify:

- market ID/policy survives discovery through evaluation;
- canonical dedupe across sources does not create duplicate market evaluations;
- a richer canonical copy may improve attribution metadata without losing provenance;
- selected fresh jobs remain bounded by existing `max_jobs_per_run` behavior;
- market metrics coexist with source metrics;
- one market/search failure does not abort other markets.

## 19. Rollout strategy

Roll out in one implementation branch but keep behavioral tuning config-driven.

Recommended rollout sequence:

1. introduce market config/model and market attribution;
2. make evaluator market-aware;
3. add market-budgeted query generation/rotation;
4. add targeted domains for the approved markets;
5. add deterministic eligibility rules;
6. add per-market observability;
7. run dry-run/tests and compare candidate mix before enabling normal schedule;
8. tune query shares from observed results without code changes.

Dedicated new source adapters are optional follow-up work when source performance data justifies them. The first rollout should favor targeted search over brittle scraping.

## 20. Success criteria

The design is successful when:

1. The bot searches all approved markets every normal run within a bounded public-search budget.
2. Germany/EU remains highest priority while Israel, London, Singapore, and NYC/SF receive guaranteed meaningful coverage.
3. Market-specific salary/language/sponsorship/remote rules replace the current one-size-fits-all evaluation assumptions.
4. Strong Israeli remote opportunities are no longer missed because search is Europe-centric.
5. Sponsorship-unknown London/Singapore/US roles remain visible instead of being prematurely discarded.
6. Explicit no-sponsorship roles in sponsorship-required markets are filtered before Gemini where deterministically known.
7. The bot discovers frontend-heavy Full-Stack transition opportunities without flooding results with backend-specialist roles.
8. New market/source breadth does not remove the existing Gemini shortlist cap.
9. Per-market logs make query-share tuning evidence-based.
10. Existing canonicalization, provenance, company watch, Gmail, Telegram, and cover-letter flows continue to work.

## 21. Explicit design decisions

The following decisions are considered approved unless this spec is revised during review:

- Market-driven discovery is preferred over source-driven or one-global-query-pool designs.
- Market list order is the source of truth for geographic priority.
- Query shares are tunable config values, not hard-coded counts.
- Every enabled market receives guaranteed search coverage when budget permits.
- Languages are defined per market, not globally.
- Salary thresholds are market-adjusted gross base floors, not direct FX conversions.
- Unknown salary, sponsorship, international-remote eligibility, and time-zone overlap do not become blockers merely because they are unknown.
- Explicit incompatibilities may be rejected before Gemini.
- Israel is remote-only with no relocation back to Israel.
- London, Singapore, NYC, and SF allow relocation; sponsorship is required but omission is treated as unknown.
- Amsterdam, Paris, and Barcelona are secondary/selective relocation markets.
- Full-Stack discovery is broadened to frontend-heavy and learnable-backend roles, including selected mid-level transition roles.
- Engineering Manager / people-management-heavy roles remain blocked.
- Hands-on Architect roles remain in scope.
- Company size and industry are not search filters.
- Permanent full-time employment is required.
- Gemini 3.5 Flash-Lite remains the fixed model for this redesign.
- Source expansion should prefer targeted public search first and dedicated adapters only when justified by stability/value.

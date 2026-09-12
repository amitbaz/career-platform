# Market-Driven Search Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the one-size-fits-all Europe/remote search policy with ordered, market-specific discovery and evaluation rules that improve job quality/variety while keeping Gemini usage bounded.

**Architecture:** Add first-class market policies under `SearchPolicy`, generate market-tagged targeted-search queries within the existing global query cap, attribute each logical job to one primary market, and pass that market policy through deterministic eligibility, ranking, Gemini evaluation, Telegram notes, and per-market logging. Preserve the existing R2 canonicalization/provenance/watchlist/Gmail/cover-letter/Telegram pipeline.

**Tech Stack:** Python 3.12+, dataclasses, PyYAML, SQLite, requests/BeautifulSoup, pytest, existing Gemini/Telegram pipeline.

**Spec:** `docs/superpowers/specs/2026-09-02-market-driven-search-strategy-design.md`

## Global Constraints

- Market list order in `config/search.yml` is the single source of truth for geographic priority.
- Initial market order: `germany_eu`, `israel_remote`, `london`, `singapore`, `us_nyc_sf`, `secondary_eu_relocation`.
- Initial query shares: `0.35`, `0.25`, `0.17`, `0.10`, `0.10`, `0.03`.
- `max_search_queries_per_run` remains the global public-search cap; every enabled market gets at least one query when the budget permits it.
- Gross base salary floors: Germany/EU EUR 90,000; Israel ILS 420,000/year; London GBP 90,000; Singapore SGD 120,000; NYC USD 180,000; SF/Bay Area USD 200,000; Amsterdam EUR 90,000; Paris EUR 80,000; Barcelona EUR 70,000.
- Required-language policy: English only for Germany/EU, London, Singapore, US, and secondary EU; Israel allows Hebrew or English.
- Unknown salary, sponsorship, international-remote eligibility, relocation detail, and time-zone overlap are not blockers merely because they are unknown.
- Israel is remote-only; explicit Israel onsite/hybrid or required physical residence in Israel is a blocker.
- London, Singapore, NYC, and SF allow relocation; sponsorship is required, explicit no-sponsorship is a blocker, omitted sponsorship is `unknown`.
- Permanent full-time employment only; explicit freelance, contractor, internship, part-time, temporary, or fixed-term-only roles are blocked.
- Full-Stack discovery includes frontend-heavy/learnable-backend roles and selected mid-level transition roles; backend-dominant jobs must not flood the shortlist.
- Engineering Manager/people-management-heavy roles remain blocked; hands-on Architect roles remain in scope.
- Company size and industry remain unfiltered.
- Market-specific source expansion uses targeted public search in this iteration; no authenticated scraping and no new direct scraper catalog.
- `max_jobs_per_run` remains the fresh Gemini shortlist cap.
- Do not modify Gemini client/quota plumbing or model selection. Production continues using the currently configured Gemini 3.5 Flash-Lite via `GEMINI_MODEL`.
- Supabase/Postgres migration is out of scope; remain SQLite-first.
- When `markets` exists, market-driven query generation is authoritative. Legacy global search fields are fallback-only when `markets` is absent.

---

## File Map

### Create

- `src/job_hunter/market_policy.py` — market lookup, attribution, market ordering, city-specific salary floor.
- `src/job_hunter/market_eligibility.py` — deterministic employment/language/salary/sponsorship/work-mode checks and unknown statuses.
- `tests/market_fixtures.py` — shared market-aware `SearchPolicy` builders for tests.
- `tests/test_market_policy.py`
- `tests/test_market_eligibility.py`
- `tests/test_duckduckgo_source.py`

### Modify

- `config/search.yml`
- `src/job_hunter/models.py`
- `src/job_hunter/config.py`
- `src/job_hunter/discovery_queries.py`
- `src/job_hunter/sources/duckduckgo.py`
- `src/job_hunter/sources/__init__.py`
- `src/job_hunter/store.py`
- `src/job_hunter/discovery.py`
- `src/job_hunter/prefilter.py`
- `src/job_hunter/ranking.py`
- `src/job_hunter/evaluation.py`
- `src/job_hunter/pipeline.py`
- `src/job_hunter/telegram_navigation.py`
- `tests/test_config.py`
- `tests/test_discovery_queries.py`
- `tests/test_prefilter.py`
- `tests/test_ranking.py`
- `tests/test_evaluation.py`
- `tests/test_store.py`
- `tests/test_discovery.py`
- `tests/test_pipeline.py`
- `tests/test_navigation_store.py`
- `tests/test_telegram_navigation.py`
- `README.md`

---

### Task 1: Add market policy models, fixtures, strict config parsing, and production market config

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/config.py`
- Modify: `config/search.yml`
- Create: `tests/market_fixtures.py`
- Modify: `tests/test_config.py`

**Interfaces:**
- Produces `SalaryPolicy`, `MarketPolicy`, `SearchQuery`, `SearchPolicy.markets`.
- Preserves `salary_floor_eur` and legacy global search fields for configs without `markets`.

- [ ] **Step 1: Write failing config tests**

Add to `tests/test_config.py`:

```python
def test_load_settings_parses_markets_in_declared_order(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: germany_eu\n"
        "    query_share: 0.35\n"
        "    locations: [Berlin, Germany, Europe]\n"
        "    allowed_languages: [English]\n"
        "    salary: {currency: EUR, gross_base_floor: 90000}\n"
        "    remote_policy: preferred\n"
        "    relocation_policy: selective\n"
        "    sponsorship_policy: not_required\n"
        "  - id: israel_remote\n"
        "    query_share: 0.25\n"
        "    locations: [Israel, Tel Aviv]\n"
        "    allowed_languages: [English, Hebrew]\n"
        "    salary: {currency: ILS, gross_base_floor: 420000}\n"
        "    remote_policy: required\n"
        "    relocation_policy: none\n"
        "    sponsorship_policy: not_required\n"
    )

    settings = load_settings(cfg)

    assert [market.id for market in settings.policy.markets] == [
        "germany_eu",
        "israel_remote",
    ]
    assert settings.policy.markets[1].allowed_languages == ["English", "Hebrew"]
    assert settings.policy.markets[1].salary.gross_base_floor == 420000


def test_load_settings_rejects_duplicate_market_ids(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - {id: london, query_share: 0.5, locations: [London], allowed_languages: [English], salary: {currency: GBP, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: required}\n"
        "  - {id: london, query_share: 0.5, locations: [London], allowed_languages: [English], salary: {currency: GBP, gross_base_floor: 90000}, remote_policy: allowed, relocation_policy: allowed, sponsorship_policy: required}\n"
    )

    with pytest.raises(ValueError, match="duplicate market id: london"):
        load_settings(cfg)
```

Add parameterized failures for negative `query_share`, empty currency, non-positive salary floors, unknown fields, and invalid policy enum values.

- [ ] **Step 2: Run config tests and confirm failure**

```bash
python -m pytest tests/test_config.py -q
```

Expected: new tests fail because market models/parsing do not exist.

- [ ] **Step 3: Add market dataclasses and parser**

In `models.py`:

```python
@dataclass(slots=True)
class SalaryPolicy:
    currency: str
    gross_base_floor: int
    location_floors: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class MarketPolicy:
    id: str
    query_share: float
    locations: list[str]
    allowed_languages: list[str]
    salary: SalaryPolicy
    remote_policy: str
    relocation_policy: str
    sponsorship_policy: str
    source_domains: list[str] = field(default_factory=list)
    query_templates: list[str] = field(default_factory=list)
    role_families: list[str] = field(default_factory=list)
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    market_id: str | None = None
```

Add to `SearchPolicy`:

```python
markets: list[MarketPolicy] = field(default_factory=list)
```

In `config.py` define exact enum sets:

```python
_REMOTE_POLICIES = {"preferred", "required", "allowed"}
_RELOCATION_POLICIES = {"none", "selective", "allowed"}
_SPONSORSHIP_POLICIES = {"not_required", "required"}
```

Implement `_parse_markets(entries)` with strict field validation, duplicate-ID rejection, non-negative query shares, positive salary floors, and nested `location_floors` validation. Wire it into `load_settings()` as:

```python
markets=_parse_markets(data.get("markets", [])),
```

- [ ] **Step 4: Create reusable market test fixtures**

Create `tests/market_fixtures.py`:

```python
from job_hunter.models import MarketPolicy, SalaryPolicy, SearchPolicy


def make_market(
    market_id: str,
    share: float,
    *,
    locations: list[str] | None = None,
    currency: str = "EUR",
    floor: int = 90000,
    location_floors: dict[str, int] | None = None,
    languages: list[str] | None = None,
    remote_policy: str = "allowed",
    relocation_policy: str = "allowed",
    sponsorship_policy: str = "not_required",
    domains: list[str] | None = None,
    templates: list[str] | None = None,
) -> MarketPolicy:
    return MarketPolicy(
        id=market_id,
        query_share=share,
        locations=locations or [market_id],
        allowed_languages=languages or ["English"],
        salary=SalaryPolicy(
            currency=currency,
            gross_base_floor=floor,
            location_floors=location_floors or {},
        ),
        remote_policy=remote_policy,
        relocation_policy=relocation_policy,
        sponsorship_policy=sponsorship_policy,
        source_domains=domains or ["wellfound.com", "jobs.ashbyhq.com"],
        query_templates=templates or ['"{role}" React TypeScript'],
    )


def make_market_policy(*, max_queries: int = 30) -> SearchPolicy:
    return SearchPolicy(
        target_titles=[
            "senior frontend engineer",
            "staff frontend engineer",
            "senior product engineer",
            "product engineer",
            "full-stack engineer",
        ],
        positive_keywords=["react", "typescript", "next.js", "node.js", "graphql"],
        blocked_title_keywords=["junior", "qa", "devops", "engineering manager"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        role_families=[
            "senior frontend engineer",
            "staff frontend engineer",
            "senior product engineer",
            "product engineer",
            "full-stack engineer",
        ],
        max_search_queries_per_run=max_queries,
        markets=[
            make_market("germany_eu", 0.35, locations=["Berlin", "Germany", "Europe"], remote_policy="preferred", relocation_policy="selective"),
            make_market("israel_remote", 0.25, locations=["Israel", "Tel Aviv"], currency="ILS", floor=420000, languages=["English", "Hebrew"], remote_policy="required", relocation_policy="none"),
            make_market("london", 0.17, locations=["London", "UK", "United Kingdom"], currency="GBP", floor=90000, sponsorship_policy="required"),
            make_market("singapore", 0.10, locations=["Singapore"], currency="SGD", floor=120000, sponsorship_policy="required"),
            make_market("us_nyc_sf", 0.10, locations=["New York", "NYC", "San Francisco", "Bay Area"], currency="USD", floor=180000, location_floors={"San Francisco": 200000, "Bay Area": 200000}, sponsorship_policy="required"),
            make_market("secondary_eu_relocation", 0.03, locations=["Amsterdam", "Paris", "Barcelona"], floor=70000, location_floors={"Amsterdam": 90000, "Paris": 80000, "Barcelona": 70000}, relocation_policy="selective"),
        ],
    )
```

- [ ] **Step 5: Update `config/search.yml` to the approved strategy**

Keep global `role_families` ordered as:

```yaml
role_families:
  - senior frontend engineer
  - staff frontend engineer
  - frontend technical lead
  - frontend lead
  - senior product engineer
  - product engineer
  - frontend architect
  - software architect
  - senior full-stack engineer
  - full-stack product engineer
  - full-stack engineer
  - ai product engineer
```

Keep `engineering manager` blocked and add pure backend title phrases:

```yaml
  - backend engineer
  - back-end engineer
  - backend developer
  - back-end developer
```

Add the six markets in the approved order with the exact salary/language/work-mode/sponsorship values from the spec. Use these targeted-search domains:

```text
germany_eu: wellfound.com, welcometothejungle.com, app.welcometothejungle.com, jobs.ashbyhq.com, jobs.lever.co, boards.greenhouse.io, ycombinator.com
israel_remote: jobs.techaviv.com, devjobs.co.il, gotfriends.co.il, startup.jobs, jobs.ashbyhq.com, jobs.lever.co, boards.greenhouse.io, ycombinator.com
london: workvisajobs.co.uk, welcometothejungle.com, builtin.com, wellfound.com, ycombinator.com, jobs.ashbyhq.com, jobs.lever.co, boards.greenhouse.io
singapore: nodeflair.com, sg.jobstreet.com, glints.com, mycareersfuture.gov.sg, builtin.com, jobs.ashbyhq.com, jobs.lever.co, boards.greenhouse.io
us_nyc_sf: builtin.com, wellfound.com, ycombinator.com, startup.jobs, jobs.ashbyhq.com, jobs.lever.co, boards.greenhouse.io
secondary_eu_relocation: wellfound.com, welcometothejungle.com, jobs.ashbyhq.com, jobs.lever.co, boards.greenhouse.io
```

Use query templates that explicitly cover remote Germany/Europe, remote Israel/Tel Aviv, London+sponsorship, Singapore+sponsorship, NYC/SF+sponsorship, and Amsterdam/Paris/Barcelona+English. Do not add direct adapters in this task.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest tests/test_config.py -q
```

Expected: PASS.

```bash
git add src/job_hunter/models.py src/job_hunter/config.py config/search.yml tests/market_fixtures.py tests/test_config.py
git commit -m "feat: add market search policy config"
```

---

### Task 2: Allocate and rotate market-tagged search queries

**Files:**
- Modify: `src/job_hunter/discovery_queries.py`
- Modify: `src/job_hunter/sources/duckduckgo.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `src/job_hunter/models.py`
- Modify: `tests/test_discovery_queries.py`
- Create: `tests/test_duckduckgo_source.py`

**Interfaces:**
- Produces `allocate_market_query_slots(markets, budget) -> dict[str, int]`.
- Produces `generate_search_queries(policy, run_date=None) -> list[SearchQuery]`.
- `DuckDuckGoSource` consumes `SearchQuery | str`, sets `Job.market_hint`, and exposes planned/attempted/succeeded counts by market.

- [ ] **Step 1: Write failing allocation/rotation tests**

In `tests/test_discovery_queries.py` import `make_market_policy` from `tests.market_fixtures` and add:

```python
from datetime import date

from tests.market_fixtures import make_market_policy


def test_initial_30_query_allocation_matches_approved_shape():
    policy = make_market_policy(max_queries=30)
    assert allocate_market_query_slots(policy.markets, 30) == {
        "germany_eu": 10,
        "israel_remote": 8,
        "london": 5,
        "singapore": 3,
        "us_nyc_sf": 3,
        "secondary_eu_relocation": 1,
    }


def test_same_day_query_rotation_is_stable_and_next_day_changes():
    policy = make_market_policy(max_queries=12)
    first = generate_search_queries(policy, date(2026, 9, 2))
    repeated = generate_search_queries(policy, date(2026, 9, 2))
    next_day = generate_search_queries(policy, date(2026, 9, 3))
    assert first == repeated
    assert first != next_day
    assert len({query.text for query in first}) == len(first)
    assert all(query.market_id for query in first)
```

Also test budget `< market count`, zero budget, and legacy config without `markets` returning `SearchQuery(..., market_id=None)`.

- [ ] **Step 2: Run and confirm failure**

```bash
python -m pytest tests/test_discovery_queries.py -q
```

- [ ] **Step 3: Implement deterministic allocation and rotation**

Keep current logic as `_generate_legacy_search_queries()` and make it return `SearchQuery` objects. For market mode:

1. Start each enabled market at `max(1, round(query_share * budget))` when budget >= enabled market count.
2. If total is too high, remove from the most over-allocated market without going below 1; ties resolve by list order.
3. If total is too low, add to the most under-allocated market; ties resolve by list order.
4. When budget < market count, allocate one to the first `budget` markets.

Use a stable date rotation key:

```python
def _rotation_key(run_date: date, market_id: str, text: str) -> str:
    return hashlib.sha256(
        f"{run_date.isoformat()}:{market_id}:{text}".encode("utf-8")
    ).hexdigest()
```

Build candidates from `market.role_families or policy.role_families`, `market.query_templates`, and `market.source_domains`. Include both direct queries and `site:<domain>` variants. Preserve role/template order as the priority tier and use the hash only to rotate equal-priority combinations. Remove duplicate text within a run and take exactly each market's allocated unique count when enough combinations exist.

- [ ] **Step 4: Add market hint fields and DuckDuckGo stats**

Add to `Job`:

```python
market_hint: str | None = None
market_id: str | None = None
```

In `sources/duckduckgo.py` add:

```python
@dataclass(slots=True)
class DuckDuckGoStats:
    planned_by_market: dict[str, int] = field(default_factory=dict)
    attempted_by_market: dict[str, int] = field(default_factory=dict)
    succeeded_by_market: dict[str, int] = field(default_factory=dict)
```

Normalize raw-string constructor inputs to `SearchQuery(text=raw)`. For each query, use `query.text`; set `Job.market_hint=query.market_id`; increment stats under `query.market_id or "legacy"`.

Create `tests/test_duckduckgo_source.py` with a fake HTTP client asserting the request query text and returned job hint:

```python
def test_duckduckgo_preserves_query_market_hint():
    source = DuckDuckGoSource(
        FakeHttp('<a class="result__a" href="https://example.test/job">Senior FE</a>'),
        [SearchQuery('"senior frontend engineer" London', "london")],
    )
    jobs = source.discover()
    assert jobs[0].market_hint == "london"
    assert source.stats.attempted_by_market == {"london": 1}
    assert source.stats.succeeded_by_market == {"london": 1}
```

Define `FakeHttp/FakeResponse` directly in this new test file.

- [ ] **Step 5: Make source construction accept a deterministic query date**

Change:

```python
def build_sources(settings, http, search_breaker=None, query_date: date | None = None):
```

and pass `generate_search_queries(settings.policy, query_date)` to DuckDuckGo.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest tests/test_discovery_queries.py tests/test_duckduckgo_source.py -q
```

Expected: PASS.

```bash
git add src/job_hunter/models.py src/job_hunter/discovery_queries.py src/job_hunter/sources/duckduckgo.py src/job_hunter/sources/__init__.py tests/test_discovery_queries.py tests/test_duckduckgo_source.py
git commit -m "feat: allocate market search queries"
```

---

### Task 3: Attribute and persist one primary market

**Files:**
- Create: `src/job_hunter/market_policy.py`
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/store.py`
- Create: `tests/test_market_policy.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Produces `market_by_id(policy, market_id)`.
- Produces `salary_floor_for_job(job, market)`.
- Produces `attribute_market(job, markets)`.
- Produces `JobStore.set_job_market(job_id, market_id)`.
- Persists `jobs.market_id` and `evaluations.market_id`.

- [ ] **Step 1: Write failing attribution tests**

Create `tests/test_market_policy.py`:

```python
from job_hunter.market_policy import attribute_market, market_by_id, salary_floor_for_job
from job_hunter.models import Job
from tests.market_fixtures import make_market_policy


def test_london_hybrid_maps_to_london():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="London, UK - Hybrid", remote=False)
    assert attribute_market(job, policy.markets) == "london"


def test_remote_germany_beats_london_query_hint():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="Remote Germany", remote=True, market_hint="london")
    assert attribute_market(job, policy.markets) == "germany_eu"


def test_city_specific_salary_floors():
    policy = make_market_policy()
    us = market_by_id(policy, "us_nyc_sf")
    secondary = market_by_id(policy, "secondary_eu_relocation")
    assert salary_floor_for_job(Job(source="x", title="x", location="New York City"), us) == 180000
    assert salary_floor_for_job(Job(source="x", title="x", location="San Francisco Bay Area"), us) == 200000
    assert salary_floor_for_job(Job(source="x", title="x", location="Amsterdam"), secondary) == 90000
    assert salary_floor_for_job(Job(source="x", title="x", location="Paris"), secondary) == 80000
    assert salary_floor_for_job(Job(source="x", title="x", location="Barcelona"), secondary) == 70000
```

Also cover Israeli remote, Singapore onsite, Paris, and ambiguous `Remote Europe`.

- [ ] **Step 2: Write failing store migration/round-trip tests**

In `tests/test_store.py` add assertions that both `jobs` and `evaluations` gain `market_id`, then:

```python
def test_job_market_round_trip(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_logical_job(Job(source="x", title="Senior Frontend Engineer", location="London"))
    store.set_job_market(job_id, "london")
    assert store.get_job(job_id).market_id == "london"
```

Save an `Evaluation(..., market_id="london")` and assert `get_evaluation()` returns it.

- [ ] **Step 3: Run and confirm failure**

```bash
python -m pytest tests/test_market_policy.py tests/test_store.py -q
```

- [ ] **Step 4: Implement attribution helpers**

In `market_policy.py`, normalize text and score evidence in this order:

```text
400 explicit job.location match
300 explicit remote country/region scope in location/description
200 explicit sponsorship/relocation language tied to a market
100 Job.market_hint
fallback first compatible enabled market in configured order
```

Query hint must never override stronger actual location evidence. A remote role with no usable location can fall back to the first enabled market; market uncertainty alone must not drop the job.

Implement city floor matching against normalized `job.location`, with `market.salary.gross_base_floor` as fallback.

- [ ] **Step 5: Add SQLite/model persistence**

Add `market_id: str = ""` to `Evaluation`. Extend the existing jobs migration dictionary with:

```python
"market_id": "TEXT NOT NULL DEFAULT ''",
```

Add a separate evaluation migration dictionary:

```python
_MARKET_EVALUATION_COLUMNS = {"market_id": "TEXT NOT NULL DEFAULT ''"}
```

Call it after `_CREATE_EVALUATIONS`. Update all job/evaluation row mappers and evaluation save/load SQL. Add:

```python
def set_job_market(self, job_id: int, market_id: str | None) -> None:
    with self._conn:
        self._conn.execute(
            "UPDATE jobs SET market_id = ? WHERE id = ?",
            (market_id or "", job_id),
        )
```

Do not include market ID in job identity/dedupe keys.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest tests/test_market_policy.py tests/test_store.py -q
```

```bash
git add src/job_hunter/market_policy.py src/job_hunter/models.py src/job_hunter/store.py tests/test_market_policy.py tests/test_store.py
git commit -m "feat: attribute and persist job markets"
```

---

### Task 4: Add conservative market-specific pre-Gemini eligibility

**Files:**
- Create: `src/job_hunter/market_eligibility.py`
- Modify: `src/job_hunter/prefilter.py`
- Create: `tests/test_market_eligibility.py`
- Modify: `tests/test_prefilter.py`

**Interfaces:**
- Produces `MarketEligibilityResult`.
- Produces `evaluate_market_eligibility(job, market)`.
- Changes `prefilter_job(job, policy, market=None)`.

- [ ] **Step 1: Write failing blocker/unknown tests**

Create `tests/test_market_eligibility.py`; each test creates `policy = make_market_policy()` and selects a market with `market_by_id()`.

Required cases:

```python
def test_berlin_german_required_is_blocked():
    policy = make_market_policy()
    market = market_by_id(policy, "germany_eu")
    result = evaluate_market_eligibility(
        Job(source="x", title="Senior Frontend Engineer", location="Berlin", description="Fluent German is required. English is used daily."),
        market,
    )
    assert result.allowed is False
    assert result.reason_code == "required_language"


def test_berlin_german_nice_to_have_survives():
    policy = make_market_policy()
    market = market_by_id(policy, "germany_eu")
    result = evaluate_market_eligibility(
        Job(source="x", title="Senior Frontend Engineer", location="Berlin", description="English required. German is a nice to have."),
        market,
    )
    assert result.allowed is True


def test_london_sponsorship_omitted_is_unknown():
    policy = make_market_policy()
    market = market_by_id(policy, "london")
    result = evaluate_market_eligibility(Job(source="x", title="Senior Frontend Engineer", location="London"), market)
    assert result.allowed is True
    assert result.sponsorship_status == "unknown"
```

Also test: Israeli Hebrew allowed; Israel remote with international scope omitted -> unknown/allowed; Israel onsite/hybrid -> blocked; London explicit no sponsorship -> blocked; Singapore sponsorship omitted -> allowed; NYC/SF explicit no sponsorship -> blocked; time-zone overlap -> warning only; salary missing -> allowed; London GBP 80k base max -> blocked; Singapore SGD 9k/month -> blocked; Israel ILS 34k/month -> blocked; NYC USD 190k -> allowed; SF USD 190k -> blocked; explicit freelance/contractor/part-time/fixed-term/internship -> blocked.

- [ ] **Step 2: Run and confirm failure**

```bash
python -m pytest tests/test_market_eligibility.py tests/test_prefilter.py -q
```

- [ ] **Step 3: Implement conservative eligibility/status extraction**

Create:

```python
@dataclass(frozen=True, slots=True)
class MarketEligibilityResult:
    allowed: bool
    reason_code: str = "passed"
    reason: str = "passed market eligibility"
    sponsorship_status: str = "not_applicable"
    international_remote_status: str = "not_applicable"
    warnings: tuple[str, ...] = ()
    disclosed_salary_max: int | None = None
```

Use sentence-level language detection. Treat a non-allowed language as required only when the same sentence includes strong markers (`required`, `must speak`, `fluent`, `professional proficiency`, `C1`, `C2`) and does not include optional markers (`nice to have`, `preferred`, `plus`, `bonus`, `advantage`). Include at least German, French, Dutch, Spanish, Italian, Portuguese, Polish, Swedish, Danish, Norwegian, Finnish, Mandarin/Chinese, Japanese, Korean, Arabic, Russian, Turkish in the known-language vocabulary.

Use explicit sponsorship negatives:

```python
_NO_SPONSORSHIP = (
    "no visa sponsorship",
    "cannot sponsor",
    "unable to sponsor",
    "without sponsorship",
    "must already be authorized to work",
    "must have the right to work",
)
```

and positives:

```python
_SPONSORSHIP_AVAILABLE = (
    "visa sponsorship available",
    "visa sponsorship provided",
    "we sponsor",
    "skilled worker sponsorship",
    "employment pass sponsorship",
)
```

Time-zone overlap/core-hours sentences become warnings, never blockers.

Employment type blocks only explicit unambiguous terms: `freelance`, `contractor`, `part-time`, `internship`, `fixed-term`, `fixed term`, `temporary role`. Do not block generic `employment contract` wording.

For Israel, `must be based in Israel`, explicit onsite, or explicit hybrid blocks. `worldwide/global/anywhere/international remote` marks international remote available; otherwise remote Israel remains unknown.

- [ ] **Step 4: Implement conservative gross-base salary extraction**

Only compare salary when the sentence clearly says `base salary`, `base pay`, `gross base`, or a salary range and the market currency is identifiable. Ignore total-comp/equity-only text.

Currency markers:

```python
_CURRENCY_MARKERS = {
    "EUR": ("€", "eur"),
    "GBP": ("£", "gbp"),
    "ILS": ("₪", "ils", "nis"),
    "SGD": ("s$", "sgd"),
    "USD": ("$", "usd", "us$"),
}
```

Expand `k`, multiply explicit monthly amounts by 12, take the maximum base amount, and compare against `salary_floor_for_job(job, market)`. If currency/period/base nature is not provable, return unknown and do not block.

- [ ] **Step 5: Make prefilter market-aware**

Change:

```python
def prefilter_job(job: Job, policy: SearchPolicy, market: MarketPolicy | None = None) -> PrefilterResult:
```

Keep blocked title/profession and relevance checks. If `market is None`, preserve the old remote-only fallback. If a market exists, remove the global non-remote blocker and delegate explicit market incompatibility to `evaluate_market_eligibility()`.

Do not hard-reject ambiguous Full-Stack FE/BE balance here.

- [ ] **Step 6: Run tests and commit**

```bash
python -m pytest tests/test_market_eligibility.py tests/test_prefilter.py -q
```

```bash
git add src/job_hunter/market_eligibility.py src/job_hunter/prefilter.py tests/test_market_eligibility.py tests/test_prefilter.py
git commit -m "feat: add market eligibility rules"
```

---

### Task 5: Make ranking market-aware and Full-Stack-transition-aware

**Files:**
- Modify: `src/job_hunter/ranking.py`
- Modify: `tests/test_ranking.py`

**Interfaces:**
- Produces `market_priority_bonus(job, policy)`.
- Produces bounded `_backend_transition_penalty(job)`.
- Preserves source-diversity selection.

- [ ] **Step 1: Write failing ranking tests**

Import `make_market_policy` from `tests.market_fixtures` and add:

```python
def test_frontend_heavy_full_stack_beats_backend_heavy_full_stack():
    policy = make_market_policy()
    preferences = make_preferences()
    frontend_heavy = Job(source="wellfound", title="Full-Stack Engineer", market_id="germany_eu", description="React Next.js TypeScript frontend, Node.js REST APIs and PostgreSQL")
    backend_heavy = Job(source="wellfound", title="Full-Stack Engineer", market_id="germany_eu", description="Go Java Kubernetes distributed systems event-driven backend architecture")
    assert profile_priority_score(frontend_heavy, preferences, policy) > profile_priority_score(backend_heavy, preferences, policy)


def test_london_hybrid_gets_nonzero_location_fit():
    policy = make_market_policy()
    job = Job(source="x", title="Senior Frontend Engineer", location="London - Hybrid", remote=False, market_id="london", description="React TypeScript")
    assert profile_priority_score(job, make_preferences(), policy) > 0
```

Add one test proving a much stronger London role can outrank a weaker Germany role so market priority is a bonus, not an absolute partition.

- [ ] **Step 2: Run and confirm failure**

```bash
python -m pytest tests/test_ranking.py -q
```

- [ ] **Step 3: Add market-aware location score and modest priority bonus**

Use:

```python
def market_priority_bonus(job: Job, policy: SearchPolicy) -> int:
    if not job.market_id:
        return 0
    for index, market in enumerate(policy.markets):
        if market.id == job.market_id:
            return max(0, len(policy.markets) - index)
    return 0
```

When a market exists, do not require `job.remote` to give location points. Germany/EU remote gets highest fit; feasible Berlin/Germany local gets lower positive fit. Relocation markets get positive fit for explicit target-city hybrid/onsite. Israel onsite never reaches ranking because eligibility blocks it.

- [ ] **Step 4: Add bounded Full-Stack backend-heavy penalty and targeted-source quality**

Only apply to titles containing `full stack`/`full-stack`.

Frontend signals:

```python
("react", "next.js", "nextjs", "frontend", "front-end", "typescript", "design system")
```

Backend-heavy signals:

```python
("distributed systems", "kubernetes", "golang", "java", "event-driven architecture", "backend architecture", "high-throughput", "message queues")
```

Penalty: 15 when no frontend signals and >=2 backend-heavy signals; 6 when >=1 frontend and >=2 backend-heavy; otherwise 0. Subtract it before clamping score to 0..100.

Give approved specialist-board URLs a source-quality score below direct ATS but above generic web results: Wellfound, TechAviv, DevJobs, WorkVisaJobs, NodeFlair, JobStreet SG, MyCareersFuture, Built In, Startup.jobs, YC.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/test_ranking.py -q
```

```bash
git add src/job_hunter/ranking.py tests/test_ranking.py
git commit -m "feat: rank jobs by market and transition fit"
```

---

### Task 6: Make Gemini evaluation market-aware

**Files:**
- Modify: `src/job_hunter/evaluation.py`
- Modify: `src/job_hunter/models.py`
- Modify: `tests/test_evaluation.py`

**Interfaces:**
- Keeps `evaluate_job(job, context, policy, gemini)` signature.
- Consumes attributed market policy and deterministic eligibility statuses.
- Produces `Evaluation.market_id` and market-aware `location_note` expectations.

- [ ] **Step 1: Write failing evaluator prompt tests**

Import `make_market_policy` and add tests asserting:

```python
# London hybrid prompt contains GBP 90000, relocation allowed, sponsorship required,
# and omitted sponsorship is unknown rather than a blocker.
# SF prompt contains USD 200000.
# Full-Stack prompt states senior frontend strength and "do not invent senior backend".
# Legacy policy without markets still mentions policy.salary_floor_eur and remote-only fallback.
```

Use the existing `FakeGemini`, `_valid_payload()`, and `context` fixture already present in `tests/test_evaluation.py`.

- [ ] **Step 2: Run and confirm failure**

```bash
python -m pytest tests/test_evaluation.py -q
```

- [ ] **Step 3: Build structured market prompt rules**

Resolve:

```python
market = market_by_id(policy, job.market_id)
```

When present, include:

```text
Market ID
Gross base salary floor + currency
Allowed required languages
Remote policy
Relocation policy
Sponsorship policy
Deterministic sponsorship status
Deterministic international-remote status
Deterministic warnings
```

Prompt rules must state:

```text
Missing salary is unknown, not a blocker.
Disclosed gross base max below market floor is a blocker.
Hybrid/onsite/relocation is not a blocker when market policy allows it.
Explicit no-sponsorship is a blocker when sponsorship is required; omission is unknown.
Disallowed language blocks only when explicitly required; nice-to-have does not.
Time-zone overlap is informational and must be preserved in location_note.
Sponsorship/international-remote uncertainty must be preserved in location_note.
```

For Full-Stack roles append:

```text
The candidate is a senior frontend engineer but is earlier than junior-level in backend depth today. Treat React/Next.js/TypeScript ownership as senior evidence. Node.js/TypeScript APIs, REST/GraphQL, PostgreSQL/Supabase and similar product-backend work may be realistic ramp-up areas. Do not invent senior backend experience. Backend-dominant ownership is a gap and may make the role unsuitable.
```

If no market is attributed, keep legacy global prompt behavior.

- [ ] **Step 4: Store market ID on evaluation without changing Gemini resource controls**

Return:

```python
market_id=job.market_id or "",
```

Do not change `purpose="job_evaluation"`, `thinking_level="low"`, `max_output_tokens=1200`, JSON mode, thresholds, model, or quota tracking.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/test_evaluation.py -q
```

```bash
git add src/job_hunter/evaluation.py src/job_hunter/models.py tests/test_evaluation.py
git commit -m "feat: evaluate jobs with market policy"
```

---

### Task 7: Surface sponsorship/remote/time-zone notes in Telegram cards

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/telegram_navigation.py`
- Modify: `tests/test_navigation_store.py`
- Modify: `tests/test_telegram_navigation.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Adds default `market_id`/`market_note` to `DigestItem` and `NavigationCard`.
- Existing stored cards without new keys remain loadable because fields have defaults.

- [ ] **Step 1: Write failing card/backward-compatibility tests**

In `tests/test_telegram_navigation.py`:

```python
def test_navigation_card_surfaces_market_note():
    card = NavigationCard(
        job_id=1,
        title="Senior Frontend Engineer",
        company="Acme",
        location="London",
        score=91,
        url="https://example.test/job",
        market_id="london",
        market_note="Visa sponsorship: unknown. Requires 4 hours overlap with EST.",
    )
    text, _ = build_navigation_card(card, "session", 0, 1)
    assert "Visa sponsorship: unknown" in text
    assert "4 hours overlap with EST" in text
```

In `tests/test_navigation_store.py`, manually insert legacy `cards_json` without the two new keys and assert `get_navigation_session()` loads a card with empty defaults.

- [ ] **Step 2: Run and confirm failure**

```bash
python -m pytest tests/test_telegram_navigation.py tests/test_navigation_store.py -q
```

- [ ] **Step 3: Add note fields and wire evaluation location note through the pipeline**

Add:

```python
market_id: str = ""
market_note: str = ""
```

to `DigestItem` and `NavigationCard` after existing required fields.

Every `DigestItem(...)` construction in `pipeline.py` must include:

```python
market_id=evaluation.market_id or job.market_id or "",
market_note=evaluation.location_note or "",
```

including normal evaluation, pending-delivery requeue, and pending-cover-letter paths. `_build_navigation_session()` copies both fields to `NavigationCard`.

- [ ] **Step 4: Render note compactly**

In `build_navigation_card()` append:

```python
note_line = f"\nNote: {card.market_note}" if card.market_note else ""
```

and include `note_line` after Match. Telegram remains a renderer; no market business logic belongs there.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/test_telegram_navigation.py tests/test_navigation_store.py tests/test_pipeline.py -q
```

```bash
git add src/job_hunter/models.py src/job_hunter/pipeline.py src/job_hunter/telegram_navigation.py tests/test_navigation_store.py tests/test_telegram_navigation.py tests/test_pipeline.py
git commit -m "feat: surface market notes in job cards"
```

---

### Task 8: Integrate market attribution/eligibility into discovery and add per-market observability

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- `collect_candidates()` attributes market after enrichment and uses market-aware prefilter.
- `DiscoveryStats` adds per-market raw/unique/rejected/eligible counters.
- `_evaluate_and_deliver_job()` returns `(promoted, quota_blocked, decision_or_none)`.
- Pipeline logs query/discovery/selection/evaluation/delivery counts per market.

- [ ] **Step 1: Write failing discovery integration tests**

`tests/test_discovery.py` already defines `FakeSource`, `NoOpHttp`, `store`, and `policy`. Add a separate market policy fixture in this file:

```python
@pytest.fixture
def market_policy():
    return make_market_policy()
```

Then add:

```python
def test_collect_candidates_attributes_market_before_prefilter(store, market_policy):
    source = FakeSource([
        Job(
            source="fake",
            title="Senior Frontend Engineer",
            location="London - Hybrid",
            remote=False,
            description="React TypeScript. Visa sponsorship available.",
        )
    ])
    result = collect_candidates([source], store, NoOpHttp(), market_policy)
    assert len(result.eligible) == 1
    job_id, job = result.eligible[0]
    assert job.market_id == "london"
    assert store.get_job(job_id).market_id == "london"
    assert result.stats.eligible_by_market == {"london": 1}
```

Also add: Israel onsite rejected; unknown-market remote survives; canonical duplicate remains one logical job/market evaluation.

- [ ] **Step 2: Write failing pipeline metrics/budget tests**

`tests/test_pipeline.py` already defines `FakeSource`, `FakeGemini`, `_job`, Settings helpers, and injected pipeline tests. Use those existing helpers plus `make_market_policy()` to create a Settings object. With `caplog`, assert a line containing:

```text
market=london queries_planned=... queries_attempted=... raw=... eligible=... selected=... high_priority=... delivered=...
```

Inject more eligible jobs than `max_jobs_per_run` and assert `fake_gemini.eval_calls <= max_jobs_per_run` for fresh work.

- [ ] **Step 3: Run and confirm failure**

```bash
python -m pytest tests/test_discovery.py tests/test_pipeline.py -q
```

- [ ] **Step 4: Integrate attribution before prefilter and re-attribution after canonical resolution**

Extend `DiscoveryStats`:

```python
raw_by_market: dict[str, int] = field(default_factory=dict)
unique_by_market: dict[str, int] = field(default_factory=dict)
rejected_by_market: dict[str, int] = field(default_factory=dict)
eligible_by_market: dict[str, int] = field(default_factory=dict)
```

After enrichment/upsert:

```python
job.market_id = attribute_market(job, policy.markets) if policy.markets else None
if job.market_id:
    store.set_job_market(job_id, job.market_id)
market = market_by_id(policy, job.market_id)
prefilter_result = prefilter_job(job, policy, market)
```

Count raw jobs using `market_hint` or cheap attribution when possible. Count unique/rejected/eligible under the final attributed market or `unattributed`.

After canonical resolution, re-run attribution before final append so stronger real location evidence can replace an earlier query hint. Attribution uncertainty alone must not drop a job.

- [ ] **Step 5: Pass configured-local date to query rotation**

In `run_pipeline()`:

```python
query_date = datetime.now(ZoneInfo(settings.timezone)).date()
base_sources = sources if sources is not None else build_sources(
    settings,
    http,
    search_breaker=search_breaker,
    query_date=query_date,
)
```

- [ ] **Step 6: Track and log market counts**

Add `_market_counts()` for ranked/selected items. Change `_evaluate_and_deliver_job()` to return the newly produced decision or `None`; update its current callers accordingly. Aggregate evaluation outcomes by `(market_id, decision)`.

Aggregate DuckDuckGo `planned_by_market`, `attempted_by_market`, `succeeded_by_market` from `base_sources`. Count delivered `DigestItem`s by `market_id`.

Log one line per configured market:

```text
market=<id> queries_planned=<n> queries_attempted=<n> queries_succeeded=<n> raw=<n> unique=<n> rejected=<n> eligible=<n> selected=<n> high_priority=<n> package_match=<n> possible_match=<n> skip=<n> blocked=<n> delivered=<n>
```

Keep all existing source-level logs.

- [ ] **Step 7: Run and commit**

```bash
python -m pytest tests/test_discovery.py tests/test_pipeline.py -q
```

```bash
git add src/job_hunter/discovery.py src/job_hunter/pipeline.py src/job_hunter/sources/__init__.py tests/test_discovery.py tests/test_pipeline.py
git commit -m "feat: integrate market discovery pipeline"
```

---

### Task 9: Document tuning and run full verification

**Files:**
- Modify: `README.md`
- Verify: full `tests/` suite

- [ ] **Step 1: Document market-driven search**

Add a README section explaining:

```markdown
### Market-driven search

When `markets:` exists in `config/search.yml`, it is authoritative.
List order is priority order. `query_share` divides the bounded
`max_search_queries_per_run` budget, while every enabled market receives
at least one slot when the budget permits it.

Each market owns locations, required languages, gross base salary floor,
remote/relocation behavior, sponsorship policy, source domains, and query templates.
Unknown salary/sponsorship is not rejection; explicit incompatibility is.
```

Include a six-market table with the approved salary floors and explain that normal tuning should change `query_share`, source-domain order, or query-template order in config rather than code.

- [ ] **Step 2: Run focused feature tests**

```bash
python -m pytest \
  tests/test_config.py \
  tests/test_discovery_queries.py \
  tests/test_duckduckgo_source.py \
  tests/test_market_policy.py \
  tests/test_market_eligibility.py \
  tests/test_prefilter.py \
  tests/test_ranking.py \
  tests/test_evaluation.py \
  tests/test_store.py \
  tests/test_discovery.py \
  tests/test_pipeline.py \
  tests/test_navigation_store.py \
  tests/test_telegram_navigation.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run complete regression suite**

```bash
python -m pytest -q
```

Expected: zero failures, including existing canonicalization/provenance/watchlist/Gmail/Gemini-guardrail/cover-letter/Telegram tests.

- [ ] **Step 4: Preview generated queries without starting the pipeline**

With the normal project environment loaded:

```bash
python - <<'PY'
from datetime import date
from pathlib import Path
from job_hunter.config import load_settings
from job_hunter.discovery_queries import allocate_market_query_slots, generate_search_queries

settings = load_settings(Path("config/search.yml"))
allocation = allocate_market_query_slots(settings.policy.markets, settings.policy.max_search_queries_per_run)
print(allocation)
queries = generate_search_queries(settings.policy, date(2026, 9, 2))
for query in queries:
    print(f"{query.market_id}: {query.text}")
assert len(queries) == len({query.text for query in queries})
PY
```

Expected 30-query allocation:

```text
{'germany_eu': 10, 'israel_remote': 8, 'london': 5, 'singapore': 3, 'us_nyc_sf': 3, 'secondary_eu_relocation': 1}
```

- [ ] **Step 5: Run one guarded production-environment dry run as the acceptance check**

This command makes real discovery and Gemini calls even though Telegram delivery is disabled, so run it only after tests pass and under the existing free-tier guardrails:

```bash
JOB_HUNTER_DRY_RUN=1 python -m job_hunter run --config config/search.yml
```

Expected: no Telegram delivery, no uncaught exception, existing Gemini guardrails active, source logs preserved, one per-market log line for all six markets, fresh evaluations bounded by `max_jobs_per_run`, and unknown sponsorship/international-remote/time-zone details retained as notes rather than silent rejections.

- [ ] **Step 6: Commit docs after verification**

```bash
git diff --check
git status --short
git add README.md
git commit -m "docs: explain market search tuning"
```

---

## Self-Review Coverage

- Ordered markets/query shares: Task 1.
- Country/startup targeted-search expansion: Tasks 1-2.
- Bounded query allocation/daily rotation: Task 2.
- Primary market attribution/city salary floors/persistence: Task 3.
- Explicit blocker vs unknown behavior: Task 4.
- Frontend-heavy Full-Stack transition lane: Tasks 1, 5, 6.
- Market-aware Gemini prompt without model/quota changes: Task 6.
- User-visible sponsorship/international-remote/time-zone notes: Task 7.
- Existing shortlist/Gemini cap and per-market observability: Task 8.
- Legacy fallback and R2/Gmail/Telegram regression safety: Tasks 1-3, 8-9.
- No Supabase migration/authenticated scraping/new scraper catalog: Global Constraints.

## Expected Commit Sequence

```text
feat: add market search policy config
feat: allocate market search queries
feat: attribute and persist job markets
feat: add market eligibility rules
feat: rank jobs by market and transition fit
feat: evaluate jobs with market policy
feat: surface market notes in job cards
feat: integrate market discovery pipeline
docs: explain market search tuning
```

Each implementation commit must pass the focused tests in its task before proceeding.
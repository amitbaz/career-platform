# Source Coverage Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn market-specific source configuration into real job ingestion by adding persistent ATS learning/scanning plus direct DevJobs and Wellfound adapters, while using Brave only as scarce source discovery and making source quality measurable.

**Architecture:** Keep the existing discovery/dedupe/market/prefilter/Gemini pipeline intact. Add a persistent ATS registry learned from every job URL, a composite learned-ATS source that reuses the existing Ashby/Lever/Greenhouse adapters, and two direct HTML adapters for the highest-confidence Phase 1 sources. Replace ambiguous `source_domains` semantics with explicit `direct_sources` and `discovery_domains`; market web search runs only through the metered Brave allocation, never through DuckDuckGo fallback.

**Tech Stack:** Python 3.12+, dataclasses, PyYAML, SQLite, requests via existing `HttpClient`, BeautifulSoup, pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-source-coverage-expansion-design.md`

## Global Constraints

- This plan implements **Phase 1 only**: ATS registry/harvesting/scanning, DevJobs, Wellfound, Brave-as-source-discovery, and source-quality telemetry.
- Phase 2 sources (JobStreet, Built In, GotFriends, Glints, TechAviv direct adapter, NodeFlair, MyCareersFuture, WorkVisaJobs) are out of scope for this plan.
- Preserve current market order, query shares, salary floors, language rules, sponsorship rules, relocation rules, role targeting, `max_jobs_per_run`, Gemini model/quota settings, Telegram behavior, Gmail behavior, and SQLite state restore workflow.
- Existing broad feeds remain enabled: Remotive, Arbeitnow, Jobicy, Himalayas, RemoteOK, WeWorkRemotely, Hacker News, and YC.
- Existing static `ats:` config remains supported and additive to learned ATS boards.
- Learned ATS scan cap defaults to `75` boards per run.
- Learned ATS board failures are isolated per board; one failed board is paused for 24 hours and never fails the whole run.
- Ordinary rediscovery of a paused ATS board updates `last_seen_at` but **must not clear an unexpired pause**. The board becomes due naturally after `paused_until`.
- The existing Brave persisted monthly budget remains `250` by default and is not increased.
- DuckDuckGo is removed only from market-specific targeted discovery. Canonical-resolution search is not redesigned in this plan.
- Direct source adapters use normal `HttpClient` HTTP and server-rendered HTML; no browser automation, authenticated scraping, or new paid API.
- A configured `direct_sources` entry must correspond to an adapter instantiated by code. A `discovery_domains` entry is search/seed-only and must never be counted as direct source coverage.
- DevJobs and Wellfound parsers use deterministic synthetic HTML fixtures in tests and fail open on markup/network errors.
- Do not change pre-Gemini relevance filtering or candidate-context extraction in this feature.

---

## File Map

### Create

- `src/job_hunter/ats_registry.py` — ATS reference harvesting and deterministic board selection.
- `src/job_hunter/sources/learned_ats.py` — scan due learned ATS boards through existing Ashby/Lever/Greenhouse adapters and persist board health.
- `src/job_hunter/sources/devjobs.py` — direct Israel DevJobs adapter.
- `src/job_hunter/sources/wellfound.py` — direct Wellfound role/location adapter.
- `tests/test_ats_registry.py`
- `tests/test_learned_ats_source.py`
- `tests/test_devjobs_source.py`
- `tests/test_wellfound_source.py`

### Modify

- `config/search.yml`
- `src/job_hunter/models.py`
- `src/job_hunter/config.py`
- `src/job_hunter/discovery_queries.py`
- `src/job_hunter/discovery.py`
- `src/job_hunter/store.py`
- `src/job_hunter/sources/__init__.py`
- `src/job_hunter/pipeline.py`
- `tests/market_fixtures.py`
- `tests/test_config.py`
- `tests/test_discovery_queries.py`
- `tests/test_discovery.py`
- `tests/test_sources.py`
- `tests/test_store.py`
- `tests/test_pipeline.py`

---

### Task 1: Make source semantics explicit in config

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/config.py`
- Modify: `src/job_hunter/discovery_queries.py`
- Modify: `config/search.yml`
- Modify: `tests/market_fixtures.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_discovery_queries.py`

**Interfaces:**
- `MarketPolicy.direct_sources: list[str]`
- `MarketPolicy.discovery_domains: list[str]`
- `SearchPolicy.max_learned_ats_boards_per_run: int = 75`
- Legacy YAML `source_domains` is accepted only as an alias for `discovery_domains`; if both keys are supplied for one market, loading raises `ValueError`.
- `generate_search_queries()` uses only `market.discovery_domains` for site-qualified variants.

- [ ] **Step 1: Write failing config tests**

In `tests/test_config.py`, reuse `_set_required_bot_env()` and write complete temporary configs directly:

```python
def test_market_source_config_distinguishes_direct_and_discovery(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: israel_remote\n"
        "    query_share: 1.0\n"
        "    locations: [Israel]\n"
        "    allowed_languages: [English, Hebrew]\n"
        "    salary: {currency: ILS, gross_base_floor: 420000}\n"
        "    remote_policy: required\n"
        "    relocation_policy: none\n"
        "    sponsorship_policy: not_required\n"
        "    direct_sources: [devjobs]\n"
        "    discovery_domains: [jobs.techaviv.com, jobs.ashbyhq.com]\n"
    )

    market = load_settings(cfg).policy.markets[0]

    assert market.direct_sources == ["devjobs"]
    assert market.discovery_domains == ["jobs.techaviv.com", "jobs.ashbyhq.com"]


def test_legacy_source_domains_are_discovery_only(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: london\n"
        "    query_share: 1.0\n"
        "    locations: [London]\n"
        "    allowed_languages: [English]\n"
        "    salary: {currency: GBP, gross_base_floor: 90000}\n"
        "    remote_policy: allowed\n"
        "    relocation_policy: allowed\n"
        "    sponsorship_policy: required\n"
        "    source_domains: [wellfound.com]\n"
    )

    market = load_settings(cfg).policy.markets[0]

    assert market.direct_sources == []
    assert market.discovery_domains == ["wellfound.com"]


def test_market_rejects_source_domains_and_discovery_domains_together(monkeypatch, tmp_path):
    _set_required_bot_env(monkeypatch)
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        "thresholds: {package: 75, possible: 65}\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "markets:\n"
        "  - id: london\n"
        "    query_share: 1.0\n"
        "    locations: [London]\n"
        "    allowed_languages: [English]\n"
        "    salary: {currency: GBP, gross_base_floor: 90000}\n"
        "    remote_policy: allowed\n"
        "    relocation_policy: allowed\n"
        "    sponsorship_policy: required\n"
        "    source_domains: [wellfound.com]\n"
        "    discovery_domains: [jobs.ashbyhq.com]\n"
    )

    with pytest.raises(ValueError, match="cannot define both source_domains and discovery_domains"):
        load_settings(cfg)
```

Also add a default assertion to an existing default-settings test:

```python
assert settings.policy.max_learned_ats_boards_per_run == 75
```

- [ ] **Step 2: Write failing query-generation test**

Update `tests/market_fixtures.py::make_market()` to accept the new arguments only after the red test is in place. In `tests/test_discovery_queries.py`, construct:

```python
market = make_market(
    "london",
    1.0,
    direct_sources=["wellfound"],
    discovery_domains=["jobs.ashbyhq.com"],
)
policy = SearchPolicy(
    target_titles=[],
    positive_keywords=[],
    blocked_title_keywords=[],
    salary_floor_eur=90000,
    thresholds={"package": 75, "possible": 65},
    role_families=["senior frontend engineer"],
    max_search_queries_per_run=2,
    markets=[market],
)
queries = generate_search_queries(policy, date(2026, 9, 3))
texts = [query.text for query in queries]
assert any("site:jobs.ashbyhq.com" in text for text in texts)
assert all("site:wellfound" not in text for text in texts)
```

- [ ] **Step 3: Run red**

```bash
python -m pytest tests/test_config.py tests/test_discovery_queries.py -q
```

Expected: FAIL because the new fields do not exist.

- [ ] **Step 4: Implement the model/parser migration**

Replace `MarketPolicy.source_domains` with:

```python
direct_sources: list[str] = field(default_factory=list)
discovery_domains: list[str] = field(default_factory=list)
```

Add to `SearchPolicy`:

```python
max_learned_ats_boards_per_run: int = 75
```

In `_parse_markets()`:

```python
if "source_domains" in entry and "discovery_domains" in entry:
    raise ValueError(
        f"markets[{index}] cannot define both source_domains and discovery_domains"
    )
discovery_domains = entry.get(
    "discovery_domains",
    entry.get("source_domains", []),
)
```

Allow `direct_sources`, `discovery_domains`, and legacy `source_domains` in strict field validation. Parse `max_learned_ats_boards_per_run` from top-level YAML and reject non-positive/non-integer values.

Change `generate_search_queries()` to:

```python
variants = [base] + [
    f"site:{domain} {base}" for domain in market.discovery_domains
]
```

- [ ] **Step 5: Migrate production config without policy drift**

Use:

```yaml
max_learned_ats_boards_per_run: 75
```

Phase-1 direct-source mapping:

```yaml
# germany_eu
direct_sources: [wellfound]

# israel_remote
direct_sources: [devjobs]

# london
direct_sources: [wellfound]

# singapore
direct_sources: []

# us_nyc_sf
direct_sources: [wellfound]

# secondary_eu_relocation
direct_sources: []
```

Rename each existing `source_domains` block to `discovery_domains`. Keep ATS domains there so Brave can discover reusable boards. Do not alter any market priority, salary, language, sponsorship, relocation, or remote rule.

- [ ] **Step 6: Update shared fixtures, run, commit**

Update `make_market()` to accept:

```python
direct_sources: list[str] | None = None,
discovery_domains: list[str] | None = None,
```

and construct:

```python
direct_sources=direct_sources or [],
discovery_domains=discovery_domains or ["wellfound.com", "jobs.ashbyhq.com"],
```

Then:

```bash
python -m pytest tests/test_config.py tests/test_discovery_queries.py -q
git add config/search.yml src/job_hunter/models.py src/job_hunter/config.py src/job_hunter/discovery_queries.py tests/market_fixtures.py tests/test_config.py tests/test_discovery_queries.py
git commit -m "feat: distinguish direct and discovery sources"
```

---

### Task 2: Add persistent ATS registry storage and health state

**Files:**
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/models.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- `AtsRegistryEntry` dataclass.
- `JobStore.upsert_ats_board(provider, board_identifier, company_name="", market_hint="") -> bool`
- `JobStore.list_due_ats_boards(now: datetime) -> list[AtsRegistryEntry]`
- `JobStore.record_ats_scan_success(provider, board_identifier, now, job_count) -> None`
- `JobStore.record_ats_scan_failure(provider, board_identifier, now) -> None`
- `JobStore.record_ats_eligible_job(provider, board_identifier, now) -> None`
- `JobStore.count_ats_boards() -> int`

- [ ] **Step 1: Write failing schema/upsert tests**

```python
def test_ats_registry_upsert_is_provider_board_unique():
    store = JobStore(":memory:")

    created = store.upsert_ats_board(
        provider="ashby",
        board_identifier="omnea",
        company_name="Omnea",
        market_hint="london",
    )
    repeated = store.upsert_ats_board(
        provider="ashby",
        board_identifier="omnea",
        company_name="Omnea Ltd",
        market_hint="london",
    )

    assert created is True
    assert repeated is False
    assert store.count_ats_boards() == 1


def test_ats_registry_rejects_unsupported_provider():
    store = JobStore(":memory:")
    with pytest.raises(ValueError, match="unsupported ATS provider"):
        store.upsert_ats_board(provider="workday", board_identifier="x")
```

- [ ] **Step 2: Write failing pause semantics test**

```python
def test_ats_failure_pauses_board_without_rediscovery_bypassing_pause():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="acme")

    store.record_ats_scan_failure("lever", "acme", now)
    store.upsert_ats_board(provider="lever", board_identifier="acme")

    assert store.list_due_ats_boards(now + timedelta(hours=1)) == []
    due = store.list_due_ats_boards(now + timedelta(hours=25))
    assert [(entry.provider, entry.board_identifier) for entry in due] == [
        ("lever", "acme")
    ]
```

Add a success test asserting `job_count`, success timestamp, and failure reset.

- [ ] **Step 3: Run red**

```bash
python -m pytest tests/test_store.py -q
```

- [ ] **Step 4: Add model and table**

In `models.py`:

```python
@dataclass(frozen=True, slots=True)
class AtsRegistryEntry:
    provider: str
    board_identifier: str
    company_name: str
    market_hint: str
    first_seen_at: str
    last_seen_at: str
    last_checked_at: str | None
    last_success_at: str | None
    last_eligible_at: str | None
    last_job_count: int
    eligible_jobs_seen: int
    consecutive_failures: int
    active: bool
    paused_until: str | None
```

Create in `_init_db()`:

```sql
CREATE TABLE IF NOT EXISTS ats_registry (
    provider TEXT NOT NULL,
    board_identifier TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    market_hint TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_eligible_at TEXT,
    last_job_count INTEGER NOT NULL DEFAULT 0,
    eligible_jobs_seen INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    PRIMARY KEY(provider, board_identifier)
)
```

Supported providers remain exactly `ashby`, `lever`, `greenhouse`.

- [ ] **Step 5: Implement health semantics**

`upsert_ats_board()` updates display metadata and `last_seen_at` on conflict, sets `active=1`, but preserves any future `paused_until` and current failure count.

`record_ats_scan_failure()` sets `last_checked_at=now`, increments failures, and sets `paused_until=now+24h`.

`record_ats_scan_success()` sets `last_checked_at`, `last_success_at`, `last_job_count`, clears `paused_until`, and resets failures to zero.

`list_due_ats_boards()` returns active rows whose pause is null or `<= now`; ordering/cap is Task 3.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/test_store.py -q
git add src/job_hunter/models.py src/job_hunter/store.py tests/test_store.py
git commit -m "feat: persist learned ATS boards"
```

---

### Task 3: Harvest ATS boards before relevance filtering and select due boards

**Files:**
- Create: `src/job_hunter/ats_registry.py`
- Modify: `src/job_hunter/discovery.py`
- Create: `tests/test_ats_registry.py`
- Modify: `tests/test_discovery.py`

**Interfaces:**
- `extract_ats_reference(job: Job) -> AtsReference | None`
- `harvest_ats_board(store: JobStore, job: Job, market_hint: str | None = None) -> bool`
- `select_ats_boards(entries: list[AtsRegistryEntry], market_order: list[str], limit: int, now: datetime) -> list[AtsRegistryEntry]`

- [ ] **Step 1: Write extraction tests**

```python
@pytest.mark.parametrize(
    ("url", "provider", "board"),
    [
        ("https://jobs.ashbyhq.com/omnea/123", "ashby", "omnea"),
        ("https://jobs.lever.co/acme/abc", "lever", "acme"),
        ("https://boards.greenhouse.io/brex/jobs/999", "greenhouse", "brex"),
    ],
)
def test_extract_ats_reference_from_supported_url(url, provider, board):
    ref = extract_ats_reference(Job(source="feed", title="x", url=url))
    assert ref is not None
    assert (ref.provider, ref.board) == (provider, board)
```

Also test precedence: populated ATS fields, then `canonical_url`, then `url`, then `original_url`.

- [ ] **Step 2: Write deterministic selector test**

Construct four `AtsRegistryEntry` values and fixed `now`. Verify selection priority is:

1. board with `last_eligible_at` within the previous 30 days;
2. configured market order;
3. never checked before checked;
4. otherwise oldest `last_checked_at` first;
5. lexical `(provider, board_identifier)` final tie-breaker.

Use a limit of `3` and assert exact selected identifiers.

- [ ] **Step 3: Write discovery integration tests**

Backend-only role must still teach the ATS board:

```python
job = Job(
    source="feed",
    title="Backend Engineer",
    company="Example",
    url="https://jobs.ashbyhq.com/example/backend-1",
    description="Python backend services",
)
result = collect_candidates(
    [FakeSource([job])],
    store,
    http,
    policy,
)
assert result.eligible == []
assert store.count_ats_boards() == 1
```

Add a canonical-resolution test that returns `CanonicalResolution` with `AtsReference(provider="greenhouse", board="acme", job_id="123")`; assert `greenhouse/acme` is persisted.

- [ ] **Step 4: Run red**

```bash
python -m pytest tests/test_ats_registry.py tests/test_discovery.py -q
```

- [ ] **Step 5: Implement harvesting**

`extract_ats_reference()` reuses `canonical.parse_supported_ats_url()`.

`harvest_ats_board()` calls:

```python
return store.upsert_ats_board(
    provider=ref.provider,
    board_identifier=ref.board,
    company_name=job.company,
    market_hint=market_hint or job.market_hint or job.market_id or "",
)
```

In `collect_candidates()` harvest every raw job immediately after raw market attribution and before `needs_evaluation`/prefilter exits. Harvest again after canonical resolution mutates ATS fields. After a job survives prefilter, call `record_ats_eligible_job()` when it has a supported board.

Catch/log ATS registry write failures without dropping the job.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/test_ats_registry.py tests/test_discovery.py -q
git add src/job_hunter/ats_registry.py src/job_hunter/discovery.py tests/test_ats_registry.py tests/test_discovery.py
git commit -m "feat: learn ATS boards from discovered jobs"
```

---

### Task 4: Scan learned ATS boards through existing adapters

**Files:**
- Create: `src/job_hunter/sources/learned_ats.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `src/job_hunter/pipeline.py`
- Create: `tests/test_learned_ats_source.py`
- Modify: `tests/test_sources.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- `LearnedAtsStats(boards_scanned: int = 0, boards_successful: int = 0, boards_failed: int = 0, jobs_raw: int = 0)`
- `LearnedAtsSource(store, http, *, limit: int, market_order: list[str], now=utc_now)`
- `build_sources(settings, http, *, store: JobStore | None = None, search_breaker=None, query_date=None)`

- [ ] **Step 1: Write learned-source success/failure tests**

Create registry entries for one Ashby, one Lever, and one Greenhouse board. Use an HTTP fake that returns each adapter's already-tested JSON shape. Assert native source labels stay `ashby`, `lever`, `greenhouse` and:

```python
assert source.stats.boards_scanned == 3
assert source.stats.boards_successful == 3
assert source.stats.boards_failed == 0
assert source.stats.jobs_raw == 3
```

Add a test where the first board's `get_json()` raises and the second succeeds; assert one failure is recorded and the second board still returns jobs.

- [ ] **Step 2: Run red**

```bash
python -m pytest tests/test_learned_ats_source.py -q
```

- [ ] **Step 3: Implement composite source**

Follow `CompanyWatchSource` by defining a private `_HealthTrackingHttp` that captures a `get_json()` exception while letting the native ATS adapter preserve its fail-open contract.

Use:

```python
_ATS_SOURCE_TYPES = {
    "ashby": AshbySource,
    "lever": LeverSource,
    "greenhouse": GreenhouseSource,
}
```

At discovery start:

```python
entries = select_ats_boards(
    self._store.list_due_ats_boards(checked_at),
    self._market_order,
    self._limit,
    checked_at,
)
```

For each board, record success with `len(jobs)` or record failure and continue.

- [ ] **Step 4: Pass the existing store into source construction**

Change `build_sources()` to accept optional `store`. Preserve existing tests/callers by defaulting to `None`.

In `run_pipeline()` change production construction to:

```python
build_sources(
    settings,
    http,
    store=store,
    search_breaker=search_breaker,
    query_date=query_date,
)
```

When `store is not None` and `max_learned_ats_boards_per_run > 0`, append exactly one `LearnedAtsSource` using:

```python
market_order = [market.id for market in settings.policy.markets if market.enabled]
```

Static `ats:` adapters remain additive.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/test_learned_ats_source.py tests/test_sources.py tests/test_pipeline.py -q
git add src/job_hunter/sources/learned_ats.py src/job_hunter/sources/__init__.py src/job_hunter/pipeline.py tests/test_learned_ats_source.py tests/test_sources.py tests/test_pipeline.py
git commit -m "feat: scan learned ATS boards"
```

---

### Task 5: Add DevJobs as a real Israel source

**Files:**
- Create: `src/job_hunter/sources/devjobs.py`
- Create: `tests/test_devjobs_source.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- `DevJobsSource(http, *, max_jobs_per_category: int = 30)`
- Categories: `Frontend`, `Full Stack`.
- Jobs use `source="devjobs"`, numeric detail ID as `source_job_id`, and `market_hint="israel_remote"`.

Production routes verified during design:

```text
https://www.devjobs.co.il/jobs-grid?developerTypes=Frontend
https://www.devjobs.co.il/jobs-grid?developerTypes=Full+Stack
```

- [ ] **Step 1: Write synthetic listing/detail parser test**

Listing fixture:

```html
<a href="/job-details/4458634930">Frontend Engineer</a>
```

Detail fixture:

```html
<html>
  <head><title>Frontend Engineer - Loora - Tel Aviv-Yafo | DevJobs</title></head>
  <body>
    <h3>Frontend Engineer</h3>
    <div>Job Type Remote</div>
    <div>Location Tel Aviv-Yafo</div>
    <div>Skills</div><span>TypeScript</span><span>React</span>
    <p>Build and own our web products with React and TypeScript.</p>
  </body>
</html>
```

Assert exact title/company/location/remote/source/source ID/market hint and that description contains the posting text.

Parameterized work-mode expectations: `Remote -> True`, `On-site -> False`, `Hybrid -> False`.

- [ ] **Step 2: Write isolation/cap tests**

Test:

- listing request failure returns `[]`;
- one detail request failure skips only that posting;
- duplicate detail IDs are fetched once;
- `max_jobs_per_category=2` limits detail requests to two per category.

- [ ] **Step 3: Run red**

```bash
python -m pytest tests/test_devjobs_source.py -q
```

- [ ] **Step 4: Implement parser**

Listing link extraction:

```python
for anchor in soup.select('a[href^="/job-details/"]'):
    path = urlparse(anchor["href"]).path
    match = re.fullmatch(r"/job-details/(\d+)", path)
```

Deduplicate IDs in listing order and apply cap before detail fetches.

Detail title parser:

```python
raw = title_tag.get_text(" ", strip=True).removesuffix(" | DevJobs")
job_title, company, location = [part.strip() for part in raw.rsplit(" - ", 2)]
```

If the title does not split into three non-empty parts, log a bounded warning and skip that posting rather than inventing metadata.

Use cleaned visible body text as `description`. Parse exact work-mode values from normalized visible text; unknown becomes `None`.

- [ ] **Step 5: Wire config-driven construction**

Instantiate one `DevJobsSource(http)` when any enabled market includes `devjobs` in `direct_sources`. Do not instantiate one per market.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/test_devjobs_source.py tests/test_sources.py -q
git add src/job_hunter/sources/devjobs.py src/job_hunter/sources/__init__.py tests/test_devjobs_source.py tests/test_sources.py
git commit -m "feat: ingest Israel jobs from DevJobs"
```

---

### Task 6: Add Wellfound as a real Europe/London/US startup source

**Files:**
- Create: `src/job_hunter/sources/wellfound.py`
- Create: `tests/test_wellfound_source.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- `WellfoundListing(url: str, market_id: str)` dataclass.
- `WellfoundSource(http, listings: list[WellfoundListing], *, max_jobs_per_listing: int = 12)`.
- Jobs use `source="wellfound"` and the first configured listing that reveals the job determines `market_hint`.

Phase-1 listing routes:

```python
_WELLFOUND_LISTINGS = {
    "germany_eu": [
        "https://wellfound.com/role/l/frontend-engineer/europe",
        "https://wellfound.com/role/l/full-stack-engineer/europe",
    ],
    "london": [
        "https://wellfound.com/role/l/frontend-engineer/london",
        "https://wellfound.com/role/l/full-stack-engineer/london",
    ],
    "us_nyc_sf": [
        "https://wellfound.com/role/l/frontend-engineer/new-york",
        "https://wellfound.com/role/l/frontend-engineer/san-francisco",
        "https://wellfound.com/role/l/full-stack-engineer/new-york",
        "https://wellfound.com/role/l/full-stack-engineer/san-francisco",
    ],
}
```

Phase 1 reads page 1 only; pagination tuning waits for production-yield evidence.

- [ ] **Step 1: Write listing/detail parser test**

Listing fixture:

```html
<a href="/jobs/4639071-frontend-engineer">Frontend Engineer</a>
<a href="/jobs/2404013-senior-frontend-engineer-remote-europe">Senior Frontend Engineer - Remote Europe</a>
```

Detail fixture:

```html
<html>
  <head><title>Frontend Engineer at Omnea • London | Wellfound</title></head>
  <body>
    <h1>Frontend Engineer</h1>
    <div>£90k – £160k</div>
    <div>Full Time</div>
    <div>Job Location</div><div>London</div>
    <div>Visa Sponsorship</div><div>Not Available</div>
    <div>Relocation Not Allowed</div>
    <h2>About the job</h2>
    <p>Own the design system and front-end architecture.</p>
  </body>
</html>
```

Assert numeric source ID, title, `company="Omnea"`, `location="London"`, market hint, and description containing salary/sponsorship text.

Remote-policy tests: `Remote Work Policy Remote only -> True`; `Remote Work Policy In office -> False`; `Hybrid -> None` while preserving the text in description.

- [ ] **Step 2: Write isolation/cap/global-dedupe test**

Test:

- one listing failure does not block another listing;
- one detail failure skips only that job;
- duplicate links within one page are fetched once;
- the same job appearing in two configured market listing routes is fetched once for the whole `WellfoundSource` run and keeps the first listing's market hint;
- listing order is built from configured market order, making that first-market choice deterministic;
- `max_jobs_per_listing` bounds newly encountered detail URLs per listing.

- [ ] **Step 3: Run red**

```bash
python -m pytest tests/test_wellfound_source.py -q
```

- [ ] **Step 4: Implement parser**

```python
_JOB_PATH_RE = re.compile(r"^/jobs/(\d+)-")
for anchor in soup.select('a[href^="/jobs/"]'):
    match = _JOB_PATH_RE.match(urlparse(anchor["href"]).path)
```

Prefer `<h1>` for title. Parse company only when page title contains `" at "` and `" • "`; otherwise leave company empty and allow existing enrichment/dedupe logic to proceed rather than crashing.

Use cleaned body text as description so salary, sponsorship, relocation, and work-mode phrases remain visible to existing market eligibility.

- [ ] **Step 5: Build listing definitions from enabled markets**

Iterate enabled markets in configured order. For each market that declares `wellfound`, append its routes from `_WELLFOUND_LISTINGS`. Instantiate exactly one `WellfoundSource` with the combined ordered list.

- [ ] **Step 6: Run and commit**

```bash
python -m pytest tests/test_wellfound_source.py tests/test_sources.py -q
git add src/job_hunter/sources/wellfound.py src/job_hunter/sources/__init__.py tests/test_wellfound_source.py tests/test_sources.py
git commit -m "feat: ingest startup jobs from Wellfound"
```

---

### Task 7: Use Brave only for the metered market-discovery slice

**Files:**
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`
- Modify: `tests/test_brave_budget.py`

**Interfaces:**
- Market targeted discovery uses existing `BraveSearchBackend` directly for the daily allocated subset.
- Non-selected queries are deferred, not sent to DuckDuckGo.
- Existing `build_search_backend()` remains unchanged for canonical-resolution callers.

- [ ] **Step 1: Write source-builder regression tests**

With a Brave key and a fake budget that permits two queries, assert the built market-search sources contain one `TargetedSearchSource` and zero `DuckDuckGoSource` instances.

With no Brave key, assert zero `TargetedSearchSource` and zero `DuckDuckGoSource` instances are added for market discovery. Also assert direct sources and `LearnedAtsSource` are unaffected.

- [ ] **Step 2: Run red**

```bash
python -m pytest tests/test_sources.py tests/test_brave_budget.py -q
```

- [ ] **Step 3: Change market-search construction**

Keep full query generation. Split only to choose today's metered slice:

```python
brave_queries, deferred_queries = split_queries_for_brave(
    queries,
    limit=brave_limit,
)
```

Instantiate:

```python
TargetedSearchSource(
    BraveSearchBackend(
        http,
        brave_api_key,
        on_attempt=lambda: ledger.record(
            provider="brave",
            occurred_at=datetime.now(timezone.utc),
        ),
    ),
    brave_queries,
    breaker=search_breaker,
)
```

Do not create a DuckDuckGo source for `deferred_queries`.

Log:

```text
Brave source-discovery budget: monthly_limit=250 available_today=9 selected=9 deferred=21
```

When no key or allowance exists, direct feeds and ATS scans continue normally.

- [ ] **Step 4: Run and commit**

```bash
python -m pytest tests/test_sources.py tests/test_brave_budget.py tests/test_first_run_hardening.py -q
git add src/job_hunter/sources/__init__.py tests/test_sources.py tests/test_brave_budget.py
git commit -m "fix: stop relying on DuckDuckGo market discovery"
```

---

### Task 8: Add source-quality and ATS-registry telemetry

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- `DiscoveryStats.unique_by_source`, `rejected_by_source`, `eligible_by_source`, `ats_boards_discovered`.
- One final `source_quality` log line per bounded source label.
- One final `ats_registry` line.

- [ ] **Step 1: Write source-counter test**

Given three unique DevJobs jobs where one is eligible and two are rejected, assert:

```python
assert stats.unique_by_source == {"devjobs": 3}
assert stats.eligible_by_source == {"devjobs": 1}
assert stats.rejected_by_source == {"devjobs": 2}
```

Use `metric_source_label()` for all keys.

- [ ] **Step 2: Write final-log test with concrete values**

Use a tiny pipeline fixture that produces one selected/delivered DevJobs job and no ATS scan failure. Assert the captured log contains all field names and concrete counts, for example:

```text
source_quality source=devjobs raw=1 unique=1 rejected=0 eligible=1 selected=1 high_priority=1 package_match=0 possible_match=0 skip=0 blocked=0 delivered=1
```

For a learned ATS source fake with one successful board returning two jobs, assert:

```text
ats_registry total=1 discovered=0 scanned=1 successful=1 failed=0 jobs_raw=2
```

- [ ] **Step 3: Run red**

```bash
python -m pytest tests/test_discovery.py tests/test_pipeline.py -q
```

- [ ] **Step 4: Implement source counters and run-level metrics**

At raw discovery, retain existing `per_source` behavior. After dedupe, use the representative job's bounded source label for `unique/rejected/eligible` counters while provenance remains in `job_sources`.

In pipeline, count selected and fresh evaluation decisions by bounded source label. Count delivered only after Telegram delivery is persisted, matching market telemetry semantics.

ATS values:

- `total = store.count_ats_boards()` at end of run;
- `discovered = discovery.stats.ats_boards_discovered`;
- `scanned/successful/failed/jobs_raw` from the `LearnedAtsSource.stats` instance in this run, or zero if no learned source ran.

- [ ] **Step 5: Run and commit**

```bash
python -m pytest tests/test_discovery.py tests/test_pipeline.py -q
git add src/job_hunter/discovery.py src/job_hunter/pipeline.py tests/test_discovery.py tests/test_pipeline.py
git commit -m "feat: report source discovery quality"
```

---

### Task 9: Full regression and production-readiness verification

**Files:**
- Review only unless a regression requires a scoped fix.

- [ ] **Step 1: Run the complete suite**

```bash
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run source/config regression group**

```bash
python -m pytest \
  tests/test_config.py \
  tests/test_discovery_queries.py \
  tests/test_store.py \
  tests/test_ats_registry.py \
  tests/test_learned_ats_source.py \
  tests/test_devjobs_source.py \
  tests/test_wellfound_source.py \
  tests/test_sources.py \
  tests/test_discovery.py \
  tests/test_pipeline.py -q
```

- [ ] **Step 3: Verify no policy drift**

```bash
git diff main...HEAD -- config/search.yml src/job_hunter/models.py src/job_hunter/config.py
```

Confirm no change to market order/shares, salary floors, allowed languages, sponsorship/relocation/remote rules, role filters, `max_jobs_per_run`, or Gemini environment handling.

- [ ] **Step 4: Verify production-shaped source construction**

A production-shaped test with a real `JobStore` must contain all legacy sources plus:

```text
DevJobsSource
WellfoundSource
LearnedAtsSource
```

and must not contain `DuckDuckGoSource` solely for market-query fallback.

- [ ] **Step 5: Commit only if verification required a fix**

```bash
git status --short
```

If clean, do not create an empty commit. If a scoped verification fix was necessary, rerun the full suite before committing it.

---

## Production Validation After Merge

The first production runs should show concrete lines such as:

```text
discovery source contribution: arbeitnow=350 devjobs=42 wellfound=67 ashby=18 lever=11 greenhouse=9
ats_registry total=24 discovered=7 scanned=17 successful=16 failed=1 jobs_raw=38
source_quality source=devjobs raw=42 unique=40 rejected=31 eligible=9 selected=6 high_priority=1 package_match=0 possible_match=2 skip=3 blocked=0 delivered=3
source_quality source=wellfound raw=67 unique=61 rejected=45 eligible=16 selected=8 high_priority=2 package_match=0 possible_match=2 skip=4 blocked=0 delivered=4
```

The numbers above are examples of log shape, not expected thresholds. Actual success signals are:

1. DevJobs and Wellfound contribute non-zero raw jobs without Brave.
2. `ats_registry total` grows from jobs already present in broad/direct feeds.
3. Later runs scan learned ATS boards even when Brave daily allowance is zero.
4. At least one learned ATS board eventually produces a job absent from the source that first revealed the board.
5. Market coverage no longer depends on generic `search_results` being non-zero.

Do not tune market shares or prefilter strictness from the first run alone; first establish that the new source layer supplies genuinely new jobs.

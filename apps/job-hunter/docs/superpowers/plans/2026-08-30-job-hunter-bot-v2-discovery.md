# Job Hunter Bot v2 Discovery Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Broaden public job discovery and globally rank all eligible candidates before spending the per-run Gemini evaluation budget, so source ordering no longer hides stronger opportunities.

**Architecture:** Add configuration-driven query generation, three additional public discovery adapters, a focused discovery aggregation layer, and a deterministic pre-Gemini ranking layer. Refactor `run_pipeline()` so all sources are collected and deduplicated first, then the best `max_jobs_per_run` candidates are evaluated while existing SQLite, cover-letter, Telegram retry, and scheduling behavior remain compatible.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, PyYAML, standard-library `xml.etree.ElementTree`, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-08-30-job-hunter-bot-v2-discovery-design.md`

## Global Constraints

- Do not change the default Gemini model as part of this plan.
- Do not add a paid search API, authenticated scraping, browser automation, or employer-form submission.
- Keep all source failures isolated; one source must never abort the run.
- Preserve compatibility with existing SQLite state artifacts.
- Preserve current evaluation caching, cover-letter/PDF generation, Telegram delivery retry, and scheduled-run behavior.
- Apply `max_jobs_per_run` only after global deterministic ranking of all eligible candidates.
- Enrich search-derived records from the actual posting before Gemini evaluation when possible.
- Do not make external network calls in tests.
- Run `pytest -q` before completion.

---

### Task 1: Discovery configuration and query generation

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/config.py`
- Modify: `config/search.yml`
- Create: `src/job_hunter/discovery_queries.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_discovery_queries.py`

**Interfaces:**
- Produces: `SearchPolicy.role_families: list[str]`
- Produces: `SearchPolicy.search_query_templates: list[str]`
- Produces: `SearchPolicy.search_domains: list[str]`
- Produces: `SearchPolicy.max_search_queries_per_run: int`
- Produces: `generate_search_queries(policy: SearchPolicy) -> list[str]`
- Preserves: existing `SearchPolicy.search_queries` as explicit legacy/additional queries.

- [ ] **Step 1: Write failing configuration tests**

Extend `tests/test_config.py` with YAML containing:

```yaml
max_search_queries_per_run: 4
role_families:
  - staff product engineer
  - senior software engineer frontend
search_query_templates:
  - '"{role}" React TypeScript remote Europe'
search_domains:
  - jobs.ashbyhq.com
search_queries:
  - '"Senior Product Engineer" remote'
```

Assert:

```python
assert settings.policy.max_search_queries_per_run == 4
assert settings.policy.role_families == [
    "staff product engineer",
    "senior software engineer frontend",
]
assert settings.policy.search_query_templates == [
    '"{role}" React TypeScript remote Europe'
]
assert settings.policy.search_domains == ["jobs.ashbyhq.com"]
assert settings.policy.search_queries == ['"Senior Product Engineer" remote']
```

- [ ] **Step 2: Run the config test and verify it fails**

```bash
pytest tests/test_config.py -q
```

Expected: failure because the new `SearchPolicy` fields are not defined or loaded.

- [ ] **Step 3: Add policy fields and loader support**

Update `SearchPolicy` in `models.py`:

```python
@dataclass(slots=True)
class SearchPolicy:
    target_titles: list
    positive_keywords: list
    blocked_title_keywords: list
    salary_floor_eur: int
    thresholds: dict
    max_jobs_per_run: int = 25
    search_queries: list = field(default_factory=list)
    ats: dict = field(default_factory=dict)
    role_families: list[str] = field(default_factory=list)
    search_query_templates: list[str] = field(default_factory=list)
    search_domains: list[str] = field(default_factory=list)
    max_search_queries_per_run: int = 30
```

Load the four new values explicitly in `config.py`.

- [ ] **Step 4: Write failing query-generation tests**

Create `tests/test_discovery_queries.py` with a complete policy factory and these assertions:

```python
from job_hunter.discovery_queries import generate_search_queries
from job_hunter.models import SearchPolicy


def make_policy(limit: int = 10) -> SearchPolicy:
    return SearchPolicy(
        target_titles=[],
        positive_keywords=[],
        blocked_title_keywords=[],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        search_queries=['"Senior Product Engineer" remote'],
        role_families=["staff product engineer", "senior software engineer frontend"],
        search_query_templates=['"{role}" React TypeScript remote Europe'],
        search_domains=["jobs.ashbyhq.com"],
        max_search_queries_per_run=limit,
    )


def test_generate_search_queries_is_deterministic():
    queries = generate_search_queries(make_policy())
    assert queries == [
        '"staff product engineer" React TypeScript remote Europe',
        'site:jobs.ashbyhq.com "staff product engineer" React TypeScript remote Europe',
        '"senior software engineer frontend" React TypeScript remote Europe',
        'site:jobs.ashbyhq.com "senior software engineer frontend" React TypeScript remote Europe',
        '"Senior Product Engineer" remote',
    ]


def test_generate_search_queries_enforces_limit():
    assert len(generate_search_queries(make_policy(limit=3))) == 3
```

Add one more test constructing a policy with empty role/template/domain lists and asserting explicit `search_queries` are returned unchanged.

- [ ] **Step 5: Run query tests and verify failure**

```bash
pytest tests/test_discovery_queries.py -q
```

Expected: import/function failure.

- [ ] **Step 6: Implement deterministic capped query generation**

Create `src/job_hunter/discovery_queries.py`:

```python
from job_hunter.models import SearchPolicy


def generate_search_queries(policy: SearchPolicy) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    def add(query: str) -> None:
        normalized = " ".join(query.split())
        if normalized and normalized not in seen:
            seen.add(normalized)
            queries.append(normalized)

    for role in policy.role_families:
        for template in policy.search_query_templates:
            base = template.format(role=role)
            add(base)
            for domain in policy.search_domains:
                add(f"site:{domain} {base}")

    for query in policy.search_queries:
        add(query)

    return queries[: policy.max_search_queries_per_run]
```

- [ ] **Step 7: Update `config/search.yml`**

Add `max_search_queries_per_run: 30`, the role families from the spec, a finite list of query templates, and the ATS search domains `jobs.ashbyhq.com`, `jobs.lever.co`, and `boards.greenhouse.io`. Keep explicit search queries only when they add coverage.

- [ ] **Step 8: Run focused tests**

```bash
pytest tests/test_config.py tests/test_discovery_queries.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/config.py src/job_hunter/discovery_queries.py config/search.yml tests/test_config.py tests/test_discovery_queries.py
git commit -m "feat: add configurable discovery query families"
```

---

### Task 2: Deterministic pre-Gemini ranking

**Files:**
- Create: `src/job_hunter/ranking.py`
- Create: `tests/test_ranking.py`

**Interfaces:**
- Produces: `source_quality(job: Job) -> int`
- Produces: `priority_score(job: Job, policy: SearchPolicy) -> int`
- Produces: `rank_jobs(jobs: list[tuple[int, Job]], policy: SearchPolicy) -> list[tuple[int, Job, int]]`

- [ ] **Step 1: Write failing ranking tests**

Create `tests/test_ranking.py` with a local policy fixture and these concrete cases:

```python
from job_hunter.models import Job, SearchPolicy
from job_hunter.ranking import priority_score, rank_jobs, source_quality


def make_policy() -> SearchPolicy:
    return SearchPolicy(
        target_titles=["senior product engineer", "staff product engineer", "senior frontend engineer"],
        positive_keywords=["react", "typescript", "next.js", "product ownership", "design system"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
    )


def test_product_engineer_outranks_generic_react_role():
    policy = make_policy()
    strong = Job(
        source="ashby",
        title="Staff Product Engineer",
        company="A",
        location="Remote Europe",
        remote=True,
        description="React TypeScript product ownership architecture end-to-end ownership",
    )
    generic = Job(
        source="remotive",
        title="React Developer",
        company="B",
        location="Worldwide",
        remote=True,
        description="React React React React React",
    )
    assert priority_score(strong, policy) > priority_score(generic, policy)


def test_explicit_europe_remote_outranks_unknown_location():
    policy = make_policy()
    europe = Job(source="remotive", title="Senior Frontend Engineer", location="Remote Europe", remote=True, description="React TypeScript")
    unknown = Job(source="remotive", title="Senior Frontend Engineer", location="", remote=True, description="React TypeScript")
    assert priority_score(europe, policy) > priority_score(unknown, policy)


def test_ats_url_gets_higher_source_quality_than_general_web_result():
    ats = Job(source="duckduckgo", title="Senior Product Engineer", url="https://jobs.ashbyhq.com/acme/123")
    web = Job(source="duckduckgo", title="Senior Product Engineer", url="https://example.com/jobs/123")
    assert source_quality(ats) > source_quality(web)


def test_keyword_repetition_is_capped():
    policy = make_policy()
    normal = Job(source="remotive", title="React Developer", description="React TypeScript product ownership")
    spammy = Job(source="remotive", title="React Developer", description="React " * 100)
    assert priority_score(spammy, policy) < priority_score(normal, policy) + 20


def test_rank_jobs_is_stable_for_equal_scores():
    policy = make_policy()
    jobs = [
        (2, Job(source="remotive", title="Senior Frontend Engineer", company="Beta", description="React TypeScript")),
        (1, Job(source="remotive", title="Senior Frontend Engineer", company="Acme", description="React TypeScript")),
    ]
    ranked = rank_jobs(jobs, policy)
    assert [job_id for job_id, _job, _score in ranked] == [1, 2]
```

- [ ] **Step 2: Run ranking tests and verify failure**

```bash
pytest tests/test_ranking.py -q
```

Expected: import failure.

- [ ] **Step 3: Implement bounded scoring helpers**

In `ranking.py`, define source quality exactly as:

```python
_ATS_HOSTS = ("jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io")


def source_quality(job: Job) -> int:
    url = (job.url or "").lower()
    if any(host in url for host in _ATS_HOSTS):
        return 10
    if job.source in {"ashby", "lever", "greenhouse"}:
        return 10
    if job.source in {"remoteok", "remotive", "weworkremotely", "arbeitnow"}:
        return 7
    if job.source == "hackernews":
        return 5
    return 3
```

Implement title fit as 0-40, strength evidence as 0-25, career-direction evidence as 0-15, location evidence as 0-10, and source quality as 0-10. Count unique matched signals rather than occurrences. Clamp the total to 0-100.

- [ ] **Step 4: Implement stable global ordering**

```python
def rank_jobs(jobs: list[tuple[int, Job]], policy: SearchPolicy) -> list[tuple[int, Job, int]]:
    scored = [(job_id, job, priority_score(job, policy)) for job_id, job in jobs]
    return sorted(
        scored,
        key=lambda item: (
            -item[2],
            (item[1].company or "").lower(),
            (item[1].title or "").lower(),
            item[0],
        ),
    )
```

- [ ] **Step 5: Run ranking tests**

```bash
pytest tests/test_ranking.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/ranking.py tests/test_ranking.py
git commit -m "feat: rank discovery candidates before Gemini"
```

---

### Task 3: Aggregate, enrich, persist, and collapse candidates before evaluation

**Files:**
- Create: `src/job_hunter/discovery.py`
- Create: `tests/test_discovery.py`
- Use: `src/job_hunter/fetching.py`
- Use: `src/job_hunter/normalize.py`
- Use: `src/job_hunter/prefilter.py`
- Use: `src/job_hunter/store.py`

**Interfaces:**
- Produces: `DiscoveryStats`
- Produces: `DiscoveryResult`
- Produces: `collect_candidates(sources: list, store: JobStore, http: HttpClient, policy: SearchPolicy) -> DiscoveryResult`

- [ ] **Step 1: Write failing aggregation tests**

Create `tests/test_discovery.py` with fake sources. Cover these exact behaviors:

```python
def test_collect_candidates_continues_after_source_failure():
    broken = BrokenSource()
    good = FakeSource([Job(source="x", source_job_id="1", title="Senior Product Engineer", description="React TypeScript", remote=True)])
    result = collect_candidates([broken, good], store, http, policy)
    assert result.stats.raw == 1
    assert len(result.eligible) == 1
```

```python
def test_collect_candidates_collapses_same_canonical_url():
    jobs = [
        Job(source="duckduckgo", title="Senior Product Engineer", url="https://jobs.ashbyhq.com/acme/1?utm_source=x"),
        Job(source="ashby", source_job_id="1", title="Senior Product Engineer", company="Acme", url="https://jobs.ashbyhq.com/acme/1", description="React TypeScript", remote=True),
    ]
    result = collect_candidates([FakeSource(jobs)], store, http, policy)
    assert result.stats.unique == 1
    assert len(result.eligible) == 1
    assert result.eligible[0][1].company == "Acme"
```

Add concrete tests for enrichment of a URL-only result, prefilter rejection counting, and exclusion of an already-evaluated unchanged job from `eligible`.

- [ ] **Step 2: Run discovery tests and verify failure**

```bash
pytest tests/test_discovery.py -q
```

Expected: import failure.

- [ ] **Step 3: Define result dataclasses**

```python
@dataclass(slots=True)
class DiscoveryStats:
    raw: int = 0
    unique: int = 0
    prefilter_rejected: int = 0
    eligible: int = 0
    per_source: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryResult:
    eligible: list[tuple[int, Job]]
    rediscovered_job_ids: list[int]
    stats: DiscoveryStats
```

- [ ] **Step 4: Implement in-run duplicate identity**

Use canonical URL as the primary key. Maintain a second exact normalized company/title/location key to collapse aggregator/original records when URLs differ but job identity matches.

```python
def candidate_url_key(job: Job) -> str | None:
    if not job.url:
        return None
    return canonicalize_url(job.url)


def candidate_identity_key(job: Job) -> str:
    return "|".join(normalize_text(value) for value in (job.company, job.title, job.location))
```

- [ ] **Step 5: Implement deterministic richer-record preference**

Rank duplicates by these booleans in order: ATS URL, has description, has company, has location, has explicit remote value. Keep the richer record and preserve any non-empty fields from the weaker record when doing so cannot overwrite better data.

- [ ] **Step 6: Implement collection flow**

For every source, catch exceptions, update per-source/raw counts, enrich only jobs with URL and missing description, collapse duplicates, then upsert unique jobs. Run `store.needs_evaluation(job_id)` and `prefilter_job(job, policy)` only after aggregation. Return only jobs needing evaluation and passing prefilter in `eligible`.

- [ ] **Step 7: Run discovery tests**

```bash
pytest tests/test_discovery.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/discovery.py tests/test_discovery.py
git commit -m "feat: aggregate and dedupe discovery candidates"
```

---

### Task 4: Add Remote OK public feed adapter

**Files:**
- Create: `src/job_hunter/sources/remoteok.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- Produces: `RemoteOKSource(http).discover() -> list[Job]`
- Endpoint: `https://remoteok.com/api`
- Source name: `remoteok`

- [ ] **Step 1: Write failing normalization test**

Add this fake response:

```python
fake_http.json_data = [
    {"last_updated": 1, "legal": "terms"},
    {
        "id": "123",
        "slug": "senior-product-engineer-acme",
        "position": "Senior Product Engineer",
        "company": "Acme",
        "location": "Europe",
        "url": "https://remoteok.com/remote-jobs/123",
        "description": "<p>React TypeScript product ownership</p>",
        "tags": ["react", "typescript"],
    },
]
```

Assert one job is returned, the metadata row is skipped, description is plain text, `remote is True`, and `source_job_id == "123"`.

- [ ] **Step 2: Run focused test and verify failure**

```bash
pytest tests/test_sources.py -q -k remoteok
```

Expected: import/class failure.

- [ ] **Step 3: Implement adapter**

Use `HttpClient.get_json()` and existing `strip_html()`. Skip any array entry with no `position` value. Map `position`, `company`, `location`, `url`, `description`, and `id` to `Job` and set `remote=True`.

- [ ] **Step 4: Export adapter and test**

```bash
pytest tests/test_sources.py -q -k remoteok
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/sources/remoteok.py src/job_hunter/sources/__init__.py tests/test_sources.py
git commit -m "feat: add Remote OK discovery source"
```

---

### Task 5: Add We Work Remotely public RSS adapter

**Files:**
- Create: `src/job_hunter/sources/weworkremotely.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- Produces: `WeWorkRemotelySource(http, feed_urls: list[str] | None = None).discover() -> list[Job]`
- Default feeds:
  - `https://weworkremotely.com/categories/remote-front-end-programming-jobs.rss`
  - `https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss`
  - `https://weworkremotely.com/categories/remote-product-jobs.rss`
- Source name: `weworkremotely`

- [ ] **Step 1: Write failing RSS parsing test**

Use this RSS body:

```xml
<rss><channel>
  <item>
    <title>Acme: Senior Product Engineer</title>
    <link>https://weworkremotely.com/remote-jobs/acme-senior-product-engineer</link>
    <description><![CDATA[<p>React TypeScript product ownership</p>]]></description>
    <category>Full-Stack Programming</category>
  </item>
</channel></rss>
```

Assert company is `Acme`, title is `Senior Product Engineer`, description is stripped, URL is preserved, and `remote=True`. Add a second malformed `<item>` without title/link and assert it is ignored.

- [ ] **Step 2: Run focused test and verify failure**

```bash
pytest tests/test_sources.py -q -k weworkremotely
```

Expected: import/class failure.

- [ ] **Step 3: Implement adapter with `xml.etree.ElementTree`**

Fetch each feed independently. Parse `<item>` nodes, split `Company: Role` on the first colon, strip description HTML through `strip_html()`, and continue when a feed request or single item is malformed.

- [ ] **Step 4: Export adapter and run tests**

```bash
pytest tests/test_sources.py -q -k weworkremotely
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/sources/weworkremotely.py src/job_hunter/sources/__init__.py tests/test_sources.py
git commit -m "feat: add We Work Remotely RSS discovery"
```

---

### Task 6: Add Hacker News Who Is Hiring adapter

**Files:**
- Create: `src/job_hunter/sources/hackernews.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- Produces: `HackerNewsHiringSource(http).discover() -> list[Job]`
- Uses public HN Algolia API.
- Source name: `hackernews`

- [ ] **Step 1: Write failing thread/comment test**

Mock a search response:

```python
{
    "hits": [
        {
            "objectID": "999",
            "title": "Ask HN: Who is hiring? (August 2026)",
            "created_at_i": 1788200000,
        }
    ]
}
```

Mock the item response:

```python
{
    "id": 999,
    "children": [
        {
            "id": 1001,
            "text": "Acme | Senior Product Engineer | Remote EU | React, TypeScript | https://jobs.ashbyhq.com/acme/123",
            "children": [],
        },
        {
            "id": 1002,
            "text": "Discussion reply with no job opening",
            "children": [],
        },
    ],
}
```

Assert only comment `1001` becomes a job, with company `Acme`, title `Senior Product Engineer`, location `Remote EU`, extracted URL, full comment text as description, `remote=True`, and `source_job_id == "1001"`.

- [ ] **Step 2: Run focused test and verify failure**

```bash
pytest tests/test_sources.py -q -k hackernews
```

Expected: import/class failure.

- [ ] **Step 3: Implement latest-thread lookup**

Query the Algolia search endpoint for `Ask HN: Who is hiring?`, restrict to stories, sort candidate hits by `created_at_i` descending, choose the newest title beginning with `Ask HN: Who is hiring?`, then fetch `/api/v1/items/{objectID}`.

- [ ] **Step 4: Implement conservative comment normalization**

Strip HTML from comment text, split pipe-delimited comments, require at least company and role segments, extract the first HTTP(S) URL by regular expression, and infer remote only when the text explicitly contains `remote`.

- [ ] **Step 5: Export adapter and run tests**

```bash
pytest tests/test_sources.py -q -k hackernews
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/sources/hackernews.py src/job_hunter/sources/__init__.py tests/test_sources.py
git commit -m "feat: add Hacker News hiring discovery"
```

---

### Task 7: Wire expanded default source construction

**Files:**
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- `build_sources(settings, http)` includes Remotive, Arbeitnow, Remote OK, We Work Remotely, Hacker News, DuckDuckGo generated queries, and configured ATS adapters.

- [ ] **Step 1: Update failing `build_sources` test**

Assert the built source class names contain:

```python
expected = {
    "RemotiveSource",
    "ArbeitnowSource",
    "RemoteOKSource",
    "WeWorkRemotelySource",
    "HackerNewsHiringSource",
    "DuckDuckGoSource",
    "AshbySource",
    "LeverSource",
    "GreenhouseSource",
}
assert expected.issubset(set(kinds))
```

Add a small public property or constructor seam on `DuckDuckGoSource` if required so the test can assert generated queries equal `generate_search_queries(settings.policy)`.

- [ ] **Step 2: Run focused test and verify failure**

```bash
pytest tests/test_sources.py -q -k build_sources
```

Expected: FAIL because the new default sources and generated queries are not wired.

- [ ] **Step 3: Wire the source registry**

Instantiate the five always-on structured/community sources, then `DuckDuckGoSource(http, generate_search_queries(settings.policy))`, then configured Ashby/Lever/Greenhouse boards.

- [ ] **Step 4: Keep ATS configuration public-only**

Retain `ats.ashby`, `ats.lever`, and `ats.greenhouse` lists in YAML. Do not add secrets or authenticated endpoints.

- [ ] **Step 5: Run source/query/config tests**

```bash
pytest tests/test_sources.py tests/test_discovery_queries.py tests/test_config.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/sources/__init__.py tests/test_sources.py
git commit -m "feat: expand default discovery coverage"
```

---

### Task 8: Refactor pipeline to apply a global ranked Gemini budget

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/store.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_store.py`
- Use: `src/job_hunter/discovery.py`
- Use: `src/job_hunter/ranking.py`

**Interfaces:**
- Consumes: `collect_candidates(sources: list, store: JobStore, http: HttpClient, policy: SearchPolicy) -> DiscoveryResult`
- Consumes: `rank_jobs(jobs: list[tuple[int, Job]], policy: SearchPolicy) -> list[tuple[int, Job, int]]`
- Preserves: `run_pipeline(settings, sources=None, store=None, gemini=None, telegram=None, http=None) -> RunSummary`
- Adds: `JobStore.pending_delivery_job_ids() -> list[int]`

- [ ] **Step 1: Write the critical failing source-order regression test**

Set `max_jobs_per_run=1`. Use an early fake source returning three jobs titled `React Developer` with only `React` evidence. Use a late fake source returning `Staff Product Engineer` with `React TypeScript product ownership architecture` and `Remote Europe`.

Enhance `FakeGemini` so JSON-mode calls append the prompt to `self.eval_prompts`.

Assert after the run:

```python
assert gemini.eval_calls == 1
assert len(gemini.eval_prompts) == 1
assert "Staff Product Engineer" in gemini.eval_prompts[0]
```

Repeat with source order reversed and assert the same strong title is evaluated.

- [ ] **Step 2: Run regression test and verify failure**

```bash
pytest tests/test_pipeline.py -q -k global_ranked_budget
```

Expected: FAIL under source-by-source evaluation.

- [ ] **Step 3: Write store test for pending delivery IDs**

Create jobs/evaluations using existing store helpers and assert:

```python
assert ready_without_delivery_id in store.pending_delivery_job_ids()
assert possible_without_message_id in store.pending_delivery_job_ids()
assert fully_delivered_ready_id not in store.pending_delivery_job_ids()
assert ready_with_message_but_no_document_id in store.pending_delivery_job_ids()
```

- [ ] **Step 4: Implement `pending_delivery_job_ids()`**

Query the latest evaluation decision for each job. A `possible_match` is pending when `telegram_message` is missing. A `high_priority` or `package_match` is pending when either `telegram_message` or `telegram_document` is missing. Do not change the schema.

- [ ] **Step 5: Refactor discovery/evaluation phase**

Replace the source-local evaluation loop with:

```python
discovery = collect_candidates(sources, store, http, settings.policy)
ranked = rank_jobs(discovery.eligible, settings.policy)
selected = ranked[: settings.policy.max_jobs_per_run]
```

Loop over `selected`, preserving current evaluation exception handling, `save_evaluation()`, material generation, PDF rendering, and summary decisions.

- [ ] **Step 6: Preserve pending delivery retries independent of fresh discovery**

Build retry IDs using:

```python
retry_ids = set(discovery.rediscovered_job_ids)
retry_ids.update(store.pending_delivery_job_ids())
```

Exclude jobs already queued by a fresh current-run evaluation, then call `_requeue_pending_delivery()` for the remaining IDs.

- [ ] **Step 7: Keep `RunSummary` semantics unchanged**

Historical retries must not increment `ready_to_apply`, `possible_matches`, or `skipped`. Those counters continue to represent current-run processing.

- [ ] **Step 8: Run pipeline/store tests**

```bash
pytest tests/test_pipeline.py tests/test_store.py -q
```

Expected: PASS, including existing delivery retry coverage.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/pipeline.py src/job_hunter/store.py tests/test_pipeline.py tests/test_store.py
git commit -m "feat: globally rank jobs before Gemini evaluation"
```

---

### Task 9: Discovery funnel logging and documentation

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/discovery.py`
- Modify: `tests/test_pipeline.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Logs discovery funnel counts at INFO level.
- Documents the v2 architecture and configuration.

- [ ] **Step 1: Write failing logging test**

Use `caplog` with a multi-source fake run. Assert one message starts with `discovery: raw=` and contains each of `unique=`, `prefilter_rejected=`, `eligible=`, and `selected=`. Assert another message starts with `selected sources:`.

- [ ] **Step 2: Run logging test and verify failure**

```bash
pytest tests/test_pipeline.py -q -k discovery_logging
```

Expected: FAIL because the funnel summary is not logged.

- [ ] **Step 3: Add concise run-level logging**

After ranking/selection:

```python
logger.info(
    "discovery: raw=%s unique=%s prefilter_rejected=%s eligible=%s selected=%s",
    discovery.stats.raw,
    discovery.stats.unique,
    discovery.stats.prefilter_rejected,
    discovery.stats.eligible,
    len(selected),
)
```

Build selected-source counts from the selected `Job.source` values and emit one stable sorted line such as `selected sources: ashby=4 remoteok=2 weworkremotely=1`.

- [ ] **Step 4: Update README**

Document the v2 source list, public-only discovery boundary, query-family configuration, global ranking before Gemini, new `max_jobs_per_run` semantics, ATS board configuration, and discovery-funnel logs.

- [ ] **Step 5: Update AGENTS.md**

Change the architecture overview to:

```text
all sources -> enrich/dedupe -> deterministic rank -> top-N Gemini -> materials -> Telegram
```

Add `discovery.py`, `discovery_queries.py`, `ranking.py`, `remoteok.py`, `weworkremotely.py`, and `hackernews.py` to the module map.

- [ ] **Step 6: Run the full test suite**

```bash
pytest -q
```

Expected: all tests PASS.

- [ ] **Step 7: Verify no private source material was committed**

```bash
git grep -n "CANDIDATE_PROFILE_B64=" -- ':!docs/superpowers/*' || true
git grep -n "COVER_LETTER_TEMPLATE_B64=" -- ':!docs/superpowers/*' || true
```

Expected: no secret values; documentation may mention variable names only.

- [ ] **Step 8: Commit documentation/logging changes**

```bash
git add src/job_hunter/pipeline.py src/job_hunter/discovery.py tests/test_pipeline.py README.md AGENTS.md
git commit -m "docs: document v2 discovery pipeline"
```

---

## Final verification

- [ ] Run the complete test suite:

```bash
pytest -q
```

Expected: PASS.

- [ ] Run a local dry run with configured test credentials/profile:

```bash
JOB_HUNTER_DRY_RUN=1 python -m job_hunter run
```

Expected: all configured sources are attempted, discovery-funnel counts are logged, only globally top-ranked eligible jobs consume Gemini budget, and Telegram sending is skipped.

- [ ] Inspect scope:

```bash
git status --short
git log --oneline --max-count=12
git diff --stat main...HEAD
```

Expected: changes are limited to v2 discovery quality, tests, configuration, and related documentation.

- [ ] Push the implementation branch and confirm `.github/workflows/ci.yml` passes before merging.

## Implementation success check

The implementation is successful when a test with `max_jobs_per_run=1` proves that a high-signal job from the last source is selected over many earlier mediocre jobs, Remote OK/We Work Remotely/Hacker News discovery is active, cross-source duplicates are evaluated once per run, discovery funnel metrics are visible, and the existing persistence/evaluation/material/delivery test suite remains green.
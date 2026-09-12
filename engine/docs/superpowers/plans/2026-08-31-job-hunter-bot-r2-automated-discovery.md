# Job Hunter Bot R2 — Automated Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand the standalone Job Hunter Bot's automated discovery coverage with a self-expanding company watchlist, canonical employer/ATS resolution, cross-source provenance/deduplication, and a small curated specialist-source layer while preserving the existing SQLite-first, fail-open pipeline.

**Architecture:** Every discovery path still produces normal `Job` candidates. Existing sources, Gmail staging, a public YC adapter, specialist-domain search, and watched-company checks feed a shared canonical-resolution and logical-job identity layer before the existing profession gate, ranking, Gemini evaluation, cover-letter generation, and Telegram delivery. Final `high_priority` and `package_match` evaluations can promote a company into the persistent watchlist; later runs check the best known ATS/careers endpoint directly.

**Tech Stack:** Python 3.12, SQLite, `requests`, `beautifulsoup4`, existing Ashby/Lever/Greenhouse adapters, existing DuckDuckGo HTML search adapter, Gemini REST client, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-31-job-hunter-bot-r2-automated-discovery-design.md`

## Global Constraints

- R2 remains SQLite-first; do not introduce Supabase/Postgres or Relay dependencies.
- User-driven/Telegram job-URL ingestion remains deferred.
- Do not automate authenticated LinkedIn, Wellfound, Welcome to the Jungle, or other logged-in browsing.
- Do not submit job applications or automate CAPTCHA, 2FA, work-authorization attestations, salary commitments, or demographic answers.
- Canonical resolution is aggressive but best-effort: resolution failure preserves the original usable job URL and never drops the candidate.
- Fuzzy similarity alone never automatically merges jobs.
- Each source, watched company, targeted search, and canonical-resolution attempt fails open independently.
- R1 Gmail lifecycle tracking, backfill, staging, and review behavior remain unchanged.
- Automatic watch promotion occurs only after a final evaluation decision of `high_priority` or `package_match`; `possible_match`, `skip`, and `blocked` never auto-promote.
- Manual watch entries are never automatically deleted or permanently disabled.
- Watch health rule is exact: after 3 consecutive failed due checks, set `paused_until` to 24 hours after the third failure; a successful check resets failures to zero and clears the pause.
- Prefer a verified supported ATS target over a generic careers URL; never replace a working structured endpoint with a weaker guess.
- Preserve every trustworthy source URL and source ID as provenance after canonicalization.
- Preserve evaluation, material, delivery, and application-event history when duplicate jobs collapse.
- R2 source mechanics are fixed: YC uses a public-page adapter; Wellfound, Welcome to the Jungle, VC portfolio boards, and smaller specialist boards use targeted public search plus canonical resolution.

---

## File Structure

### New production files

- `src/job_hunter/job_identity.py` — conservative company/title/location normalization and strong logical-job identity helpers.
- `src/job_hunter/canonical.py` — supported ATS URL parsing, canonical resolution, confidence rules, and targeted resolution search interface.
- `src/job_hunter/watchlist.py` — promotion policy, manual seed sync, endpoint upgrade policy, and health constants.
- `src/job_hunter/sources/company_watch.py` — fail-open source for due watched companies.
- `src/job_hunter/sources/yc.py` — public YC job-page source.

### Modified production files

- `src/job_hunter/models.py` — add R2 job metadata plus `AtsReference`, `CanonicalResolution`, and `CompanyWatchSeed` dataclasses.
- `src/job_hunter/config.py` — load specialist search settings and manual watch seeds.
- `config/search.yml` — configure YC pages, specialist domains/templates, and manual watch seeds.
- `src/job_hunter/discovery_queries.py` — add specialist domain queries within the existing global search-query budget.
- `src/job_hunter/sources/__init__.py` — register YC; watched-company source remains constructed after `JobStore` exists.
- `src/job_hunter/fetching.py` — expose deterministic public-page metadata/link extraction used by canonicalization and generic careers pages.
- `src/job_hunter/store.py` — backward-compatible R2 schema, provenance, logical upsert/merge, and watch persistence.
- `src/job_hunter/discovery.py` — canonicalize before final dedupe/upsert and collect R2 discovery stats.
- `src/job_hunter/pipeline.py` — sync manual seeds, add watch discovery, promote strong companies, and log R2 metrics.
- `README.md` — document R2 behavior and configuration.

### New tests

- `tests/test_job_identity.py`
- `tests/test_canonical.py`
- `tests/test_yc_source.py`
- `tests/test_watchlist.py`
- `tests/test_company_watch_source.py`

### Existing tests to extend

- `tests/test_store.py`
- `tests/test_fetching.py`
- `tests/test_config.py`
- `tests/test_discovery_queries.py`
- `tests/test_discovery.py`
- `tests/test_pipeline.py`

---

### Task 1: Add Source-Independent Identity Types and Normalization

**Files:**
- Create: `src/job_hunter/job_identity.py`
- Modify: `src/job_hunter/models.py`
- Test: `tests/test_job_identity.py`

**Interfaces:**
- Produces `normalize_company_name(value: str) -> str`.
- Produces `normalize_job_title(value: str) -> str`.
- Produces `normalize_location(value: str) -> str`.
- Produces `company_identity_key(company: str) -> str`.
- Produces `job_fallback_identity(company: str, title: str, location: str) -> str | None`.
- Produces `locations_compatible(left: str, right: str) -> bool`.
- Adds `AtsReference(provider: str, board: str, job_id: str | None)`.
- Adds `CanonicalResolution(url: str, ats: AtsReference | None, confidence: float, method: str)`.
- Adds `CompanyWatchSeed(company_name: str, careers_url: str = "", ats_provider: str | None = None, ats_identifier: str | None = None)`.
- Extends `Job` with defaulted `original_url`, `canonical_url`, `ats_provider`, `ats_board`, and `ats_job_id` fields.

- [ ] **Step 1: Write failing identity tests**

Create `tests/test_job_identity.py` with:

```python
from job_hunter.job_identity import (
    company_identity_key,
    job_fallback_identity,
    locations_compatible,
    normalize_company_name,
    normalize_job_title,
)


def test_company_identity_ignores_safe_legal_suffix_and_punctuation():
    assert normalize_company_name("Acme GmbH") == "acme"
    assert normalize_company_name("ACME, GmbH") == "acme"
    assert company_identity_key("Acme GmbH") == company_identity_key("ACME")


def test_company_identity_keeps_meaningful_words():
    assert company_identity_key("Meta Platforms") != company_identity_key("Meta")


def test_title_normalization_is_exact_not_fuzzy():
    assert normalize_job_title(" Senior  Frontend Engineer ") == "senior frontend engineer"
    assert normalize_job_title("Senior Frontend Engineer") != normalize_job_title("Staff Frontend Engineer")


def test_fallback_identity_requires_company_and_title():
    assert job_fallback_identity("Acme", "Senior Frontend Engineer", "Berlin") == (
        "acme|senior frontend engineer|berlin"
    )
    assert job_fallback_identity("", "Senior Frontend Engineer", "Berlin") is None


def test_locations_compatible_allows_missing_or_contained_location():
    assert locations_compatible("", "Berlin") is True
    assert locations_compatible("Berlin, Germany", "Berlin") is True
    assert locations_compatible("Berlin", "New York") is False
```

- [ ] **Step 2: Verify the test fails**

```bash
pytest tests/test_job_identity.py -v
```

Expected: FAIL because `job_hunter.job_identity` is missing.

- [ ] **Step 3: Implement conservative normalizers**

In `job_identity.py`, tokenize using lowercase alphanumeric words and remove only trailing legal suffix tokens from this exact set:

```python
_SAFE_LEGAL_SUFFIXES = {
    "gmbh",
    "ag",
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "llc",
    "corp",
    "corporation",
}
```

`job_fallback_identity()` returns a key only when normalized company and title are both non-empty. `locations_compatible()` returns true when either side is empty, exact normalized locations match, or one normalized location string contains the other as a whole phrase.

- [ ] **Step 4: Extend `Job` and add R2 dataclasses**

Append fields so existing `Job(...)` callers remain source-compatible:

```python
original_url: str = ""
canonical_url: str = ""
ats_provider: str | None = None
ats_board: str | None = None
ats_job_id: str | None = None
```

Add the three dataclasses listed in **Interfaces** to `models.py`.

- [ ] **Step 5: Run focused regressions**

```bash
pytest tests/test_job_identity.py tests/test_normalize.py tests/test_discovery.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/job_identity.py src/job_hunter/models.py tests/test_job_identity.py
git commit -m "feat: add source-independent job identity"
```

---

### Task 2: Add Backward-Compatible Provenance and Company-Watch Schema

**Files:**
- Modify: `src/job_hunter/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces `JobStore.record_job_source(job_id: int, *, source: str, source_job_id: str | None, source_url: str) -> None`.
- Produces `JobStore.list_job_sources(job_id: int) -> list[sqlite3.Row]`.
- Produces `JobStore.find_job_by_canonical_url(url: str) -> int | None`.
- Produces `JobStore.find_job_by_ats(provider: str, board: str, job_id: str | None) -> int | None`.
- Produces `JobStore.find_job_by_identity(company: str, title: str, location: str) -> int | None`.

- [ ] **Step 1: Add a concrete legacy-schema test helper and failing migration test**

Append this helper to `tests/test_store.py`:

```python
import sqlite3


def _create_r1_jobs_only_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '',
            source_job_id TEXT,
            url TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            remote INTEGER,
            description TEXT NOT NULL DEFAULT '',
            description_hash TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jobs
            (fingerprint, source, url, company, title, description_hash,
             first_seen_at, last_seen_at)
        VALUES ('legacy', 'gmail:linkedin', 'https://example.test/job',
                'Acme', 'Frontend Engineer', '',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()


def test_r2_schema_upgrades_legacy_jobs_table(tmp_path):
    db = tmp_path / "state.sqlite3"
    _create_r1_jobs_only_db(db)

    store = JobStore(db)

    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(jobs)")}
    assert "canonical_url" in columns
    assert "ats_provider" in columns
    assert "ats_board" in columns
    assert "ats_job_id" in columns
    assert store.count_jobs() == 1
    tables = {
        row["name"]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "job_sources" in tables
    assert "company_watch" in tables
```

- [ ] **Step 2: Verify the migration test fails**

```bash
pytest tests/test_store.py::test_r2_schema_upgrades_legacy_jobs_table -v
```

Expected: FAIL because the R2 columns/tables are absent.

- [ ] **Step 3: Add idempotent schema migration**

Add columns only when absent using `PRAGMA table_info(jobs)`:

```text
canonical_url TEXT NOT NULL DEFAULT ''
ats_provider TEXT
ats_board TEXT
ats_job_id TEXT
```

Create:

```sql
CREATE TABLE IF NOT EXISTS job_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_job_id TEXT,
    source_url TEXT NOT NULL DEFAULT '',
    identity_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(job_id, identity_key)
);

CREATE TABLE IF NOT EXISTS company_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    normalized_company_name TEXT NOT NULL UNIQUE,
    careers_url TEXT NOT NULL DEFAULT '',
    ats_provider TEXT,
    ats_identifier TEXT,
    discovered_from_job_id INTEGER REFERENCES jobs(id),
    promotion_source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    first_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_successful_check_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

- [ ] **Step 4: Write provenance idempotency test**

```python
def test_record_job_source_is_idempotent(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(
        Job(source="yc", title="Frontend Engineer", company="Acme", url="https://yc.test/job/123")
    )

    store.record_job_source(
        job_id,
        source="yc",
        source_job_id="123",
        source_url="https://yc.test/job/123",
    )
    store.record_job_source(
        job_id,
        source="yc",
        source_job_id="123",
        source_url="https://yc.test/job/123",
    )

    rows = store.list_job_sources(job_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "yc"
    assert rows[0]["source_job_id"] == "123"
```

- [ ] **Step 5: Implement provenance and strong lookup methods**

`record_job_source()` uses an `identity_key` of `id:<source>:<source_job_id>` when source job ID exists, otherwise `url:<canonicalized source URL>`. `find_job_by_identity()` uses Task 1's exact normalized company/title plus compatible location; it returns a job only when exactly one row matches.

- [ ] **Step 6: Run store tests**

```bash
pytest tests/test_store.py -v
```

Expected: PASS, including R1 persistence behavior.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/store.py tests/test_store.py
git commit -m "feat: persist job provenance and company watches"
```

---

### Task 3: Implement ATS Parsing and Canonical Resolution

**Files:**
- Create: `src/job_hunter/canonical.py`
- Modify: `src/job_hunter/fetching.py`
- Test: `tests/test_canonical.py`
- Test: `tests/test_fetching.py`

**Interfaces:**
- Produces `parse_supported_ats_url(url: str) -> AtsReference | None`.
- Produces `CanonicalResolver(http, search_candidates: Callable[[Job], list[Job]], watch_target: Callable[[str], AtsReference | None])`.
- Produces `CanonicalResolver.resolve(job: Job) -> CanonicalResolution | None`.
- Produces `extract_job_page_links(html: str, base_url: str) -> list[str]` in `fetching.py`.

- [ ] **Step 1: Write failing ATS parsing tests**

Create `tests/test_canonical.py` with:

```python
from job_hunter.canonical import parse_supported_ats_url


def test_parse_lever_reference():
    ref = parse_supported_ats_url("https://jobs.lever.co/acme/abc-123")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("lever", "acme", "abc-123")


def test_parse_ashby_reference():
    ref = parse_supported_ats_url("https://jobs.ashbyhq.com/acme/xyz")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("ashby", "acme", "xyz")


def test_parse_greenhouse_reference():
    ref = parse_supported_ats_url("https://boards.greenhouse.io/acme/jobs/456")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("greenhouse", "acme", "456")
```

- [ ] **Step 2: Write deterministic resolver tests without placeholders**

Add a tiny response stub inside `tests/test_canonical.py`:

```python
class _Response:
    def __init__(self, *, url: str, text: str = "", status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.response
```

Then add:

```python
def test_direct_ats_url_wins_without_search():
    http = _Http(_Response(url="https://unused.test"))
    resolver = CanonicalResolver(http, search_candidates=lambda job: [], watch_target=lambda company: None)
    result = resolver.resolve(Job(source="yc", title="Frontend Engineer", company="Acme", url="https://jobs.lever.co/acme/abc"))
    assert result is not None
    assert result.url == "https://jobs.lever.co/acme/abc"
    assert result.confidence == 1.0
    assert http.calls == 0


def test_redirect_to_supported_ats_is_accepted():
    http = _Http(_Response(url="https://jobs.lever.co/acme/abc"))
    resolver = CanonicalResolver(http, search_candidates=lambda job: [], watch_target=lambda company: None)
    result = resolver.resolve(Job(source="board", title="Frontend Engineer", company="Acme", url="https://board.test/job"))
    assert result is not None
    assert result.method == "redirect"
    assert result.confidence == 0.98


def test_targeted_search_rejects_wrong_company():
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(source="duckduckgo", title="Frontend Engineer", company="Other", url="https://jobs.lever.co/other/abc")
        ],
        watch_target=lambda company: None,
    )
    result = resolver.resolve(Job(source="board", title="Frontend Engineer", company="Acme", url="https://board.test/job"))
    assert result is None


def test_resolution_failure_is_non_blocking():
    class _FailingHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("network down")

    resolver = CanonicalResolver(_FailingHttp(), search_candidates=lambda job: [], watch_target=lambda company: None)
    result = resolver.resolve(Job(source="board", title="Frontend Engineer", company="Acme", url="https://board.test/job"))
    assert result is None
```

- [ ] **Step 3: Verify resolver tests fail**

```bash
pytest tests/test_canonical.py -v
```

Expected: FAIL because `canonical.py` is absent.

- [ ] **Step 4: Add reusable HTML metadata extraction**

In `fetching.py`, implement `extract_job_page_links()` to return absolute URLs found in:

```text
<link rel="canonical">
JSON-LD objects with @type == "JobPosting" and a url field
anchors whose href is on jobs.lever.co, jobs.ashbyhq.com, or boards.greenhouse.io
```

Keep `enrich_job()` behavior unchanged.

- [ ] **Step 5: Implement resolver order and confidence exactly**

Use:

```text
1.00 direct supported ATS job URL already on candidate
0.98 response final URL redirects to supported ATS job URL
0.95 structured/embedded supported ATS job URL from fetched page
0.92 known watch ATS board plus exact normalized title match from targeted candidates
0.90 targeted search result with same normalized company, exact normalized title, and compatible location
```

Return `None` below `0.90`. No Gemini call is used for canonical resolution.

- [ ] **Step 6: Run canonical/fetching tests**

```bash
pytest tests/test_canonical.py tests/test_fetching.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/canonical.py src/job_hunter/fetching.py tests/test_canonical.py tests/test_fetching.py
git commit -m "feat: resolve canonical employer job postings"
```

---

### Task 4: Add Logical Job Upsert, Merge, and Cross-Source Dedupe

**Files:**
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/discovery.py`
- Test: `tests/test_store.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces `JobStore.upsert_logical_job(job: Job) -> tuple[int, bool, bool]`, retaining the existing `(job_id, is_new, description_changed)` shape.
- Produces `JobStore.merge_jobs(survivor_id: int, duplicate_id: int) -> int`.
- Extends `DiscoveryStats` with `canonical_resolved`, `canonical_unresolved`, and `cross_source_duplicates`.

- [ ] **Step 1: Write cross-source logical-upsert tests**

Append to `tests/test_store.py`:

```python
def test_same_canonical_job_from_two_sources_uses_one_job_id(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    first = Job(
        source="gmail:linkedin",
        title="Senior Frontend Engineer",
        company="Acme",
        url="https://linkedin.test/1",
        original_url="https://linkedin.test/1",
        canonical_url="https://jobs.lever.co/acme/abc",
        ats_provider="lever",
        ats_board="acme",
        ats_job_id="abc",
    )
    second = Job(
        source="yc",
        title="Senior Frontend Engineer",
        company="Acme GmbH",
        url="https://yc.test/2",
        original_url="https://yc.test/2",
        canonical_url="https://jobs.lever.co/acme/abc",
        ats_provider="lever",
        ats_board="acme",
        ats_job_id="abc",
    )

    first_id, _, _ = store.upsert_logical_job(first)
    second_id, _, _ = store.upsert_logical_job(second)

    assert first_id == second_id
    assert {row["source"] for row in store.list_job_sources(first_id)} == {
        "gmail:linkedin",
        "yc",
    }
```

Add false-merge tests:

```python
def test_different_titles_at_same_company_do_not_merge(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    first_id, _, _ = store.upsert_logical_job(
        Job(source="a", title="Senior Frontend Engineer", company="Acme", location="Berlin")
    )
    second_id, _, _ = store.upsert_logical_job(
        Job(source="b", title="Staff Frontend Engineer", company="Acme", location="Berlin")
    )
    assert first_id != second_id


def test_same_title_at_different_companies_does_not_merge(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    first_id, _, _ = store.upsert_logical_job(
        Job(source="a", title="Senior Frontend Engineer", company="Acme", location="Berlin")
    )
    second_id, _, _ = store.upsert_logical_job(
        Job(source="b", title="Senior Frontend Engineer", company="Beta", location="Berlin")
    )
    assert first_id != second_id
```

- [ ] **Step 2: Verify focused tests fail**

```bash
pytest tests/test_store.py -k "canonical_job or different_titles or different_companies" -v
```

Expected: FAIL because `upsert_logical_job()` is absent.

- [ ] **Step 3: Implement logical lookup precedence**

Use exact precedence:

```text
canonical_url
supported ATS provider + board + job ID
unique exact normalized company + title + compatible location
legacy fingerprint
insert new job
```

For a resolved job, persist `jobs.url = canonical_url`; otherwise persist the original URL. Always record the original source/provenance.

- [ ] **Step 4: Implement transactional `merge_jobs()`**

Before deleting duplicate job, re-parent:

```text
job_sources
application_events
materials
deliveries
evaluations
company_watch.discovered_from_job_id
```

Deduplicate `job_sources` during re-parenting. Preserve richer non-empty job fields and the canonical/ATS identity. Delete the duplicate job last.

- [ ] **Step 5: Integrate canonical resolution before in-run dedupe**

Change `collect_candidates()` to accept an optional resolver argument; pipeline supplies the real resolver, tests can inject a fake. For each candidate with a URL:

```python
job.original_url = job.original_url or job.url
resolution = resolver.resolve(job)
if resolution is not None:
    job.canonical_url = resolution.url
    job.url = resolution.url
    if resolution.ats is not None:
        job.ats_provider = resolution.ats.provider
        job.ats_board = resolution.ats.board
        job.ats_job_id = resolution.ats.job_id
```

If resolver returns `None`, keep `job.url` unchanged.

- [ ] **Step 6: Update `_dedupe()` strong keys**

Union only on:

```text
canonical URL equality
ATS provider + board + non-empty job ID equality
exact fallback identity from Task 1
```

Set `cross_source_duplicates = raw - unique` after in-run dedupe.

- [ ] **Step 7: Add end-to-end multi-source collapse test**

In `tests/test_discovery.py`, create three existing simple fake sources returning the same company/title/location with different URLs. Inject a resolver stub whose `resolve()` always returns:

```python
CanonicalResolution(
    url="https://jobs.lever.co/acme/abc",
    ats=AtsReference(provider="lever", board="acme", job_id="abc"),
    confidence=1.0,
    method="test",
)
```

Assert `stats.raw == 3`, `stats.unique == 1`, `stats.cross_source_duplicates == 2`, `store.count_jobs() == 1`, and three provenance records exist.

- [ ] **Step 8: Run discovery/store/Gmail staging regressions**

```bash
pytest tests/test_store.py tests/test_discovery.py tests/test_gmail_staged_source.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/store.py src/job_hunter/discovery.py tests/test_store.py tests/test_discovery.py
git commit -m "feat: dedupe jobs across discovery sources"
```

---

### Task 5: Add YC and Specialist-Domain Discovery

**Files:**
- Create: `src/job_hunter/sources/yc.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/config.py`
- Modify: `src/job_hunter/discovery_queries.py`
- Modify: `config/search.yml`
- Test: `tests/test_yc_source.py`
- Test: `tests/test_config.py`
- Test: `tests/test_discovery_queries.py`

**Interfaces:**
- Produces `YCSource(http, urls: list[str])`.
- Extends `SearchPolicy` with `specialist_search_domains`, `specialist_query_templates`, `yc_job_pages`, and `manual_company_watch`.

- [ ] **Step 1: Add specialist query test**

In `tests/test_discovery_queries.py`, construct `SearchPolicy` with the existing required fields plus:

```python
specialist_search_domains=["wellfound.com", "app.welcometothejungle.com"],
specialist_query_templates=['"{role}" remote Europe'],
yc_job_pages=[],
manual_company_watch=[],
```

Assert generated queries include:

```text
site:wellfound.com "senior frontend engineer" remote Europe
site:app.welcometothejungle.com "senior frontend engineer" remote Europe
```

and total query count never exceeds `max_search_queries_per_run`.

- [ ] **Step 2: Add exact R2 configuration**

Append to `config/search.yml`:

```yaml
specialist_search_domains:
  - wellfound.com
  - app.welcometothejungle.com
specialist_query_templates:
  - '"{role}" remote Europe'
  - '"{role}" Berlin'
yc_job_pages:
  - https://www.ycombinator.com/jobs/role
  - https://www.ycombinator.com/jobs/location/berlin
manual_company_watch: []
```

VC portfolio domains remain user-extensible through `specialist_search_domains`; do not hard-code a VC list.

- [ ] **Step 3: Add public YC fixture test**

Create `tests/test_yc_source.py` with a fake HTTP response whose HTML contains two job links with explicit data attributes used by the adapter contract:

```html
<a class="ycdc-card" href="/companies/acme/jobs/abc"
   data-company="Acme"
   data-title="Senior Product Engineer"
   data-location="Berlin, Germany">Senior Product Engineer</a>
```

The adapter may also support current YC page markup, but the parser must normalize this fixture into one `Job` with source `yc`, company `Acme`, title `Senior Product Engineer`, location `Berlin, Germany`, and absolute URL `https://www.ycombinator.com/companies/acme/jobs/abc`.

Add a second test where the first configured page returns HTTP 500 and the second returns a valid fixture; discovery returns jobs from the second page rather than raising.

- [ ] **Step 4: Implement YC public-page adapter**

Use `BeautifulSoup` and public HTML only. Support the explicit fixture contract plus current public job-card anchors/links. Never call an authenticated/private YC endpoint.

- [ ] **Step 5: Register YC and specialist settings**

`build_sources()` appends `YCSource` when `yc_job_pages` is non-empty. Wellfound and Welcome to the Jungle remain DuckDuckGo targeted-domain search; no new source classes are created for them.

- [ ] **Step 6: Run source/config/query tests**

```bash
pytest tests/test_yc_source.py tests/test_config.py tests/test_discovery_queries.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/sources/yc.py src/job_hunter/sources/__init__.py src/job_hunter/models.py src/job_hunter/config.py src/job_hunter/discovery_queries.py config/search.yml tests/test_yc_source.py tests/test_config.py tests/test_discovery_queries.py
git commit -m "feat: expand specialist job discovery"
```

---

### Task 6: Implement Watch Promotion, Manual Seeds, and Endpoint Upgrades

**Files:**
- Create: `src/job_hunter/watchlist.py`
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/config.py`
- Test: `tests/test_watchlist.py`
- Test: `tests/test_store.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces `should_auto_promote(evaluation: Evaluation) -> bool`.
- Produces `sync_manual_watch_seeds(store: JobStore, seeds: list[CompanyWatchSeed]) -> None`.
- Produces `promote_company(store: JobStore, *, job_id: int, job: Job, evaluation: Evaluation, confidence: float = 1.0) -> int | None`.
- Produces `JobStore.upsert_company_watch(...) -> int`.
- Produces `JobStore.get_company_watch(company_name: str) -> sqlite3.Row | None`.

- [ ] **Step 1: Add exact promotion-policy tests**

Create `tests/test_watchlist.py` and use the repository's `Evaluation` dataclass directly:

```python
import pytest
from job_hunter.models import Evaluation
from job_hunter.watchlist import should_auto_promote


def _evaluation(decision):
    return Evaluation(
        job_id=1,
        total_score=80,
        scores={},
        decision=decision,
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="test",
    )


@pytest.mark.parametrize(
    "decision,expected",
    [
        ("high_priority", True),
        ("package_match", True),
        ("possible_match", False),
        ("skip", False),
        ("blocked", False),
    ],
)
def test_auto_promotion_uses_final_decision(decision, expected):
    assert should_auto_promote(_evaluation(decision)) is expected
```

- [ ] **Step 2: Add manual seed and upgrade tests**

Add tests that:

```text
syncing the same manual Greenhouse seed twice creates one row
manual seed row has promotion_source == "manual"
a generic automatic careers URL cannot replace manual Greenhouse metadata
a high-confidence Greenhouse target upgrades an automatic generic careers-only entry
```

Use concrete seeds:

```python
CompanyWatchSeed(company_name="Acme GmbH", ats_provider="greenhouse", ats_identifier="acme")
CompanyWatchSeed(company_name="Beta", careers_url="https://beta.test/careers")
```

- [ ] **Step 3: Implement watch persistence and safe upgrade ordering**

Endpoint strength ordering is exact:

```text
verified supported ATS = 3
generic careers/jobs URL = 2
company-only entry = 1
```

An update can replace the target only when its strength is higher, or strength is equal and confidence is greater. Manual `promotion_source` remains `manual` once set.

- [ ] **Step 4: Implement automatic promotion from final job metadata**

If decision qualifies and `job.company` normalizes non-empty:

```text
supported ATS metadata on Job -> watch provider/board
canonical employer URL without ATS -> careers_url
no usable endpoint -> store company-only watch entry for later repair
```

Never derive an ATS board slug from company name.

- [ ] **Step 5: Run watch/config/store tests**

```bash
pytest tests/test_watchlist.py tests/test_store.py tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/watchlist.py src/job_hunter/store.py src/job_hunter/config.py tests/test_watchlist.py tests/test_store.py tests/test_config.py
git commit -m "feat: learn relevant company watch targets"
```

---

### Task 7: Add Watched-Company Discovery and Health Backoff

**Files:**
- Create: `src/job_hunter/sources/company_watch.py`
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/watchlist.py`
- Test: `tests/test_company_watch_source.py`
- Test: `tests/test_watchlist.py`

**Interfaces:**
- Produces `CompanyWatchSource(store: JobStore, http, now: Callable[[], datetime] = utc_now)` implementing `discover() -> list[Job]`.
- Produces `JobStore.list_due_company_watches(now: datetime) -> list[sqlite3.Row]`.
- Produces `JobStore.record_watch_success(watch_id: int, now: datetime) -> None`.
- Produces `JobStore.record_watch_failure(watch_id: int, now: datetime) -> None`.

- [ ] **Step 1: Add deterministic health tests**

In `tests/test_watchlist.py`, use:

```python
from datetime import datetime, timezone


def test_third_failure_pauses_for_24_hours(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = store.upsert_company_watch(
        company_name="Acme",
        careers_url="https://acme.test/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)
    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 3
    assert row["paused_until"] == "2026-09-01T12:00:00+00:00"


def test_success_clears_failures_and_pause(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = store.upsert_company_watch(
        company_name="Acme",
        careers_url="https://acme.test/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    store.record_watch_failure(watch_id, now)
    store.record_watch_success(watch_id, now)
    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 0
    assert row["paused_until"] is None
```

- [ ] **Step 2: Add structured ATS source test**

Create `tests/test_company_watch_source.py` with a Greenhouse watch entry and fake `get_json()` response matching the existing `GreenhouseSource` API. Assert returned job source is `watch:greenhouse` while source job ID and employer URL remain intact.

- [ ] **Step 3: Implement due-watch query and health writes**

`list_due_company_watches(now)` returns active rows where `paused_until IS NULL OR paused_until <= now`. On the third failure set the 24-hour pause. Successful check resets counters/pause and updates `last_successful_check_at` and `last_verified_at`.

- [ ] **Step 4: Implement ATS delegation**

For a watch row with supported provider, instantiate the existing provider adapter using `ats_identifier`, discover, and rewrite only `job.source` to `watch:<provider>`.

- [ ] **Step 5: Implement generic careers-page fallback**

Fetch one `careers_url` page only. Parse structured JSON-LD `JobPosting` entries and explicit links returned by `extract_job_page_links()`. Do not recursively crawl additional pages in R2.

Wrap each company check independently; an exception records a watch failure and continues to the next company.

- [ ] **Step 6: Run watch source tests**

```bash
pytest tests/test_company_watch_source.py tests/test_watchlist.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/sources/company_watch.py src/job_hunter/store.py src/job_hunter/watchlist.py tests/test_company_watch_source.py tests/test_watchlist.py
git commit -m "feat: discover jobs from watched companies"
```

---

### Task 8: Integrate R2 Discovery and Promotion Into the Pipeline

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes `CanonicalResolver`, `CompanyWatchSource`, `sync_manual_watch_seeds`, and `promote_company`.
- Keeps `run_pipeline(...) -> RunSummary` unchanged.

- [ ] **Step 1: Add package-match promotion integration test**

Use the existing `tests/test_pipeline.py` fake source/store/Gemini patterns. Configure a single job whose fake Gemini response produces `package_match`. After `run_pipeline()` assert:

```python
row = store.get_company_watch("Acme")
assert row is not None
assert row["promotion_source"] == "automatic"
```

Add a second test producing `possible_match` and assert `get_company_watch("Acme") is None`.

- [ ] **Step 2: Add watch failure isolation test**

Inject a normal fake source returning one valid job and monkeypatch `CompanyWatchSource.discover` to raise `RuntimeError("watch down")`. Assert the normal job still reaches evaluation and `summary.errors` is not incremented merely because the discovery source fails open.

- [ ] **Step 3: Build resolver dependencies after store/http exist**

Pipeline constructs:

```text
manual watch seed sync
base sources
GmailStagedSource(store)
CompanyWatchSource(store, http)
CanonicalResolver(http, targeted search callback, watch lookup callback)
collect_candidates(..., resolver=resolver)
```

The targeted canonical-search callback uses the existing DuckDuckGo HTML search mechanism with a small company/title query and returns `Job` candidates; it does not create a second general discovery run.

- [ ] **Step 4: Promote only after successful evaluation persistence**

Immediately after `store.save_evaluation(job_id, evaluation)`, call `promote_company()` inside a dedicated `try/except`. Promotion failure is logged and counted separately, but cover-letter/PDF/delivery continues.

- [ ] **Step 5: Run pipeline and R1 regressions**

```bash
pytest tests/test_pipeline.py tests/test_discovery.py tests/test_gmail_sync.py tests/test_gmail_staged_source.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/pipeline.py src/job_hunter/sources/__init__.py tests/test_pipeline.py
git commit -m "feat: integrate automated company discovery"
```

---

### Task 9: Preserve History During Late Canonicalization and Job Merge

**Files:**
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/discovery.py`
- Test: `tests/test_store.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Hardens `merge_jobs()` and logical upsert; no new interface.

- [ ] **Step 1: Add concrete merge-history test**

In `tests/test_store.py`:

1. Insert legacy job A with aggregator URL.
2. Save an evaluation, material, Telegram delivery, and `INTERVIEW` application event on A using existing store methods.
3. Insert job B with the same exact normalized company/title/location and canonical Lever metadata.
4. Call the merge path.

Assert:

```python
assert store.count_jobs() == 1
assert store.get_evaluation(survivor_id) is not None
assert store.get_material(survivor_id) is not None
assert store.has_delivery(survivor_id, "telegram_message")
assert store.current_application_state(survivor_id) == "INTERVIEW"
assert store.get_job(survivor_id).url == "https://jobs.lever.co/acme/abc"
```

- [ ] **Step 2: Implement deterministic survivor selection**

When two stored rows need consolidation, choose survivor in this exact order:

```text
row with application events
row with any evaluations/materials/deliveries
older first_seen_at
lower job ID
```

When a new canonical candidate matches one existing row, keep the existing row ID and enrich it rather than replacing it.

- [ ] **Step 3: Ensure provenance from both jobs survives merge**

Before deleting duplicate, upsert every duplicate `job_sources` record onto survivor and preserve first/last seen bounds.

- [ ] **Step 4: Run history regressions**

```bash
pytest tests/test_store.py tests/test_discovery.py tests/test_gmail_matching.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/job_hunter/store.py src/job_hunter/discovery.py tests/test_store.py tests/test_discovery.py
git commit -m "fix: preserve history during canonical job merges"
```

---

### Task 10: Add R2 Observability and Documentation

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `README.md`
- Test: `tests/test_discovery.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Finalizes R2 counters/logs; no new external API.

- [ ] **Step 1: Add metric assertions**

Extend discovery tests to assert:

```text
raw=3
unique=1
cross_source_duplicates=2
canonical_resolved >= 1
```

Extend pipeline tests to assert one qualifying evaluation increments/logs one company promotion and that watch checks report successes/failures without private content.

- [ ] **Step 2: Emit compact ownership-specific logs**

`discovery.py` logs source counts plus:

```text
canonical_resolved=<n> canonical_unresolved=<n> cross_source_duplicates=<n>
```

`pipeline.py` logs:

```text
companies_promoted=<n> watch_checks=<n> watch_paused=<n>
```

Never log Gmail bodies, secrets, CV text, or raw Gemini prompts.

- [ ] **Step 3: Document R2 architecture**

README must describe:

```text
Gmail + existing sources + YC + specialist-domain search + company watch
  -> canonical resolution + provenance/dedupe
  -> existing filter/rank/evaluate/deliver
  -> high_priority/package_match may promote company
```

- [ ] **Step 4: Document manual watch configuration**

Include:

```yaml
manual_company_watch:
  - company_name: Example GmbH
    ats_provider: greenhouse
    ats_identifier: example
  - company_name: Another Company
    careers_url: https://example.com/careers
```

Explain the exact three-failure/24-hour pause rule and that manual entries are preserved.

- [ ] **Step 5: Document specialist source mechanics and privacy boundary**

State that YC uses public pages; Wellfound, Welcome to the Jungle, and added portfolio domains use public targeted search; R2 performs no authenticated scraping.

- [ ] **Step 6: Run observability regressions**

```bash
pytest tests/test_discovery.py tests/test_pipeline.py tests/test_config.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/discovery.py src/job_hunter/pipeline.py README.md tests/test_discovery.py tests/test_pipeline.py
git commit -m "docs: document R2 automated discovery"
```

---

### Task 11: Full Verification and Release Readiness

**Files:**
- No planned production changes; fix only concrete defects revealed by verification and commit each fix separately.

**Interfaces:** None.

- [ ] **Step 1: Run the complete test suite**

```bash
pytest -q
```

Expected: all tests pass.

- [ ] **Step 2: Run focused R1 regressions**

```bash
pytest tests/test_gmail_auth.py tests/test_gmail_client.py tests/test_gmail_classifier.py tests/test_gmail_matching.py tests/test_gmail_sync.py tests/test_gmail_staged_source.py -q
```

Expected: all pass.

- [ ] **Step 3: Run focused R2 regressions**

```bash
pytest tests/test_job_identity.py tests/test_canonical.py tests/test_yc_source.py tests/test_watchlist.py tests/test_company_watch_source.py tests/test_store.py tests/test_discovery.py tests/test_pipeline.py -v
```

Expected: all pass.

- [ ] **Step 4: Run repository CI commands locally**

Read `.github/workflows/ci.yml` and execute exactly the Python install/test/static commands already used there. Do not introduce a new formatter or linter for R2.

- [ ] **Step 5: Run dry-run smoke test**

With the existing local development secrets/profile variables loaded:

```bash
JOB_HUNTER_DRY_RUN=1 python -m job_hunter run
```

Expected behavior:

```text
existing sources continue if YC or one watch fails
specialist-domain queries run through normal search
canonical failures retain original URLs
watch failures do not abort the run
Telegram is not called in dry-run mode
```

- [ ] **Step 6: Inspect R2 SQLite state**

```bash
sqlite3 var/job_hunter.sqlite3 "SELECT COUNT(*) FROM job_sources;"
sqlite3 var/job_hunter.sqlite3 "SELECT company_name,promotion_source,active,paused_until,consecutive_failures FROM company_watch ORDER BY company_name;"
sqlite3 var/job_hunter.sqlite3 "SELECT id,company,title,url,canonical_url,ats_provider,ats_board,ats_job_id FROM jobs ORDER BY id DESC LIMIT 20;"
```

Confirm multi-source examples are one logical job with multiple provenance rows.

- [ ] **Step 7: Verify branch scope**

```bash
git diff main...HEAD --stat
git status --short
```

Expected: only R2 spec/plan plus implementation/test/docs files intentionally required by this plan; working tree clean after commits.

---

## Implementation Order and Review Gates

Execute Tasks 1–11 in order:

```text
identity/types
  -> persistence/provenance
  -> canonical resolver
  -> logical dedupe/merge
  -> specialist discovery
  -> watch promotion
  -> watch discovery/health
  -> pipeline integration
  -> late-canonical history preservation
  -> observability/docs
  -> verification
```

Recommended review checkpoints:

1. After Task 4, verify canonical identity and merge safety before expanding sources.
2. After Task 8, verify automatic watch promotion is strictly downstream of final strong evaluation.
3. After Task 9, verify R1 application/evaluation/delivery history survives canonical merging.
4. After Task 11, perform final spec-conformance review before PR merge.

## Explicit Non-Goals During Execution

Do not add:

- Supabase/Postgres persistence.
- Relay integration.
- Telegram inbound URL ingestion.
- LinkedIn logged-in automation.
- Wellfound or Welcome to the Jungle authenticated scraping.
- A generic recursive crawler framework.
- Automatic application submission.
- Application-outcome learning/ranking feedback.
- Additional dedicated specialist-board scrapers beyond YC without revising the approved R2 spec.

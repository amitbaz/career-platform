# Trustworthy Job Evaluation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give job evaluation a persisted content-trust signal, upgrade job descriptions to authoritative ATS content when canonical resolution finds it, make Gemini explicitly separate must-have from preferred requirements and check each against real candidate evidence, and deterministically prevent thin content or a major unsupported must-have from producing a confident `package_match`/`high_priority` decision.

**Architecture:** A new `content_confidence` module defines a 5-tier trust scale (`official_ats` > `canonical_employer_page` > `source_detail_page` > `aggregator_text` > `partial_unknown`) and a rank helper. The tier is set once per raw job right after discovery (from `job.source`), travels with the description through in-run dedup and store-level persistence via confidence-aware merge (replacing today's pure-length `_richer_text` comparison), gets upgraded by `fetching.enrich_job` when HTML enrichment fills a description, and gets upgraded again by canonical resolution when it lands on a supported ATS board (reusing that ATS adapter's existing board-listing endpoint, matched by URL). `evaluate_job` is extended to receive the tier implicitly via `job.content_confidence`, ask Gemini to extract must-have/preferred requirements with a depth and a candidate-support classification before scoring, and deterministically cap the decision below `package_match`/`high_priority` whenever content is `partial_unknown` or any must-have requirement beyond "familiarity" depth comes back `unsupported`.

**Tech Stack:** Python 3, SQLite (via `job_hunter.store.JobStore`), pytest, existing `HttpClient`/`GeminiClient` wrappers. No new dependencies.

---

## Resolved design decisions (from brainstorming)

1. **5 trust tiers**, matching the issue's own wording: `official_ats`, `canonical_employer_page`, `source_detail_page`, `aggregator_text`, `partial_unknown`.
2. **Description upgrade** reuses each ATS adapter's existing board-listing endpoint (already returns full description for every job on the board in one call) — no new per-job API surface. Matched by canonicalized URL, not by job id (the id encoded in a public ATS URL is not guaranteed to equal the API's internal id).
3. **Requirement extraction folds into the existing `evaluate_job` Gemini call** (extends the current prompt/schema) rather than a second call, to keep Gemini call count flat per the issue's free-tier-guardrail constraint.
4. **Gating rule:** `package_match`/`high_priority` are unavailable when `job.content_confidence == "partial_unknown"`, OR when any `must_have` requirement has `candidate_support == "unsupported"` and `depth != "familiarity"`. Preferred/nice-to-have gaps never gate. This lets a role stay a visible `possible_match` lead even when gated — it is never silently dropped.

---

### Task 1: Content-confidence tier module + model fields

**Files:**
- Create: `src/job_hunter/content_confidence.py`
- Modify: `src/job_hunter/models.py` (`Job` dataclass, `Evaluation` dataclass)
- Test: `tests/test_content_confidence.py`

**Step 1: Write the failing test**

```python
# tests/test_content_confidence.py
from job_hunter.content_confidence import (
    AGGREGATOR_TEXT,
    CANONICAL_EMPLOYER_PAGE,
    OFFICIAL_ATS,
    PARTIAL_UNKNOWN,
    SOURCE_DETAIL_PAGE,
    infer_content_confidence,
    is_sufficient,
    tier_rank,
)


def test_ats_sources_infer_official_ats():
    assert infer_content_confidence("ashby", "Full JD text") == OFFICIAL_ATS
    assert infer_content_confidence("lever", "Full JD text") == OFFICIAL_ATS
    assert infer_content_confidence("greenhouse", "Full JD text") == OFFICIAL_ATS


def test_empty_description_is_always_partial_unknown_regardless_of_source():
    assert infer_content_confidence("ashby", "") == PARTIAL_UNKNOWN
    assert infer_content_confidence("ashby", "   ") == PARTIAL_UNKNOWN


def test_weak_and_unknown_sources():
    assert infer_content_confidence("hackernews", "some comment text") == AGGREGATOR_TEXT
    assert infer_content_confidence("wellfound", "full page body") == SOURCE_DETAIL_PAGE
    assert infer_content_confidence("some_future_source", "text") == AGGREGATOR_TEXT


def test_gmail_prefixed_sources_collapse_before_lookup():
    assert infer_content_confidence("gmail:abc123", "forwarded JD text") == AGGREGATOR_TEXT


def test_tier_rank_orders_best_to_worst():
    assert tier_rank(OFFICIAL_ATS) < tier_rank(CANONICAL_EMPLOYER_PAGE)
    assert tier_rank(CANONICAL_EMPLOYER_PAGE) < tier_rank(SOURCE_DETAIL_PAGE)
    assert tier_rank(SOURCE_DETAIL_PAGE) < tier_rank(AGGREGATOR_TEXT)
    assert tier_rank(AGGREGATOR_TEXT) < tier_rank(PARTIAL_UNKNOWN)


def test_tier_rank_treats_unset_as_worse_than_partial_unknown():
    assert tier_rank("") > tier_rank(PARTIAL_UNKNOWN)
    assert tier_rank("not_a_real_tier") > tier_rank(PARTIAL_UNKNOWN)


def test_is_sufficient():
    assert is_sufficient(OFFICIAL_ATS) is True
    assert is_sufficient(AGGREGATOR_TEXT) is True
    assert is_sufficient(PARTIAL_UNKNOWN) is False
    assert is_sufficient("") is False
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_content_confidence.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_hunter.content_confidence'`

**Step 3: Write minimal implementation**

```python
# src/job_hunter/content_confidence.py
from __future__ import annotations

OFFICIAL_ATS = "official_ats"
CANONICAL_EMPLOYER_PAGE = "canonical_employer_page"
SOURCE_DETAIL_PAGE = "source_detail_page"
AGGREGATOR_TEXT = "aggregator_text"
PARTIAL_UNKNOWN = "partial_unknown"

# Ordered most trustworthy first. Index doubles as the rank used for merge
# comparisons, so appending a new tier here must go in the right position.
TIERS = [
    OFFICIAL_ATS,
    CANONICAL_EMPLOYER_PAGE,
    SOURCE_DETAIL_PAGE,
    AGGREGATOR_TEXT,
    PARTIAL_UNKNOWN,
]

# Sources not listed here (including empty/future/unrecognized ones) default
# to AGGREGATOR_TEXT: not trusted as authoritative, but not treated as if it
# had no content either, since a description string is actually present.
_SOURCE_TIER: dict[str, str] = {
    "ashby": OFFICIAL_ATS,
    "lever": OFFICIAL_ATS,
    "greenhouse": OFFICIAL_ATS,
    "wellfound": SOURCE_DETAIL_PAGE,
    "devjobs": SOURCE_DETAIL_PAGE,
    "arbeitnow": AGGREGATOR_TEXT,
    "himalayas": AGGREGATOR_TEXT,
    "jobicy": AGGREGATOR_TEXT,
    "remoteok": AGGREGATOR_TEXT,
    "remotive": AGGREGATOR_TEXT,
    "weworkremotely": AGGREGATOR_TEXT,
    "hackernews": AGGREGATOR_TEXT,
    "yc": PARTIAL_UNKNOWN,
    "targeted_search": PARTIAL_UNKNOWN,
    "duckduckgo": PARTIAL_UNKNOWN,
    "company_watch": CANONICAL_EMPLOYER_PAGE,
}

_DEFAULT_TIER = AGGREGATOR_TEXT


def tier_rank(tier: str) -> int:
    """Lower is more trustworthy. An unrecognized/empty tier ranks worst of all."""
    try:
        return TIERS.index(tier)
    except ValueError:
        return len(TIERS)


def infer_content_confidence(source: str, description: str) -> str:
    """Infer a description's trust tier from its source and content.

    Call this once per raw job, immediately after a source adapter returns
    it and before any cross-source merge, so the tier always describes the
    exact description text it travels with.
    """
    if not description or not description.strip():
        return PARTIAL_UNKNOWN
    normalized_source = (source or "").split(":", 1)[0]
    return _SOURCE_TIER.get(normalized_source, _DEFAULT_TIER)


def is_sufficient(tier: str) -> bool:
    """Whether this tier is trustworthy enough to support a confident decision."""
    return tier == "" or tier is None or tier != PARTIAL_UNKNOWN and tier != ""
```

Fix the last function before running tests — the boolean logic above is deliberately wrong as a paste error; write it as:

```python
def is_sufficient(tier: str) -> bool:
    """Whether this tier is trustworthy enough to support a confident decision."""
    return bool(tier) and tier != PARTIAL_UNKNOWN
```

Then add the two new fields to `models.py`. In `Job` (around `models.py:96`, right after `source_page_html: str = ""`):

```python
    content_confidence: str = ""
```

In `Evaluation` (around `models.py:154`, right after `market_id: str = ""`):

```python
    content_confidence: str = ""
    requirements: dict = field(default_factory=dict)
```

(`field` is already imported at the top of `models.py`.)

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_content_confidence.py -v`
Expected: PASS (7 tests)

**Step 5: Commit**

```bash
git add src/job_hunter/content_confidence.py src/job_hunter/models.py tests/test_content_confidence.py
git commit -m "feat: add content-confidence tier model for job evaluation"
```

---

### Task 2: Discovery sets and merges content confidence

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Test: `tests/test_discovery.py`

**Step 1: Write the failing tests**

Add to `tests/test_discovery.py` (adapt imports/fixtures to match the file's existing style — it already builds `Job(...)` instances and calls `collect_candidates`/`_dedupe` directly per the recon notes):

```python
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS, PARTIAL_UNKNOWN
from job_hunter.discovery import _dedupe


def test_raw_jobs_get_content_confidence_from_source(store, http):
    # via collect_candidates with a fake AshbySource-like source returning
    # one job with description set; assert the persisted/returned job has
    # content_confidence == OFFICIAL_ATS. Follow the existing fake-source
    # fixture pattern already used elsewhere in this file.
    ...


def test_dedupe_prefers_higher_confidence_description_over_longer_weaker_one():
    weak = Job(
        source="hackernews",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-1",
        description="a" * 500,  # long but low-trust
        content_confidence=AGGREGATOR_TEXT,
    )
    strong = Job(
        source="ashby",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-1",
        description="short but authoritative JD",
        content_confidence=OFFICIAL_ATS,
    )
    merged, _ = _dedupe([weak, strong])
    assert len(merged) == 1
    assert merged[0].description == "short but authoritative JD"
    assert merged[0].content_confidence == OFFICIAL_ATS


def test_dedupe_still_fills_empty_description_from_weaker_source():
    empty = Job(
        source="targeted_search",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-2",
        description="",
        content_confidence=PARTIAL_UNKNOWN,
    )
    weak = Job(
        source="hackernews",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-2",
        description="a comment about the role",
        content_confidence=AGGREGATOR_TEXT,
    )
    merged, _ = _dedupe([empty, weak])
    assert merged[0].description == "a comment about the role"
    assert merged[0].content_confidence == AGGREGATOR_TEXT
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py -k "content_confidence or dedupe_prefers or dedupe_still_fills" -v`
Expected: FAIL — merged job keeps the longer/weaker description (current `_richness_key`/`_merge_fields` only look at `bool(description)` and raw presence).

**Step 3: Write minimal implementation**

In `discovery.py`, add the import (near the top, alongside the existing `job_hunter` imports):

```python
from job_hunter import content_confidence
```

Set the tier right after each raw job is collected, inside `collect_candidates`'s first loop (`discovery.py:286-293`):

```python
        for job in jobs:
            stats.raw += 1
            stats.per_source[job.source] = stats.per_source.get(job.source, 0) + 1
            if job.url:
                job.original_url = job.original_url or job.url
            job.content_confidence = content_confidence.infer_content_confidence(
                job.source, job.description
            )
            raw_market_id = _cheap_market_attribution(job, policy)
            _bump(stats.raw_by_market, raw_market_id or _UNATTRIBUTED)
            raw_jobs.append(job)
```

Replace `_richness_key` (`discovery.py:91-98`) to weigh confidence tier instead of a plain bool:

```python
def _richness_key(job: Job) -> tuple[bool, int, bool, bool, bool]:
    tier_score = len(content_confidence.TIERS) - 1 - content_confidence.tier_rank(
        job.content_confidence
    )
    return (
        _is_ats_url(job),
        tier_score,
        bool(job.company),
        bool(job.location),
        job.remote is not None,
    )
```

Replace the description-merge branch in `_merge_fields` (`discovery.py:108-109`):

```python
    if content_confidence.tier_rank(weaker.content_confidence) < content_confidence.tier_rank(
        richer.content_confidence
    ):
        richer.description = weaker.description
        richer.content_confidence = weaker.content_confidence
```

(This single condition also covers the "richer has no description yet" case, since an empty/unset tier always ranks worse than any real tier — see `test_tier_rank_treats_unset_as_worse_than_partial_unknown` in Task 1.)

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_discovery.py -v`
Expected: PASS, including all pre-existing discovery tests (no regressions — `_richness_key`'s other four tuple elements are unchanged, and `_merge_fields`'s new branch is a strict generalization of the old one).

**Step 5: Commit**

```bash
git add src/job_hunter/discovery.py tests/test_discovery.py
git commit -m "feat: make discovery dedup confidence-aware instead of description-presence-only"
```

---

### Task 3: `fetching.enrich_job` sets content confidence

**Files:**
- Modify: `src/job_hunter/fetching.py`
- Test: `tests/test_fetching.py`

**Step 1: Write the failing tests**

```python
from job_hunter.content_confidence import CANONICAL_EMPLOYER_PAGE, SOURCE_DETAIL_PAGE


def test_enrich_job_sets_canonical_employer_page_tier_from_json_ld(...):
    # existing test fixture that returns a page with a JobPosting JSON-LD
    # block; after enrich_job(job, http), assert:
    assert job.content_confidence == CANONICAL_EMPLOYER_PAGE


def test_enrich_job_sets_source_detail_page_tier_from_body_fallback(...):
    # existing test fixture that returns a page with no JSON-LD, only body text
    assert job.content_confidence == SOURCE_DETAIL_PAGE


def test_enrich_job_does_not_touch_confidence_when_description_already_present(...):
    job = Job(source="hackernews", title="x", description="already have text",
              content_confidence="aggregator_text", url="https://example.com/x")
    enrich_job(job, http)
    assert job.content_confidence == "aggregator_text"
```

Match these to the fixture/mocking conventions already used in `tests/test_fetching.py` (recon didn't detail them — check the file's existing `enrich_job` tests and mirror their HTTP-mock setup).

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_fetching.py -k content_confidence -v`
Expected: FAIL with `AttributeError` or the field staying at its default `""`.

**Step 3: Write minimal implementation**

In `fetching.py`, add the import and tag both description-producing branches of `extract_job_from_html`:

```python
from job_hunter import content_confidence
```

In the JSON-LD branch (`fetching.py:132-134`):

```python
        if desc := posting.get("description"):
            result["description"] = _strip_html(desc)
            result["description_confidence"] = content_confidence.CANONICAL_EMPLOYER_PAGE
```

In the fallback branch (`fetching.py:169-171`):

```python
    body_text = _strip_html(str(soup.body)) if soup.body else _strip_html(html)
    if body_text:
        result["description"] = body_text
        result["description_confidence"] = content_confidence.SOURCE_DETAIL_PAGE
```

In `enrich_job` (`fetching.py:199-200`):

```python
    if not job.description and (desc := data.get("description")):
        job.description = desc
        job.content_confidence = data.get(
            "description_confidence", content_confidence.SOURCE_DETAIL_PAGE
        )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_fetching.py -v`
Expected: PASS, all existing + new tests.

**Step 5: Commit**

```bash
git add src/job_hunter/fetching.py tests/test_fetching.py
git commit -m "feat: tag HTML-enriched descriptions with a content-confidence tier"
```

---

### Task 4: Store schema + confidence-aware persistence merge

**Files:**
- Modify: `src/job_hunter/store.py`
- Test: `tests/test_store.py`

**Step 1: Write the failing tests**

```python
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS


def test_upsert_logical_job_persists_content_confidence(store):
    job = Job(source="ashby", title="Eng", description="full JD", content_confidence=OFFICIAL_ATS)
    job_id, _, _ = store.upsert_logical_job(job)
    stored = store.get_job(job_id)
    assert stored.content_confidence == OFFICIAL_ATS


def test_upsert_logical_job_upgrades_description_by_confidence_not_length(store):
    weak = Job(
        source="hackernews", title="Eng", company="Acme", location="Remote",
        canonical_url="https://jobs.example.com/acme/1",
        description="a" * 300, content_confidence=AGGREGATOR_TEXT,
    )
    job_id, _, _ = store.upsert_logical_job(weak)

    strong = Job(
        source="ashby", title="Eng", company="Acme", location="Remote",
        canonical_url="https://jobs.example.com/acme/1",
        description="short authoritative JD", content_confidence=OFFICIAL_ATS,
    )
    same_id, _, changed = store.upsert_logical_job(strong)

    assert same_id == job_id
    assert changed is True
    stored = store.get_job(job_id)
    assert stored.description == "short authoritative JD"
    assert stored.content_confidence == OFFICIAL_ATS


def test_upsert_logical_job_keeps_stronger_description_against_weaker_update(store):
    strong = Job(
        source="ashby", title="Eng", company="Acme", location="Remote",
        canonical_url="https://jobs.example.com/acme/2",
        description="authoritative JD text", content_confidence=OFFICIAL_ATS,
    )
    job_id, _, _ = store.upsert_logical_job(strong)

    weak = Job(
        source="hackernews", title="Eng", company="Acme", location="Remote",
        canonical_url="https://jobs.example.com/acme/2",
        description="a" * 5000, content_confidence=AGGREGATOR_TEXT,
    )
    store.upsert_logical_job(weak)

    stored = store.get_job(job_id)
    assert stored.description == "authoritative JD text"
    assert stored.content_confidence == OFFICIAL_ATS


def test_save_evaluation_persists_content_confidence_and_requirements(store):
    job_id, _, _ = store.upsert_job(Job(source="ashby", title="Eng", description="JD", content_confidence=OFFICIAL_ATS))
    evaluation = Evaluation(
        job_id=job_id, total_score=80, scores={}, decision="package_match",
        hard_blockers=[], strengths=[], gaps=[], salary_note="", location_note="",
        rationale="", model="test", content_confidence=OFFICIAL_ATS,
        requirements={"must_have": [], "preferred": []},
    )
    store.save_evaluation(job_id, evaluation)
    saved = store.get_evaluation(job_id)
    assert saved.content_confidence == OFFICIAL_ATS
    assert saved.requirements == {"must_have": [], "preferred": []}
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py -k content_confidence -v`
Expected: FAIL — column doesn't exist / field not round-tripped / weaker description still wins on length.

**Step 3: Write minimal implementation**

Add the import:

```python
from job_hunter import content_confidence
```

Add a new migration dict near `_R2_JOB_COLUMNS` (`store.py:51-57`):

```python
_R3_JOB_COLUMNS = {
    "content_confidence": "TEXT NOT NULL DEFAULT ''",
}
```

Wire it into `_init_db`/add a migration method mirroring the R2 pattern (`store.py:317-340`):

```python
    def _migrate_jobs_to_r3_schema(self) -> None:
        self._add_missing_columns("jobs", _R3_JOB_COLUMNS)
```

and call `self._migrate_jobs_to_r3_schema()` right after `self._migrate_jobs_to_r2_schema()` in `_init_db`.

Add a matching dict for the `evaluations` table near `_MARKET_EVALUATION_COLUMNS` (`store.py:115`):

```python
_CONTENT_TRUST_EVALUATION_COLUMNS = {
    "content_confidence_at_eval": "TEXT NOT NULL DEFAULT ''",
    "requirements_json": "TEXT NOT NULL DEFAULT '{}'",
}
```

and call `self._add_missing_columns("evaluations", _CONTENT_TRUST_EVALUATION_COLUMNS)` right after the existing `_MARKET_EVALUATION_COLUMNS` call (`store.py:325`).

Replace the `_richer_text` staticmethod (`store.py:953-956`) with a confidence-aware version:

```python
    @staticmethod
    def _better_description(
        current: str, current_confidence: str, candidate: str, candidate_confidence: str
    ) -> tuple[str, str]:
        if not candidate:
            return current, current_confidence
        if not current:
            return candidate, candidate_confidence
        current_rank = content_confidence.tier_rank(current_confidence)
        candidate_rank = content_confidence.tier_rank(candidate_confidence)
        if candidate_rank < current_rank:
            return candidate, candidate_confidence
        if candidate_rank > current_rank:
            return current, current_confidence
        if len(candidate.strip()) > len(current.strip()):
            return candidate, candidate_confidence
        return current, current_confidence
```

Before deleting `_richer_text`, grep for other callers:

Run: `grep -rn "_richer_text" src/job_hunter/store.py`

If (as expected from Task recon) its only callers are `_update_logical_job` and `_merge_jobs`, delete it; otherwise leave it in place for the remaining caller and just stop using it in these two methods.

Update `_update_logical_job` (`store.py:740-787`): replace the single-value description line with the pair-aware call, and persist both columns:

```python
        description, description_confidence = self._better_description(
            row["description"], row["content_confidence"], job.description, job.content_confidence
        )
        description_changed = description_hash(description) != row["description_hash"]
```

and add `content_confidence = ?` to the `UPDATE jobs SET ...` statement, with `description_confidence` added to the parameter tuple (in place of/alongside the existing `description`/`description_hash` values already there).

Update `_merge_jobs`'s description merge (`store.py:828-830`) the same way:

```python
        description, description_confidence = self._better_description(
            survivor["description"], survivor["content_confidence"],
            duplicate["description"], duplicate["content_confidence"],
        )
```

and thread `content_confidence` into that method's `UPDATE jobs SET ...` statement alongside `description`/`description_hash`.

Update `_insert_logical_job` (`store.py:705-738`) to include `content_confidence` in its `INSERT INTO jobs` column list and values (`job.content_confidence or ""`).

Update `upsert_job` (`store.py:540-628`, the legacy per-source path) to include `content_confidence` in both its INSERT and UPDATE statements — this path does no richness comparison today (it always overwrites), so just pass `job.content_confidence or ""` through unconditionally like it already does for `description`.

Update `get_job` (`store.py:1939-1960`) to select and return `content_confidence`:

```python
            SELECT source, title, company, location, url, description,
                   source_job_id, remote, market_id, content_confidence
            FROM jobs WHERE id = ?
```
```python
            content_confidence=row["content_confidence"] or "",
```

Update `save_evaluation` (`store.py:1834-1867`) to read and persist the new columns:

```python
            row = self._conn.execute(
                "SELECT description_hash, content_confidence FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            description_hash_value = row["description_hash"] if row else ""
            content_confidence_value = row["content_confidence"] if row else ""
            self._conn.execute(
                """
                INSERT INTO evaluations
                    (job_id, total_score, scores_json, decision,
                     hard_blockers_json, strengths_json, gaps_json,
                     salary_note, location_note, rationale, model, status,
                     market_id, description_hash_at_eval, content_confidence_at_eval,
                     requirements_json, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, evaluation.total_score, json.dumps(evaluation.scores),
                    evaluation.decision, json.dumps(evaluation.hard_blockers),
                    json.dumps(evaluation.strengths), json.dumps(evaluation.gaps),
                    evaluation.salary_note, evaluation.location_note, evaluation.rationale,
                    evaluation.model, evaluation.status, evaluation.market_id,
                    description_hash_value, content_confidence_value,
                    json.dumps(evaluation.requirements), _now_iso(),
                ),
            )
```

(Note this now uses the job's *current* `content_confidence` at save time, not `evaluation.content_confidence` from the caller, mirroring how `description_hash_at_eval` already snapshots from `jobs` rather than trusting the caller — keeps a single source of truth.)

Update `get_evaluation` (`store.py:1962-1991`) to select and return the two new columns:

```python
            SELECT total_score, scores_json, decision, hard_blockers_json,
                   strengths_json, gaps_json, salary_note, location_note,
                   rationale, model, status, market_id, content_confidence_at_eval,
                   requirements_json
            FROM evaluations
```
```python
            content_confidence=row["content_confidence_at_eval"],
            requirements=json.loads(row["requirements_json"]) if row["requirements_json"] else {},
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -v`
Expected: PASS, all existing + new tests. Existing tests that build `Evaluation(...)` positionally/by-keyword without the two new fields must still work since both have defaults.

**Step 5: Commit**

```bash
git add src/job_hunter/store.py tests/test_store.py
git commit -m "feat: persist content confidence and make description merge confidence-aware"
```

---

### Task 5: ATS adapters expose a description-by-URL fetch

**Files:**
- Modify: `src/job_hunter/sources/ashby.py`, `src/job_hunter/sources/lever.py`, `src/job_hunter/sources/greenhouse.py`
- Modify: `src/job_hunter/canonical.py`
- Test: `tests/test_ats_adapters.py`, `tests/test_canonical.py`

**Step 1: Write the failing tests**

In `tests/test_ats_adapters.py` (mirroring however it currently fakes `http.get_json` for the three adapters' `discover()` tests):

```python
def test_ashby_fetch_description_matches_by_url(fake_http_returning_board_json):
    result = ashby.fetch_description("acme", "https://jobs.ashbyhq.com/acme/abc", fake_http)
    assert result == "Full JD text"


def test_ashby_fetch_description_returns_none_when_url_not_found(fake_http_returning_board_json):
    result = ashby.fetch_description("acme", "https://jobs.ashbyhq.com/acme/does-not-exist", fake_http)
    assert result is None
```

Add equivalent `test_lever_fetch_description_*` and `test_greenhouse_fetch_description_*` pairs.

In `tests/test_canonical.py`:

```python
def test_fetch_authoritative_description_dispatches_by_provider(monkeypatch):
    ats = AtsReference(provider="ashby", board="acme", job_id="abc")
    called = {}
    def fake_fetch(board, url, http):
        called["args"] = (board, url)
        return "authoritative text"
    monkeypatch.setattr("job_hunter.sources.ashby.fetch_description", fake_fetch)
    result = fetch_authoritative_description(ats, "https://jobs.ashbyhq.com/acme/abc", http=object())
    assert result == "authoritative text"
    assert called["args"] == ("acme", "https://jobs.ashbyhq.com/acme/abc")


def test_fetch_authoritative_description_returns_none_for_unsupported_provider():
    ats = AtsReference(provider="unknown_provider", board="acme", job_id="abc")
    assert fetch_authoritative_description(ats, "https://example.com/x", http=object()) is None


def test_fetch_authoritative_description_swallows_fetch_errors(monkeypatch):
    ats = AtsReference(provider="ashby", board="acme", job_id="abc")
    def raising_fetch(board, url, http):
        raise RuntimeError("network error")
    monkeypatch.setattr("job_hunter.sources.ashby.fetch_description", raising_fetch)
    assert fetch_authoritative_description(ats, "https://jobs.ashbyhq.com/acme/abc", http=object()) is None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ats_adapters.py tests/test_canonical.py -k "fetch_description or fetch_authoritative" -v`
Expected: FAIL with `AttributeError`/`ImportError` — functions don't exist yet.

**Step 3: Write minimal implementation**

Add to `ashby.py` (uses the same `_URL_TEMPLATE` and `strip_html` already imported there; add `canonicalize_url`):

```python
from job_hunter.normalize import canonicalize_url


def fetch_description(board: str, target_url: str, http) -> str | None:
    """Return the full description for the job at target_url on this board.

    Reuses the same board-listing endpoint discover() already hits, since
    it returns every job's full description in one response — no separate
    per-job endpoint needed.
    """
    data = http.get_json(_URL_TEMPLATE.format(board=board))
    target = canonicalize_url(target_url)
    for item in data.get("jobs", []):
        if canonicalize_url(item.get("jobUrl", "")) == target:
            description = item.get("descriptionPlain") or item.get("descriptionHtml", "")
            return strip_html(description) or None
    return None
```

Add to `lever.py`:

```python
from job_hunter.normalize import canonicalize_url


def fetch_description(site: str, target_url: str, http) -> str | None:
    data = http.get_json(_URL_TEMPLATE.format(site=site))
    target = canonicalize_url(target_url)
    for item in data:
        if canonicalize_url(item.get("hostedUrl", "")) == target:
            description = item.get("descriptionPlain") or item.get("description", "")
            return strip_html(description) or None
    return None
```

Add to `greenhouse.py`:

```python
from job_hunter.normalize import canonicalize_url


def fetch_description(token: str, target_url: str, http) -> str | None:
    data = http.get_json(_URL_TEMPLATE.format(token=token))
    target = canonicalize_url(target_url)
    for item in data.get("jobs", []):
        if canonicalize_url(item.get("absolute_url", "")) == target:
            return strip_html(item.get("content", "")) or None
    return None
```

Add to `canonical.py` (near the top, with the other imports):

```python
from job_hunter.sources import ashby as ashby_source
from job_hunter.sources import greenhouse as greenhouse_source
from job_hunter.sources import lever as lever_source

_DESCRIPTION_FETCHERS = {
    "ashby": ashby_source.fetch_description,
    "lever": lever_source.fetch_description,
    "greenhouse": greenhouse_source.fetch_description,
}


def fetch_authoritative_description(
    ats: AtsReference, target_url: str, http: "HttpClient"
) -> str | None:
    """Fetch the full official description for a resolved ATS posting.

    Non-fatal by design, matching the rest of this module: a fetch failure
    here should never take down canonical resolution.
    """
    fetcher = _DESCRIPTION_FETCHERS.get(ats.provider)
    if fetcher is None:
        return None
    try:
        return fetcher(ats.board, target_url, http)
    except Exception:
        return None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ats_adapters.py tests/test_canonical.py -v`
Expected: PASS, all existing + new tests.

**Step 5: Commit**

```bash
git add src/job_hunter/sources/ashby.py src/job_hunter/sources/lever.py src/job_hunter/sources/greenhouse.py src/job_hunter/canonical.py tests/test_ats_adapters.py tests/test_canonical.py
git commit -m "feat: let ATS adapters fetch a job's authoritative description by URL"
```

---

### Task 6: Discovery upgrades description when canonical resolution lands on an ATS posting

**Files:**
- Modify: `src/job_hunter/discovery.py`
- Test: `tests/test_discovery.py`

**Step 1: Write the failing test**

```python
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS


def test_canonical_resolution_upgrades_description_when_ats_found(
    store, http, monkeypatch
):
    # Build a resolver whose .resolve() returns a CanonicalResolution with
    # ats=AtsReference(provider="ashby", board="acme", job_id="abc") and
    # url="https://jobs.ashbyhq.com/acme/abc", for a job that started on
    # hackernews with AGGREGATOR_TEXT confidence and weak description text.
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: "The real authoritative JD",
    )
    # ... run collect_candidates with that resolver and a single hackernews job ...
    eligible = result.eligible
    assert eligible[0][1].description == "The real authoritative JD"
    assert eligible[0][1].content_confidence == OFFICIAL_ATS


def test_canonical_resolution_skips_description_fetch_when_already_official_ats(
    store, http, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: calls.append(1) or "should not be used",
    )
    # ... job that already has source="ashby", content_confidence=OFFICIAL_ATS,
    # and a real description, going through the direct-ATS resolution path ...
    assert calls == []


def test_canonical_resolution_keeps_existing_description_when_fetch_fails(
    store, http, monkeypatch
):
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: None,
    )
    # ... hackernews job with description "original weak text" ...
    assert eligible[0][1].description == "original weak text"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_discovery.py -k canonical_resolution_upgrades -v`
Expected: FAIL — description stays as the original weak text; no fetch is attempted.

**Step 3: Write minimal implementation**

Add the import in `discovery.py`:

```python
from job_hunter.canonical import (
    CanonicalResolver,
    fetch_authoritative_description,
    parse_supported_ats_url,
)
```

In the canonical-resolution success branch (`discovery.py:415-420`, right after `ats_job_id` is set and the board is harvested):

```python
                    if resolution.ats is not None:
                        job.ats_provider = resolution.ats.provider
                        job.ats_board = resolution.ats.board
                        job.ats_job_id = resolution.ats.job_id
                        if _harvest_ats_board_safely(store, job):
                            stats.ats_boards_discovered += 1
                        if job.content_confidence != content_confidence.OFFICIAL_ATS:
                            authoritative = fetch_authoritative_description(
                                resolution.ats, resolution.url, http
                            )
                            if authoritative:
                                job.description = authoritative
                                job.content_confidence = content_confidence.OFFICIAL_ATS
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_discovery.py -v`
Expected: PASS, all existing + new tests.

**Step 5: Commit**

```bash
git add src/job_hunter/discovery.py tests/test_discovery.py
git commit -m "feat: upgrade job description to the authoritative ATS posting on canonical resolution"
```

---

### Task 7: Requirement-aware evaluation prompt, schema, and deterministic gating

**Files:**
- Modify: `src/job_hunter/evaluation.py`
- Test: `tests/test_evaluation.py`

This is the task that closes out the acceptance criteria directly, so its tests map onto the issue's own scenario list: official ATS full content, deep required-skill mismatch, acceptable stretch-skill, insufficient content.

**Step 1: Update the shared fixture first**

`_valid_payload()` in `tests/test_evaluation.py` must grow a `requirements` block, since it will become required. Update it (existing tests keep passing unchanged since they don't inspect this field):

```python
def _valid_payload(**overrides):
    payload = {
        "scores": {
            "role_seniority": 28,
            "technical": 22,
            "product_architecture": 18,
            "career_direction": 8,
            "location_language": 9,
            "company_environment": 4,
        },
        "total_score": 89,
        "hard_blockers": [],
        "strengths": ["React expertise"],
        "gaps": ["No Rust experience"],
        "salary_note": "Not disclosed",
        "location_note": "Remote EU friendly",
        "decision": "high_priority",
        "rationale": "Strong fit",
        "requirements": {
            "must_have": [
                {"requirement": "React", "depth": "experience", "candidate_support": "supported"}
            ],
            "preferred": [
                {"requirement": "GraphQL", "depth": "familiarity", "candidate_support": "unknown"}
            ],
        },
    }
    payload.update(overrides)
    return payload
```

Also update the `job` fixture to default `content_confidence="official_ats"` (it currently uses `source="ashby"`, so this matches):

```python
@pytest.fixture
def job():
    return Job(
        source="ashby",
        title="Senior Product Engineer",
        description="React TypeScript remote",
        content_confidence="official_ats",
    )
```

**Step 2: Write the new failing tests**

```python
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS, PARTIAL_UNKNOWN


def test_missing_requirements_field_is_rejected(fake_gemini, job, policy, context):
    payload = _valid_payload()
    del payload["requirements"]
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="requirements"):
        evaluate_job(job, context, policy, fake_gemini)


def test_invalid_requirement_depth_is_rejected(fake_gemini, job, policy, context):
    payload = _valid_payload()
    payload["requirements"]["must_have"][0]["depth"] = "nonsense"
    fake_gemini.text = json.dumps(payload)
    with pytest.raises(EvaluationError, match="depth"):
        evaluate_job(job, context, policy, fake_gemini)


def test_major_unsupported_must_have_caps_below_high_priority(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Deep PostgreSQL expertise", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.total_score == 89
    assert evaluation.decision not in ("high_priority", "package_match")
    assert evaluation.decision == "possible_match"


def test_familiarity_depth_unsupported_must_have_does_not_gate(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["must_have"] = [
        {"requirement": "Basic SQL familiarity", "depth": "familiarity", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "high_priority"


def test_unsupported_preferred_requirement_does_not_gate(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89)
    payload["requirements"]["preferred"] = [
        {"requirement": "PostgreSQL", "depth": "deep_expert", "candidate_support": "unsupported"}
    ]
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "high_priority"


def test_insufficient_content_confidence_caps_below_high_priority(fake_gemini, job, policy, context):
    job.content_confidence = PARTIAL_UNKNOWN
    payload = _valid_payload(total_score=89)
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "possible_match"


def test_insufficient_content_still_allows_possible_match_and_skip(fake_gemini, job, policy, context):
    job.content_confidence = PARTIAL_UNKNOWN
    payload = _valid_payload(total_score=50)
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "skip"  # below possible threshold on its own merits


def test_hard_blockers_still_force_blocked_regardless_of_requirements(fake_gemini, job, policy, context):
    payload = _valid_payload(total_score=89, hard_blockers=["Below salary floor"])
    fake_gemini.text = json.dumps(payload)
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.decision == "blocked"


def test_evaluation_persists_content_confidence_and_requirements(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluation = evaluate_job(job, context, policy, fake_gemini)
    assert evaluation.content_confidence == "official_ats"
    assert evaluation.requirements["must_have"][0]["requirement"] == "React"


def test_prompt_includes_content_confidence_tier(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]
    assert "official_ats" in prompt


def test_prompt_instructs_requirement_extraction(fake_gemini, job, policy, context):
    fake_gemini.text = json.dumps(_valid_payload())
    evaluate_job(job, context, policy, fake_gemini)
    prompt = fake_gemini.prompts[0][0]
    assert "must-have" in prompt.lower() or "must_have" in prompt
```

**Step 3: Run tests to verify they fail**

Run: `pytest tests/test_evaluation.py -v`
Expected: The new tests FAIL (no `requirements` validation, no gating, `Evaluation` construction doesn't set the two new fields). Confirm the *existing* tests also currently fail once `_valid_payload`/`job` fixtures are updated but the implementation isn't yet — that's expected and resolves in the next step.

**Step 4: Write the implementation**

Add the import and constants near the top of `evaluation.py`:

```python
from job_hunter import content_confidence

_VALID_DEPTHS = {"familiarity", "experience", "deep_expert"}
_VALID_SUPPORT = {"supported", "partial", "unsupported", "unknown"}

_TIER_PROMPT_HINTS = {
    content_confidence.OFFICIAL_ATS: "This is the official employer/ATS posting text.",
    content_confidence.CANONICAL_EMPLOYER_PAGE: "This was extracted from the employer's own careers page.",
    content_confidence.SOURCE_DETAIL_PAGE: "This is a full detail-page scrape; likely complete but not confirmed authoritative.",
    content_confidence.AGGREGATOR_TEXT: "This is third-party aggregator or community text and may be incomplete or stale.",
    content_confidence.PARTIAL_UNKNOWN: "This content is thin or unverified. Prefer 'unknown' candidate_support over guessing when the posting doesn't clearly state a requirement.",
}
```

Add a requirement-extraction instruction block, used by both prompt branches:

```python
_REQUIREMENT_EXTRACTION_RULES = """Before scoring, extract the posting's explicit requirements:
- must_have: requirements the posting states or clearly implies are required.
- preferred: requirements stated as nice-to-have, a plus, or preferred.
For each, state its required depth (familiarity, experience, or deep_expert) and classify
candidate_support strictly from the candidate context evidence below: supported, partial,
unsupported, or unknown. Do not infer expertise from adjacent technology mentions alone
(for example, React experience is not backend expertise, and API collaboration is not
evidence of independently designing backend systems). Do not invent requirements that are
not stated or clearly implied by the posting."""
```

Extend `_build_evaluation_prompt` (`evaluation.py:142-199`) — add the confidence line and the extraction rules to both the market and no-market branches, and extend the returned JSON shape. For the no-market branch:

```python
    if market is None:
        return f"""You are evaluating a job posting against a candidate profile for a remote-only job search.

Score EXACTLY these components, each an integer from 0 up to its stated maximum:
{maxima_lines}

{_REQUIREMENT_EXTRACTION_RULES}

Rules:
- Only use evidence present in the candidate context and job description below. Never invent candidate facts.
- Unstated or unclear requirements are gaps, not invented facts.
- Compensation floor is EUR {policy.salary_floor_eur}. A disclosed maximum below the floor is a hard blocker.
- A role that is not remote, or requires relocation, is a hard blocker.
- List every hard blocker in hard_blockers; otherwise leave it empty.

Return ONLY JSON with this exact shape and no markdown fences:
{{"scores": {{"role_seniority": int, "technical": int, "product_architecture": int, "career_direction": int, "location_language": int, "company_environment": int}}, "total_score": int, "hard_blockers": [string], "strengths": [string], "gaps": [string], "salary_note": string, "location_note": string, "decision": string, "rationale": string, "requirements": {{"must_have": [{{"requirement": string, "depth": string, "candidate_support": string}}], "preferred": [{{"requirement": string, "depth": string, "candidate_support": string}}]}}}}

Candidate context:
{_serialize_context(context)}

Job title: {job.title}
Company: {job.company}
Location: {job.location}
Remote: {job.remote}
Job content confidence: {job.content_confidence or content_confidence.PARTIAL_UNKNOWN} — {_TIER_PROMPT_HINTS.get(job.content_confidence, _TIER_PROMPT_HINTS[content_confidence.PARTIAL_UNKNOWN])}
Job description:
{job.description}
"""
```

Apply the equivalent additions (extraction rules block, extended JSON shape, content-confidence line) to the market branch below it.

Add the validator helper, above `evaluate_job`:

```python
def _validate_requirement_list(items: object, label: str) -> list[dict]:
    if not isinstance(items, list):
        raise EvaluationError(f"requirements.{label} must be a list")
    validated = []
    for item in items:
        if not isinstance(item, dict):
            raise EvaluationError(f"each requirements.{label} entry must be an object")
        requirement = item.get("requirement")
        depth = item.get("depth")
        support = item.get("candidate_support")
        if not isinstance(requirement, str) or not requirement:
            raise EvaluationError(f"requirements.{label}.requirement must be a non-empty string")
        if depth not in _VALID_DEPTHS:
            raise EvaluationError(f"requirements.{label}.depth {depth!r} must be one of {sorted(_VALID_DEPTHS)}")
        if support not in _VALID_SUPPORT:
            raise EvaluationError(
                f"requirements.{label}.candidate_support {support!r} must be one of {sorted(_VALID_SUPPORT)}"
            )
        validated.append({"requirement": requirement, "depth": depth, "candidate_support": support})
    return validated
```

In `evaluate_job` (`evaluation.py:202-268`), after the existing `hard_blockers` validation (`:240-242`) and before the decision `if/elif` chain, add:

```python
    requirements = data.get("requirements")
    if not isinstance(requirements, dict) or "must_have" not in requirements or "preferred" not in requirements:
        raise EvaluationError("requirements must be an object with 'must_have' and 'preferred' lists")
    must_have = _validate_requirement_list(requirements["must_have"], "must_have")
    preferred = _validate_requirement_list(requirements["preferred"], "preferred")

    major_unsupported_must_have = any(
        item["candidate_support"] == "unsupported" and item["depth"] != "familiarity"
        for item in must_have
    )
    insufficient_content = not content_confidence.is_sufficient(job.content_confidence)
    confident_decision_available = not major_unsupported_must_have and not insufficient_content
```

Replace the existing decision `if/elif` chain (`evaluation.py:244-253`):

```python
    if hard_blockers:
        decision = "blocked"
    elif total >= HIGH_PRIORITY_THRESHOLD and confident_decision_available:
        decision = "high_priority"
    elif total >= policy.thresholds.get("package", 75) and confident_decision_available:
        decision = "package_match"
    elif total >= policy.thresholds.get("possible", 65):
        decision = "possible_match"
    else:
        decision = "skip"
```

Update the final `Evaluation(...)` construction (`evaluation.py:255-268`) to pass the two new fields:

```python
    return Evaluation(
        job_id=0,
        total_score=total,
        scores=scores,
        decision=decision,
        hard_blockers=hard_blockers,
        strengths=data.get("strengths") or [],
        gaps=data.get("gaps") or [],
        salary_note=data.get("salary_note", "") or "",
        location_note=data.get("location_note", "") or "",
        rationale=data.get("rationale", "") or "",
        model=gemini.model,
        market_id=job.market_id or "",
        content_confidence=job.content_confidence or content_confidence.PARTIAL_UNKNOWN,
        requirements={"must_have": must_have, "preferred": preferred},
    )
```

**Step 5: Run tests to verify they pass**

Run: `pytest tests/test_evaluation.py -v`
Expected: PASS — all new tests, and every pre-existing test (they all go through the updated `_valid_payload()`/`job` fixtures, which already carry a valid `requirements` block and `content_confidence="official_ats"`, so `confident_decision_available` is always `True` for them and behavior is unchanged).

**Step 6: Commit**

```bash
git add src/job_hunter/evaluation.py tests/test_evaluation.py
git commit -m "feat: require Gemini to extract must-have/preferred requirements and gate decisions on them"
```

---

### Task 8: Full suite run and acceptance-criteria walkthrough

**Files:** none (verification only)

**Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS, zero failures. Pay particular attention to `tests/test_pipeline.py` (evaluate_job call sites), `tests/test_gmail_sync.py`/`tests/test_watchlist.py`/`tests/test_pipeline_navigator.py` (all use `store.upsert_job`, touched in Task 4) — these must be unaffected since `upsert_job` keeps its existing always-overwrite behavior, just with one more column threaded through.

**Step 2: Walk the issue's acceptance criteria checklist**

For each of the 11 checkboxes in GitHub issue #43, confirm which task addressed it and where its test lives:

- Weak-source job upgraded to authoritative content before evaluation → Task 6 (`test_canonical_resolution_upgrades_description_when_ats_found`)
- Canonical resolution cannot silently keep a weaker description → Task 2 + Task 4 (`test_dedupe_prefers_higher_confidence...`, `test_upsert_logical_job_upgrades_description_by_confidence_not_length`)
- Persisted, testable content-provenance/confidence notion → Task 1 + Task 4
- Insufficient/unknown-content jobs cannot become `package_match`/`high_priority` → Task 7 (`test_insufficient_content_confidence_caps_below_high_priority`)
- Gemini output explicitly represents must-have/preferred requirements vs candidate evidence → Task 7 (prompt + schema)
- Deep-PostgreSQL-style must-have gap with no evidence cannot produce an 80+ ready-to-apply result → Task 7 (`test_major_unsupported_must_have_caps_below_high_priority`)
- Normal/preferred PostgreSQL mention can still score strongly on a frontend-heavy role → Task 7 (`test_unsupported_preferred_requirement_does_not_gate`)
- Backend-dominant ownership not credited without evidence → already covered by the existing `_FULL_STACK_BACKEND_RAMP_PARAGRAPH` prompt rule (`evaluation.py:84-91`), now reinforced by requirement-level `candidate_support` classification — no new gap.
- Explanations expose material strengths/gaps, not just generic rationale → `strengths`/`gaps`/`requirements` are all persisted and already surfaced in `strengths`/`gaps` today; `requirements` adds the structured version.
- Deterministic validation prevents contradictory output → Task 7 gating (does not trust Gemini's own `decision` field, exactly like the pre-existing `hard_blockers` handling).
- Existing salary/location/language/sponsorship tests still pass → verified by the full suite run in Step 1 (no market-policy code paths were touched).

**Step 3: No commit for this task** — it is verification-only. If any acceptance criterion is found unaddressed, add a task above this one before considering the plan complete.

---

## Execution notes

- Every task after Task 1 depends on Task 1's `content_confidence` module and the two new dataclass fields — do not reorder.
- Task 6 depends on Task 5's `fetch_authoritative_description`.
- Task 7 depends on Task 4's `Evaluation.content_confidence`/`requirements` fields, but not on Tasks 2, 3, 5, or 6 — if time-constrained, Task 7 alone already delivers the requirement-aware scoring half of the issue, with content-confidence gating driven by whatever tier a job happens to carry (defaulting to `partial_unknown` for any job that predates this migration, which is the safe default).

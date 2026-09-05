# Job Hunter Bot v2.2 Profile-Aware Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add public source coverage, profile-aware prioritization, and source-diverse shortlisting while reducing full Gemini evaluations to a default ceiling of 35.

**Architecture:** Discover and hard-filter candidates as today, extract a compact `CandidatePreferences` model from the CV once per run, score candidates against those preferences, and select a source-diverse shortlist before full Gemini evaluation. Existing SQLite, decision, delivery, retry, and score-floor behavior remains compatible.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, PyYAML, SQLite, pytest, existing Gemini and Telegram clients.

**Spec:** `docs/superpowers/specs/2026-08-30-job-hunter-bot-v2-2-profile-aware-discovery-design.md`

## Global Constraints

- Do not change the Gemini model or existing component score weights.
- Do not add authenticated scraping, paid search APIs, browser automation, or employer-form submission.
- Keep current sources and add only public unauthenticated Jobicy and Himalayas adapters.
- Preserve SQLite artifacts and existing evaluation/material/delivery retry behavior.
- Keep hard gates before ranking; description keywords cannot turn a blocked profession into an eligible candidate.
- Default `max_jobs_per_run` is 35; it remains configurable.
- Scores `<=60` remain persisted but are never delivered, retried, or rendered.
- Do not log profile text, job descriptions, cover-letter text, credentials, or tokens.
- Tests must not make live network calls.
- Run `pytest -q` before completion.

---

### Task 1: Candidate preference extraction

**Files:**
- Create: `src/job_hunter/preferences.py`
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_preferences.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `CandidatePreferences` dataclass.
- Produces: `extract_candidate_preferences(profile: str, gemini: GeminiClient, policy: SearchPolicy) -> CandidatePreferences`.
- Produces: deterministic fallback from `SearchPolicy` when Gemini JSON is invalid or unavailable.

- [ ] **Step 1: Write failing tests** for valid JSON extraction, malformed output fallback, and empty-profile fallback.
- [ ] **Step 2: Run `pytest tests/test_preferences.py -q` and verify the import/function failure.**
- [ ] **Step 3: Implement strict JSON parsing** with the seven exact preference fields from the spec, list/string type checks, bounded list lengths, and no logging of profile content.
- [ ] **Step 4: Implement the fallback** using policy role families/target titles, positive keywords, location words, and blocked profession phrases.
- [ ] **Step 5: Integrate one extraction call per pipeline run** before ranking; pass the resulting object to later selection code.
- [ ] **Step 6: Run focused tests** and verify the profile is included in the extraction prompt but never in logs.
- [ ] **Step 7: Commit** with `feat: extract compact candidate preferences`.

### Task 2: Add Jobicy and Himalayas public adapters

**Files:**
- Create: `src/job_hunter/sources/jobicy.py`
- Create: `src/job_hunter/sources/himalayas.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `tests/test_sources.py`

**Interfaces:**
- `JobicySource(http, max_pages: int = 1).discover() -> list[Job]` using `https://jobicy.com/api/v2/remote-jobs`.
- `HimalayasSource(http, max_pages: int = 2).discover() -> list[Job]` using `https://himalayas.app/jobs/api` and cursor pagination.

- [ ] **Step 1: Write failing fake-response tests** for title/company/location/remote/description/source ID/application URL normalization and malformed-record skipping.
- [ ] **Step 2: Run the focused source tests and verify failure.**
- [ ] **Step 3: Implement Jobicy pagination and normalization** with independent failure isolation and HTML stripping.
- [ ] **Step 4: Implement Himalayas cursor pagination and normalization** with independent failure isolation and HTML stripping.
- [ ] **Step 5: Export both adapters and wire them into `build_sources()`** without removing existing sources.
- [ ] **Step 6: Run `pytest tests/test_sources.py -q` and verify no live calls.**
- [ ] **Step 7: Commit** with `feat: add Jobicy and Himalayas discovery sources`.

### Task 3: Profile-aware ranking and source-diverse selection

**Files:**
- Modify: `src/job_hunter/ranking.py`
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/config.py`
- Modify: `config/search.yml`
- Create: `tests/test_ranking.py` additions

**Interfaces:**
- Produces: `profile_priority_score(job: Job, preferences: CandidatePreferences, policy: SearchPolicy) -> int`.
- Produces: `select_diverse_candidates(ranked: list[tuple[int, Job, int]], limit: int, minimum_per_source: int, max_share: float) -> list[tuple[int, Job, int]]`.

- [ ] **Step 1: Write failing tests** for preferred-role scoring, avoid-signal penalties, unique-signal caps, minimum-per-source selection, max-share enforcement, and stable ties.
- [ ] **Step 2: Run ranking tests and verify failure.**
- [ ] **Step 3: Add config fields** `source_minimum_per_run: 2`, `source_max_share: 0.5`, and `max_jobs_per_run: 35` with safe defaults and YAML values.
- [ ] **Step 4: Implement profile-aware scoring** using the 35/30/15/10 component bounds from the spec and deterministic normalization.
- [ ] **Step 5: Implement two-pass source-diverse selection**: take up to the per-source minimum, then fill by global score without exceeding the source share.
- [ ] **Step 6: Run focused ranking/config tests.**
- [ ] **Step 7: Commit** with `feat: rank candidates against profile preferences`.

### Task 4: Integrate shortlist selection and diagnostics

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes `CandidatePreferences`, `profile_priority_score()`, and `select_diverse_candidates()`.
- Preserves `run_pipeline(...) -> RunSummary` and current delivery/retry behavior.

- [ ] **Step 1: Write failing pipeline tests** for 18 candidates evaluating 18, 100 candidates evaluating exactly 35, source diversity, profile-extraction fallback, and `deferred_by_budget` logging.
- [ ] **Step 2: Run the focused pipeline tests and verify failure.**
- [ ] **Step 3: Refactor pipeline selection** to use profile-aware scores and source-diverse selection before full evaluation while retaining hard-gate and cache behavior.
- [ ] **Step 4: Add diagnostics** for profile extraction mode, per-source eligible/selected counts, and deferred budget without private content.
- [ ] **Step 5: Run pipeline/store regression tests.**
- [ ] **Step 6: Commit** with `feat: integrate profile-aware diverse shortlist`.

### Task 5: Telegram score-floor and documentation regression coverage

**Files:**
- Modify: `src/job_hunter/telegram.py`
- Modify: `src/job_hunter/store.py`
- Modify: `tests/test_telegram.py`
- Modify: `tests/test_store.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Preserves `select_deliverable_items()` and `pending_delivery_job_ids()` while enforcing score `>60`.

- [ ] **Step 1: Add failing retry tests** proving a score-60 ready/possible evaluation is not pending and score-61 remains pending when undelivered.
- [ ] **Step 2: Run the focused tests and verify failure if needed.**
- [ ] **Step 3: Verify score-floor enforcement** across new digest construction, rediscovery retry, and PDF delivery paths.
- [ ] **Step 4: Document** source expansion, profile-aware ranking, source diversity, 35-job default, fallback behavior, and score-floor semantics.
- [ ] **Step 5: Run the full suite and secret-safety checks.**
- [ ] **Step 6: Commit** with `docs: document v2.2 profile-aware discovery`.

## Final verification

- [ ] Run `pytest -q` and confirm all tests pass.
- [ ] Run a local dry run with configured secrets/profile; confirm logs include profile mode, source counts, and budget deferral without private content.
- [ ] Inspect `git diff --check`, `git status --short --branch`, and tracked secret-name scans.

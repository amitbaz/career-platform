# Job Hunter Bot v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily GitHub Actions job-hunter that discovers public remote roles, deduplicates them in SQLite, evaluates/tailors with Gemini, renders PDF cover letters, and delivers a Telegram digest.

**Architecture:** A Python 3.12 batch application with focused source adapters and services. GitHub Actions restores the latest SQLite artifact, runs the pipeline once, sends outbound Telegram messages/documents, and uploads updated state. All external HTTP is behind injectable clients so tests run without credentials/network.

**Tech Stack:** Python 3.12, stdlib `sqlite3`, `requests`, `beautifulsoup4`, `PyYAML`, `reportlab`, `pytest`, GitHub Actions, Gemini Developer API REST, Telegram Bot API.

**Spec:** `docs/superpowers/specs/2026-08-30-job-hunter-bot-design.md`

## Global Constraints

- Remote-only job search.
- Compensation floor is EUR 90,000 base when an explicit comparable ceiling is available.
- Scoring weights are exactly 30/25/20/10/10/5 and total 100.
- Thresholds are exactly: 85 high priority, 75 good match/package, 65 possible match/digest only, below 65 skip; hard blocker overrides score.
- No automated employer-form submission in v1.
- Do not invent candidate facts; CV/profile and cover-letter template are provided at runtime through base64 GitHub secrets.
- No paid search API.
- GitHub-hosted runner state is persisted as a SQLite artifact.
- Daily target time is 09:00 `Europe/Berlin` using dual UTC crons plus a local-hour guard.
- External source failure must not abort other sources.
- All test calls to external services are mocked/faked.

---

## File Map

- `pyproject.toml`: package metadata, runtime/test dependencies, pytest config.
- `.gitignore`: local DB, output PDFs, caches, virtualenvs.
- `.env.example`: environment variable names only.
- `config/search.yml`: search queries, thresholds, title/keyword policy, optional ATS board seeds.
- `src/job_hunter/models.py`: immutable/shared dataclasses.
- `src/job_hunter/config.py`: YAML + environment/base64 secret parsing.
- `src/job_hunter/http.py`: retrying `requests.Session` wrapper.
- `src/job_hunter/normalize.py`: canonical URLs, fingerprints, text normalization.
- `src/job_hunter/fetching.py`: JSON-LD/HTML job page extraction.
- `src/job_hunter/prefilter.py`: cheap title/remote/blocker gate.
- `src/job_hunter/store.py`: SQLite schema/repository.
- `src/job_hunter/sources/*`: public discovery adapters.
- `src/job_hunter/gemini.py`: Gemini REST transport + JSON extraction.
- `src/job_hunter/evaluation.py`: evaluation prompt + strict score validation.
- `src/job_hunter/cover_letter.py`: tailoring prompt + placeholder validation.
- `src/job_hunter/pdf.py`: ReportLab letter renderer.
- `src/job_hunter/telegram.py`: digest chunking and Bot API calls.
- `src/job_hunter/pipeline.py`: orchestration.
- `src/job_hunter/cli.py`, `__main__.py`: run entrypoint and schedule guard.
- `scripts/restore_state.py`: locate/download/extract latest GitHub Actions state artifact.
- `.github/workflows/ci.yml`: tests on push/PR.
- `.github/workflows/daily.yml`: restore state, run bot, upload state.
- `README.md`: setup, secrets, manual run, scheduling, source configuration.

---

### Task 1: Package foundation, configuration, models, normalization, and prefilter

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `config/search.yml`
- Create: `src/job_hunter/__init__.py`
- Create: `src/job_hunter/models.py`
- Create: `src/job_hunter/config.py`
- Create: `src/job_hunter/normalize.py`
- Create: `src/job_hunter/prefilter.py`
- Test: `tests/test_config.py`
- Test: `tests/test_normalize.py`
- Test: `tests/test_prefilter.py`

**Interfaces:**
- Produces `Job`, `Evaluation`, `Material`, `PrefilterResult`, `Settings`, `SearchPolicy` dataclasses.
- Produces `load_settings(config_path: Path) -> Settings`.
- Produces `canonicalize_url(url: str) -> str`, `job_fingerprint(job: Job) -> str`, `description_hash(text: str) -> str`.
- Produces `prefilter_job(job: Job, policy: SearchPolicy) -> PrefilterResult`.

- [ ] **Step 1: Write failing tests for URL normalization and fingerprints**

```python
from job_hunter.models import Job
from job_hunter.normalize import canonicalize_url, job_fingerprint


def test_canonicalize_url_drops_tracking_and_fragment():
    url = "https://example.com/jobs/42?utm_source=x&gh_src=abc&keep=1#apply"
    assert canonicalize_url(url) == "https://example.com/jobs/42?keep=1"


def test_fingerprint_prefers_source_job_id():
    job = Job(source="ashby", source_job_id="abc", url="https://x/y", company="X", title="Senior Product Engineer")
    assert job_fingerprint(job) == job_fingerprint(Job(source="ashby", source_job_id="abc", url="https://different", company="Y", title="Other"))
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_normalize.py -q`

Expected: import/module failures because implementation does not exist.

- [ ] **Step 3: Implement shared dataclasses and normalization**

```python
@dataclass(slots=True)
class Job:
    source: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    source_job_id: str | None = None
    remote: bool | None = None


def job_fingerprint(job: Job) -> str:
    if job.source_job_id:
        raw = f"id:{job.source.lower()}:{job.source_job_id}"
    elif job.url:
        raw = f"url:{canonicalize_url(job.url)}"
    else:
        raw = "fallback:" + "|".join(normalize_text(v) for v in (job.company, job.title, job.location))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

`canonicalize_url()` must remove fragments and common tracking parameters (`utm_*`, `gh_src`, `lever-source`, `source`, `ref`) while retaining functional query parameters.

- [ ] **Step 4: Write failing config tests**

```python
import base64
from pathlib import Path
from job_hunter.config import load_settings


def test_load_settings_decodes_private_sources(monkeypatch, tmp_path: Path):
    cfg = tmp_path / "search.yml"
    cfg.write_text("timezone: Europe/Berlin\nscheduled_hour: 9\nthresholds: {package: 75, possible: 65}\n")
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv("COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode())
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")
    settings = load_settings(cfg)
    assert settings.candidate_profile == "profile"
    assert settings.cover_letter_template == "template"
    assert settings.timezone == "Europe/Berlin"
```

- [ ] **Step 5: Implement config parsing and committed YAML**

`Settings` contains runtime secrets plus `SearchPolicy`. In dry-run mode Telegram secrets are optional; otherwise they are required. `config/search.yml` contains:

```yaml
timezone: Europe/Berlin
scheduled_hour: 9
max_jobs_per_run: 25
thresholds:
  package: 75
  possible: 65
salary_floor_eur: 90000
target_titles:
  - senior product engineer
  - senior frontend engineer
  - frontend technical lead
  - staff frontend engineer
  - product engineer
  - full-stack product engineer
  - ai product engineer
positive_keywords:
  - react
  - next.js
  - typescript
  - design system
  - graphql
  - product ownership
  - b2b
  - saas
  - agentic
  - ai-assisted
  - monorepo
blocked_title_keywords:
  - junior
  - qa
  - sre
  - devops
  - security engineer
  - engineering manager
search_queries:
  - '"Senior Product Engineer" remote React TypeScript'
  - '"Senior Frontend Engineer" remote React TypeScript Europe'
  - 'site:jobs.ashbyhq.com "Product Engineer" remote TypeScript'
  - 'site:boards.greenhouse.io "Senior Frontend Engineer" remote'
  - 'site:jobs.lever.co "Senior Product Engineer" remote'
ats:
  ashby: []
  lever: []
  greenhouse: []
```

- [ ] **Step 6: Write failing prefilter tests**

```python
from job_hunter.models import Job
from job_hunter.prefilter import prefilter_job


def test_prefilter_blocks_explicit_non_remote(policy):
    result = prefilter_job(Job(source="x", title="Senior Frontend Engineer", location="Berlin - onsite", remote=False), policy)
    assert result.hard_blocker is True
    assert "remote" in result.reason.lower()


def test_prefilter_keeps_relevant_remote_role(policy):
    result = prefilter_job(Job(source="x", title="Senior Product Engineer", description="React TypeScript remote product ownership"), policy)
    assert result.should_evaluate is True
```

- [ ] **Step 7: Implement deterministic prefilter**

The prefilter must reject only obvious cases: `remote is False`, blocked title keyword, or no target-title/positive-keyword evidence. Ambiguous location/salary/skill requirements remain for Gemini rather than being guessed.

- [ ] **Step 8: Run task tests**

Run: `python -m pytest tests/test_config.py tests/test_normalize.py tests/test_prefilter.py -q`

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml .gitignore .env.example config src/job_hunter tests/test_config.py tests/test_normalize.py tests/test_prefilter.py
git commit -m "feat: add job hunter core models and policy"
```

---

### Task 2: HTTP fetching, JSON-LD extraction, and SQLite persistence

**Files:**
- Create: `src/job_hunter/http.py`
- Create: `src/job_hunter/fetching.py`
- Create: `src/job_hunter/store.py`
- Test: `tests/test_fetching.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes `Job`, `Evaluation`, `Material`, normalization helpers.
- Produces `HttpClient.get/post` with timeout/retry behavior.
- Produces `enrich_job(job: Job, http: HttpClient) -> Job`.
- Produces `JobStore(path)`, `upsert_job(job) -> tuple[int, bool, bool]` where booleans are `is_new`, `description_changed`.
- Produces `needs_evaluation(job_id)`, `save_evaluation`, `save_material`, `mark_delivered`, and digest query methods.

- [ ] **Step 1: Write failing JSON-LD extraction tests**

```python
from job_hunter.fetching import extract_job_from_html


def test_extracts_jobposting_json_ld():
    html = '''<script type="application/ld+json">{"@type":"JobPosting","title":"Senior Product Engineer","description":"<p>React and TypeScript</p>","hiringOrganization":{"name":"Acme"},"jobLocationType":"TELECOMMUTE"}</script>'''
    data = extract_job_from_html(html)
    assert data["title"] == "Senior Product Engineer"
    assert data["company"] == "Acme"
    assert data["remote"] is True
    assert "React and TypeScript" in data["description"]
```

- [ ] **Step 2: Implement retrying HTTP and extraction**

`HttpClient` uses one `requests.Session`, a user-agent identifying the project, `(5, 25)` connect/read timeouts, and retries at most twice for `429, 500, 502, 503, 504` with short exponential backoff. `extract_job_from_html()` searches JSON-LD dictionaries/lists for `@type == JobPosting`, strips HTML from description, and falls back to visible page text/title.

- [ ] **Step 3: Write failing persistence tests**

```python
from job_hunter.models import Job
from job_hunter.store import JobStore


def test_upsert_dedupes_and_detects_description_change(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(source="lever", source_job_id="1", title="Senior Product Engineer", description="React")
    job_id, is_new, changed = store.upsert_job(job)
    assert (is_new, changed) == (True, False)

    same_id, is_new, changed = store.upsert_job(job)
    assert same_id == job_id
    assert (is_new, changed) == (False, False)

    job.description = "React TypeScript"
    same_id, is_new, changed = store.upsert_job(job)
    assert (is_new, changed) == (False, True)
```

- [ ] **Step 4: Implement SQLite schema and repository methods**

Create tables exactly described by the spec and enable foreign keys. Store JSON fields with `json.dumps`. Use `INSERT ... ON CONFLICT(fingerprint) DO UPDATE` while preserving `first_seen_at` and updating `last_seen_at`/description hash. `needs_evaluation()` returns true when no evaluation exists, evaluation status is failed, or description hash has changed after evaluation.

- [ ] **Step 5: Run task tests**

Run: `python -m pytest tests/test_fetching.py tests/test_store.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/http.py src/job_hunter/fetching.py src/job_hunter/store.py tests/test_fetching.py tests/test_store.py
git commit -m "feat: add job fetching and sqlite persistence"
```

---

### Task 3: Public discovery adapters

**Files:**
- Create: `src/job_hunter/sources/__init__.py`
- Create: `src/job_hunter/sources/base.py`
- Create: `src/job_hunter/sources/remotive.py`
- Create: `src/job_hunter/sources/arbeitnow.py`
- Create: `src/job_hunter/sources/duckduckgo.py`
- Create: `src/job_hunter/sources/ashby.py`
- Create: `src/job_hunter/sources/lever.py`
- Create: `src/job_hunter/sources/greenhouse.py`
- Test: `tests/test_sources.py`

**Interfaces:**
- Consumes `HttpClient`, `Job`, `SearchPolicy`.
- Produces protocol `JobSource.discover() -> list[Job]`.
- Produces `build_sources(settings: Settings, http: HttpClient) -> list[JobSource]`.

- [ ] **Step 1: Write failing adapter normalization tests**

Use a fake HTTP client returning representative API payloads.

```python

def test_ashby_maps_public_posting(fake_http, policy):
    fake_http.json_data = {"jobs": [{"id": "a1", "title": "Senior Product Engineer", "location": "Remote Europe", "jobUrl": "https://jobs.ashbyhq.com/acme/a1", "descriptionPlain": "React TypeScript", "isRemote": True}]}
    jobs = AshbySource("acme", fake_http).discover()
    assert jobs[0].source_job_id == "a1"
    assert jobs[0].remote is True
```

Also cover Remotive, Arbeitnow, Lever, Greenhouse, and DuckDuckGo result-link parsing.

- [ ] **Step 2: Implement `JobSource` and remote-board adapters**

Remotive endpoint: `https://remotive.com/api/remote-jobs`.

Arbeitnow endpoint: `https://www.arbeitnow.com/api/job-board-api` (follow pagination only up to a small configured page cap; v1 default 2 pages).

Normalize HTML descriptions to text.

- [ ] **Step 3: Implement ATS adapters**

Ashby: `GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true`.

Lever: `GET https://api.lever.co/v0/postings/{site}?mode=json`.

Greenhouse: `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`.

Each adapter sets source/source_job_id/URL/title/company/location/description and remote hint when the source provides reliable evidence.

- [ ] **Step 4: Implement best-effort DuckDuckGo discovery**

POST/GET the HTML endpoint with each configured query, parse result anchors, discard DuckDuckGo navigation links, canonicalize outbound URLs, and return lightweight `Job` records whose missing fields will be enriched by the fetcher. One query failure logs and continues.

- [ ] **Step 5: Implement source factory**

Always include Remotive, Arbeitnow, and DuckDuckGo. Add configured Ashby/Lever/Greenhouse board slugs from YAML.

- [ ] **Step 6: Run task tests**

Run: `python -m pytest tests/test_sources.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/sources tests/test_sources.py
git commit -m "feat: add public job discovery sources"
```

---

### Task 4: Gemini evaluation and cover-letter tailoring

**Files:**
- Create: `src/job_hunter/gemini.py`
- Create: `src/job_hunter/evaluation.py`
- Create: `src/job_hunter/cover_letter.py`
- Test: `tests/test_gemini.py`
- Test: `tests/test_evaluation.py`
- Test: `tests/test_cover_letter.py`

**Interfaces:**
- Consumes `Settings`, `Job`, `Evaluation`.
- Produces `GeminiClient.generate_text(prompt: str, *, json_mode: bool = False) -> str`.
- Produces `evaluate_job(job, profile, policy, gemini) -> Evaluation`.
- Produces `generate_cover_letter(job, evaluation, profile, template, gemini, today) -> str`.

- [ ] **Step 1: Write failing Gemini transport test**

Mock `requests.Session.post` and assert the client posts to:

```text
https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent
```

with `x-goog-api-key`, content parts, and JSON MIME type when `json_mode=True`.

- [ ] **Step 2: Implement Gemini transport**

Parse `candidates[0].content.parts[*].text`, concatenate text parts, and raise a typed `GeminiError` for missing content/non-2xx responses after the HTTP retry layer.

- [ ] **Step 3: Write failing evaluation validation tests**

```python

def test_evaluation_rejects_component_over_max(fake_gemini, job, policy):
    fake_gemini.text = '{"scores":{"role_seniority":31,"technical":20,"product_architecture":15,"career_direction":8,"location_language":8,"company_environment":4},"total_score":86,"hard_blockers":[],"strengths":[],"gaps":[],"salary_note":"","location_note":"","decision":"high_priority","rationale":""}'
    with pytest.raises(ValueError):
        evaluate_job(job, "profile", policy, fake_gemini)
```

Also reject total != sum and unknown/missing score keys.

- [ ] **Step 4: Implement evaluation prompt and parser**

The prompt includes exact maxima and blocker policy and says: only use evidence in the supplied profile/job; unknowns are gaps, not invented facts. Parse JSON after stripping optional Markdown code fences. Map total/decision to `Evaluation`, but recompute total from validated components rather than trusting the model.

- [ ] **Step 5: Write failing cover-letter guard tests**

```python

def test_cover_letter_rejects_unreplaced_placeholders(fake_gemini, job, evaluation):
    fake_gemini.text = "Dear Hiring Team, I want [Position] at [Company]."
    with pytest.raises(ValueError):
        generate_cover_letter(job, evaluation, "profile", "template", fake_gemini, date(2026, 8, 30))
```

- [ ] **Step 6: Implement cover-letter generation**

Prompt Gemini with the current template, candidate profile, job description, strengths/gaps, date/company/title, and explicit `NEVER invent facts` instruction. Reject empty output and any remaining bracket placeholder from a fixed known placeholder set.

- [ ] **Step 7: Run task tests**

Run: `python -m pytest tests/test_gemini.py tests/test_evaluation.py tests/test_cover_letter.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/gemini.py src/job_hunter/evaluation.py src/job_hunter/cover_letter.py tests/test_gemini.py tests/test_evaluation.py tests/test_cover_letter.py
git commit -m "feat: add gemini evaluation and tailoring"
```

---

### Task 5: PDF rendering and Telegram delivery

**Files:**
- Create: `src/job_hunter/pdf.py`
- Create: `src/job_hunter/telegram.py`
- Test: `tests/test_pdf.py`
- Test: `tests/test_telegram.py`

**Interfaces:**
- Produces `render_cover_letter_pdf(text, company, role, out_dir) -> Path`.
- Produces `build_digest(items: Sequence[DigestItem]) -> str` and `chunk_message(text, limit=3900) -> list[str]`.
- Produces `TelegramClient.send_message(text) -> str | None`, `send_document(path, caption) -> str | None`.

- [ ] **Step 1: Write failing PDF test**

```python

def test_render_pdf_has_pdf_signature(tmp_path):
    path = render_cover_letter_pdf("Amit Baz\n\nDear Hiring Team,\nHello.", "Acme", "Senior Product Engineer", tmp_path)
    assert path.name == "Acme_Senior_Product_Engineer_Cover_Letter.pdf"
    assert path.read_bytes().startswith(b"%PDF")
```

- [ ] **Step 2: Implement ReportLab renderer**

Use `SimpleDocTemplate(A4)`, built-in Helvetica fonts, 20mm-ish margins, paragraph spacing, and `xml.sax.saxutils.escape` before creating ReportLab `Paragraph`s. Empty lines create spacers. Sanitize filename to ASCII-ish `[A-Za-z0-9._-]` segments.

- [ ] **Step 3: Write failing Telegram tests**

Assert long text is chunked under 3900 characters and fake HTTP receives `sendMessage` and multipart `sendDocument` calls with the configured chat id.

- [ ] **Step 4: Implement digest and Telegram client**

Digest groups exactly:

```text
Ready to apply
Possible matches
Needs review / blockers
```

Each line includes score, company, role, and URL when available. `send_document` caption includes company/role/score/URL and is truncated conservatively below Telegram caption limits.

- [ ] **Step 5: Run task tests**

Run: `python -m pytest tests/test_pdf.py tests/test_telegram.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/pdf.py src/job_hunter/telegram.py tests/test_pdf.py tests/test_telegram.py
git commit -m "feat: add pdf and telegram delivery"
```

---

### Task 6: End-to-end pipeline and CLI

**Files:**
- Create: `src/job_hunter/pipeline.py`
- Create: `src/job_hunter/cli.py`
- Create: `src/job_hunter/__main__.py`
- Test: `tests/test_pipeline.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes every earlier module.
- Produces `run_pipeline(settings, sources=None, store=None, gemini=None, telegram=None, http=None) -> RunSummary`.
- Produces CLI `python -m job_hunter run [--scheduled] [--config config/search.yml]`.

- [ ] **Step 1: Write failing pipeline integration test with fakes**

Create two jobs from a fake source: one strong remote role and one duplicate. Fake Gemini returns score 90 and a valid cover letter. Fake Telegram records messages/documents.

Assertions:

```python
assert summary.ready_to_apply == 1
assert len(fake_telegram.documents) == 1
assert store.count_jobs() == 1
assert store.has_delivery(job_id)
```

Run the pipeline a second time with the same job and assert Gemini evaluation call count does not increase.

- [ ] **Step 2: Implement orchestration**

For each source independently:

1. discover with source-level exception isolation;
2. enrich missing descriptions/metadata when URL exists;
3. fingerprint/upsert;
4. skip evaluation when unchanged and already successfully evaluated;
5. apply prefilter;
6. evaluate at most `max_jobs_per_run` jobs;
7. persist evaluation;
8. if eligible >=75 and no blocker, generate/persist cover letter and PDF;
9. collect digest items.

After processing, send digest then PDFs unless dry-run. Mark delivery only after successful send; in dry-run leave evaluation/material persisted but do not mark Telegram-delivered.

- [ ] **Step 3: Write failing schedule guard test**

Inject a datetime and verify scheduled mode runs only when `ZoneInfo("Europe/Berlin")` local hour equals configured `scheduled_hour`.

- [ ] **Step 4: Implement CLI**

`run --scheduled` returns exit 0 with a clear log message when the time guard says this is the DST duplicate cron. Manual `run` always proceeds. Configure stdlib logging and create output/database parent directories.

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest -q`

Expected: PASS, no network calls.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/pipeline.py src/job_hunter/cli.py src/job_hunter/__main__.py tests/test_pipeline.py tests/test_cli.py
git commit -m "feat: orchestrate daily job hunter pipeline"
```

---

### Task 7: State artifact restore, CI/daily workflows, and operator documentation

**Files:**
- Create: `scripts/restore_state.py`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/daily.yml`
- Create: `README.md`
- Test: `tests/test_restore_state.py`

**Interfaces:**
- `scripts/restore_state.py` accepts `--repo`, `--token`, `--name job-hunter-state`, `--dest var/job_hunter.sqlite3`; exits 0 when no artifact exists.

- [ ] **Step 1: Write failing artifact-restore tests**

Mock GitHub API list/download responses. Verify the script chooses the newest non-expired artifact with matching name, downloads ZIP bytes, and extracts only the expected SQLite file path (zip-slip safe).

- [ ] **Step 2: Implement restore script**

Use stdlib `urllib.request`/`zipfile` or existing `requests`. Required headers include bearer token and GitHub API accept header. Validate ZIP member basename before extraction and create destination parent directories.

- [ ] **Step 3: Add CI workflow**

```yaml
name: CI
on:
  push:
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - run: pip install -e '.[test]'
      - run: pytest -q
```

- [ ] **Step 4: Add daily workflow**

Requirements:

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: '5 7 * * *'
    - cron: '5 8 * * *'
```

Steps:

1. checkout;
2. Python 3.12;
3. install package;
4. restore prior `job-hunter-state` using `GITHUB_TOKEN` (continue normally if none exists);
5. run `python -m job_hunter run` for manual dispatch or `python -m job_hunter run --scheduled` for schedule;
6. upload `var/job_hunter.sqlite3` with `actions/upload-artifact@v4`, artifact name `job-hunter-state`, retention 90 days, even when the bot run fails after database creation (`if: always() && hashFiles('var/job_hunter.sqlite3') != ''`).

Inject secrets only into the run step:

```yaml
env:
  GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
  TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
  TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
  CANDIDATE_PROFILE_B64: ${{ secrets.CANDIDATE_PROFILE_B64 }}
  COVER_LETTER_TEMPLATE_B64: ${{ secrets.COVER_LETTER_TEMPLATE_B64 }}
```

- [ ] **Step 5: Write README setup**

README must include:

- architecture summary;
- required GitHub secrets and base64 commands for CV/template text;
- how to create a Telegram bot with BotFather and obtain chat id without exposing tokens in git;
- Gemini API key setup and configurable `GEMINI_MODEL`;
- local dry-run command;
- manual GitHub Actions dispatch;
- schedule/DST behavior;
- how to add ATS board slugs to `config/search.yml`;
- v1 non-submission safety boundary;
- troubleshooting for no prior state artifact, Gemini quota, Telegram errors, and flaky web sources.

- [ ] **Step 6: Run full verification**

Run:

```bash
python -m pytest -q
python -m compileall -q src scripts
```

Expected: both commands exit 0.

- [ ] **Step 7: Commit**

```bash
git add scripts .github README.md tests/test_restore_state.py
git commit -m "ci: schedule and persist daily job hunter"
```

---

## Final Review Checklist

- [ ] Every spec requirement maps to a task above.
- [ ] No private CV/template text is committed.
- [ ] `pytest -q` passes.
- [ ] `python -m compileall -q src scripts` passes.
- [ ] `git diff --check` passes.
- [ ] CI workflow triggers on push/PR.
- [ ] Daily workflow supports manual dispatch and dual cron.
- [ ] No employer submission code exists.
- [ ] README documents all required secrets and a dry-run path.

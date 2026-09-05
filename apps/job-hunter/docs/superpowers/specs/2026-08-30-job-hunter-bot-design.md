# Job Hunter Bot v1 Design

**Date:** 2026-08-30

## 1. Purpose

Build a mostly hands-off daily job-hunting assistant that runs on GitHub Actions, discovers current public job postings, deduplicates them with SQLite, evaluates them against the approved Job Search Automation Policy, uses Gemini to produce structured fit analysis and tailored cover letters, renders strong-match cover letters as PDFs, and delivers a concise daily package through Telegram.

The optimization objective is interview-quality applications, not raw application count.

## 2. v1 Scope

### In scope

- Daily and manual GitHub Actions runs.
- Public-web and public-job-source discovery without a paid search API.
- Source adapters for public remote-job APIs plus direct ATS/job-board discovery.
- Full job-description fetching when a source only provides a URL/snippet.
- Deterministic normalization, deduplication, and hard-filter prechecks.
- Gemini API evaluation using the approved scoring rubric.
- Gemini-tailored cover letters grounded only in the candidate profile, approved template, job description, and discovered company/job facts.
- PDF cover-letter rendering.
- Telegram digest delivery plus PDF documents for strong matches.
- SQLite state persisted between GitHub Actions runs.
- Dry-run/local execution and automated tests.

### Out of scope for v1

- Automated submission to employer forms.
- Answering legal attestations, visa/work-authorization questions, salary commitments, notice period, demographic questions, or other consequential personal questions.
- CAPTCHA/2FA handling.
- Browser automation.
- Paid search APIs.
- Maintaining multiple CV variants automatically.
- A hosted backend or always-on Telegram webhook service.

The bot prepares and delivers application-ready material; the user remains responsible for final employer-site review/submission.

## 3. Source-of-truth rules

The implementation follows the approved project materials:

- Current CV: factual source of truth.
- Generic cover-letter template: default voice and structure.
- Job Search Automation Policy: target roles, blockers, scoring weights, thresholds, remote-only rule, compensation floor, and application-material rules.
- Existing Job Search Tracker: reference schema for concepts such as found date, company, role, location, remote, compensation, score, decision, status, URL, source, key match, blockers, and next action.

Personal CV/template content is **not committed to the repository**. GitHub Actions receives the current source text through encrypted secrets so the private source material can be updated independently of code.

## 4. Candidate policy encoded in v1

### Target positioning

Prioritize senior product/frontend roles with substantial React/TypeScript product engineering, frontend architecture, ownership, and room to broaden cross-stack responsibility.

### Remote and geography

- Remote-only at application time.
- Prioritize Europe and Israel.
- Other markets are acceptable only when remote from Germany/EU or exceptional enough to surface for review.

### Compensation

- Base salary floor: EUR 90,000 or clearly comparable local-currency equivalent.
- If a stated maximum is below the floor, treat it as a hard blocker.
- If compensation is undisclosed, do not reject solely for that reason; mark salary as needing verification.
- v1 does not depend on live FX for the first deterministic filter. Gemini may flag non-EUR compensation for review; live FX can be added later without changing pipeline boundaries.

### Hard blockers

A hard blocker overrides numeric score. Important blockers include:

- not remote;
- explicit compensation ceiling below floor;
- mandatory professional German when English is insufficient;
- backend-only role requiring deep production backend expertise as a hard prerequisite;
- pure mobile, embedded, ML research/data science, DevOps/SRE, security, QA, or people-management role;
- clearly junior/mid-level role;
- relocation-only role;
- fundamental mandatory credential/skill absent from candidate evidence.

### Scoring weights

- Role and seniority fit: 30
- Technical fit: 25
- Product ownership and architecture fit: 20
- Career-direction/cross-stack growth: 10
- Location and language fit: 10
- Company/product interest and environment: 5

### Thresholds

- 85–100: high priority; generate package.
- 75–84: good match; generate package unless material ambiguity/risk requires user review.
- 65–74: possible match; include in digest with the trade-off, no PDF by default.
- Below 65: skip from the main digest.
- Hard blocker: do not generate application material.

## 5. Architecture

A single Python package runs as a finite batch pipeline. There is no server process.

```text
GitHub Actions schedule/manual dispatch
        |
        v
load config + secrets + restore SQLite state
        |
        v
public discovery adapters
(Remotive / Arbeitnow / web search / ATS URLs)
        |
        v
normalize + fetch full description + fingerprint
        |
        v
SQLite upsert + dedupe against prior runs
        |
        v
cheap deterministic prefilter
        |
        v
Gemini structured evaluation
        |
        +--> <65 / hard blocker -> persist result
        |
        +--> 65-74 -> digest only
        |
        +--> >=75 -> Gemini cover letter -> PDF
        |
        v
Telegram digest + PDF documents
        |
        v
mark delivered + upload SQLite state artifact
```

### Main boundaries

- `config`: validates environment/secrets and YAML runtime policy.
- `models`: shared dataclasses for jobs, evaluations, and materials.
- `sources`: public discovery adapters with one interface.
- `fetching`: full-description extraction from job pages, preferring JSON-LD `JobPosting`.
- `store`: SQLite schema and repository methods.
- `prefilter`: deterministic, cheap rejection/priority hints before LLM use.
- `gemini`: Gemini REST client and strict JSON parsing.
- `evaluation`: prompt construction and score validation.
- `cover_letter`: prompt construction and placeholder/factuality guardrails.
- `pdf`: deterministic ReportLab rendering.
- `telegram`: Telegram Bot API delivery.
- `pipeline`: orchestration only; business logic stays in focused modules.
- `cli`: local/manual/scheduled entrypoint.

## 6. Discovery strategy

v1 must work without a paid search provider and must fail open per source: one broken source cannot abort the whole run.

### Remote-job APIs

Use public JSON APIs as broad discovery inputs. Initial adapters:

- Remotive remote jobs API.
- Arbeitnow job board API.

The adapters normalize source records into the common `Job` model and apply title/query relevance before expensive enrichment.

### Public web discovery

Use best-effort DuckDuckGo HTML search with targeted queries such as:

- `"Senior Product Engineer" remote React TypeScript`
- `"Senior Frontend Engineer" remote React TypeScript Europe`
- `site:jobs.ashbyhq.com ...`
- `site:boards.greenhouse.io ...`
- `site:jobs.lever.co ...`

Search results are treated as discovery hints, never as sufficient job evidence. The bot fetches the result page and extracts a full job description before evaluation.

DuckDuckGo discovery is deliberately isolated behind an adapter because HTML search endpoints can change. If it fails, API/ATS sources still run.

### ATS support

The fetcher recognizes public ATS URLs. Dedicated configured board adapters may be added without changing the pipeline. v1 supports the public interfaces needed for direct board ingestion:

- Ashby public Job Postings API: `https://api.ashbyhq.com/posting-api/job-board/{board}`.
- Lever public Postings API: `https://api.lever.co/v0/postings/{site}?mode=json`.
- Greenhouse public job-board pages/API where a board token is configured.

The default configuration can run with no company-specific ATS seeds; users may add high-signal board slugs over time.

## 7. Normalization and deduplication

Every job receives a stable SHA-256 fingerprint based on the strongest available identity:

1. source + source job ID, when available;
2. canonical URL with tracking parameters removed;
3. normalized company + title + location fallback.

SQLite keeps both first-seen and last-seen timestamps. Rediscovered jobs update `last_seen_at` without triggering another Gemini evaluation unless their description materially changes or the previous evaluation failed.

Description changes are detected using a separate SHA-256 description hash.

## 8. SQLite persistence

Use Python's standard-library `sqlite3` with migrations executed at startup.

### Tables

`jobs`
- internal integer id
- fingerprint (unique)
- source/source_job_id
- URL
- company/title/location
- remote hint
- description and description hash
- first_seen_at/last_seen_at
- current processing status

`evaluations`
- one current evaluation per job
- total score
- component scores as JSON
- decision
- hard blockers, strengths, gaps as JSON
- salary/location notes
- short rationale
- Gemini model
- evaluated_at

`materials`
- cover-letter text
- generated_at

`deliveries`
- job id
- delivery type/status
- delivered_at
- Telegram message/document identifier when available

### Actions persistence

GitHub-hosted runners are ephemeral. At run start, the workflow restores the most recent `job-hunter-state` artifact, if present, and extracts `var/job_hunter.sqlite3`. At run end, it uploads the updated database as a new `job-hunter-state` artifact with long retention.

If no prior artifact exists, the database is initialized automatically. Losing an old artifact does not break the bot; it only loses dedupe history, so the workflow logs this clearly.

## 9. Gemini integration

Use the Gemini Developer API over HTTPS rather than coupling business logic to an SDK. The model is configurable through `GEMINI_MODEL`; default: `gemini-2.5-flash-lite` because it is a stable, cost-efficient model suitable for high-volume classification/extraction and supports structured output capabilities.

The API key comes from `GEMINI_API_KEY`.

### Evaluation call

Input contains:

- compact candidate profile text from `CANDIDATE_PROFILE_B64`;
- exact scoring rubric and blocker policy;
- normalized job metadata;
- full job description.

The response must be JSON with:

- six component scores within their allowed maxima;
- `total_score` equal to the component sum;
- `hard_blockers[]`;
- `strengths[]`;
- `gaps[]`;
- `salary_note`;
- `location_note`;
- `decision`;
- concise `rationale`.

The client rejects malformed JSON, out-of-range component values, or inconsistent totals. Failed evaluations remain retryable on a later run.

### Cover-letter call

Only eligible jobs (normally >=75 and no hard blocker) reach this call. Input contains:

- current candidate profile;
- current template from `COVER_LETTER_TEMPLATE_B64`;
- job description;
- evaluation strengths/gaps;
- discovered company/job facts.

Prompt rules explicitly forbid inventing facts and require a concise ready-to-send letter. Output is plain text/Markdown-like paragraphs, not JSON.

Post-generation checks reject unreplaced template placeholders such as `[Company]`, `[Position]`, or `[Date]`.

## 10. PDF generation

Render with ReportLab to an A4 PDF using a simple professional letter layout:

- sensible margins;
- readable built-in font;
- paragraphs and spacing preserved;
- automatic page breaks;
- sanitized filename: `Company_Role_Cover_Letter.pdf`.

No external font files are required. PDF generation is deterministic and unit-testable by checking the PDF signature and generated content path.

## 11. Telegram delivery

Use the Telegram Bot API directly with `requests`.

Required secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Daily delivery consists of:

1. one concise digest grouped as:
   - ready to apply (>=75, PDF generated),
   - possible matches (65–74),
   - needs review / notable blockers;
2. one PDF document per ready-to-apply job, with caption containing company, role, score, and job URL.

Long digests are chunked below Telegram message limits. Delivery failures are recorded but do not erase evaluation/material state.

The Bot API is used outbound-only in v1; there is no polling/webhook listener.

## 12. GitHub Actions scheduling

Workflow triggers:

- `workflow_dispatch` for manual testing/runs;
- two daily UTC cron candidates covering 09:00 Europe/Berlin across CET/CEST.

The CLI receives `--scheduled`. For scheduled runs it checks `Europe/Berlin` and proceeds only when the local hour is 09, preventing double delivery across daylight-saving changes. Manual dispatch bypasses the time guard.

Workflow permissions stay minimal (`contents: read`, `actions: read` as needed by artifact restore). Secrets are injected only into the execution step.

## 13. Configuration and secrets

Committed configuration contains search behavior and policy, not private CV content.

Required secrets:

- `GEMINI_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CANDIDATE_PROFILE_B64` — UTF-8 current CV/profile text encoded as base64
- `COVER_LETTER_TEMPLATE_B64` — UTF-8 current cover-letter template encoded as base64

Optional environment variables:

- `GEMINI_MODEL` (default `gemini-2.5-flash-lite`)
- `JOB_HUNTER_DB_PATH` (default `var/job_hunter.sqlite3`)
- `JOB_HUNTER_DRY_RUN=1` to skip Telegram transmission while still producing results locally

## 14. Failure handling

- A source failure logs a warning and other sources continue.
- HTTP calls use finite connect/read timeouts and a small retry policy for transient 429/5xx responses.
- Missing required secrets fail before discovery, except dry-run may omit Telegram credentials.
- Gemini quota/rate errors stop further Gemini calls for the current run after retry exhaustion; discovered jobs remain persisted for later evaluation.
- Invalid Gemini JSON marks only that job's evaluation as failed/retryable.
- A single PDF failure does not block other jobs or the digest.
- A Telegram PDF failure does not prevent later PDFs from being attempted.
- Unhandled exceptions return a non-zero CLI exit code so GitHub Actions visibly fails.

## 15. Testing strategy

Tests use `pytest` and no live external credentials.

Required coverage:

- URL canonicalization and fingerprints.
- JSON-LD job-description extraction.
- SQLite migrations/upsert/dedupe/description-change behavior.
- deterministic remote/title/blocker prefilter.
- Gemini request construction and strict evaluation JSON validation with mocked HTTP.
- cover-letter placeholder guard.
- PDF generation (`%PDF` signature).
- Telegram message chunking and mocked document upload.
- end-to-end pipeline with fake source/Gemini/Telegram collaborators, proving a high-score job is persisted, rendered, and marked delivered while a duplicate is not reprocessed.

CI runs the full test suite on every push and pull request.

## 16. Repository shape

```text
.github/workflows/
  ci.yml
  daily.yml
config/
  search.yml
src/job_hunter/
  __init__.py
  __main__.py
  cli.py
  config.py
  models.py
  normalize.py
  fetching.py
  prefilter.py
  store.py
  http.py
  gemini.py
  evaluation.py
  cover_letter.py
  pdf.py
  telegram.py
  pipeline.py
  sources/
    __init__.py
    base.py
    remotive.py
    arbeitnow.py
    duckduckgo.py
    ashby.py
    lever.py
    greenhouse.py
scripts/
  restore_state.py
pyproject.toml
README.md
.env.example
.gitignore
tests/
  ...
```

Files may be combined when doing so keeps responsibilities clear and avoids unnecessary abstraction, but orchestration, persistence, model calls, rendering, and delivery remain separate boundaries.

## 17. Acceptance criteria

v1 is complete when:

1. `pytest` passes locally/CI with all external calls mocked.
2. A dry run can discover/ingest jobs, persist them in SQLite, evaluate mocked jobs, and generate a valid PDF.
3. A real manual Actions run can restore/create SQLite state, call Gemini with a configured free-tier API key, and send a Telegram digest/PDF when eligible jobs exist.
4. A second run does not re-evaluate unchanged already-processed jobs.
5. One broken source cannot abort other discovery sources.
6. No committed file contains the private CV or full cover-letter source text.
7. The daily workflow runs once around 09:00 Europe/Berlin across CET/CEST using the dual-cron plus local-time guard.
8. The bot never attempts employer-form submission in v1.

## 18. External interface references

- Gemini API docs: https://ai.google.dev/gemini-api/docs
- Gemini 2.5 Flash-Lite: https://ai.google.dev/gemini-api/docs/models/gemini-2.5-flash-lite
- Ashby public Job Postings API: https://developers.ashbyhq.com/docs/public-job-posting-api
- Lever public Postings API: https://github.com/lever/postings-api

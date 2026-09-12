# Gemini Free-Tier Guardrails & Usage Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Job Hunter Bot high-quality and sustainable on Gemini Free Tier only by reducing unnecessary model work, persisting exact usage, enforcing conservative quota budgets, and safely deferring work on quota exhaustion.

**Architecture:** Keep the existing REST-based Gemini integration and SQLite state, but wrap Gemini calls in a persistent quota/usage layer. Replace repeated full-profile prompts with one cached rich `CandidateContext`, shrink Gmail semantic work, classify every Gemini call by purpose, and surface provider-limit utilization in logs and Telegram. A Google-side billing-disabled project remains a required deployment condition.

**Tech Stack:** Python 3.12, SQLite, requests, Gemini GenerateContent REST API, GitHub Actions, Telegram Bot API, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-gemini-free-tier-guardrails-design.md`

## Global Constraints

- Gemini spend must remain €0; the bot's Google project must stay on Free Tier with Cloud Billing disabled/unlinked.
- Active provider limits are supplied from AI Studio through `GEMINI_FREE_RPM`, `GEMINI_FREE_TPM`, and `GEMINI_FREE_RPD`; do not hard-code guessed provider quotas.
- Application ceilings are 80% of configured RPM, TPM, and RPD.
- Non-core work must leave 25% of the internal daily request budget available for `job_evaluation`.
- Gemini 429 responses are never automatically retried by the HTTP layer.
- Gmail historical backfill is 120 days; stale job alerts older than 14 days do not use Gemini extraction.
- Automated tests must use fake Gemini responses only; no real API calls.
- Existing scoring maxima, blockers, and decision thresholds stay unchanged.
- SQLite remains the backend for this feature.

---

## File Structure

**Create**

- `src/job_hunter/gemini_usage.py` — quota policy, persisted preflight logic, token estimation, rolling usage snapshots, and circuit-breaker state.
- `src/job_hunter/candidate_context.py` — rich candidate-context schema, extraction, validation, hashing, and cache lookup.
- `tests/test_gemini_usage.py` — quota calculations, Pacific-day reset, reserve behavior, and pause tests.
- `tests/test_candidate_context.py` — extraction/cache compatibility and invalidation tests.

**Modify**

- `src/job_hunter/models.py` — `GeminiQuotaSettings`, `GeminiUsageSummary`, `CandidateContext`, and pending-work types.
- `src/job_hunter/gmail_models.py` — attach quota settings and keep shared Gmail recency constants authoritative.
- `src/job_hunter/config.py` — require/parse Free Tier quota values.
- `src/job_hunter/store.py` — Gemini usage ledger, quota state, candidate-context cache, and pending AI work.
- `src/job_hunter/http.py` — per-request retry-status override.
- `src/job_hunter/gemini.py` — purpose-aware generation config, usage metadata capture, typed quota errors, and 429 handling.
- `src/job_hunter/preferences.py` — preserve ranking compatibility while consuming `CandidateContext` instead of a per-run Gemini request.
- `src/job_hunter/evaluation.py` — compact candidate context + low thinking + 1,200-token output ceiling.
- `src/job_hunter/cover_letter.py` — candidate context + low thinking + 800-token output ceiling.
- `src/job_hunter/gmail_classifier.py` — compact semantic prompt + minimal thinking + 800-token output ceiling.
- `src/job_hunter/gmail_sync.py` — 120-day backfill, stale-alert skip, and quota-pause short circuit.
- `src/job_hunter/pipeline.py` — shared quota-aware client, candidate-context cache, pending-work processing, and usage delivery order.
- `src/job_hunter/telegram.py` — compact quota status/warning formatting.
- `src/job_hunter/cli.py` — quota-aware client construction and 120-day help text.
- `.github/workflows/daily.yml` — pass quota configuration and stable run ID into both Gemini-using steps.
- `README.md` — zero-cost runbook and AI Studio quota configuration.
- Existing tests: `tests/test_config.py`, `tests/test_store.py`, `tests/test_http.py`, `tests/test_gemini.py`, `tests/test_preferences.py`, `tests/test_evaluation.py`, `tests/test_cover_letter.py`, `tests/test_gmail_classifier.py`, `tests/test_gmail_sync.py`, `tests/test_pipeline.py`, `tests/test_telegram.py`, `tests/test_cli.py`, `tests/test_workflow.py`.

---

### Task 1: Add Free Tier quota configuration and shared models

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/gmail_models.py`
- Modify: `src/job_hunter/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `GeminiQuotaSettings(rpm: int, tpm: int, rpd: int, ceiling_ratio: float = 0.80, core_reserve_ratio: float = 0.25, rate_pause_seconds: int = 90)`
- Produces: both `Settings.gemini_quota` and `GmailSettings.gemini_quota`
- Consumed by: Tasks 2–9

- [ ] **Step 1: Write failing config tests**

Add tests that set all three provider-limit environment variables and verify both normal and Gmail settings receive identical values. Add parameterized failures for missing, zero, negative, and non-integer values.

```python
@pytest.mark.parametrize("name", ["GEMINI_FREE_RPM", "GEMINI_FREE_TPM", "GEMINI_FREE_RPD"])
def test_gemini_free_tier_limit_is_required(monkeypatch, name):
    _set_required_bot_env(monkeypatch)
    monkeypatch.setenv("GEMINI_FREE_RPM", "10")
    monkeypatch.setenv("GEMINI_FREE_TPM", "250000")
    monkeypatch.setenv("GEMINI_FREE_RPD", "500")
    monkeypatch.delenv(name)

    with pytest.raises(ValueError, match=name):
        load_settings(CONFIG_PATH)
```

- [ ] **Step 2: Run the focused config tests and verify failure**

Run:

```bash
pytest tests/test_config.py -q
```

Expected: new tests fail because quota settings do not exist.

- [ ] **Step 3: Add `GeminiQuotaSettings` and settings fields**

Use a frozen dataclass in `models.py`:

```python
@dataclass(frozen=True, slots=True)
class GeminiQuotaSettings:
    rpm: int
    tpm: int
    rpd: int
    ceiling_ratio: float = 0.80
    core_reserve_ratio: float = 0.25
    rate_pause_seconds: int = 90
```

Add `gemini_quota: GeminiQuotaSettings` to the main `Settings` dataclass and to `GmailSettings`.

- [ ] **Step 4: Parse positive integer quota env vars in `config.py`**

Add one helper:

```python
def _require_positive_int_env(name: str) -> int:
    raw = _require_env(name)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
```

Construct one `GeminiQuotaSettings` from the three variables in both load paths.

- [ ] **Step 5: Run config tests**

```bash
pytest tests/test_config.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/gmail_models.py src/job_hunter/config.py tests/test_config.py
git commit -m "feat: configure Gemini free-tier quotas"
```

---

### Task 2: Persist Gemini usage, quota pauses, candidate context, and deferred work

**Files:**
- Modify: `src/job_hunter/store.py`
- Modify: `src/job_hunter/models.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `JobStore.record_gemini_usage(...)`
- Produces: `JobStore.gemini_usage_rows(start_at, end_at, model=None, run_id=None)`
- Produces: `JobStore.set_gemini_pause(model, paused_until, reason)` / `get_gemini_pause(model)` / `clear_gemini_pause(model)`
- Produces: `JobStore.get_candidate_context(cache_key)` / `save_candidate_context(...)`
- Produces: `JobStore.enqueue_ai_work(work_type, job_id)` / `list_pending_ai_work(work_type)` / `complete_ai_work(work_type, job_id)`

- [ ] **Step 1: Write failing store schema/round-trip tests**

Cover one successful Gemini usage row, one 429 row, pause round-trip, candidate-context cache round-trip, and idempotent pending work.

Use an in-memory `JobStore(":memory:")`; assert prompts/responses are not columns in returned rows.

- [ ] **Step 2: Run focused store tests and verify failure**

```bash
pytest tests/test_store.py -q
```

- [ ] **Step 3: Add the four persistence structures**

Add tables with these minimum shapes:

```sql
CREATE TABLE IF NOT EXISTS gemini_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    run_id TEXT,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    thinking_tokens INTEGER,
    cached_tokens INTEGER,
    total_tokens INTEGER,
    http_status INTEGER,
    error_code TEXT
)
```

```sql
CREATE TABLE IF NOT EXISTS gemini_quota_state (
    model TEXT PRIMARY KEY,
    paused_until TEXT,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)
```

```sql
CREATE TABLE IF NOT EXISTS candidate_context_cache (
    cache_key TEXT PRIMARY KEY,
    profile_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
```

```sql
CREATE TABLE IF NOT EXISTS pending_ai_work (
    work_type TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(work_type, job_id)
)
```

- [ ] **Step 4: Implement focused store methods**

Keep JSON serialization inside `store.py`; return `sqlite3.Row` for ledger queries and typed objects only where the repository already follows that pattern.

`enqueue_ai_work` must use an upsert/update timestamp so repeated budget blocks do not duplicate rows.

- [ ] **Step 5: Run store tests**

```bash
pytest tests/test_store.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/store.py src/job_hunter/models.py tests/test_store.py
git commit -m "feat: persist Gemini usage and deferred work"
```

---

### Task 3: Build the quota tracker and circuit breaker

**Files:**
- Create: `src/job_hunter/gemini_usage.py`
- Create: `tests/test_gemini_usage.py`

**Interfaces:**
- Consumes: `GeminiQuotaSettings`, Task 2 store methods
- Produces: `GeminiPurpose`
- Produces: `GeminiBudgetExceeded`, `GeminiQuotaPaused`
- Produces: `GeminiUsageTracker.preflight(purpose, prompt, now)`
- Produces: `GeminiUsageTracker.record_success(...)`, `record_error(...)`, `record_429(...)`
- Produces: `GeminiUsageTracker.snapshot(now, run_id=None) -> GeminiUsageSummary`

- [ ] **Step 1: Write failing token-estimation and Pacific-day tests**

```python
def test_estimate_input_tokens_is_conservative_character_estimate():
    assert estimate_input_tokens("x" * 10) == 4
```

Use timestamps immediately before/after midnight in `America/Los_Angeles` to prove daily rows reset on Pacific midnight rather than UTC/Berlin midnight.

- [ ] **Step 2: Write failing 80% ceiling and core-reserve tests**

With `rpd=100`, the internal daily ceiling is 80 requests. Non-core work must be blocked once fewer than 20 requests remain inside that 80-request budget, while `job_evaluation` can continue until request 80.

```python
def test_non_core_cannot_consume_core_reserve(store, tracker):
    _record_attempts(store, 60, purpose="gmail_semantic")
    with pytest.raises(GeminiBudgetExceeded):
        tracker.preflight("gmail_semantic", "email", NOW)
    tracker.preflight("job_evaluation", "job", NOW)
```

Also cover RPM and estimated TPM blocking at 80%.

- [ ] **Step 3: Write failing pause-state tests**

Prove a 90-second rate pause blocks locally with no provider attempt and that a daily pause expires at the next Pacific midnight.

- [ ] **Step 4: Implement `gemini_usage.py`**

Define purposes as a literal/enum limited to:

```python
("gmail_semantic", "candidate_context", "job_evaluation", "cover_letter")
```

Use:

```python
def estimate_input_tokens(prompt: str) -> int:
    return max(1, math.ceil(len(prompt) / 3))
```

`preflight` must record `blocked_budget` only once for the attempted caller action and raise before HTTP. Rolling minute windows use the preceding 60 seconds; daily windows use Pacific midnight boundaries.

- [ ] **Step 5: Implement `snapshot`**

Return exact provider percentages:

```text
rpd_percent = requests_today / quota.rpd * 100
rpm_peak_percent = peak_rolling_requests / quota.rpm * 100
tpm_peak_percent = peak_rolling_input_tokens / quota.tpm * 100
```

Token totals use exact usage metadata where present and fall back to the conservative estimate only for attempts without metadata. Include per-purpose call counts.

- [ ] **Step 6: Run tracker tests**

```bash
pytest tests/test_gemini_usage.py -q
```

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/gemini_usage.py tests/test_gemini_usage.py
git commit -m "feat: enforce Gemini free-tier budgets"
```

---

### Task 4: Make the HTTP/Gemini client quota-aware and capture exact usage

**Files:**
- Modify: `src/job_hunter/http.py`
- Modify: `src/job_hunter/gemini.py`
- Modify: `tests/test_http.py`
- Modify: `tests/test_gemini.py`

**Interfaces:**
- Consumes: `GeminiUsageTracker`
- Produces: `HttpClient.post(..., retry_status_codes: set[int] | None = None, **kwargs)`
- Produces: `GeminiClient.generate_text(prompt, *, purpose, thinking_level, max_output_tokens, json_mode=False, json_schema=None) -> str`

- [ ] **Step 1: Write failing HTTP retry override test**

Prove default HTTP behavior still retries 429, but an explicit set excluding 429 sends exactly one request.

```python
response = client.post(url, retry_status_codes={500, 502, 503, 504})
assert session.request.call_count == 1
```

- [ ] **Step 2: Implement the retry-status override**

`get`/`post` accept an optional wrapper-only argument and pass it to `_request`; do not forward it into `requests.Session.request`.

- [ ] **Step 3: Extend fake Gemini responses with usage metadata**

Use a fixture shape:

```python
{
    "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
    "usageMetadata": {
        "promptTokenCount": 120,
        "candidatesTokenCount": 40,
        "thoughtsTokenCount": 30,
        "cachedContentTokenCount": 0,
        "totalTokenCount": 190,
    },
}
```

- [ ] **Step 4: Write failing generation-config tests**

Assert REST payload contains:

```python
"generationConfig": {
    "thinkingConfig": {"thinkingLevel": "minimal"},
    "maxOutputTokens": 800,
    ...
}
```

and that `purpose` is passed into the usage tracker.

- [ ] **Step 5: Write failing 429 tests**

Assert Gemini calls `HttpClient.post(... retry_status_codes={500, 502, 503, 504})`, records the 429 with the tracker, and raises `GeminiQuotaPaused`/a typed Gemini quota exception rather than a generic retriable error.

Cover:

- daily-quota text/details -> pause until Pacific reset;
- minute-rate text/details -> 90-second pause;
- unknown 429 -> conservative 90-second pause.

- [ ] **Step 6: Implement task-aware `GeminiClient`**

Construct `generationConfig` whenever any output/thinking/JSON control is present. Call tracker preflight before HTTP and persist response `usageMetadata` after success.

If metadata is absent, keep the successful text but record the attempt with the estimated input count and log a warning.

- [ ] **Step 7: Run focused tests**

```bash
pytest tests/test_http.py tests/test_gemini.py tests/test_gemini_usage.py -q
```

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/http.py src/job_hunter/gemini.py tests/test_http.py tests/test_gemini.py
git commit -m "feat: track and guard Gemini requests"
```

---

### Task 5: Extract and cache one rich CandidateContext

**Files:**
- Create: `src/job_hunter/candidate_context.py`
- Create: `tests/test_candidate_context.py`
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/preferences.py`
- Modify: `tests/test_preferences.py`

**Interfaces:**
- Produces: `CandidateContext`
- Produces: `get_candidate_context(profile, policy, gemini, store) -> CandidateContext`
- Produces: `CandidateContext.preferences -> CandidatePreferences`
- Consumed by: Tasks 6 and 8

- [ ] **Step 1: Define failing CandidateContext validation tests**

The model should carry at least:

```python
@dataclass(frozen=True, slots=True)
class CandidateContext:
    preferences: CandidatePreferences
    technical_skills: list[str]
    architecture_evidence: list[str]
    leadership_ownership: list[str]
    agentic_ai_evidence: list[str]
    product_domain_evidence: list[str]
    location_language_facts: list[str]
    career_direction: list[str]
    company_environment: list[str]
    career_evidence: list[str]
    evaluation_summary: str
```

Bound lists (for example 20 items, 180 characters per evidence item) and evaluation summary (for example 1,500 characters) so cached context stays compact.

- [ ] **Step 2: Write failing cache-key tests**

Use SHA-256 of source profile plus model plus a module constant such as `CANDIDATE_CONTEXT_SCHEMA_VERSION = "1"`. Verify identical inputs use the cache and profile/model/version changes cause one new extraction.

- [ ] **Step 3: Write failing Gemini request-control test**

Assert extraction calls:

```python
gemini.generate_text(
    prompt,
    purpose="candidate_context",
    thinking_level="medium",
    max_output_tokens=1800,
    json_mode=True,
    json_schema=CANDIDATE_CONTEXT_SCHEMA,
)
```

- [ ] **Step 4: Implement extraction and strict parser**

The prompt must explicitly say every fact must be supported by the source profile and that omitted/unknown facts remain absent rather than inferred.

Persist validated JSON through Task 2 cache methods only after successful parsing.

- [ ] **Step 5: Refactor `extract_candidate_preferences` compatibility**

Do not leave a per-run Gemini preference extraction. Either replace pipeline use with `context.preferences` directly or make `extract_candidate_preferences` a pure compatibility helper over `CandidateContext`. Preserve deterministic fallback behavior for ranking tests.

- [ ] **Step 6: Run focused tests**

```bash
pytest tests/test_candidate_context.py tests/test_preferences.py -q
```

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/candidate_context.py src/job_hunter/models.py src/job_hunter/preferences.py tests/test_candidate_context.py tests/test_preferences.py
git commit -m "feat: cache compact candidate context"
```

---

### Task 6: Use CandidateContext for evaluation and cover letters

**Files:**
- Modify: `src/job_hunter/evaluation.py`
- Modify: `src/job_hunter/cover_letter.py`
- Modify: `tests/test_evaluation.py`
- Modify: `tests/test_cover_letter.py`

**Interfaces:**
- Consumes: `CandidateContext`
- Produces: `evaluate_job(job, context, policy, gemini)`
- Produces: `generate_cover_letter(job, evaluation, context, template, gemini, today)`

- [ ] **Step 1: Update evaluation tests to reject full-profile dependency**

Build a `CandidateContext` fixture and verify the prompt contains its compact evidence/summary, not an arbitrary full-profile sentinel string.

Keep existing score validation and threshold tests unchanged.

- [ ] **Step 2: Add evaluation resource-control assertion**

Assert exact call controls:

```python
purpose="job_evaluation"
thinking_level="low"
max_output_tokens=1200
```

- [ ] **Step 3: Modify evaluation prompt/signature**

Preserve:

- all six `SCORE_MAXIMA` values;
- salary floor hard blocker;
- remote/relocation hard blocker;
- decision threshold calculation in Python.

Only replace the repeated full-profile section with serialized compact factual context.

- [ ] **Step 4: Update cover-letter tests and prompt**

Use `CandidateContext.career_evidence`, `evaluation.strengths`, `evaluation.gaps`, and the existing template. Assert:

```python
purpose="cover_letter"
thinking_level="low"
max_output_tokens=800
```

Keep placeholder validation exactly as today.

- [ ] **Step 5: Run focused tests**

```bash
pytest tests/test_evaluation.py tests/test_cover_letter.py -q
```

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/evaluation.py src/job_hunter/cover_letter.py tests/test_evaluation.py tests/test_cover_letter.py
git commit -m "feat: reduce Gemini evaluation context"
```

---

### Task 7: Stop obsolete Gmail backfill work and shrink semantic prompts

**Files:**
- Modify: `src/job_hunter/gmail_sync.py`
- Modify: `src/job_hunter/gmail_classifier.py`
- Modify: `src/job_hunter/cli.py`
- Modify: `tests/test_gmail_sync.py`
- Modify: `tests/test_gmail_classifier.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `MATCH_RECENCY_DAYS`, `DISCOVERY_FRESHNESS_DAYS`, quota-aware `GeminiClient`
- Produces: 120-day backfill query and semantic extraction gating

- [ ] **Step 1: Replace one-year backfill tests with 120-day tests**

For a fixed `now`, assert `build_backfill_query` uses `now - timedelta(days=MATCH_RECENCY_DAYS)` instead of `now.year - 1`.

Also change CLI help from "12-month backfill" to "120-day backfill".

- [ ] **Step 2: Write stale deterministic job-alert test**

Create a backfill job alert 15+ days old that would otherwise require semantic extraction. Assert it is recorded/processed without any `gemini.generate_text` call and no inbound job is staged.

Add the mirror test for a fresh alert: semantic extraction still runs when required.

- [ ] **Step 3: Write semantic prompt-size tests**

Give a 20,000-character body and >20 links. Assert serialized semantic email context contains at most 6,000 normalized body characters and 20 links.

- [ ] **Step 4: Assert Gmail resource controls**

```python
purpose="gmail_semantic"
thinking_level="minimal"
max_output_tokens=800
```

Keep JSON schema validation.

- [ ] **Step 5: Write quota-pause batch short-circuit test**

If semantic processing raises `GeminiQuotaPaused`, assert Gmail stops attempting later unprocessed messages in that invocation. Already completed messages remain persisted and remaining messages stay unprocessed for the next sync.

Do not count the pause as a permanent malformed-email review item.

- [ ] **Step 6: Implement the 120-day query, freshness gate, compact prompt, and pause handling**

Compute freshness before requesting job-alert extraction. Lifecycle messages within the 120-day window keep current deterministic-first behavior.

- [ ] **Step 7: Run Gmail tests**

```bash
pytest tests/test_gmail_classifier.py tests/test_gmail_sync.py tests/test_cli.py -q
```

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/gmail_sync.py src/job_hunter/gmail_classifier.py src/job_hunter/cli.py tests/test_gmail_sync.py tests/test_gmail_classifier.py tests/test_cli.py
git commit -m "feat: minimize Gmail Gemini usage"
```

---

### Task 8: Defer quota-blocked evaluation/material work without corrupting job state

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/cli.py`
- Modify: `tests/test_pipeline.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2 pending work, CandidateContext, quota-aware `GeminiClient`
- Produces: persistent `job_evaluation` and `cover_letter` retry behavior

- [ ] **Step 1: Write failing deferred-evaluation test**

When `evaluate_job` raises `GeminiBudgetExceeded`/`GeminiQuotaPaused`, assert:

- no evaluation row is saved;
- the job is queued as `job_evaluation` pending work;
- it is not counted as a skipped/bad match.

- [ ] **Step 2: Write pending-evaluation retry test**

On a later run with quota available, load pending evaluation jobs from the store, evaluate them before lower-priority new work, save the real evaluation, and remove the queue row.

- [ ] **Step 3: Write failing deferred-cover-letter test**

A ready-to-apply evaluation whose cover-letter call is budget-blocked must:

- remain a ready-to-apply job;
- enqueue `cover_letter` pending work;
- not increment a generic model-error count;
- still be included in Telegram job delivery without a PDF.

- [ ] **Step 4: Write pending-cover-letter retry test**

On a later run, use the saved job + evaluation + cached CandidateContext to generate the missing material without repeating job evaluation. Remove pending work after material is saved.

- [ ] **Step 5: Construct one quota-aware Gemini client in both CLI flows**

Both `sync-gmail` and `run` must use the same SQLite-backed tracker and the same `GEMINI_RUN_ID` when present. Do not instantiate an untracked `GeminiClient` in `pipeline.py`.

A clean interface is:

```python
tracker = GeminiUsageTracker(store, settings.gemini_quota, settings.gemini_model, run_id=os.getenv("GEMINI_RUN_ID"))
gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model, http, tracker=tracker)
```

Pass the constructed client/tracker into `run_pipeline` and Gmail service.

- [ ] **Step 6: Replace per-run preference extraction with cached CandidateContext**

Load/create context once before ranking. Rank with `context.preferences`; evaluate/cover with the same context.

- [ ] **Step 7: Run pipeline/CLI tests**

```bash
pytest tests/test_pipeline.py tests/test_cli.py -q
```

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/pipeline.py src/job_hunter/cli.py tests/test_pipeline.py tests/test_cli.py
git commit -m "feat: defer Gemini work under quota pressure"
```

---

### Task 9: Add Telegram and log usage reporting

**Files:**
- Modify: `src/job_hunter/telegram.py`
- Modify: `src/job_hunter/pipeline.py`
- Modify: `src/job_hunter/models.py`
- Modify: `tests/test_telegram.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `GeminiUsageTracker.snapshot`
- Produces: `build_gemini_usage_status(summary) -> str`
- Produces: `build_gemini_pause_warning(summary) -> str | None`

- [ ] **Step 1: Write formatter tests for green/yellow/red states**

Expected form:

```text
Gemini 🟢 RPD 34% · RPM peak 20% · TPM peak 17% · 21 calls · 142k tokens
```

Color thresholds:

- `<60` max provider utilization -> 🟢
- `>=60 and <80` -> 🟡
- `>=80 or paused` -> 🔴

Round percentages to whole numbers. Format token total compactly (`142k`, `1.2M`) without inventing a daily token-quota percentage.

- [ ] **Step 2: Write warning de-duplication tests**

For a paused/budget-blocked run, exactly one warning message should be sent even if many calls were locally blocked.

- [ ] **Step 3: Add per-purpose structured logging**

At run completion log one line containing total calls/tokens and purpose counts, for example:

```text
gemini_usage run_calls=21 rpd_pct=34.0 rpm_peak_pct=20.0 tpm_peak_pct=17.0 input=... output=... thinking=... purposes=gmail_semantic:5,job_evaluation:13,cover_letter:2,candidate_context:1
```

- [ ] **Step 4: Integrate Telegram delivery order**

Send usage status once per normal bot run after PDFs/Gmail review messages but **before** the interactive navigator. If there is a pause warning, send it immediately before the usage status or combine delivery ordering so the navigator remains last.

Telemetry send failure is logged but does not fail the pipeline.

- [ ] **Step 5: Run Telegram/pipeline tests**

```bash
pytest tests/test_telegram.py tests/test_pipeline.py -q
```

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/telegram.py src/job_hunter/pipeline.py src/job_hunter/models.py tests/test_telegram.py tests/test_pipeline.py
git commit -m "feat: report Gemini free-tier usage"
```

---

### Task 10: Wire GitHub Actions and document the zero-cost runbook

**Files:**
- Modify: `.github/workflows/daily.yml`
- Modify: `tests/test_workflow.py`
- Modify: `README.md`

**Interfaces:**
- Produces: identical `GEMINI_RUN_ID` and quota env values for Gmail and pipeline processes
- Operational dependency: user copies current `gemini-3.6-flash` Free Tier RPM/TPM/RPD values from the AI Studio Rate Limits page into GitHub repository Actions variables before running the workflow.

- [ ] **Step 1: Write workflow contract tests**

Assert both `Sync Gmail intelligence` and `Run job hunter` expose:

```yaml
GEMINI_FREE_RPM: ${{ vars.GEMINI_FREE_RPM }}
GEMINI_FREE_TPM: ${{ vars.GEMINI_FREE_TPM }}
GEMINI_FREE_RPD: ${{ vars.GEMINI_FREE_RPD }}
GEMINI_RUN_ID: ${{ github.run_id }}
```

Keep `GEMINI_API_KEY` as a secret.

- [ ] **Step 2: Modify the workflow**

Add the four values to both Gemini-using steps. Do not add billing credentials or Google Cloud service-account access.

- [ ] **Step 3: Update README setup instructions**

Document this exact operational sequence:

1. Keep the Job Hunter Gemini Google project unlinked from Cloud Billing.
2. Open AI Studio -> Rate Limits for the bot project and selected model.
3. Copy RPM, input TPM, and RPD into GitHub Actions variables named `GEMINI_FREE_RPM`, `GEMINI_FREE_TPM`, `GEMINI_FREE_RPD`.
4. Keep `GEMINI_API_KEY` in GitHub Actions secrets.
5. When the Gemini project or `GEMINI_MODEL` changes, refresh the three quota variables before the next run.
6. Interpret Telegram RPD/RPM/TPM percentages and the 80% local ceiling.
7. A 429 pauses/defer work; it does not enable paid overflow.

Also change historical Gmail documentation from 12 months to 120 days.

- [ ] **Step 4: Run workflow/doc-adjacent tests**

```bash
pytest tests/test_workflow.py tests/test_config.py tests/test_cli.py -q
```

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/daily.yml tests/test_workflow.py README.md
git commit -m "docs: configure zero-cost Gemini operations"
```

---

### Task 11: Regression verification and quality-preservation gate

**Files:**
- No production files expected unless a regression is found.
- Test files may be corrected only to reflect intentionally changed interfaces, not to weaken assertions.

- [ ] **Step 1: Run all Gemini/Gmail/pipeline tests together**

```bash
pytest \
  tests/test_gemini_usage.py \
  tests/test_gemini.py \
  tests/test_candidate_context.py \
  tests/test_preferences.py \
  tests/test_evaluation.py \
  tests/test_cover_letter.py \
  tests/test_gmail_classifier.py \
  tests/test_gmail_sync.py \
  tests/test_pipeline.py \
  tests/test_telegram.py \
  tests/test_cli.py \
  tests/test_workflow.py -q
```

Expected: PASS.

- [ ] **Step 2: Verify scoring semantics explicitly**

Run existing evaluation tests and inspect that these constants/rules are unchanged:

```text
role_seniority=30
technical=25
product_architecture=20
career_direction=10
location_language=10
company_environment=5
HIGH_PRIORITY_THRESHOLD=85
```

Package/possible thresholds continue to come from policy. No implementation task may lower quality by changing these values.

- [ ] **Step 3: Run the complete suite**

```bash
pytest -q
```

Expected: all tests PASS with no real Gemini network call.

- [ ] **Step 4: Run static diff hygiene**

```bash
git diff --check
```

Expected: no whitespace errors.

- [ ] **Step 5: Inspect for untracked Gemini call sites**

```bash
grep -R "generate_text(" -n src/job_hunter
```

Every production call must include a valid `purpose`, `thinking_level`, and `max_output_tokens`; there must be no direct untracked Gemini REST call elsewhere.

- [ ] **Step 6: Inspect for obsolete one-year Gmail wording/code**

```bash
grep -R -n -E "12-month|12 month|now\.year - 1" src tests README.md .github || true
```

Expected: no active 12-month Gmail-backfill logic/documentation remains.

- [ ] **Step 7: Commit any test-only corrections, if needed**

Only if the previous verification exposed legitimate integration corrections:

```bash
git add tests
git commit -m "test: verify Gemini free-tier guardrails"
```

Do not create an empty commit.

---

## Post-implementation operational checklist

Before the first production workflow run after merging:

- Confirm the Job Hunter Bot Gemini project still has Cloud Billing disabled/unlinked.
- Confirm the Interviewer App is no longer using an API key from the same Gemini project.
- In AI Studio, read the active `gemini-3.6-flash` Free Tier RPM, input TPM, and RPD values for the Job Hunter project.
- Set those exact values in GitHub Actions repository variables.
- Do **not** run a forced historical Gmail backfill; the new 120-day query should naturally complete from persisted state.
- Trigger one Job Hunter workflow manually and verify Telegram reports separate RPD/RPM/TPM utilization.
- If the usage line is red or a pause warning appears, do not enable billing; inspect the per-purpose usage logs and allow the quota window to reset.

## Expected outcome

After these tasks, normal bot quality should come primarily from job evaluation rather than quota spent on old Gmail messages or repeated CV context. The same persisted SQLite artifact will carry Gmail state, candidate-context cache, pending AI work, daily usage, and circuit-breaker state between GitHub Actions runs. Quota pressure will delay optional/model-dependent work instead of generating charges or corrupting job decisions.

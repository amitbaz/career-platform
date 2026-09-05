# Job Hunter Bot R1 — Gmail Intelligence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add read-only Gmail intelligence to the standalone Job Hunter Bot so it can ingest LinkedIn/job-board alerts and recruiter opportunities, reconstruct application lifecycle events, and surface ambiguous events without disrupting the existing daily search.

**Architecture:** Implement Gmail as a separate `sync-gmail` stage sharing the existing SQLite database. Gmail messages are fetched with read-only OAuth, classified deterministically first and with Gemini only when needed, then persisted as application events or staged job candidates. The normal `run` command adds a DB-backed Gmail source so staged jobs pass through the existing enrichment, deduplication, profession gate, ranking, evaluation, cover-letter, and Telegram delivery path.

**Tech Stack:** Python 3.12, SQLite, `requests`, `beautifulsoup4`, Google OAuth (`google-auth`, `google-auth-oauthlib`), Gmail REST API v1, existing Gemini REST client, pytest, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-31-job-hunter-bot-r1-gmail-intelligence-design.md`

## Global Constraints

- Gmail access is read-only: no send, reply, archive, delete, label, mark read/unread, or other mailbox mutation.
- Do not automate logged-in LinkedIn browsing, store LinkedIn credentials/cookies, or submit applications.
- The first successful Gmail connection performs a 12-month backfill; normal runs are incremental afterward.
- Gmail message ID is the message-level idempotency key.
- Low-confidence or ambiguous lifecycle signals become `REVIEW_NEEDED`; they never mutate derived application state.
- Only high-confidence lifecycle events with a resolved `job_id` participate in derived application state.
- Derived state is timestamp-first; same-timestamp precedence is `OFFER > REJECTED > TECHNICAL > INTERVIEW > RECRUITER_CONTACT > APPLIED`.
- Full email bodies are processed in memory and are not persisted by default.
- OAuth credentials are GitHub Secrets only and must never enter SQLite or repository files.
- `sync-gmail --dry-run` may read/classify/extract but must not persist jobs, staged candidates, application events, delivery state, processed-message state, or Gmail cursors.
- Gmail failures are fail-open for the daily workflow; the existing public-source job search still runs.
- A hard per-message fetch/persistence error must not advance the Gmail history cursor or mark a backfill complete; already-successful messages remain safe to replay because message IDs are idempotent.
- Release 1 does not add company watchlists, new ATS adapters, Telegram inbound URLs, learning-to-rank, Pub/Sub, Relay integration, or application submission.

---

## File Structure

### New production files

- `src/job_hunter/gmail_models.py` — Gmail-only domain types/constants: decoded message, extracted links/jobs, classification, settings, sync summary.
- `src/job_hunter/gmail_auth.py` — read-only OAuth token provider used by the runtime Gmail client.
- `src/job_hunter/gmail_client.py` — Gmail REST calls, pagination/history handling, MIME decoding, and in-memory HTML-link extraction.
- `src/job_hunter/gmail_classifier.py` — deterministic job-signal rules, job extraction, Gemini fallback, confidence gating.
- `src/job_hunter/gmail_matching.py` — ordered job matching and derived application-state calculation.
- `src/job_hunter/gmail_sync.py` — backfill/incremental orchestration, idempotent processing, staging/events, sync metrics.
- `src/job_hunter/sources/gmail_staged.py` — DB-backed `JobSource` exposing staged candidates not already represented in `jobs`.
- `scripts/gmail_oauth_bootstrap.py` — one-time local interactive OAuth helper that prints a refresh token.

### Modified production files

- `pyproject.toml` — add Google OAuth dependencies.
- `src/job_hunter/models.py` — add `ReviewItem` only; Gmail-specific domain types stay in `gmail_models.py`.
- `src/job_hunter/config.py` — add Gmail-specific settings loader without making normal `run` require Gmail secrets.
- `src/job_hunter/store.py` — add Gmail sync/message/staging/application-event/review-delivery tables and methods.
- `src/job_hunter/pipeline.py` — append `GmailStagedSource` after store creation; deliver pending Gmail review items.
- `src/job_hunter/telegram.py` — format Gmail `REVIEW_NEEDED` items.
- `src/job_hunter/cli.py` — add `sync-gmail`, `--dry-run`, and `--force-backfill`.
- `.github/workflows/daily.yml` — run Gmail sync before normal search with `continue-on-error: true`.
- `.env.example` — document Gmail variable names without values.
- `README.md` — document OAuth bootstrap, secrets, dry-run/backfill, privacy, and fail-open behavior.

### New tests

- `tests/test_gmail_auth.py`
- `tests/test_gmail_client.py`
- `tests/test_gmail_classifier.py`
- `tests/test_gmail_matching.py`
- `tests/test_gmail_sync.py`
- `tests/test_gmail_staged_source.py`

### Existing tests to extend

- `tests/test_store.py`
- `tests/test_config.py`
- `tests/test_cli.py`
- `tests/test_pipeline.py`
- `tests/test_telegram.py`

---

### Task 1: Gmail Domain Types, Settings, and OAuth Bootstrap

**Files:**
- Create: `src/job_hunter/gmail_models.py`
- Create: `src/job_hunter/gmail_auth.py`
- Create: `scripts/gmail_oauth_bootstrap.py`
- Modify: `src/job_hunter/config.py`
- Modify: `pyproject.toml`
- Test: `tests/test_gmail_auth.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `GMAIL_READONLY_SCOPE: str`
- Produces: `AUTO_CONFIDENCE_THRESHOLD = 0.90`
- Produces: `MATCH_RECENCY_DAYS = 120`
- Produces: `DISCOVERY_FRESHNESS_DAYS = 14`
- Produces: `GmailSettings(client_id, client_secret, refresh_token, gemini_api_key, gemini_model, db_path)`
- Produces: `GmailMessage(message_id, thread_id, sender, subject, sent_at, snippet, body, links)`
- Produces: `ExtractedJob(source_platform, source_job_id, url, company, title, location, remote, description, index)`
- Produces: `GmailClassification(kind, confidence, company, role_title, source_job_id, job_urls, jobs, rationale)`
- Produces: `GmailSyncSummary(fetched, processed, job_alerts, application_events, review_needed, irrelevant, errors)`
- Produces: `AccessTokenProvider.get_access_token() -> str`
- Produces: `GoogleOAuthTokenProvider(settings: GmailSettings).get_access_token() -> str`
- Produces: `load_gmail_settings() -> GmailSettings`

- [ ] **Step 1: Write failing settings tests**

Add to `tests/test_config.py`:

```python
from job_hunter.config import load_gmail_settings


def test_load_gmail_settings_does_not_require_candidate_profile(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("CANDIDATE_PROFILE_B64", raising=False)
    settings = load_gmail_settings()
    assert settings.client_id == "client"
    assert settings.db_path == "var/job_hunter.sqlite3"


def test_load_gmail_settings_requires_refresh_token(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini")
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN", raising=False)
    with pytest.raises(ValueError, match="GMAIL_REFRESH_TOKEN"):
        load_gmail_settings()
```

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_config.py -q
```

Expected: FAIL because `load_gmail_settings`/`GmailSettings` do not exist.

- [ ] **Step 3: Add Gmail types/constants and settings loader**

Create `gmail_models.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SUPPORTED_KINDS = frozenset({
    "JOB_ALERT", "RECRUITER_CONTACT", "APPLIED", "INTERVIEW",
    "TECHNICAL", "OFFER", "REJECTED", "REVIEW_NEEDED", "IRRELEVANT",
})
AUTO_CONFIDENCE_THRESHOLD = 0.90
MATCH_RECENCY_DAYS = 120
DISCOVERY_FRESHNESS_DAYS = 14


@dataclass(frozen=True, slots=True)
class GmailSettings:
    client_id: str
    client_secret: str
    refresh_token: str
    gemini_api_key: str
    gemini_model: str = "gemini-3.6-flash"
    db_path: str = "var/job_hunter.sqlite3"


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    sent_at: datetime
    snippet: str
    body: str
    links: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExtractedJob:
    source_platform: str
    source_job_id: str | None = None
    url: str = ""
    company: str = ""
    title: str = ""
    location: str = ""
    remote: bool | None = None
    description: str = ""
    index: int = 0


@dataclass(frozen=True, slots=True)
class GmailClassification:
    kind: str
    confidence: float
    company: str = ""
    role_title: str = ""
    source_job_id: str | None = None
    job_urls: list[str] = field(default_factory=list)
    jobs: list[ExtractedJob] = field(default_factory=list)
    rationale: str = ""


@dataclass(slots=True)
class GmailSyncSummary:
    fetched: int = 0
    processed: int = 0
    job_alerts: int = 0
    application_events: int = 0
    review_needed: int = 0
    irrelevant: int = 0
    errors: int = 0
```

Add `load_gmail_settings()` in `config.py` using `_require_env()` for `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`, and `GEMINI_API_KEY`, plus current defaults for `GEMINI_MODEL`/`JOB_HUNTER_DB_PATH`.

- [ ] **Step 4: Add OAuth dependencies and failing provider test**

Update `pyproject.toml` dependencies:

```toml
"google-auth>=2.40",
"google-auth-oauthlib>=1.2",
```

Create a fake credentials object in `tests/test_gmail_auth.py`; assert the provider refreshes expired credentials and returns the cached token afterward.

- [ ] **Step 5: Implement read-only token provider and bootstrap script**

`gmail_auth.py` must construct Google credentials with exactly `[GMAIL_READONLY_SCOPE]`. The provider object retains credentials in memory so a valid access token is reused until refresh is required.

Bootstrap code:

```python
client_config = {
    "installed": {
        "client_id": os.environ["GMAIL_CLIENT_ID"],
        "client_secret": os.environ["GMAIL_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }
}
flow = InstalledAppFlow.from_client_config(client_config, scopes=[GMAIL_READONLY_SCOPE])
credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
if not credentials.refresh_token:
    raise RuntimeError("Google did not return a refresh token; revoke prior consent and retry")
print(credentials.refresh_token)
```

- [ ] **Step 6: Run tests and commit**

```bash
pytest tests/test_config.py tests/test_gmail_auth.py -q
git add pyproject.toml src/job_hunter/config.py src/job_hunter/gmail_models.py src/job_hunter/gmail_auth.py scripts/gmail_oauth_bootstrap.py tests/test_config.py tests/test_gmail_auth.py
git commit -m "feat: add Gmail OAuth configuration"
```

Expected: tests PASS before commit.

---

### Task 2: Gmail REST Client, MIME Decoding, and Link Preservation

**Files:**
- Create: `src/job_hunter/gmail_client.py`
- Test: `tests/test_gmail_client.py`

**Interfaces:**
- Consumes: `AccessTokenProvider`, `GmailMessage`
- Produces: `GmailPage(message_ids: list[str], next_page_token: str | None)`
- Produces: `GmailHistoryPage(message_ids: list[str], history_id: str, next_page_token: str | None)`
- Produces: `GmailHistoryExpired`
- Produces: `GmailClient.get_profile() -> tuple[str, str]` returning `(email_address, history_id)`
- Produces: `GmailClient.list_message_ids(query, page_token=None) -> GmailPage`
- Produces: `GmailClient.list_history(start_history_id, page_token=None) -> GmailHistoryPage`
- Produces: `GmailClient.get_message(message_id) -> GmailMessage`

- [ ] **Step 1: Write failing REST/pagination tests**

Cover profile, search pagination, history pagination, bearer auth, and history expiry:

```python
def test_list_message_ids_returns_ids_and_page_token():
    http = FakeHttp({"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"})
    client = GmailClient(http=http, token_provider=FakeTokenProvider("token"))
    page = client.list_message_ids("after:2025/08/31")
    assert page.message_ids == ["m1", "m2"]
    assert page.next_page_token == "p2"
    assert http.last_headers["Authorization"] == "Bearer token"


def test_history_404_raises_history_expired():
    client = GmailClient(http=Fake404Http(), token_provider=FakeTokenProvider("token"))
    with pytest.raises(GmailHistoryExpired):
        client.list_history("123")
```

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_gmail_client.py -q
```

Expected: FAIL because `gmail_client.py` does not exist.

- [ ] **Step 3: Implement read-only Gmail endpoints**

Use only:

```text
GET https://gmail.googleapis.com/gmail/v1/users/me/profile
GET https://gmail.googleapis.com/gmail/v1/users/me/messages
GET https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}?format=full
GET https://gmail.googleapis.com/gmail/v1/users/me/history?historyTypes=messageAdded
```

Every call sends `Authorization: Bearer <token>`. Search/history page size is 100. Deduplicate history message IDs per page. Convert a history 404 into `GmailHistoryExpired`; other HTTP failures call `raise_for_status()`.

- [ ] **Step 4: Write failing MIME/link tests**

Cover:

```python
def test_decode_prefers_plain_text_over_html(): ...
def test_decode_falls_back_to_stripped_html(): ...
def test_decode_recurses_nested_multipart(): ...
def test_decode_uses_snippet_when_body_empty(): ...
def test_html_anchor_hrefs_are_preserved_in_message_links(): ...
def test_binary_attachment_is_not_loaded(): ...
```

Use:

```python
def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
```

- [ ] **Step 5: Implement safe body decoding plus in-memory links**

`get_message()` must:

1. Normalize headers case-insensitively.
2. Parse `From`, `Subject`, and Gmail `internalDate` into UTC `datetime`.
3. Recursively inspect MIME parts.
4. Decode inline `text/plain` and `text/html` `body.data` only; never fetch binary attachments.
5. Before stripping HTML, collect unique absolute `http://`/`https://` anchor `href` values into `GmailMessage.links`.
6. Prefer joined plain-text parts as `body`; otherwise strip HTML with BeautifulSoup.
7. Fall back to `snippet` when there is no inline textual body.

This preserves LinkedIn/ATS URLs without persisting raw HTML.

- [ ] **Step 6: Run tests and commit**

```bash
pytest tests/test_gmail_client.py -q
git add src/job_hunter/gmail_client.py tests/test_gmail_client.py
git commit -m "feat: add Gmail read-only client"
```

Expected: PASS.

---

### Task 3: Gmail Persistence, Staging, Events, and Review Delivery State

**Files:**
- Modify: `src/job_hunter/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `JobStore.has_processed_gmail_message(message_id: str) -> bool`
- Produces: `JobStore.record_gmail_message(...) -> None`
- Produces: `JobStore.get_gmail_sync_state(account_id: str) -> sqlite3.Row | None`
- Produces: `JobStore.save_gmail_sync_state(account_id, history_id, last_successful_sync_at, backfill_completed_at) -> None`
- Produces: `JobStore.stage_inbound_job(source_message_id, source_candidate_key, job: ExtractedJob) -> int`
- Produces: `JobStore.list_unmaterialized_inbound_jobs() -> list[sqlite3.Row]`
- Produces: `JobStore.save_application_event(...) -> int`
- Produces: `JobStore.list_application_events(job_id: int) -> list[sqlite3.Row]`
- Produces: `JobStore.pending_review_events() -> list[sqlite3.Row]`
- Produces: `JobStore.mark_review_delivered(event_ids: list[int], telegram_message_id: str) -> None`

- [ ] **Step 1: Write failing schema/idempotency tests**

Fresh DB tests must assert the new tables exist. Add duplicate event/candidate tests:

```python
def test_application_event_source_message_is_idempotent(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    first = store.save_application_event(
        job_id=None, event_type="REVIEW_NEEDED", occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="m1", source_thread_id="t1", confidence=0.4,
        company="Acme", role_title="Frontend Engineer", rationale="ambiguous",
    )
    second = store.save_application_event(
        job_id=None, event_type="REVIEW_NEEDED", occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="m1", source_thread_id="t1", confidence=0.4,
        company="Acme", role_title="Frontend Engineer", rationale="ambiguous",
    )
    assert second == first
```

Also assert duplicate `(origin, source_message_id, source_candidate_key)` updates `last_seen_at` and returns the existing candidate ID.

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_store.py -q
```

Expected: FAIL because Gmail tables/methods do not exist.

- [ ] **Step 3: Add tables without altering existing schema semantics**

```sql
CREATE TABLE IF NOT EXISTS gmail_sync_state (
    account_id TEXT PRIMARY KEY,
    history_id TEXT,
    last_successful_sync_at TEXT,
    last_processed_message_at TEXT,
    backfill_completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gmail_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL DEFAULT 'gmail',
    source_message_id TEXT NOT NULL,
    source_candidate_key TEXT NOT NULL,
    source_platform TEXT NOT NULL DEFAULT '',
    source_job_id TEXT,
    url TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(origin, source_message_id, source_candidate_key)
);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'gmail',
    source_message_id TEXT NOT NULL UNIQUE,
    source_thread_id TEXT,
    confidence REAL NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role_title TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_deliveries (
    event_id INTEGER PRIMARY KEY REFERENCES application_events(id),
    delivered_at TEXT NOT NULL,
    telegram_message_id TEXT
);
```

No table contains full email body or OAuth credentials.

- [ ] **Step 4: Implement transactional store methods**

Rules:

- `record_gmail_message` uses `INSERT OR IGNORE` and receives no body parameter.
- `stage_inbound_job` uses `INSERT ... ON CONFLICT(origin, source_message_id, source_candidate_key) DO UPDATE SET last_seen_at=excluded.last_seen_at`.
- `save_application_event` returns the existing ID on duplicate `source_message_id`.
- `pending_review_events` joins `gmail_messages` on `source_message_id` to expose the email subject, excludes delivered events, and sorts by `occurred_at`, then event ID.
- `save_gmail_sync_state` is never called from dry-run branches.

- [ ] **Step 5: Write failing cross-source materialization tests**

Create three tests for `list_unmaterialized_inbound_jobs()`:

```python
def test_candidate_not_emitted_when_any_job_has_same_canonical_url(tmp_path): ...
def test_candidate_not_emitted_when_url_missing_but_identity_matches(tmp_path): ...
def test_candidate_emitted_when_no_existing_job_matches(tmp_path): ...
```

Use the same identity definition as `discovery.candidate_identity_key`: normalized `(company, title, location)`.

- [ ] **Step 6: Implement cross-source materialization filtering**

Inside `list_unmaterialized_inbound_jobs()`, load staged rows and the lightweight existing-job fields `source`, `source_job_id`, `url`, `company`, `title`, `location`. Exclude a staged candidate if any existing job satisfies, in order:

1. same Gmail materialization identity: `job.source == f"gmail:{source_platform}"` and `job.source_job_id == source_candidate_key`;
2. both URLs present and `canonicalize_url(job.url) == canonicalize_url(candidate.url)`;
3. candidate identity is not empty and normalized `(company, title, location)` exactly matches.

This mirrors current in-run deduplication enough to prevent a richer public-source winner from causing the Gmail staging row to reappear on later runs.

- [ ] **Step 7: Run tests and commit**

```bash
pytest tests/test_store.py -q
git add src/job_hunter/store.py tests/test_store.py
git commit -m "feat: persist Gmail intelligence state"
```

Expected: PASS.

---

### Task 4: Deterministic Classification, Job Extraction, and Gemini Fallback

**Files:**
- Create: `src/job_hunter/gmail_classifier.py`
- Test: `tests/test_gmail_classifier.py`

**Interfaces:**
- Consumes: `GmailMessage`, `GmailClassification`, `ExtractedJob`, `GeminiClient`
- Produces: `is_probably_job_related(message: GmailMessage) -> bool`
- Produces: `classify_deterministically(message: GmailMessage) -> GmailClassification | None`
- Produces: `classify_email(message: GmailMessage, gemini: GeminiClient) -> GmailClassification`
- Produces: `source_candidate_key(job: ExtractedJob) -> str`

- [ ] **Step 1: Write failing deterministic tests**

```python
@pytest.mark.parametrize((subject, body, expected), [
    ("Thanks for applying", "We received your application for Frontend Engineer.", "APPLIED"),
    ("Update on your application", "We will not be moving forward with your application.", "REJECTED"),
    ("Interview invitation", "Choose a time for your interview with our team.", "INTERVIEW"),
    ("Technical assessment", "Please complete this coding challenge by Friday.", "TECHNICAL"),
    ("Offer", "We are pleased to offer you the position.", "OFFER"),
])
def test_high_signal_templates_are_deterministic(subject, body, expected): ...
```

Add a LinkedIn alert message with sender `jobalerts-noreply@linkedin.com` and `links=["https://www.linkedin.com/jobs/view/1234567890/"]`; assert `JOB_ALERT`, confidence `1.0`, and one extracted job URL.

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_gmail_classifier.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement deterministic precedence and cheap irrelevance gate**

Use:

```text
OFFER > REJECTED > TECHNICAL > INTERVIEW > APPLIED > JOB_ALERT > RECRUITER_CONTACT
```

`is_probably_job_related()` returns true on strong terms (`application`, `interview`, `recruiter`, `hiring`, `job alert`, `position`, `technical assessment`, `coding challenge`, `offer`) or known job-platform senders. If false, `classify_email()` returns `IRRELEVANT` at confidence `1.0` without Gemini.

- [ ] **Step 4: Implement URL/job extraction and stable candidate keys**

Use `message.links` first, plus visible absolute URLs found in body text. Canonicalize with existing `canonicalize_url()`. Retain known job URLs (`linkedin.com/jobs/view`, Greenhouse, Lever, Ashby, Workable) deterministically; generic URLs are accepted only when Gemini identifies them as job URLs.

Candidate key:

```python
if job.url:
    return "url:" + canonicalize_url(job.url)
if job.source_job_id:
    return f"id:{job.source_platform.lower()}:{job.source_job_id}"
return "fallback:" + "|".join([
    normalize_text(job.company), normalize_text(job.title), str(job.index)
])
```

- [ ] **Step 5: Write failing semantic tests**

Use fake Gemini JSON responses. Cover confident recruiter outreach, low-confidence lifecycle output, malformed JSON, unsupported kind, and low-confidence irrelevant output. Example:

```python
def test_low_confidence_semantic_lifecycle_becomes_review_needed():
    gemini = FakeGemini('{"kind":"INTERVIEW","confidence":0.72,"company":"Acme","role_title":"Frontend Engineer","source_job_id":null,"job_urls":[],"jobs":[],"rationale":"possibly scheduling"}')
    result = classify_email(message("Next steps", "Can we find time to chat?"), gemini)
    assert result.kind == "REVIEW_NEEDED"
```

- [ ] **Step 6: Implement strict Gemini fallback**

Call `gemini.generate_text(prompt, json_mode=True)`. Include this schema instruction verbatim:

```text
Return one JSON object only with keys:
kind, confidence, company, role_title, source_job_id, job_urls, jobs, rationale.
kind must be one of JOB_ALERT, RECRUITER_CONTACT, APPLIED, INTERVIEW, TECHNICAL, OFFER, REJECTED, REVIEW_NEEDED, IRRELEVANT.
confidence must be a number from 0 to 1.
jobs must be an array of objects with source_platform, source_job_id, url, company, title, location, remote, description.
Do not infer facts not present in the email. Keep rationale under 160 characters.
```

Send only sender, subject, snippet, up to 20,000 body characters, and the extracted link list. Malformed/unsupported/conflicting output becomes `REVIEW_NEEDED`. Any likely-job semantic result below `0.90`, including uncertain `IRRELEVANT`, becomes `REVIEW_NEEDED`.

- [ ] **Step 7: Run tests and commit**

```bash
pytest tests/test_gmail_classifier.py tests/test_gemini.py -q
git add src/job_hunter/gmail_classifier.py tests/test_gmail_classifier.py
git commit -m "feat: classify Gmail job signals"
```

Expected: PASS.

---

### Task 5: Ordered Job Matching and Derived Application State

**Files:**
- Create: `src/job_hunter/gmail_matching.py`
- Modify: `src/job_hunter/store.py`
- Test: `tests/test_gmail_matching.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: `JobMatch(job_id: int | None, reason: str, ambiguous: bool)`
- Produces: `match_job(store, classification, message) -> JobMatch`
- Produces: `derive_application_state(events) -> str | None`
- Produces: `JobStore.list_jobs_for_matching() -> list[sqlite3.Row]`
- Produces: `JobStore.current_application_state(job_id: int) -> str | None`

- [ ] **Step 1: Write failing priority-order tests**

```python
def test_exact_canonical_url_beats_company_title(tmp_path): ...
def test_source_job_id_is_second_priority(tmp_path): ...
def test_company_and_normalized_title_matches_when_unique(tmp_path): ...
def test_company_only_recent_match_requires_single_candidate(tmp_path): ...
def test_ambiguous_company_match_returns_no_job(tmp_path): ...
```

Company-only recency is exactly 120 days, measured from message `sent_at` against job `first_seen_at`/`last_seen_at`.

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_gmail_matching.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement matching without mutation**

Apply priorities exactly:

1. canonical URL;
2. source job ID;
3. normalized company + normalized role title;
4. company-only within 120 days, only if exactly one candidate;
5. unresolved/ambiguous.

Canonicalize with `canonicalize_url()` and normalize with `normalize_text()`. Do not create placeholder jobs for unresolved historical applications.

- [ ] **Step 4: Write failing derived-state tests**

```python
def test_latest_event_wins_even_if_earlier_event_has_higher_stage(): ...
def test_same_timestamp_uses_offer_before_rejected(): ...
def test_review_needed_never_becomes_current_state(): ...
def test_unresolved_event_never_becomes_current_state(): ...
def test_low_confidence_event_never_becomes_current_state(): ...
```

- [ ] **Step 5: Implement approved derived-state rule**

Filter to resolved, confidence `>= 0.90`, non-review lifecycle events. Sort by:

```python
_STATE_TIE_PRECEDENCE = {
    "APPLIED": 1,
    "RECRUITER_CONTACT": 2,
    "INTERVIEW": 3,
    "TECHNICAL": 4,
    "REJECTED": 5,
    "OFFER": 6,
}
key = (event["occurred_at"], _STATE_TIE_PRECEDENCE[event["event_type"]], event["id"])
```

Return the maximum event type.

- [ ] **Step 6: Integrate store query/state method and commit**

```bash
pytest tests/test_gmail_matching.py tests/test_store.py -q
git add src/job_hunter/gmail_matching.py src/job_hunter/store.py tests/test_gmail_matching.py tests/test_store.py
git commit -m "feat: match Gmail events to jobs"
```

Expected: PASS.

---

### Task 6: Backfill and Incremental Gmail Sync Orchestration

**Files:**
- Create: `src/job_hunter/gmail_sync.py`
- Test: `tests/test_gmail_sync.py`

**Interfaces:**
- Consumes: Gmail client/classifier/matcher/store interfaces from Tasks 2–5 and existing `GeminiClient`
- Produces: `GmailSyncService.sync(now: datetime, dry_run: bool = False, force_backfill: bool = False) -> GmailSyncSummary`
- Produces: `build_backfill_query(now: datetime) -> str`
- Produces: `process_message(message: GmailMessage, dry_run: bool) -> GmailClassification`

- [ ] **Step 1: Write failing initial-sync/backfill tests**

```python
def test_first_sync_uses_profile_email_as_account_id(tmp_path): ...
def test_first_sync_scans_12_months_and_marks_backfill_complete(tmp_path): ...
def test_backfill_rerun_skips_processed_message_ids(tmp_path): ...
def test_backfill_error_prevents_completion_marker(tmp_path): ...
def test_message_arriving_during_backfill_is_not_skipped_by_saved_history_checkpoint(tmp_path): ...
```

`build_backfill_query()` computes the same calendar date one year earlier, clamping Feb 29 to Feb 28, and returns:

```text
after:YYYY/MM/DD {application interview recruiter hiring "job alert" position "technical assessment" "coding challenge" offer}
```

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_gmail_sync.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement sync-start checkpoint semantics**

Every sync begins with:

```python
account_id, checkpoint_history_id = gmail.get_profile()
state = store.get_gmail_sync_state(account_id)
```

For a first backfill, capture `checkpoint_history_id` **before** scanning. After a fully successful backfill, persist that captured history ID, not a newly fetched end-of-backfill ID. Therefore the next incremental history read includes any message changes that occurred while the backfill was running; already-backfilled messages are harmless because Gmail message IDs are idempotent.

`force_backfill=True` repeats the search but preserves existing events/candidates and still captures a fresh start checkpoint.

- [ ] **Step 4: Implement per-message processing with no-loss error handling**

For each message ID:

1. Increment `fetched`.
2. In non-dry-run, skip IDs already in `gmail_messages`.
3. Fetch/decode message.
4. Classify.
5. In dry-run, log classification/extraction and perform no write.
6. For `JOB_ALERT`, stage extracted jobs when eligible under Step 5.
7. For concrete recruiter outreach, stage extracted jobs and continue lifecycle handling.
8. For lifecycle/recruiter events, run `match_job()`.
9. If lifecycle matching is unresolved/ambiguous, persist `REVIEW_NEEDED` instead of the original lifecycle event.
10. If resolved and confidence `>= 0.90`, persist the lifecycle event.
11. Persist minimal `gmail_messages` metadata only after related staging/event writes succeed.
12. Increment summary counters.

Catch hard failures per message, increment `errors`, and continue processing remaining IDs for observability. Track `had_hard_errors=True`; if any hard error occurred in the backfill/history batch, do **not** mark backfill complete or advance history state. Next run replays successful IDs idempotently and retries failures.

Malformed/uncertain Gemini output that is successfully converted to `REVIEW_NEEDED` is not a hard processing failure.

- [ ] **Step 5: Add stale-alert protection for the historical backfill**

The backfill reconstructs lifecycle history for 12 months, but current discovery must not be flooded with old alert jobs. During backfill, stage `JOB_ALERT` jobs only when `now - message.sent_at <= 14 days`. Older alert messages are still classified/recorded as processed. Concrete recruiter outreach may stage an older role only when the message explicitly describes a real role; lifecycle events retain the full 12-month window.

Tests:

```python
def test_six_month_old_job_alert_is_not_staged(tmp_path): ...
def test_three_day_old_job_alert_is_staged(tmp_path): ...
```

- [ ] **Step 6: Write failing incremental/history tests**

```python
def test_second_sync_uses_saved_history_id(tmp_path): ...
def test_history_message_ids_are_idempotent(tmp_path): ...
def test_history_hard_error_does_not_advance_cursor(tmp_path): ...
def test_expired_history_uses_one_day_overlap_search(tmp_path): ...
def test_dry_run_writes_nothing_and_does_not_advance_state(tmp_path): ...
def test_force_backfill_is_idempotent_and_non_destructive(tmp_path): ...
```

- [ ] **Step 7: Implement incremental and expired-history recovery**

Normal sync uses `list_history(saved_history_id)` through all pages. If all message processing completes without hard errors, save the response's newest history ID and `last_successful_sync_at`.

For `GmailHistoryExpired`, reuse the sync-start `checkpoint_history_id`, search from one day before `last_successful_sync_at`, process idempotently, and only after success replace the stale cursor with that captured checkpoint. This prevents messages arriving during overlap recovery from being skipped.

Dry-run never calls `record_gmail_message`, `stage_inbound_job`, `save_application_event`, `mark_review_delivered`, or `save_gmail_sync_state`.

- [ ] **Step 8: Add compact metrics and commit**

Log exactly:

```text
gmail_fetched=<n> gmail_processed=<n> gmail_job_alerts=<n> gmail_application_events=<n> gmail_review_needed=<n> gmail_irrelevant=<n> gmail_errors=<n>
```

Run/commit:

```bash
pytest tests/test_gmail_sync.py -q
git add src/job_hunter/gmail_sync.py tests/test_gmail_sync.py
git commit -m "feat: sync Gmail job intelligence"
```

Expected: PASS.

---

### Task 7: Feed Staged Gmail Jobs Through Existing Discovery

**Files:**
- Create: `src/job_hunter/sources/gmail_staged.py`
- Modify: `src/job_hunter/sources/__init__.py`
- Modify: `src/job_hunter/pipeline.py`
- Test: `tests/test_gmail_staged_source.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `JobStore.list_unmaterialized_inbound_jobs()`
- Produces: `GmailStagedSource(store: JobStore).discover() -> list[Job]`
- Existing `collect_candidates()` remains the path that enriches/upserts/prefilters these jobs.

- [ ] **Step 1: Write failing staged-source tests**

```python
def test_staged_source_returns_stable_gmail_job_identity(tmp_path): ...
def test_same_canonical_url_already_materialized_by_public_source_is_not_emitted(tmp_path): ...
def test_same_identity_already_materialized_by_public_source_is_not_emitted(tmp_path): ...
```

For emitted rows assert:

```python
assert job.source == "gmail:linkedin"
assert job.source_job_id == row["source_candidate_key"]
```

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_gmail_staged_source.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement source conversion only**

```python
Job(
    source=f"gmail:{row['source_platform'] or 'unknown'}",
    source_job_id=row["source_candidate_key"],
    title=row["title"],
    company=row["company"],
    location=row["location"],
    url=row["url"],
    description=row["description"],
    remote=None if row["remote"] is None else bool(row["remote"]),
)
```

The source itself performs no HTTP calls and no store writes.

- [ ] **Step 4: Write failing pipeline integration test**

Stage one Gmail job, inject no public sources, and assert normal pipeline evaluation occurs. Add a second scenario where a richer public-source job with the same URL wins current in-run deduplication; after the run, the staging row must not be emitted on the next run because Task 3 materialization filtering recognizes the public-source job.

- [ ] **Step 5: Append Gmail source only after store creation**

```python
store = store or JobStore(settings.db_path)
base_sources = sources if sources is not None else build_sources(settings, http)
sources = [*base_sources, GmailStagedSource(store)]
```

Do not move it into `build_sources(settings, http)` because that function has no store.

- [ ] **Step 6: Run and commit**

```bash
pytest tests/test_gmail_staged_source.py tests/test_discovery.py tests/test_pipeline.py -q
git add src/job_hunter/sources/gmail_staged.py src/job_hunter/sources/__init__.py src/job_hunter/pipeline.py tests/test_gmail_staged_source.py tests/test_pipeline.py
git commit -m "feat: route Gmail jobs through discovery"
```

Expected: PASS.

---

### Task 8: Telegram Review Delivery for Ambiguous Gmail Signals

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/telegram.py`
- Modify: `src/job_hunter/pipeline.py`
- Test: `tests/test_telegram.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Produces: `ReviewItem(event_id, company, role_title, occurred_at, subject, rationale)`
- Produces: `build_gmail_review_digest(items: Sequence[ReviewItem]) -> str`
- Consumes: `pending_review_events()`/`mark_review_delivered()`

- [ ] **Step 1: Write failing review formatter tests**

Expected:

```text
Gmail review needed
- Acme — Frontend Engineer | ambiguous scheduling language
- Unknown company — Senior Engineer | could not match message to a job
```

Sort by `occurred_at`, then `event_id`; never render email body.

- [ ] **Step 2: Add `ReviewItem` and formatter**

```python
@dataclass(slots=True)
class ReviewItem:
    event_id: int
    company: str
    role_title: str
    occurred_at: str
    subject: str
    rationale: str
```

Formatter uses `company or "Unknown company"`, `role_title or "Unknown role"`, and rationale truncated to 200 chars.

- [ ] **Step 3: Write failing review retry tests**

Create pending review events and assert successful Telegram send marks all event IDs delivered; failed send leaves all pending. Assert this path is independent from normal scored-job digest selection.

- [ ] **Step 4: Deliver reviews at end of normal `run_pipeline`**

After normal message/document delivery, query pending reviews, build `ReviewItem`s, send one compact review digest, and call `mark_review_delivered()` only after a non-null Telegram message ID. Do not mix review events into `DigestItem` scoring.

- [ ] **Step 5: Run and commit**

```bash
pytest tests/test_telegram.py tests/test_pipeline.py -q
git add src/job_hunter/models.py src/job_hunter/telegram.py src/job_hunter/pipeline.py tests/test_telegram.py tests/test_pipeline.py
git commit -m "feat: surface Gmail review events"
```

Expected: PASS.

---

### Task 9: CLI, Fail-Open Workflow, and Operator Documentation

**Files:**
- Modify: `src/job_hunter/cli.py`
- Modify: `.github/workflows/daily.yml`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Gmail settings/auth/client/sync interfaces and existing `HttpClient`, `GeminiClient`, `JobStore`
- Produces: `python -m job_hunter sync-gmail [--dry-run] [--force-backfill]`

- [ ] **Step 1: Write failing CLI tests**

```python
def test_parser_accepts_sync_gmail_dry_run(): ...
def test_parser_accepts_sync_gmail_force_backfill(): ...
def test_sync_gmail_does_not_load_candidate_profile_settings(mocker): ...
def test_sync_gmail_returns_nonzero_on_fatal_auth_error(mocker): ...
def test_sync_gmail_returns_zero_when_service_completes_with_message_errors(mocker): ...
```

The last test prevents a few malformed/transient messages from generating a failed GitHub pipeline notification while the service has deliberately retained its cursor for retry.

- [ ] **Step 2: Run and verify failure**

```bash
pytest tests/test_cli.py -q
```

Expected: FAIL.

- [ ] **Step 3: Implement command and dependency construction**

```python
sync_parser = subparsers.add_parser("sync-gmail", help="Read Gmail job signals into shared state")
sync_parser.add_argument("--dry-run", action="store_true", help="Classify/extract without persisting Gmail-derived state")
sync_parser.add_argument("--force-backfill", action="store_true", help="Repeat the 12-month backfill idempotently")
```

Build Gmail dependencies only for `sync-gmail`. Fatal authorization/profile/list failures return 1. A completed sync whose summary has per-message `errors > 0` returns 0 after logging the errors because state was deliberately not advanced and those messages will retry.

- [ ] **Step 4: Add explicit fail-open workflow step**

Insert after state restore and before normal run:

```yaml
      - name: Sync Gmail intelligence
        continue-on-error: true
        env:
          GMAIL_CLIENT_ID: ${{ secrets.GMAIL_CLIENT_ID }}
          GMAIL_CLIENT_SECRET: ${{ secrets.GMAIL_CLIENT_SECRET }}
          GMAIL_REFRESH_TOKEN: ${{ secrets.GMAIL_REFRESH_TOKEN }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python -m job_hunter sync-gmail
```

Do not add Gmail secrets to the existing `Run job hunter` environment. Keep the shared SQLite restore/upload flow unchanged.

- [ ] **Step 5: Document secrets and exact operator commands**

Add to `.env.example` only empty names:

```text
GMAIL_CLIENT_ID=
GMAIL_CLIENT_SECRET=
GMAIL_REFRESH_TOKEN=
```

README bootstrap:

```bash
export GMAIL_CLIENT_ID='...'
export GMAIL_CLIENT_SECRET='...'
python scripts/gmail_oauth_bootstrap.py
```

Document storing the printed token as GitHub Secret `GMAIL_REFRESH_TOKEN`, then:

```bash
python -m job_hunter sync-gmail --dry-run
python -m job_hunter sync-gmail --force-backfill
```

State explicitly that dry-run advances no cursor and persists no Gmail-derived state; force-backfill is idempotent/non-destructive; full bodies are not stored; Gmail is read-only.

- [ ] **Step 6: Run focused/full tests and privacy scan**

```bash
pytest tests/test_cli.py -q
pytest -q
grep -RInE 'GMAIL_REFRESH_TOKEN=.+|client_secret[^A-Za-z].*[A-Za-z0-9_-]{20,}' . --exclude-dir=.git --exclude='*.md'
```

Expected: all tests PASS; grep finds no committed credential values.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/cli.py .github/workflows/daily.yml .env.example README.md tests/test_cli.py
git commit -m "feat: run Gmail intelligence before daily search"
```

---

### Task 10: Final Verification Against the Approved Spec

**Files:**
- Modify only if verification exposes a defect; do not add scope.

- [ ] **Step 1: Run full suite**

```bash
pytest -q
```

Expected: zero failures.

- [ ] **Step 2: Verify command surface**

```bash
python -m job_hunter --help
python -m job_hunter sync-gmail --help
```

Expected: `run` and `sync-gmail` listed; Gmail help includes `--dry-run` and `--force-backfill`.

- [ ] **Step 3: Verify privacy schema**

```bash
python - <<'PY'
from job_hunter.store import JobStore
from pathlib import Path
import sqlite3, tempfile
with tempfile.TemporaryDirectory() as d:
    path = Path(d) / "db.sqlite3"
    store = JobStore(path)
    store.close()
    conn = sqlite3.connect(path)
    for table in ("gmail_messages", "application_events", "inbound_job_candidates"):
        columns = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        assert "body" not in columns and "email_body" not in columns, (table, columns)
print("privacy schema check passed")
PY
```

Expected: `privacy schema check passed`.

- [ ] **Step 4: Verify workflow ordering/fail-open**

```bash
python - <<'PY'
from pathlib import Path
text = Path('.github/workflows/daily.yml').read_text()
assert 'Sync Gmail intelligence' in text
assert 'continue-on-error: true' in text
assert text.index('Sync Gmail intelligence') < text.index('Run job hunter')
print('workflow ordering check passed')
PY
```

Expected: `workflow ordering check passed`.

- [ ] **Step 5: Re-run critical Gmail integration tests**

```bash
pytest tests/test_gmail_sync.py tests/test_gmail_staged_source.py tests/test_pipeline.py tests/test_cli.py -q
```

Expected: PASS, including dry-run no-write, cursor no-loss, cross-source staging, and fail-open cases.

- [ ] **Step 6: Review implementation diff for scope discipline**

```bash
git diff main...HEAD --stat
git diff main...HEAD -- . ':!docs/superpowers/specs/*' ':!docs/superpowers/plans/*'
```

Expected: only Gmail intelligence, tests, dependencies, workflow wiring, and operator docs. No company-watchlist, Telegram-inbound, Relay, LinkedIn-browser, or application-submission code.

- [ ] **Step 7: Commit verification fixes only if files changed**

```bash
git status --short
```

If verification required fixes, stage only those files and commit:

```bash
git add <files-changed-by-verification>
git commit -m "fix: complete Gmail intelligence verification"
```

If `git status --short` is empty, do not create a commit.

# Telegram Job Navigator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the long Telegram digest with one interactive job card that supports in-place Previous/Next navigation, a job URL button, and a side-effect-free Apply placeholder while keeping the bot on its current SQLite + GitHub Actions artifact persistence model.

**Architecture:** The scheduled GitHub Actions pipeline continues to own discovery, evaluation, SQLite persistence, PDF creation, and Telegram delivery. It stores each ordered Telegram navigation session in the existing SQLite database, which is already uploaded as the `job-hunter-state` workflow artifact. A small webhook runtime receives Telegram callbacks, reads the latest SQLite state by downloading that artifact through the GitHub Actions REST API, then edits the existing Telegram message; there is no Supabase dependency or second database.

**Tech Stack:** Python 3.12, requests, SQLite, Telegram Bot API, GitHub Actions artifacts REST API, Flask + Gunicorn for the webhook runtime, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-telegram-job-navigator-design.md`

## Global Constraints

- Preserve the existing deliverability rule: score must be strictly greater than `60` and decision must be one of the currently deliverable decisions.
- Keep ready-to-apply PDF delivery unchanged.
- Do not implement application submission; `Apply` only answers the callback with `Apply functionality coming soon.`
- Previous/Next edit the same Telegram message; never send a new card per click.
- Navigation does not wrap at either edge.
- Telegram callback data must remain below the Bot API 64-byte limit.
- Keep navigation sessions in `var/job_hunter.sqlite3`; do not add Supabase or another database.
- Reuse the existing `job-hunter-state` GitHub Actions artifact as durable cross-runtime state.
- The webhook runtime accesses the private repository with a fine-grained token restricted to the repository and `Actions: read`.
- Telegram and GitHub tokens must never appear in callback data, logs, URLs, or browser code.
- Validate `X-Telegram-Bot-Api-Secret-Token` against `TELEGRAM_WEBHOOK_SECRET` using `hmac.compare_digest` before processing webhook updates.
- The webhook runtime must not require Gemini, candidate-profile, or cover-letter secrets.

---

## File Structure

### Existing files to modify

- `src/job_hunter/models.py` — add `location` to `DigestItem`; add navigation card/session dataclasses.
- `src/job_hunter/store.py` — add SQLite navigation-session schema and CRUD/pruning methods.
- `src/job_hunter/telegram.py` — add card send/edit/callback-answer methods while keeping document delivery and the no-match text message.
- `src/job_hunter/pipeline.py` — create a session, send one first card, attach its message ID, mark represented jobs delivered only after success, preserve PDF delivery.
- `src/job_hunter/http.py` — no new HTTP verbs required for the corrected design; keep existing GET/POST behavior.
- `src/job_hunter/config.py` — add a minimal webhook-settings loader that is independent from Gemini/profile settings.
- `scripts/restore_state.py` — delegate artifact download/extraction to the reusable module created in Task 3.
- `pyproject.toml` — add a `webhook` optional dependency group for Flask and Gunicorn.
- `.env.example` — document webhook-only environment variables.
- `README.md` — document navigator behavior, artifact-backed state, webhook deployment, webhook registration, and required secrets.
- `tests/test_telegram.py` — Telegram API payload coverage.
- `tests/test_pipeline.py` — navigator delivery behavior.
- `tests/test_store.py` — navigation-session persistence coverage.
- `tests/test_config.py` — webhook-settings coverage.

### New files

- `src/job_hunter/telegram_navigation.py` — pure card rendering, callback encoding/parsing, callback orchestration.
- `src/job_hunter/github_state.py` — reusable latest-artifact lookup, download, safe extraction, and local cache.
- `src/job_hunter/telegram_webhook.py` — Flask app factory and authenticated Telegram webhook route.
- `tests/test_telegram_navigation.py` — card, keyboard, callback parsing, callback-handler tests.
- `tests/test_github_state.py` — artifact listing/download/cache tests.
- `tests/test_telegram_webhook.py` — webhook authentication/routing tests.
- `scripts/set_telegram_webhook.py` — idempotent Telegram `setWebhook` helper.
- `Dockerfile.telegram-webhook` — provider-neutral container for the always-available webhook runtime.

---

### Task 1: Navigation data model, callback format, and job-card rendering

**Files:**
- Modify: `src/job_hunter/models.py`
- Create: `src/job_hunter/telegram_navigation.py`
- Create: `tests/test_telegram_navigation.py`
- Modify: `tests/test_telegram.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Produces `NavigationCard`, `NavigationSession`, `build_navigation_card()`, `encode_callback()`, `parse_callback()`, `navigation_sort_key()`.
- Consumes existing `DigestItem` and current Telegram deliverability semantics.

- [ ] **Step 1: Write failing formatter and callback tests**

Add these concrete cases to `tests/test_telegram_navigation.py`:

```python
from job_hunter.models import NavigationCard
from job_hunter.telegram_navigation import build_navigation_card, encode_callback, parse_callback


def _card(**overrides):
    data = dict(
        job_id=7,
        title="Senior Frontend Developer",
        company="Example GmbH",
        location="Berlin",
        score=87,
        url="https://example.test/jobs/7",
    )
    data.update(overrides)
    return NavigationCard(**data)


def test_build_navigation_card_middle_item():
    text, keyboard = build_navigation_card(_card(), "abc123", index=2, total=12)
    assert text == (
        "Senior Frontend Developer\n\n"
        "Company: Example GmbH\n"
        "Location: Berlin\n"
        "Match: 87%"
    )
    assert keyboard[0][0] == {"text": "View job", "url": "https://example.test/jobs/7"}
    assert keyboard[0][1]["text"] == "Apply"
    assert keyboard[1][0]["text"] == "◀ Previous"
    assert keyboard[1][1]["text"] == "3 / 12"
    assert keyboard[1][2]["text"] == "Next ▶"


def test_callback_round_trip():
    payload = encode_callback("n", "abc123", 11)
    assert parse_callback(payload) == ("n", "abc123", 11)
    assert len(payload.encode("utf-8")) <= 64
```

Also cover first-item and last-item no-wrap buttons, missing URL, missing company/location fallbacks, malformed callback strings, negative indices, unknown actions, and an oversized session ID raising `ValueError`.

- [ ] **Step 2: Run the new tests and verify failure**

```bash
pytest tests/test_telegram_navigation.py -v
```

Expected: import/collection failure because navigation types/functions do not exist yet.

- [ ] **Step 3: Add navigation dataclasses and location propagation shape**

In `src/job_hunter/models.py` add `location: str` to `DigestItem` and add:

```python
@dataclass(slots=True, frozen=True)
class NavigationCard:
    job_id: int
    title: str
    company: str
    location: str
    score: int
    url: str


@dataclass(slots=True, frozen=True)
class NavigationSession:
    session_id: str
    cards: list[NavigationCard]
    telegram_message_id: str | None
    created_at: str
    expires_at: str
```

Update every current `DigestItem(...)` construction in tests with a concrete `location` value.

- [ ] **Step 4: Implement compact callbacks and deterministic ordering**

In `src/job_hunter/telegram_navigation.py` implement:

```python
_CALLBACK_ACTIONS = {"n", "a", "x"}


def encode_callback(action: str, session_id: str, index: int) -> str:
    if action not in _CALLBACK_ACTIONS or index < 0 or not session_id:
        raise ValueError("invalid callback payload")
    payload = f"{action}|{session_id}|{index}"
    if len(payload.encode("utf-8")) > 64:
        raise ValueError("callback payload exceeds Telegram limit")
    return payload


def parse_callback(data: str) -> tuple[str, str, int] | None:
    parts = data.split("|")
    if len(parts) != 3 or parts[0] not in _CALLBACK_ACTIONS or not parts[1]:
        return None
    try:
        index = int(parts[2])
    except ValueError:
        return None
    if index < 0:
        return None
    return parts[0], parts[1], index


def navigation_sort_key(item: DigestItem) -> tuple[int, str, str, int]:
    return (-item.score, (item.company or "").lower(), (item.title or "").lower(), item.job_id)
```

- [ ] **Step 5: Implement pure card rendering**

Implement:

```python
def build_navigation_card(
    card: NavigationCard,
    session_id: str,
    index: int,
    total: int,
) -> tuple[str, list[list[dict[str, str]]]]:
    ...
```

The text must use:

```python
text = (
    f"{card.title}\n\n"
    f"Company: {card.company or 'Not specified'}\n"
    f"Location: {card.location or 'Not specified'}\n"
    f"Match: {card.score}%"
)
```

Keyboard row 1 contains `View job` only when `card.url` is non-empty, followed by `Apply`. Row 2 contains Previous, indicator, Next. Previous/Next use action `n` with their target index when available; unavailable edge buttons and the indicator use action `x`.

- [ ] **Step 6: Run focused tests**

```bash
pytest tests/test_telegram_navigation.py tests/test_telegram.py tests/test_pipeline.py -v
```

Expected: navigation formatter tests pass; existing tests compile with the enriched `DigestItem`.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/telegram_navigation.py tests/test_telegram_navigation.py tests/test_telegram.py tests/test_pipeline.py
git commit -m "feat: add Telegram job-card navigation model"
```

---

### Task 2: Persist navigation sessions inside the existing SQLite database

**Files:**
- Modify: `src/job_hunter/store.py`
- Modify: `tests/test_store.py`

**Interfaces:**
- Consumes `NavigationSession` and `NavigationCard` from Task 1.
- Produces `JobStore.create_navigation_session()`, `attach_navigation_message_id()`, `get_navigation_session()`, and `prune_navigation_sessions()`.

- [ ] **Step 1: Write failing SQLite persistence tests**

Add tests equivalent to:

```python
def test_navigation_session_round_trip(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    session = NavigationSession(
        session_id="session-1",
        cards=[NavigationCard(1, "Senior FE", "Acme", "Berlin", 91, "https://example.test/1")],
        telegram_message_id=None,
        created_at="2026-08-31T12:00:00+00:00",
        expires_at="2026-09-30T12:00:00+00:00",
    )
    store.create_navigation_session(session)
    store.attach_navigation_message_id("session-1", "42")
    loaded = store.get_navigation_session("session-1")
    assert loaded is not None
    assert loaded.telegram_message_id == "42"
    assert loaded.cards[0].location == "Berlin"


def test_prune_navigation_sessions_deletes_expired_only(tmp_path):
    ...
```

For the prune test, create one session expiring before the supplied `now` and one after it; assert only the expired session is removed.

- [ ] **Step 2: Run store tests and verify failure**

```bash
pytest tests/test_store.py -v
```

Expected: FAIL because the navigation table/methods do not exist.

- [ ] **Step 3: Add the SQLite table**

Add schema initialization:

```sql
CREATE TABLE IF NOT EXISTS telegram_navigation_sessions (
    session_id          TEXT PRIMARY KEY,
    cards_json          TEXT NOT NULL,
    telegram_message_id TEXT,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL
)
```

No migration framework is needed because `JobStore._init_db()` already creates missing tables safely on restored databases.

- [ ] **Step 4: Implement JSON serialization and CRUD**

Use `dataclasses.asdict()` for cards and `json.dumps()`/`json.loads()` for `cards_json`. `get_navigation_session()` must return `None` when the session is missing and reconstruct `NavigationCard` objects when present.

`prune_navigation_sessions(now_iso: str) -> int` executes:

```sql
DELETE FROM telegram_navigation_sessions WHERE expires_at < ?
```

and returns the deleted row count.

- [ ] **Step 5: Run store tests**

```bash
pytest tests/test_store.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/job_hunter/store.py tests/test_store.py
git commit -m "feat: persist Telegram navigation sessions in SQLite"
```

---

### Task 3: Reuse GitHub Actions artifacts as webhook-readable state

**Files:**
- Create: `src/job_hunter/github_state.py`
- Create: `tests/test_github_state.py`
- Modify: `scripts/restore_state.py`
- Modify: tests for `scripts/restore_state.py` if present.

**Interfaces:**
- Produces:

```python
@dataclass(slots=True, frozen=True)
class ArtifactStateSnapshot:
    artifact_id: int
    path: Path
    created_at: str


class GitHubArtifactStateLoader:
    def __init__(self, repo: str, token: str, artifact_name: str, cache_dir: Path, http=None): ...
    def load_latest(self) -> ArtifactStateSnapshot | None: ...
```

- The existing restore script reuses the same artifact-list/download/safe-extraction helpers.

- [ ] **Step 1: Write failing artifact loader tests**

Use fake HTTP responses to cover these cases:

```python
def test_load_latest_downloads_newest_named_nonexpired_artifact(tmp_path):
    # Fake artifact list contains older, newer, wrong-name, and expired entries.
    # Assert newest valid job-hunter-state artifact is downloaded and extracted.
    ...


def test_load_latest_reuses_cached_file_when_artifact_id_is_unchanged(tmp_path):
    # Call twice with the same artifact id and assert archive download happens once.
    ...


def test_load_latest_rejects_unsafe_zip_member(tmp_path):
    # ZIP contains ../job_hunter.sqlite3 and must raise ValueError.
    ...
```

Build test ZIP bytes with Python `zipfile.ZipFile(io.BytesIO(), "w")`; do not use real GitHub calls in unit tests.

- [ ] **Step 2: Run the tests and verify failure**

```bash
pytest tests/test_github_state.py -v
```

Expected: import failure because `github_state.py` does not exist.

- [ ] **Step 3: Implement latest-artifact listing**

Call:

```text
GET https://api.github.com/repos/{owner}/{repo}/actions/artifacts?name=job-hunter-state&per_page=30
```

with:

```python
headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/vnd.github+json",
}
```

Filter out expired artifacts, choose the newest by `created_at`, and return `None` when no valid artifact exists.

- [ ] **Step 4: Implement download, safe extraction, and cache reuse**

Download `archive_download_url`, follow redirects through the HTTP client, find only the member whose basename is `job_hunter.sqlite3`, reject absolute or `..` paths, and write the database to:

```text
<cache_dir>/artifact-<artifact_id>/job_hunter.sqlite3
```

If that exact path already exists for the newest artifact ID, skip the archive download.

- [ ] **Step 5: Refactor `scripts/restore_state.py` to share the extraction code**

Keep its CLI unchanged:

```bash
python scripts/restore_state.py --repo "$GH_REPO" --token "$GH_TOKEN" --dest var/job_hunter.sqlite3
```

The script should use the shared list/download/extraction helper, then copy the loaded database to `--dest`. Existing first-run behavior remains non-fatal when no artifact exists.

- [ ] **Step 6: Run focused tests**

```bash
pytest tests/test_github_state.py tests/test_restore_state.py -v
```

If the restore-state tests use a different filename, run that existing module instead. Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/github_state.py scripts/restore_state.py tests/test_github_state.py tests/test_restore_state.py
git commit -m "refactor: reuse GitHub artifact state loader"
```

If `tests/test_restore_state.py` does not exist, omit that path from `git add`; do not invent an empty file solely for the command.

---

### Task 4: Extend Telegram API support and implement callback handling

**Files:**
- Modify: `src/job_hunter/telegram.py`
- Modify: `src/job_hunter/telegram_navigation.py`
- Modify: `tests/test_telegram.py`
- Modify: `tests/test_telegram_navigation.py`

**Interfaces:**
- Produces `TelegramClient.send_job_card()`, `edit_job_card()`, `answer_callback()`.
- Produces `handle_callback_query(callback_query, session_loader, telegram) -> bool`.
- `session_loader(session_id)` returns `NavigationSession | None` and is supplied by the webhook runtime.

- [ ] **Step 1: Write failing Telegram payload tests**

Add:

```python
def test_send_job_card_posts_inline_keyboard():
    result = client.send_job_card("card text", [[{"text": "Apply", "callback_data": "a|s|0"}]])
    url, kwargs = http.calls[0]
    assert url.endswith("/sendMessage")
    assert kwargs["json"]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Apply", "callback_data": "a|s|0"}]]
    }
    assert result == "42"


def test_edit_job_card_edits_same_message():
    assert client.edit_job_card(
        chat_id="123",
        message_id="42",
        text="next card",
        keyboard=[[{"text": "2 / 3", "callback_data": "x|s|1"}]],
    ) is True
    url, kwargs = http.calls[0]
    assert url.endswith("/editMessageText")
    assert kwargs["json"]["chat_id"] == "123"
    assert kwargs["json"]["message_id"] == "42"


def test_answer_callback_query():
    assert client.answer_callback("cb-1", text="Apply functionality coming soon.") is True
    assert http.calls[0][0].endswith("/answerCallbackQuery")
```

- [ ] **Step 2: Run Telegram tests and verify failure**

```bash
pytest tests/test_telegram.py -v
```

Expected: FAIL because the three methods do not exist.

- [ ] **Step 3: Add Telegram client methods**

`send_job_card()` posts `sendMessage` with `reply_markup.inline_keyboard`. `edit_job_card()` posts `editMessageText`. `answer_callback()` posts `answerCallbackQuery` and supports optional `text` and `show_alert=False`.

Keep `send_message()` for `No matching jobs today.` and keep `send_document()` unchanged.

- [ ] **Step 4: Write failing callback-orchestration tests**

Test these behaviors with an injected `session_loader` function and fake Telegram client:

```python
def test_next_callback_edits_same_message():
    handled = handle_callback_query(
        callback_query={
            "id": "cb-1",
            "data": "n|session1|1",
            "message": {"message_id": 99, "chat": {"id": 123}},
        },
        session_loader=lambda _sid: session,
        telegram=telegram,
    )
    assert handled is True
    assert telegram.edits[0][0:2] == ("123", "99")
    assert "Company: Beta" in telegram.edits[0][2]


def test_apply_callback_only_answers():
    handle_callback_query(apply_query, session_loader=lambda _sid: session, telegram=telegram)
    assert telegram.edits == []
    assert telegram.answers[-1] == ("cb-apply", "Apply functionality coming soon.")
```

Also cover no-op, malformed data, missing session, expired session, target index outside bounds, and a stored Telegram message-ID mismatch.

- [ ] **Step 5: Implement `handle_callback_query()`**

Use this control flow:

1. Extract callback ID and data.
2. Parse with `parse_callback()`; malformed => answer `This action is no longer available.` and return `False`.
3. `a` => answer `Apply functionality coming soon.` and return `True` without editing.
4. `x` => answer with no text and return `True`.
5. `n` => load the session using `session_loader(session_id)`.
6. Missing/expired session => answer `This job list has expired.`.
7. Validate index and, when stored, Telegram message ID.
8. Build the target card and call `edit_job_card()` using the callback message's chat/message IDs.
9. Always answer the callback after handling; if the edit fails answer `Could not update this job right now.`.

- [ ] **Step 6: Run Telegram/navigation tests**

```bash
pytest tests/test_telegram.py tests/test_telegram_navigation.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/job_hunter/telegram.py src/job_hunter/telegram_navigation.py tests/test_telegram.py tests/test_telegram_navigation.py
git commit -m "feat: handle Telegram navigation callbacks"
```

---

### Task 5: Replace digest delivery with one persisted navigator session

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Interfaces:**
- Consumes `NavigationCard`, `NavigationSession`, `navigation_sort_key()`, `build_navigation_card()`, and `JobStore` session methods.
- Produces the pipeline behavior that sends exactly one navigator message for a non-empty deliverable batch.

- [ ] **Step 1: Write failing pipeline tests**

Update `FakeTelegram` with `send_job_card()` and assert:

```python
def test_pipeline_sends_one_navigator_card_for_multiple_jobs(settings):
    ...
    assert len(telegram.job_cards) == 1
    text, keyboard = telegram.job_cards[0]
    assert "Match: 90%" in text
    assert keyboard[1][1]["text"] == "1 / 2"
    assert len(telegram.documents) == 2
```

Add tests for:

- deterministic score/company/title/job-ID order in the stored session
- `location` copied from `Job` into the navigation snapshot
- no session when there are no deliverable jobs
- all represented jobs marked `telegram_message` delivered only after initial card send succeeds
- failed initial card send leaves all represented jobs retryable
- existing ready-to-apply PDF delivery still occurs

- [ ] **Step 2: Run the pipeline tests and verify failure**

```bash
pytest tests/test_pipeline.py -v
```

Expected: FAIL because the pipeline still builds the long digest.

- [ ] **Step 3: Populate `location` in both DigestItem construction paths**

When creating a `DigestItem` after evaluation and inside `_requeue_pending_delivery()`, set:

```python
location=job.location,
```

- [ ] **Step 4: Create and persist a navigation snapshot**

After `select_deliverable_items()`:

```python
ordered = sorted(deliverable_items, key=navigation_sort_key)
cards = [
    NavigationCard(
        job_id=item.job_id,
        title=item.title,
        company=item.company,
        location=item.location,
        score=item.score,
        url=item.url,
    )
    for item in ordered
]
```

Generate the opaque session ID with:

```python
session_id = secrets.token_urlsafe(12)
```

Use UTC timestamps and a 30-day expiry. Persist the session before sending the card so a successful card always has a matching SQLite session in the database that will be uploaded.

- [ ] **Step 5: Send one first card and attach its Telegram message ID**

Build index `0`, call `telegram.send_job_card(text, keyboard)`, then when it succeeds:

```python
store.attach_navigation_message_id(session_id, message_id)
for item in ordered:
    store.mark_delivered(item.job_id, "telegram_message", message_id)
```

When it fails, do not mark those jobs delivered.

- [ ] **Step 6: Preserve PDF behavior and minimize artifact-sync race**

Deliver ready-to-apply PDFs before the final navigator card send, or otherwise structure the end of the pipeline so the card send is the last network delivery before `run_pipeline()` returns. This lets `.github/workflows/daily.yml` reach its existing artifact-upload step immediately after the card appears.

Do not remove PDF generation/delivery retry logic.

- [ ] **Step 7: Keep the no-match path**

If no deliverable items exist, retain:

```python
telegram.send_message("No matching jobs today.")
```

and do not create a navigation session.

- [ ] **Step 8: Run pipeline and store tests**

```bash
pytest tests/test_pipeline.py tests/test_store.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/job_hunter/pipeline.py tests/test_pipeline.py
git commit -m "feat: deliver Telegram jobs as one navigator card"
```

---

### Task 6: Add the authenticated webhook runtime backed by the latest artifact

**Files:**
- Modify: `src/job_hunter/config.py`
- Modify: `tests/test_config.py`
- Modify: `pyproject.toml`
- Create: `src/job_hunter/telegram_webhook.py`
- Create: `tests/test_telegram_webhook.py`
- Create: `Dockerfile.telegram-webhook`

**Interfaces:**
- Produces `WebhookSettings` and `load_webhook_settings()`.
- Produces Flask `create_app(settings=None, state_loader=None, telegram=None)`.
- Consumes `GitHubArtifactStateLoader`, `JobStore`, and `handle_callback_query()`.

- [ ] **Step 1: Write failing webhook-settings tests**

Add a minimal settings loader that requires only:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_SECRET
GITHUB_REPOSITORY
GITHUB_STATE_TOKEN
```

and defaults:

```text
GITHUB_STATE_ARTIFACT=job-hunter-state
JOB_HUNTER_WEBHOOK_CACHE_DIR=/tmp/job-hunter-state
```

Test that it does not require `GEMINI_API_KEY`, `CANDIDATE_PROFILE_B64`, or `COVER_LETTER_TEMPLATE_B64`.

- [ ] **Step 2: Write failing Flask route tests**

Cover:

```python
def test_webhook_rejects_wrong_secret(client):
    response = client.post("/telegram/webhook", json={"update_id": 1})
    assert response.status_code == 401


def test_webhook_handles_navigation_callback(client, state_loader, telegram):
    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json={"callback_query": callback_query},
    )
    assert response.status_code == 200
    assert telegram.edits
```

Also test health endpoint, update without `callback_query`, artifact API failure, session missing from newest artifact, and invalid JSON.

- [ ] **Step 3: Add webhook optional dependencies**

In `pyproject.toml` add:

```toml
[project.optional-dependencies]
test = [
    "pytest>=8.0",
    "pytest-mock>=3.12",
]
webhook = [
    "Flask>=3.1,<4",
    "gunicorn>=23,<24",
]
```

Do not add Flask to the default daily-run dependencies.

- [ ] **Step 4: Implement webhook settings**

Add:

```python
@dataclass(slots=True, frozen=True)
class WebhookSettings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    github_repository: str
    github_state_token: str
    github_state_artifact: str = "job-hunter-state"
    cache_dir: str = "/tmp/job-hunter-state"
```

`load_webhook_settings()` reads only the webhook-specific environment variables listed above.

- [ ] **Step 5: Implement `create_app()`**

Routes:

```text
GET /health           -> 200 {"ok": true}
POST /telegram/webhook
```

For `/telegram/webhook`:

1. Read `X-Telegram-Bot-Api-Secret-Token`.
2. Compare with configured secret using `hmac.compare_digest`; mismatch => HTTP 401.
3. Parse JSON; invalid JSON => HTTP 400.
4. If no `callback_query`, return HTTP 200.
5. `state_loader.load_latest()` obtains the newest cached/extracted SQLite snapshot.
6. Open it with `JobStore` in read-only mode. Add a `read_only=True` constructor option if needed so webhook requests cannot mutate the artifact copy.
7. Resolve the requested navigation session and call `handle_callback_query()`.
8. Return HTTP 200 after Telegram callback handling.

When artifact loading fails, answer the Telegram callback with `Could not load this job list right now.` and still return HTTP 200 so Telegram does not repeatedly retry a successfully received update.

When the requested session is absent from the newest artifact, answer `Job list is still syncing. Try again shortly.`. The next button press will re-check the newest artifact ID.

- [ ] **Step 6: Add the production container**

`Dockerfile.telegram-webhook`:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir -e '.[webhook]'
ENV PORT=8080
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} 'job_hunter.telegram_webhook:create_app()'"]
```

If editable installation requires additional packaging metadata already present in the repository, copy only those existing files needed by `pip install -e '.[webhook]'`; do not copy secrets or `var/`.

- [ ] **Step 7: Run webhook/config tests**

```bash
pip install -e '.[test,webhook]'
pytest tests/test_config.py tests/test_github_state.py tests/test_telegram_webhook.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/job_hunter/config.py src/job_hunter/telegram_webhook.py pyproject.toml Dockerfile.telegram-webhook tests/test_config.py tests/test_telegram_webhook.py
git commit -m "feat: add Telegram navigator webhook runtime"
```

---

### Task 7: Add webhook registration, documentation, and full verification

**Files:**
- Create: `scripts/set_telegram_webhook.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: tests if the registration helper has a test module.

**Interfaces:**
- Produces an idempotent command that calls Telegram `setWebhook` with the public endpoint and secret token.

- [ ] **Step 1: Add the webhook registration helper**

The script reads:

```text
TELEGRAM_BOT_TOKEN
TELEGRAM_WEBHOOK_URL
TELEGRAM_WEBHOOK_SECRET
```

and posts:

```python
requests.post(
    f"https://api.telegram.org/bot{token}/setWebhook",
    json={
        "url": webhook_url.rstrip("/") + "/telegram/webhook",
        "secret_token": webhook_secret,
        "allowed_updates": ["callback_query"],
    },
    timeout=(5, 25),
)
```

Exit non-zero when Telegram returns HTTP >= 400 or JSON `ok` is not true. Never print the bot token or webhook secret.

- [ ] **Step 2: Update `.env.example`**

Keep existing bot variables and add a separate documented webhook section:

```dotenv
# Telegram navigator webhook runtime
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_WEBHOOK_URL=
GITHUB_REPOSITORY=amitbaz/job-hunter-bot
GITHUB_STATE_TOKEN=
GITHUB_STATE_ARTIFACT=job-hunter-state
JOB_HUNTER_WEBHOOK_CACHE_DIR=/tmp/job-hunter-state
```

Do not add any Supabase variables.

- [ ] **Step 3: Update README architecture and setup docs**

Document these exact facts:

- Daily job discovery/evaluation still runs in GitHub Actions.
- SQLite remains the source of truth and is still persisted as `job-hunter-state`.
- The navigator session is stored inside that same SQLite database.
- A small public webhook service is required because GitHub Actions cannot receive Telegram clicks after a run ends.
- The webhook downloads the newest state artifact and reads SQLite; it does not use Supabase.
- `GITHUB_STATE_TOKEN` should be a fine-grained token for this private repository with `Actions: read` only.
- `TELEGRAM_WEBHOOK_SECRET` protects the inbound route.
- `Apply` remains a placeholder and performs no application action.

Include local webhook test commands:

```bash
pip install -e '.[test,webhook]'
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_WEBHOOK_SECRET=...
export GITHUB_REPOSITORY=amitbaz/job-hunter-bot
export GITHUB_STATE_TOKEN=...
gunicorn --bind 127.0.0.1:8080 'job_hunter.telegram_webhook:create_app()'
```

and registration:

```bash
export TELEGRAM_WEBHOOK_URL=https://your-public-webhook.example
python scripts/set_telegram_webhook.py
```

- [ ] **Step 4: Run the complete test suite**

```bash
pip install -e '.[test,webhook]'
pytest -v
```

Expected: all tests PASS.

- [ ] **Step 5: Verify dry-run behavior remains side-effect free**

```bash
JOB_HUNTER_DRY_RUN=1 python -m job_hunter run
```

Expected: discovery/evaluation can run with existing required dry-run secrets, no Telegram message/document is sent, and no webhook runtime is started.

- [ ] **Step 6: Verify the branch contains no Supabase integration**

```bash
grep -Rni "supabase" src tests scripts .github pyproject.toml .env.example README.md || true
```

Expected: no Supabase references introduced by this feature.

- [ ] **Step 7: Commit**

```bash
git add scripts/set_telegram_webhook.py .env.example README.md
git commit -m "docs: document Telegram navigator webhook setup"
```

---

## Final Verification Checklist

- [ ] `pytest -v` passes with `.[test,webhook]` installed.
- [ ] Existing discovery/ranking/evaluation tests remain unchanged in behavior.
- [ ] `DigestItem.location` is populated for normal and retry delivery.
- [ ] Multiple deliverable jobs generate exactly one initial navigator message.
- [ ] `Previous`/`Next` use `editMessageText` and never create a second navigator message.
- [ ] First/last navigation does not wrap.
- [ ] `View job` uses the posting URL.
- [ ] `Apply` only answers `Apply functionality coming soon.`.
- [ ] Session snapshots are stored in the existing SQLite DB.
- [ ] Existing `job-hunter-state` artifact contains the navigation session after the daily run.
- [ ] Webhook reads the newest artifact with GitHub `Actions: read` credentials and opens SQLite read-only.
- [ ] Artifact-sync race returns a harmless retry message.
- [ ] Existing ready-to-apply PDFs still deliver/retry as before.
- [ ] Webhook rejects an invalid Telegram secret header.
- [ ] No Supabase dependency, schema, secret, or migration exists in the feature.

## Execution Handoff

Implementation belongs in Codex/Claude Code. Execute this plan task-by-task using Superpowers subagent-driven development or executing-plans, with TDD and the commits specified above. Do not merge the branch until the full verification checklist passes.
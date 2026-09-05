# Stale ATS Board Hygiene Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop learned ATS boards that return a permanent-looking 404 from spamming duplicate full tracebacks, and make repeated 404s eventually pause/deactivate the board via the existing registry health model, while leaving transient-failure and success behavior unchanged.

**Architecture:** Add a small `is_stale_board_error(exc)` classifier (in `sources/base.py`, backed by `requests.HTTPError.response.status_code == 404`) used in two places: (1) the three ATS adapters (`greenhouse.py`, `lever.py`, `ashby.py`) log a compact one-liner for a 404 instead of a full traceback, keeping `exc_info=True` only for genuinely unexpected errors; (2) `LearnedAtsSource.discover()` classifies the re-raised error the same way, logs a compact summary (never a second full traceback), and passes `permanent=<is 404>` into a new `JobStore.record_ats_scan_failure(..., permanent=...)` parameter. That parameter drives an escalation: permanent failures still pause the board 24h like today, but after 3 consecutive permanent failures the board is deactivated (`active=0`) instead of being paused forever — it stays in the registry (history preserved) and reactivates automatically the next time `upsert_ats_board` rediscovers it, exactly like the existing rediscovery path already does for any inactive board.

**Tech Stack:** Python 3.12, pytest, sqlite3 (via `job_hunter.store.JobStore`), `requests`.

---

## Reference: issue

GitHub issue #36. Full text already gathered in-session; do not re-fetch. Key acceptance criteria this plan must satisfy:

- A learned Lever/Greenhouse board returning 404 does not produce duplicate full tracebacks.
- Repeated stale-board failures cause the board to be paused/de-prioritized according to a documented bounded policy.
- Transient failures are not treated identically to permanent-looking 404 failures.
- Successful boards continue to be scanned and contribute jobs normally.
- A failing board remains isolated from all other board scans.
- ATS registry health fields remain internally consistent after success/failure transitions.
- Aggregate ATS telemetry remains available and useful.
- Tests cover stale 404 behavior, repeated failures, recovery/success, and per-board isolation.
- Full test suite passes.

Scope is bounded to the files the issue names: `src/job_hunter/sources/learned_ats.py`, `greenhouse.py`, `lever.py`, `ashby.py`, `src/job_hunter/ats_registry.py` (inspected, no change needed — see Task 5 note), `src/job_hunter/store.py`. Do **not** touch `src/job_hunter/sources/company_watch.py` even though it shares the same adapters and has a similar duplicate-log shape — that is explicitly out of scope for issue #36.

---

### Task 1: Add the stale-board (404) classifier

**Files:**
- Modify: `src/job_hunter/sources/base.py`
- Test: `tests/test_ats_adapters.py` (new file)

**Step 1: Write the failing test**

Create `tests/test_ats_adapters.py`:

```python
import requests

from job_hunter.sources.base import is_stale_board_error


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _http_error(status_code: int) -> requests.HTTPError:
    return requests.HTTPError(f"status {status_code}", response=_Response(status_code))


def test_is_stale_board_error_true_for_404():
    assert is_stale_board_error(_http_error(404)) is True


def test_is_stale_board_error_false_for_other_status_codes():
    assert is_stale_board_error(_http_error(500)) is False
    assert is_stale_board_error(_http_error(429)) is False


def test_is_stale_board_error_false_for_non_http_errors():
    assert is_stale_board_error(RuntimeError("network down")) is False
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_ats_adapters.py -q`
Expected: FAIL with `ImportError: cannot import name 'is_stale_board_error'`

**Step 3: Write minimal implementation**

In `src/job_hunter/sources/base.py`, add near the top (after the existing imports) and after `strip_html`:

```python
import requests
```

(add to the existing import block, keeping `bs4` import order as-is)

Then append this function to the file:

```python
def is_stale_board_error(exc: Exception) -> bool:
    """Return whether `exc` looks like a permanent 404 for a stale ATS board.

    A 404 from Lever/Greenhouse/Ashby means the board identifier no longer
    exists (renamed or removed company). That is expected registry noise,
    not a bug worth a full traceback.
    """
    return isinstance(exc, requests.HTTPError) and getattr(
        exc.response, "status_code", None
    ) == 404
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_ats_adapters.py -q`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add src/job_hunter/sources/base.py tests/test_ats_adapters.py
git commit -m "feat: add stale ATS board 404 classifier"
```

---

### Task 2: Adapters log a compact line for 404s, not a full traceback

**Files:**
- Modify: `src/job_hunter/sources/greenhouse.py`
- Modify: `src/job_hunter/sources/lever.py`
- Modify: `src/job_hunter/sources/ashby.py`
- Test: `tests/test_ats_adapters.py`

**Step 1: Write the failing tests**

Append to `tests/test_ats_adapters.py`:

```python
import logging

from job_hunter.sources.ashby import AshbySource
from job_hunter.sources.greenhouse import GreenhouseSource
from job_hunter.sources.lever import LeverSource


class _RaisingHttp:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_json(self, url, **kwargs):
        raise self._exc


def test_greenhouse_404_logs_compact_without_traceback(caplog):
    with caplog.at_level(logging.INFO):
        jobs = GreenhouseSource("dead-token", _RaisingHttp(_http_error(404))).discover()
    assert jobs == []
    records = [r for r in caplog.records if "dead-token" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None


def test_greenhouse_unexpected_error_logs_full_traceback(caplog):
    with caplog.at_level(logging.WARNING):
        jobs = GreenhouseSource("acme", _RaisingHttp(RuntimeError("boom"))).discover()
    assert jobs == []
    records = [r for r in caplog.records if "acme" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_lever_404_logs_compact_without_traceback(caplog):
    with caplog.at_level(logging.INFO):
        jobs = LeverSource("dead-site", _RaisingHttp(_http_error(404))).discover()
    assert jobs == []
    records = [r for r in caplog.records if "dead-site" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None


def test_ashby_404_logs_compact_without_traceback(caplog):
    with caplog.at_level(logging.INFO):
        jobs = AshbySource("dead-board", _RaisingHttp(_http_error(404))).discover()
    assert jobs == []
    records = [r for r in caplog.records if "dead-board" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_ats_adapters.py -q`
Expected: the 404 tests FAIL (current code always logs with `exc_info=True`, so `records[0].exc_info is None` fails).

**Step 3: Write minimal implementation**

In `src/job_hunter/sources/greenhouse.py`, replace:

```python
        try:
            data = self._http.get_json(_URL_TEMPLATE.format(token=self._token))
        except Exception:
            logger.warning("greenhouse discovery failed for token %s", self._token, exc_info=True)
            return []
```

with:

```python
        try:
            data = self._http.get_json(_URL_TEMPLATE.format(token=self._token))
        except Exception as exc:
            if is_stale_board_error(exc):
                logger.info("greenhouse board not found (404) for token %s", self._token)
            else:
                logger.warning(
                    "greenhouse discovery failed for token %s", self._token, exc_info=True
                )
            return []
```

and update the import line:

```python
from .base import is_stale_board_error, logger, strip_html
```

Apply the equivalent change to `src/job_hunter/sources/lever.py` (message: `"lever board not found (404) for site %s", self._site` / `"lever discovery failed for site %s", self._site`) and `src/job_hunter/sources/ashby.py` (message: `"ashby board not found (404) for board %s", self._board` / `"ashby discovery failed for board %s", self._board`), each updating their `from .base import ...` line to include `is_stale_board_error`.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_ats_adapters.py -q`
Expected: PASS (all tests)

**Step 5: Commit**

```bash
git add src/job_hunter/sources/greenhouse.py src/job_hunter/sources/lever.py src/job_hunter/sources/ashby.py tests/test_ats_adapters.py
git commit -m "fix: log stale ATS 404s compactly instead of a full traceback"
```

---

### Task 3: `JobStore.record_ats_scan_failure` gets a bounded permanent-failure policy

**Files:**
- Modify: `src/job_hunter/store.py`
- Test: `tests/test_store.py`

**Step 1: Write the failing tests**

Add to `tests/test_store.py` (near the existing `test_ats_failure_pauses_board_without_rediscovery_bypassing_pause` / `test_ats_scan_success_records_job_count_and_resets_failures` tests, same style):

```python
def test_ats_permanent_failure_deactivates_board_after_three_in_a_row():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="dead-co")

    for i in range(3):
        store.record_ats_scan_failure(
            "lever", "dead-co", now + timedelta(hours=25 * i), permanent=True
        )

    # Deactivated boards are never returned by list_due_ats_boards, even
    # once any pause would have expired.
    much_later = now + timedelta(days=30)
    assert store.list_due_ats_boards(much_later) == []


def test_ats_permanent_failure_stays_active_below_threshold():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="maybe-dead")

    store.record_ats_scan_failure("lever", "maybe-dead", now, permanent=True)
    store.record_ats_scan_failure(
        "lever", "maybe-dead", now + timedelta(hours=25), permanent=True
    )

    due = store.list_due_ats_boards(now + timedelta(hours=50))
    assert [e.board_identifier for e in due] == ["maybe-dead"]
    assert due[0].consecutive_failures == 2


def test_ats_transient_failure_never_deactivates_board():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="greenhouse", board_identifier="flaky")

    for i in range(10):
        store.record_ats_scan_failure(
            "greenhouse", "flaky", now + timedelta(hours=25 * i)
        )

    due = store.list_due_ats_boards(now + timedelta(days=30))
    assert due[0].board_identifier == "flaky"
    assert due[0].consecutive_failures == 10
    assert due[0].active is True


def test_ats_deactivated_board_reactivates_on_rediscovery():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="reborn-co")
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", "reborn-co", now + timedelta(hours=25 * i), permanent=True
        )
    assert store.list_due_ats_boards(now + timedelta(days=30)) == []

    # The board resurfaces in a freshly discovered job pointing at the same
    # provider/board — ordinary rediscovery reactivates it (existing
    # upsert_ats_board behavior), but the still-unexpired pause still holds.
    store.upsert_ats_board(provider="lever", board_identifier="reborn-co")
    last_pause_start = now + timedelta(hours=25 * 2)
    still_paused_check = last_pause_start + timedelta(hours=1)
    assert store.list_due_ats_boards(still_paused_check) == []
    after_pause = last_pause_start + timedelta(hours=25)
    due = store.list_due_ats_boards(after_pause)
    assert [e.board_identifier for e in due] == ["reborn-co"]
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_store.py -q -k ats_permanent or ats_transient or ats_deactivated`
Expected: FAIL with `TypeError: record_ats_scan_failure() got an unexpected keyword argument 'permanent'`

**Step 3: Write minimal implementation**

In `src/job_hunter/store.py`, add a module-level constant near `_DELIVERABLE_SCORE_FLOOR` (around line 277):

```python
_STALE_BOARD_DEACTIVATION_THRESHOLD = 3
```

Replace the existing `record_ats_scan_failure` method body with:

```python
    def record_ats_scan_failure(
        self,
        provider: str,
        board_identifier: str,
        now: datetime,
        *,
        permanent: bool = False,
    ) -> None:
        """Increment scan failures and back off the board.

        Every failure is isolated per board and gets the same 24h pause so
        it retries soon. `permanent=True` (a stale-looking 404) additionally
        escalates: after `_STALE_BOARD_DEACTIVATION_THRESHOLD` consecutive
        permanent failures the board is deactivated (`active = 0`) instead
        of being paused forever. Deactivation preserves registry history —
        it is not a delete — and `upsert_ats_board` already reactivates any
        inactive board the next time it is rediscovered.

        Transient failures (`permanent=False`) never deactivate a board:
        a network blip is not evidence the board is gone.
        """
        normalized_now = _normalize_utc(now)
        timestamp = normalized_now.isoformat()
        paused_until = (normalized_now + timedelta(hours=24)).isoformat()
        with self._conn:
            if permanent:
                self._conn.execute(
                    """
                    UPDATE ats_registry SET
                        last_checked_at = ?,
                        consecutive_failures = consecutive_failures + 1,
                        paused_until = ?,
                        active = CASE
                            WHEN consecutive_failures + 1 >= ? THEN 0
                            ELSE active
                        END
                    WHERE provider = ? AND board_identifier = ?
                    """,
                    (
                        timestamp,
                        paused_until,
                        _STALE_BOARD_DEACTIVATION_THRESHOLD,
                        provider,
                        board_identifier,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE ats_registry SET
                        last_checked_at = ?,
                        consecutive_failures = consecutive_failures + 1,
                        paused_until = ?
                    WHERE provider = ? AND board_identifier = ?
                    """,
                    (timestamp, paused_until, provider, board_identifier),
                )
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_store.py -q`
Expected: PASS (all tests in the file, including the pre-existing ones — `permanent` defaults to `False` so old call sites are unaffected)

**Step 5: Commit**

```bash
git add src/job_hunter/store.py tests/test_store.py
git commit -m "feat: deactivate learned ATS boards after 3 consecutive stale 404s"
```

---

### Task 4: `LearnedAtsSource` classifies failures, avoids double logging, and marks permanent failures

**Files:**
- Modify: `src/job_hunter/sources/learned_ats.py`
- Test: `tests/test_learned_ats_source.py`

**Step 1: Write the failing tests**

Add to `tests/test_learned_ats_source.py`. First extend the `RoutingHttp` fake with a way to raise a real 404 `requests.HTTPError` (not just `RuntimeError`), and add `import logging` and `import requests` at the top of the file:

```python
import logging

import requests
```

Add a small response fake and extend `RoutingHttp`:

```python
class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _http_error(status_code: int) -> requests.HTTPError:
    return requests.HTTPError(f"status {status_code}", response=_Response(status_code))
```

Modify `RoutingHttp.__init__` to accept a `not_found_urls` set and `get_json` to raise a 404 for those markers:

```python
class RoutingHttp:
    def __init__(self, responses=None, fail_urls=None, not_found_urls=None):
        self.responses = responses or {}
        self.fail_urls = fail_urls or set()
        self.not_found_urls = not_found_urls or set()
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append(url)
        for marker in self.not_found_urls:
            if marker in url:
                raise _http_error(404)
        for marker in self.fail_urls:
            if marker in url:
                raise RuntimeError("network down")
        for marker, payload in self.responses.items():
            if marker in url:
                return payload
        raise RuntimeError(f"no fake response configured for {url}")
```

Now add the new tests:

```python
def test_learned_ats_source_404_logs_compact_and_marks_board_permanent(caplog):
    store = JobStore(":memory:")
    _seed_board(store, "lever", "dead-co")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(not_found_urls={"lever.co"})

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    with caplog.at_level(logging.INFO):
        jobs = source.discover()

    assert jobs == []
    assert source.stats.boards_failed == 1

    # No full traceback anywhere for an expected 404: neither the adapter's
    # nor LearnedAtsSource's own log record carries exc_info.
    relevant = [r for r in caplog.records if "dead-co" in r.getMessage()]
    assert relevant
    assert all(r.exc_info is None for r in relevant)

    entries = {e.board_identifier: e for e in store.list_due_ats_boards(now + timedelta(hours=25))}
    assert entries["dead-co"].consecutive_failures == 1


def test_learned_ats_source_unexpected_error_logs_exactly_one_full_traceback(caplog):
    store = JobStore(":memory:")
    _seed_board(store, "greenhouse", "flaky-co")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(fail_urls={"greenhouse.io"})

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    with caplog.at_level(logging.WARNING):
        source.discover()

    traceback_records = [
        r for r in caplog.records if "flaky-co" in r.getMessage() and r.exc_info is not None
    ]
    assert len(traceback_records) == 1


def test_learned_ats_source_deactivates_board_after_repeated_404s():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "dead-co")
    _seed_board(store, "lever", "healthy-co")
    base = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={
            "healthy-co": [
                {
                    "id": "1",
                    "text": "Engineer",
                    "categories": {"location": "Remote"},
                    "hostedUrl": "https://jobs.lever.co/healthy-co/1",
                    "descriptionPlain": "x",
                    "workplaceType": "remote",
                }
            ],
        },
        not_found_urls={"dead-co"},
    )

    for i in range(3):
        checked_at = base + timedelta(hours=25 * i)
        source = LearnedAtsSource(
            store, http, limit=10, market_order=["berlin"], now=lambda t=checked_at: t
        )
        source.discover()

    final_check = base + timedelta(hours=25 * 3)
    due_identifiers = {
        e.board_identifier for e in store.list_due_ats_boards(final_check)
    }
    assert due_identifiers == {"healthy-co"}
```

Note: this last test requires `RoutingHttp` marker matching to disambiguate the two boards. The board identifier itself (`dead-co` / `healthy-co`) is part of the constructed URL (`https://api.lever.co/v0/postings/{site}?mode=json`) and neither identifier is a substring of the other, so using the bare identifiers as markers unambiguously routes each board's request. This already works with the existing substring-based fake.

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_learned_ats_source.py -q`
Expected: FAIL — currently every failure (including 404) logs `exc_info=True` in `discover()`, so the "no full traceback for 404" and "exactly one full traceback" assertions fail; the deactivation test fails because `record_ats_scan_failure` is never called with `permanent=True` yet.

**Step 3: Write minimal implementation**

In `src/job_hunter/sources/learned_ats.py`, update the import:

```python
from .base import is_stale_board_error, logger
```

Replace the `except Exception:` block inside `discover()`:

```python
            try:
                jobs = self._scan_board(source_type, entry.board_identifier)
            except Exception:
                logger.warning(
                    "learned ATS board scan failed for %s:%s",
                    entry.provider,
                    entry.board_identifier,
                    exc_info=True,
                )
                self.stats.boards_failed += 1
                try:
                    self._store.record_ats_scan_failure(
                        entry.provider, entry.board_identifier, checked_at
                    )
                except Exception:
                    logger.warning(
                        "learned ATS failure health write failed for %s:%s",
                        entry.provider,
                        entry.board_identifier,
                        exc_info=True,
                    )
                continue
```

with:

```python
            try:
                jobs = self._scan_board(source_type, entry.board_identifier)
            except Exception as exc:
                permanent = is_stale_board_error(exc)
                # The adapter already logged the diagnostic for this exact
                # failure (a full traceback for an unexpected error, a
                # compact line for an expected 404) — logging it again here
                # would duplicate that, so this is a compact health-state
                # summary only, never exc_info=True.
                logger.info(
                    "learned ATS board scan failed for %s:%s (%s)",
                    entry.provider,
                    entry.board_identifier,
                    "404, stale board" if permanent else "see prior warning above",
                )
                self.stats.boards_failed += 1
                try:
                    self._store.record_ats_scan_failure(
                        entry.provider,
                        entry.board_identifier,
                        checked_at,
                        permanent=permanent,
                    )
                except Exception:
                    logger.warning(
                        "learned ATS failure health write failed for %s:%s",
                        entry.provider,
                        entry.board_identifier,
                        exc_info=True,
                    )
                continue
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_learned_ats_source.py -q`
Expected: PASS (all tests, including the two pre-existing ones — they never assert on `exc_info` or `permanent`, so the changed log level/message doesn't break them)

**Step 5: Commit**

```bash
git add src/job_hunter/sources/learned_ats.py tests/test_learned_ats_source.py
git commit -m "fix: classify learned ATS 404s and stop duplicate traceback logging"
```

---

### Task 5: Full regression pass

**Files:** none (verification only)

**Step 1: Run the full suite**

Run: `pytest -q`
Expected: all tests pass, including everything under `tests/test_ats_registry.py`, `tests/test_store.py`, `tests/test_learned_ats_source.py`, `tests/test_ats_adapters.py`, `tests/test_company_watch_source.py` (unaffected — `company_watch.py` was not touched), and `tests/test_discovery.py`.

**Step 2: Spot-check `ats_registry.py` needed no change**

`select_ats_boards` in `src/job_hunter/ats_registry.py` ranks entries already filtered to `active=1` and pause-expired by `list_due_ats_boards` — a deactivated board is excluded before ranking even runs, and a still-active-but-failing board keeps its normal rank (oldest `last_checked_at` first). No change needed there; this step is just confirming that read during planning still holds after Tasks 1-4.

**Step 3: If everything passes, work is done.** No further commit needed beyond Task 4's (this task is verification-only, per YAGNI don't add a no-op commit).

---

## Notes for the executing agent

- Do not modify `src/job_hunter/sources/company_watch.py`. It shares the same three adapters and has an analogous duplicate-traceback shape, but issue #36 explicitly scopes to the learned-ATS registry; fixing company_watch.py is unrelated scope creep for this issue.
- `_STALE_BOARD_DEACTIVATION_THRESHOLD = 3` and the 24h pause are the "bounded policy" the acceptance criteria ask for — document them in the docstring (already done in Task 3's implementation) rather than inventing a config flag; nothing in the issue asks for configurability.
- Only 404 is treated as "permanent-looking." Do not extend the classifier to other 4xx codes (410, 400, etc.) — the production evidence in the issue is 404s only, and inventing extra permanent-failure codes without evidence would be scope creep.

# Yield-Driven Per-Source Crawling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each job source crawl on its own cadence, derived from how much genuinely new material it produces, as one message on the `crawl_source` queue rather than a step in a shared per-user run.

**Architecture:** A shared, identity-free source registry gives every source a durable row carrying its kind and its display obligation. Every crawl writes a row to a shared crawl ledger recording what it fetched and how much was new; a scheduler bands each source on that novelty and installs one `pg_cron` entry per source through a fixed `job_hunter_schedule_stage_enqueue`. Conditional requests live in `HttpClient` so all eighteen adapters get them at one site, and the description-hash short-circuit happens client-side before staging rather than inside `job_hunter_upsert_posting` where it is too late to prevent work.

**Tech Stack:** Python 3.11+, psycopg 3 with `psycopg_pool`, requests, Supabase Postgres with `pgmq` 1.5.1 and `pg_cron` 1.6.4, pgTAP, pytest.

**Spec:** `docs/superpowers/specs/2026-09-09-job-hunter-yield-driven-source-crawl-design.md`

## Global Constraints

- **Branch:** `feat/job-hunter-yield-driven-source-crawl`, based on `ab35e3f`. Never commit to `main`.
- **Blocked behind #179.** Tasks 1–3 are pure Python and may proceed now. Tasks 4–10 touch the schema or `supabase/tests/pgtap/job_hunter_isolation.sql` and must not start until `feat/job-hunter-shared-table-writers` merges to `main` and this branch is rebased onto it.
- **One migration file** for the whole ticket: `supabase/migrations/29999999000000_job_hunter_source_registry.sql`. `29999999000000` is deliberately a year-2999 sentinel, not a real timestamp. The real timestamp is allocated at pull-request open, in merge order, per PR #202 — ask for it then.
- **Tests:** `pnpm job-hunter:test` with `SUPABASE_TEST_URL`, `SUPABASE_TEST_PUBLISHABLE_KEY`, `SUPABASE_TEST_SIGNING_KEY_B64` **and `SUPABASE_TEST_DB_URL`** exported. The last one became required at `conftest.py:55` in the #179 merge (`a975945`): without it no posting, facet, company or board can be persisted at all, so every write path under test is unreachable. Get it from `supabase status -o env` as `export SUPABASE_TEST_DB_URL="$DB_URL"`. Partial configuration always fails loudly since #208; a run that names one of these variables is that, not your diff. This worktree needs its own `.venv` first (present and verified).
- **Tasks 1–3 only** may use `JOB_HUNTER_ALLOW_MISSING_STACK=1` — they touch no database. Tasks 4–10 must never use it: the escape hatch on a schema task is exactly how a migration gets verified by nothing.
- **The placeholder filename MUST be numeric.** `supabase db reset` **silently skips** any migration whose filename does not start with a number — no error, no warning, and the migration is simply never applied. A word-placeholder like `PLACEHOLDER_...` therefore produces a migration that no test ever runs, while pgTAP either fails confusingly or, worse, passes against objects left on the shared stack by an earlier hand-application. `29999999000000` is the right sentinel and the one `AGENTS.md:421` names: numeric so the CLI applies it, year 2999 so nobody mistakes it for real, and sorting last so it can never sort before an applied migration. **Verify a migration only after `pnpm db:reset`**, never against a stack you have hand-applied to — the reset is what proves the committed tree stands on its own.
- **pgTAP runs as `pnpm db:test`, never as a bare `supabase test db`.** The bare form scans all of `supabase/tests/`, which contains `202608310001_planned_practice_sessions.verify.sql` — a standalone psql script that RAISEs rather than emitting TAP, so the run always ends `No plan found in TAP output / Result: FAIL` on a perfectly green tree. `pnpm db:test` scopes to `supabase/tests/pgtap/` and also takes the shared stack lock, which matters with parallel agents. Same for `pnpm db:reset` over `supabase db reset`. (`--linked=false` is also not a valid flag on CLI 2.116; it takes `--local`.)
- **A new shared table or `security definer` function must join #179's enforced inventories**, or their assertions fail: `pg_temp.job_hunter_shared_tables` and `pg_temp.job_hunter_ingestion_tables` in `job_hunter_isolation.sql`, the read-only-definer list in `job_hunter_shared_writes.sql`, and both the function array and the definer list in `job_hunter_store_functions.sql`. Shared *knowledge* goes on the shared-tables list; shared *machinery* goes on the ingestion list.
- **Any new stage's test fixture gets the ingestion connection.** #185 shipped two live platform-key cost defects — a posting enqueued once per persist phase (three per crawl) and a failed read re-draining the same message in the same run — and both were invisible because the store fixture held no `IngestionDatabase` while production always sets `SUPABASE_DB_URL`. Both are fixed on `main` as of `a975945`, along with a per-run enqueue dedup that Task 9's stage must not defeat.
- **Two table shapes, and they are not interchangeable.** Shared *knowledge* (`job_hunter_sources`) takes #179's shape: `select` to `authenticated`, writes revoked from `anon`, `authenticated` and `service_role`, pgTAP proving the refusal. Shared *machinery* (`job_hunter_source_crawls`, `job_hunter_source_cursors`) takes #183's shape: row-level security enabled with **no policy**, and grants revoked as well.
- **No `user_id` in the display-credit path.** `job_hunter_posting_display_credit` resolves from the posting to the source and nowhere else.
- **No operator-tuned frequencies.** The cadence bands are derived from data with zero operator knowledge. A `Settings` override may pin one source; it is never the mechanism.
- **An empty result carries its reason.** Every crawl writes a `job_hunter_source_crawls` row, including the ones that produced nothing, and `outcome` distinguishes `not_modified` from `rate_limited` from `failed`.
- **Never verify a database path with a fake alone.** #179 exposed that `conftest.py`'s `store` fixture had no `IngestionDatabase`, so every path behind `if self._ingestion is None` was dead in the whole suite while production always sets `SUPABASE_DB_URL` — two live cost defects shipped through that gap. Any task whose code issues SQL gets an `integration`-marked test against the real fixture, and the run is only believed if that test **ran** rather than skipped.
- **Naming:** the display obligation is `display_credit`. Never `attribution` — that word means market attribution in ~20 places in `discovery.py`.
- **Commit trailers:** every commit ends with
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_017ktaGiEP783wpVmAuqjCKw
  ```

---

## File Structure

**Created:**
- `apps/job-hunter/src/job_hunter/source_schedule.py` — the cadence ladder and cron-expression rendering. Pure functions, no I/O, so the scheduling policy is testable without a database.
- `apps/job-hunter/src/job_hunter/crawl_source.py` — the `crawl_source` stage handler. Mirrors `resolve_persist.py`: no user-scoped store, no matching, no scoring, no credentials.
- `apps/job-hunter/tests/test_source_schedule.py`
- `apps/job-hunter/tests/test_crawl_source.py`
- `apps/job-hunter/tests/test_http_conditional.py`
- `supabase/migrations/29999999000000_job_hunter_source_registry.sql`
- `supabase/tests/pgtap/job_hunter_source_registry.sql`

**Modified:**
- `apps/job-hunter/src/job_hunter/http.py` — validator-aware `get_json`, `NotModified` sentinel.
- `apps/job-hunter/src/job_hunter/sources/base.py` — `JobSource` gains `source_key`.
- `apps/job-hunter/src/job_hunter/sources/__init__.py` — `build_source` single-source form.
- `apps/job-hunter/src/job_hunter/search_budget.py` — platform ledger, `user_id` gone.
- `apps/job-hunter/src/job_hunter/postgres_store.py` — `merge_posting_batch` accepts a pre-filtered batch.
- `apps/job-hunter/tests/conftest.py:160` — cleanup list.
- `apps/job-hunter/tests/test_brave_budget.py`
- `supabase/tests/pgtap/job_hunter_stage_queues.sql:100–170`
- `supabase/tests/pgtap/job_hunter_store_functions.sql:150–172`
- `supabase/tests/pgtap/job_hunter_isolation.sql:67,157`
- `supabase/tests/pgtap/job_hunter_write_idempotency.sql:29–52`
- `supabase/migrations/20260909200000_job_hunter_ats_boards.sql` — comment correction only.
- `apps/job-hunter/AGENTS.md`

---

### Task 1: `source_key` on the source protocol

Every adapter already carries `source_label` (`remotive`, `lever:acme`). The registry keys on the same string, so no new naming scheme is introduced — but `source_label` is documented as a *metrics* label, and the stage handler needs a name it can look up a row by. `source_key` is that name, defaulting to `source_label` so no adapter changes.

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/sources/base.py`
- Test: `apps/job-hunter/tests/test_sources_incremental.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `JobSource.source_key: str` — a read-only property on the Protocol; `source_key_for(source: JobSource) -> str` returning `getattr(source, "source_key", None) or source.source_label`.

- [ ] **Step 1: Write the failing test**

Append to `apps/job-hunter/tests/test_sources_incremental.py`:

```python
from job_hunter.sources.base import source_key_for


class _LabelOnly:
    source_label = "remotive"

    def discover(self):
        yield from ()


class _KeyedBoard:
    source_label = "lever:acme"
    source_key = "lever:acme"

    def discover(self):
        yield from ()


def test_source_key_falls_back_to_the_metrics_label():
    """An adapter that never heard of source_key still has one."""
    assert source_key_for(_LabelOnly()) == "remotive"


def test_source_key_is_used_when_the_adapter_declares_one():
    assert source_key_for(_KeyedBoard()) == "lever:acme"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm job-hunter:test -- tests/test_sources_incremental.py -k source_key -v`
Expected: FAIL with `ImportError: cannot import name 'source_key_for'`

- [ ] **Step 3: Write minimal implementation**

In `apps/job-hunter/src/job_hunter/sources/base.py`, add to the `JobSource` Protocol and below it:

```python
class JobSource(Protocol):
    source_label: str

    # The durable name this source is keyed by in `job_hunter_sources`,
    # in `job_hunter_source_crawls` and in its own pg_cron entry. It
    # defaults to `source_label` because the two have always been the
    # same string; it is separate because `source_label` is documented
    # as a metrics label and a registry key must not drift with it.
    source_key: str

    def discover(self) -> Iterator[Job]: ...


def source_key_for(source) -> str:
    """Return `source`'s registry key, falling back to its metrics label.

    `source_key` is declared on the Protocol but is not required of an
    adapter: every existing one predates it, and none of them needs
    changing for the key to be correct.
    """
    return getattr(source, "source_key", None) or source.source_label
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm job-hunter:test -- tests/test_sources_incremental.py -v`
Expected: PASS, and every pre-existing test in that file still passes.

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/sources/base.py apps/job-hunter/tests/test_sources_incremental.py
git commit -m "feat(job-hunter): give every source a durable registry key (#184)"
```

---

### Task 2: Conditional requests in `HttpClient`

All eighteen adapters funnel through `HttpClient.get_json()`, which calls `raise_for_status()` and would therefore throw on a `304`. Adding the validator path here gives every source conditional requests at one site. This is also why the `304` test is a unit test at this level: if this path is wrong, it is wrong everywhere at once.

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/http.py`
- Test: `apps/job-hunter/tests/test_http_conditional.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `job_hunter.http.NOT_MODIFIED` — a module-level sentinel object.
  - `job_hunter.http.Validators` — `@dataclass(frozen=True)` with `etag: str = ""` and `last_modified: str = ""`.
  - `HttpClient.get_json(url, *, validators: Validators | None = None, **kwargs) -> dict | list | object` — returns `NOT_MODIFIED` on a 304.
  - `HttpClient.last_validators() -> Validators` — the validators from the most recent response, for the caller to persist.

- [ ] **Step 1: Write the failing test**

Create `apps/job-hunter/tests/test_http_conditional.py`:

```python
from __future__ import annotations

import pytest
import requests

from job_hunter.http import NOT_MODIFIED, HttpClient, Validators


class _FakeResponse:
    def __init__(self, status_code: int, headers: dict[str, str], payload):
        self.status_code = status_code
        self.headers = headers
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class _RecordingSession:
    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response

    headers: dict[str, str] = {}


def _client_with(response) -> tuple[HttpClient, _RecordingSession]:
    client = HttpClient()
    session = _RecordingSession(response)
    client._session = session
    return client, session


def test_a_304_returns_the_sentinel_rather_than_raising():
    """An unchanged board must be distinguishable from an empty one."""
    client, _ = _client_with(_FakeResponse(304, {}, None))
    result = client.get_json("https://example.test/jobs", validators=Validators(etag='"abc"'))
    assert result is NOT_MODIFIED


def test_validators_are_sent_as_conditional_headers():
    client, session = _client_with(_FakeResponse(304, {}, None))
    client.get_json(
        "https://example.test/jobs",
        validators=Validators(etag='"abc"', last_modified="Wed, 21 Oct 2026 07:28:00 GMT"),
    )
    _method, _url, kwargs = session.calls[0]
    assert kwargs["headers"]["If-None-Match"] == '"abc"'
    assert kwargs["headers"]["If-Modified-Since"] == "Wed, 21 Oct 2026 07:28:00 GMT"


def test_no_validators_means_no_conditional_headers():
    """A first crawl must not send an empty If-None-Match."""
    client, session = _client_with(_FakeResponse(200, {}, {"jobs": []}))
    client.get_json("https://example.test/jobs")
    _method, _url, kwargs = session.calls[0]
    assert "If-None-Match" not in kwargs.get("headers", {})
    assert "If-Modified-Since" not in kwargs.get("headers", {})


def test_a_200_returns_the_payload_and_exposes_new_validators():
    response = _FakeResponse(
        200,
        {"ETag": '"def"', "Last-Modified": "Thu, 22 Oct 2026 07:28:00 GMT"},
        {"jobs": [{"id": 1}]},
    )
    client, _ = _client_with(response)
    payload = client.get_json("https://example.test/jobs", validators=Validators(etag='"abc"'))
    assert payload == {"jobs": [{"id": 1}]}
    assert client.last_validators() == Validators(
        etag='"def"', last_modified="Thu, 22 Oct 2026 07:28:00 GMT"
    )


def test_a_500_still_raises():
    """Conditional support must not swallow a real failure."""
    client, _ = _client_with(_FakeResponse(500, {}, None))
    with pytest.raises(requests.HTTPError):
        client.get_json("https://example.test/jobs", retry=False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm job-hunter:test -- tests/test_http_conditional.py -v`
Expected: FAIL with `ImportError: cannot import name 'NOT_MODIFIED' from 'job_hunter.http'`

- [ ] **Step 3: Write minimal implementation**

In `apps/job-hunter/src/job_hunter/http.py`, add above `class HttpClient`:

```python
from dataclasses import dataclass


class _NotModified:
    """The response to a conditional request whose resource did not change.

    A distinct object rather than `None` or an empty list, because the whole
    point of a conditional request is that "unchanged" and "empty" are
    different answers and must not be able to collapse into one. A source
    that returns this reports `not_modified`; a source that genuinely
    returned no jobs reports `fetched` with zero.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NOT_MODIFIED"


NOT_MODIFIED = _NotModified()


@dataclass(frozen=True)
class Validators:
    """The HTTP cache validators a source carries between crawls."""

    etag: str = ""
    last_modified: str = ""

    def as_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers
```

Add `304` to the non-retryable path by leaving `_RETRY_STATUS_CODES` alone (304 is not in it), add `self._last_validators = Validators()` to `__init__`, and replace `get_json`:

```python
    def get_json(self, url: str, *, validators: Validators | None = None, **kwargs):
        """GET and decode JSON, honouring a conditional request.

        Returns `NOT_MODIFIED` when the server answers 304. Every other
        status keeps the previous behaviour exactly, `raise_for_status`
        included, so a real failure is still a failure.
        """
        if validators is not None:
            conditional = validators.as_headers()
            if conditional:
                headers = dict(kwargs.pop("headers", None) or {})
                headers.update(conditional)
                kwargs["headers"] = headers
        response = self.get(url, **kwargs)
        self._last_validators = Validators(
            etag=response.headers.get("ETag", "") or "",
            last_modified=response.headers.get("Last-Modified", "") or "",
        )
        if response.status_code == 304:
            return NOT_MODIFIED
        response.raise_for_status()
        return response.json()

    def last_validators(self) -> Validators:
        """The validators from the most recent response, for persisting."""
        return self._last_validators
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm job-hunter:test -- tests/test_http_conditional.py -v`
Expected: PASS (5 tests)

Then run the whole source suite to prove no adapter regressed:

Run: `pnpm job-hunter:test -- tests/test_sources_incremental.py tests/test_wellfound_source.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/http.py apps/job-hunter/tests/test_http_conditional.py
git commit -m "feat(job-hunter): make HttpClient issue conditional requests (#184)"
```

---

### Task 3: The cadence ladder

Pure functions, no I/O. This is the scheduling *policy*, kept apart from the scheduler so it can be tested exhaustively without a database and so "no operator sets these" is visible in one file.

**Files:**
- Create: `apps/job-hunter/src/job_hunter/source_schedule.py`
- Test: `apps/job-hunter/tests/test_source_schedule.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BANDS: tuple[int, ...]` — cadence in minutes, fastest first.
  - `next_band(current_index: int, *, outcome: str, novelty: int) -> int`
  - `cron_expression(band_index: int, *, source_key: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `apps/job-hunter/tests/test_source_schedule.py`:

```python
from __future__ import annotations

import pytest

from job_hunter.source_schedule import BANDS, cron_expression, next_band


def test_novelty_promotes_one_band():
    assert next_band(3, outcome="fetched", novelty=1) == 2


def test_an_empty_crawl_demotes_one_band():
    assert next_band(1, outcome="fetched", novelty=0) == 2


def test_not_modified_counts_as_empty():
    """An unchanged board is healthy but produced nothing; visit it less."""
    assert next_band(0, outcome="not_modified", novelty=0) == 1


def test_a_rate_limited_source_backs_off_even_when_it_returned_something():
    assert next_band(0, outcome="rate_limited", novelty=9) == 1


def test_a_failing_source_backs_off():
    assert next_band(2, outcome="failed", novelty=0) == 3


def test_the_fastest_band_is_a_floor():
    assert next_band(0, outcome="fetched", novelty=5) == 0


def test_the_slowest_band_is_a_ceiling():
    last = len(BANDS) - 1
    assert next_band(last, outcome="fetched", novelty=0) == last


def test_a_source_producing_nothing_converges_on_the_slowest_band():
    """Five consecutive empty crawls take the fastest source to the floor."""
    index = 0
    for _ in range(len(BANDS) - 1):
        index = next_band(index, outcome="fetched", novelty=0)
    assert BANDS[index] == 10080


def test_recovery_is_one_band_at_a_time_not_a_jump():
    index = len(BANDS) - 1
    index = next_band(index, outcome="fetched", novelty=3)
    assert index == len(BANDS) - 2


def test_an_unknown_outcome_is_rejected_rather_than_treated_as_healthy():
    with pytest.raises(ValueError):
        next_band(0, outcome="probably_fine", novelty=0)


@pytest.mark.parametrize("index", range(len(BANDS)))
def test_every_band_renders_a_five_field_cron_expression(index):
    expression = cron_expression(index, source_key="remotive")
    assert len(expression.split()) == 5


def test_two_sources_on_the_same_band_do_not_fire_at_the_same_minute():
    """Eighteen sources all firing on minute zero is a self-inflicted spike."""
    a = cron_expression(1, source_key="remotive")
    b = cron_expression(1, source_key="lever:acme")
    assert a != b


def test_the_offset_is_stable_for_a_given_source():
    assert cron_expression(1, source_key="remotive") == cron_expression(
        1, source_key="remotive"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm job-hunter:test -- tests/test_source_schedule.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_hunter.source_schedule'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/job-hunter/src/job_hunter/source_schedule.py`:

```python
"""The per-source crawl cadence, derived from measured novelty (issue #184).

Kept apart from the scheduler that installs the cron entries so that the
policy -- how often a source is worth visiting -- is a pure function over
what the last crawl produced, and can be tested exhaustively without a
database.

No operator sets these frequencies. A source earns a faster band by
producing material the corpus did not already have, and loses one by
producing none, and that is the whole mechanism. Configuration may pin one
source as an override; it is never how a cadence is arrived at.
"""

from __future__ import annotations

import hashlib

#: Crawl cadence in minutes, fastest first. A source moves one step at a
#: time in either direction, so the ladder's spacing is the recovery rate as
#: much as it is the range: five empty crawls take the most-favoured source
#: to the floor, and five productive ones bring it back.
BANDS: tuple[int, ...] = (15, 60, 360, 1440, 4320, 10080)

_HEALTHY_OUTCOMES = frozenset({"fetched", "not_modified"})
_BACKOFF_OUTCOMES = frozenset({"rate_limited", "failed"})


def next_band(current_index: int, *, outcome: str, novelty: int) -> int:
    """Return the band a source moves to after one crawl.

    `not_modified` is healthy but produced nothing, so it demotes exactly
    like an empty fetch: a board that keeps answering "unchanged" is
    telling us it does not need visiting this often.

    `rate_limited` and `failed` demote regardless of what the crawl
    returned, because the constraint is the source's tolerance rather than
    its productivity -- and because a source that returns rows *and* a 429
    is precisely the one to slow down.
    """
    if outcome in _BACKOFF_OUTCOMES:
        return min(current_index + 1, len(BANDS) - 1)
    if outcome not in _HEALTHY_OUTCOMES:
        raise ValueError(f"unknown crawl outcome: {outcome!r}")
    if novelty > 0:
        return max(current_index - 1, 0)
    return min(current_index + 1, len(BANDS) - 1)


def _offset(source_key: str, modulus: int) -> int:
    """A stable per-source offset, so sources on one band do not stampede.

    Derived from the key rather than from a counter so it survives a source
    being removed and re-added, and so two deployments schedule the same
    source at the same minute.
    """
    digest = hashlib.sha256(source_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def cron_expression(band_index: int, *, source_key: str) -> str:
    """Render one band as a five-field pg_cron expression.

    The 72-hour band uses a day-of-month step, which pg_cron restarts each
    month: days 1, 4, ... 31 then 1 gives one shortened gap at a month
    boundary. That is accepted rather than worked around -- a crawl arriving
    a day early once a month costs one extra conditional request.
    """
    if not 0 <= band_index < len(BANDS):
        raise ValueError(f"band index out of range: {band_index}")
    minute = _offset(source_key, 60)
    hour = _offset(source_key + ":hour", 24)
    minutes = BANDS[band_index]
    if minutes == 15:
        return f"{minute % 15}-59/15 * * * *"
    if minutes == 60:
        return f"{minute} * * * *"
    if minutes == 360:
        return f"{minute} {hour % 6}-23/6 * * *"
    if minutes == 1440:
        return f"{minute} {hour} * * *"
    if minutes == 4320:
        return f"{minute} {hour} 1-31/3 * *"
    return f"{minute} {hour} * * {_offset(source_key + ':dow', 7)}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm job-hunter:test -- tests/test_source_schedule.py -v`
Expected: PASS (17 tests, counting the parametrised band cases)

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/source_schedule.py apps/job-hunter/tests/test_source_schedule.py
git commit -m "feat(job-hunter): derive crawl cadence from measured novelty (#184)"
```

---

> **STOP.** Tasks 4 onwards touch the schema and the enforced isolation inventory. Do not start until #179 (`feat/job-hunter-shared-table-writers`) has merged to `main` and this branch is rebased onto it. Verify with `git log origin/main --oneline | grep -i shared-table-writers` before continuing.

---

### Task 4: `job_hunter_sources` and the display-credit resolver

**Files:**
- Create: `supabase/migrations/29999999000000_job_hunter_source_registry.sql`
- Create: `supabase/tests/pgtap/job_hunter_source_registry.sql`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Table `public.job_hunter_sources (id uuid, source_key text unique, kind text, display_credit jsonb, enabled boolean, first_seen_at timestamptz, created_at timestamptz)`.
  - `public.job_hunter_posting_display_credit(p_posting_id uuid) returns jsonb` — `security definer`, returns `null` when the posting's source declares no obligation.

- [ ] **Step 1: Write the failing test**

Create `supabase/tests/pgtap/job_hunter_source_registry.sql`:

```sql
begin;
select plan(11);

-- Shared knowledge: readable by any authenticated user, written by none ----

select has_table('public', 'job_hunter_sources', 'the source registry exists');

insert into public.job_hunter_sources (source_key, kind, display_credit)
values
  ('remotive', 'crawl', '{}'::jsonb),
  ('example_licensed', 'licensed', jsonb_build_object(
     'required', true,
     'text', 'Jobs by Example',
     'link_text', 'Jobs',
     'link_url', 'https://example.test/',
     'badge_url', 'https://example.test/logo.png',
     'badge_min_px', jsonb_build_array(116, 23)));

select ok(
  has_table_privilege('authenticated', 'public.job_hunter_sources', 'select'),
  'an authenticated user may read the registry');
select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_sources', 'insert, update, delete') $$,
  'but may not write it');
select is_empty(
  $$ select 1 where has_table_privilege(
       'anon', 'public.job_hunter_sources', 'insert, update, delete') $$,
  'nor may anon');
select is_empty(
  $$ select 1 where has_table_privilege(
       'service_role', 'public.job_hunter_sources', 'insert, update, delete') $$,
  'nor the unused service role');

-- "source" does not imply "crawl" ------------------------------------------

select throws_ok(
  $$ insert into public.job_hunter_sources (source_key, kind)
     values ('bad_kind', 'scrape') $$,
  '23514',
  null,
  'kind is constrained to the two source kinds');
select is(
  (select kind from public.job_hunter_sources where source_key = 'example_licensed'),
  'licensed',
  'a licensed API is a first-class source kind, not a variant of a crawl');

-- The obligation resolves with no user in the path -------------------------

insert into public.job_hunter_postings (fingerprint, source, first_seen_at, last_seen_at)
values
  ('18400000-fingerprint-licensed', 'example_licensed', now(), now()),
  ('18400000-fingerprint-scraped', 'remotive', now(), now());

select is(
  public.job_hunter_posting_display_credit(
    (select id from public.job_hunter_postings
      where fingerprint = '18400000-fingerprint-licensed')) ->> 'text',
  'Jobs by Example',
  'a posting resolves its source''s required credit');
select is(
  public.job_hunter_posting_display_credit(
    (select id from public.job_hunter_postings
      where fingerprint = '18400000-fingerprint-licensed')) ->> 'link_url',
  'https://example.test/',
  'including the link a surface that cannot render the badge still owes');
select ok(
  public.job_hunter_posting_display_credit(
    (select id from public.job_hunter_postings
      where fingerprint = '18400000-fingerprint-scraped')) is null,
  'a source declaring no obligation resolves to nothing rather than an empty object');
select ok(
  public.job_hunter_posting_display_credit(gen_random_uuid()) is null,
  'an unknown posting resolves to nothing rather than raising');

select * from finish();
rollback;
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm db:test`
Expected: FAIL — `relation "public.job_hunter_sources" does not exist`

- [ ] **Step 3: Write minimal implementation**

Create `supabase/migrations/29999999000000_job_hunter_source_registry.sql` with this as its first section:

```sql
-- Sources become an entity, and crawling follows measured novelty (issue #184).
--
-- PLACEHOLDER is deliberately not a timestamp. Migration timestamps are
-- allocated at pull-request open, in merge order (PR #202).

-- The source registry ------------------------------------------------------
--
-- Until now a source was a bare text label on a posting and a Python class
-- with a `source_label`. Two of this ticket's requirements need a row: a
-- source has a *kind*, and a source can oblige a surface to display
-- something.
--
-- `kind` exists because a licensed API is a first-class source kind and not
-- a variant of a crawl. Its rate accounting is a contractual quota of calls
-- rather than a politeness delay, its cadence is bounded by that call budget
-- rather than by how hard we dare hit it, and its rows overlap heavily with
-- scraped rows for the same posting -- which the fingerprint already handles.
-- Carrying `kind` from the start is what stops "source" from silently
-- meaning "crawl". No licensed source is enabled here; the shape is present
-- and the portfolio stays scraped.
--
-- `display_credit` is NOT called `attribution`. That word already means
-- market attribution throughout `discovery.py` -- `_cheap_market_attribution`,
-- `_record_reattribution`, `stats.reattributed_*` -- and a second meaning on
-- one word would be misread by everybody including us. "Credit" is the
-- licensing term of art and covers both halves of the obligation: the
-- required text and the required badge.
--
-- Shape of `display_credit`, written as the general rule rather than as one
-- provider's special case:
--
--   {"required": true,
--    "text": "Jobs by Adzuna",
--    "link_text": "Jobs",
--    "link_url": "https://...",
--    "badge_url": "https://.../logo.png",
--    "badge_min_px": [116, 23]}
--
-- A surface that cannot render `badge_url` -- Telegram's sendMessage has no
-- image entity -- contributes `text` and `link_url` and never the advert
-- body. #188 is where that is read.
--
-- This is shared *knowledge* in the #179 sense: what a source says is true
-- for everybody, so select is open to authenticated and every write is
-- revoked from every role a user can hold.
create table public.job_hunter_sources (
  id uuid primary key default gen_random_uuid(),
  source_key text not null unique,
  kind text not null default 'crawl' check (kind in ('crawl', 'licensed')),
  display_credit jsonb not null default '{}'::jsonb
    check (jsonb_typeof(display_credit) = 'object'),
  enabled boolean not null default true,
  first_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

comment on table public.job_hunter_sources is
  'Every source the engine crawls or licenses, keyed by the same string the '
  'Python adapters expose as source_label (issue #184). Carries the source '
  'kind and any display obligation the source imposes on a surface.';
comment on column public.job_hunter_sources.display_credit is
  'What a surface is obliged to display for a posting from this source. A '
  'property of the source, never of the reader: nothing in the path that '
  'resolves it takes a user_id. Empty object means no obligation.';

alter table public.job_hunter_sources enable row level security;
create policy select_authenticated on public.job_hunter_sources
  for select to authenticated using (true);
revoke insert, update, delete on public.job_hunter_sources
  from anon, authenticated, service_role;

-- Seed the registry from the corpus the crawl has already produced, so a
-- source that exists in postings has a row without anyone typing it in.
insert into public.job_hunter_sources (source_key, first_seen_at)
select distinct p.source, min(p.first_seen_at)
  from public.job_hunter_postings p
 where coalesce(p.source, '') <> ''
 group by p.source
on conflict (source_key) do nothing;

-- The display obligation, resolved with no user identity in the path -------
--
-- security definer because job_hunter_sources' select policy is open to
-- authenticated but the join runs from a posting, and a surface rendering a
-- digest should not need a session at all. It takes a posting id and
-- nothing else: there is deliberately no overload that accepts a user.
create or replace function public.job_hunter_posting_display_credit(
  p_posting_id uuid
)
returns jsonb
language sql
security definer
stable
set search_path = ''
as $$
  select case
           when s.display_credit = '{}'::jsonb then null
           else s.display_credit
         end
    from public.job_hunter_postings p
    join public.job_hunter_sources s on s.source_key = p.source
   where p.id = p_posting_id;
$$;

comment on function public.job_hunter_posting_display_credit(uuid) is
  'What a surface must display alongside this posting, or null when its '
  'source imposes nothing. No user_id anywhere in the path -- the obligation '
  'belongs to the posting''s source and not to whoever is reading it '
  '(issue #184, read by #188).';

grant execute on function public.job_hunter_posting_display_credit(uuid)
  to authenticated;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm db:reset && pnpm db:test`
Expected: PASS — `job_hunter_source_registry.sql .. ok`, 11/11

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/29999999000000_job_hunter_source_registry.sql supabase/tests/pgtap/job_hunter_source_registry.sql
git commit -m "feat(job-hunter): give sources a row, a kind and a display obligation (#184)"
```

---

### Task 5: The crawl ledger and the cursor store

**Files:**
- Modify: `supabase/migrations/29999999000000_job_hunter_source_registry.sql` (append)
- Modify: `supabase/tests/pgtap/job_hunter_source_registry.sql` (append; raise `plan(11)` to `plan(17)`)

**Interfaces:**
- Consumes: `public.job_hunter_sources`.
- Produces:
  - `public.job_hunter_source_crawls (id, source_key, started_at, finished_at, outcome, fetched, new_to_corpus, changed, unchanged_by_hash, requests, elapsed_ms, error)`.
  - `public.job_hunter_source_cursors (source_key primary key, etag, last_modified, high_water_at, updated_at)`.

- [ ] **Step 1: Write the failing test**

Append to `supabase/tests/pgtap/job_hunter_source_registry.sql`, before `select * from finish();`, and change `select plan(11);` to `select plan(17);`:

```sql
-- Shared machinery: nobody reads the engine's own telemetry ----------------

select has_table('public', 'job_hunter_source_crawls', 'the crawl ledger exists');
select has_table('public', 'job_hunter_source_cursors', 'the cursor store exists');

select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_source_crawls',
       'select, insert, update, delete') $$,
  'a user cannot reach the crawl ledger at all');
select is_empty(
  $$ select 1 where has_table_privilege(
       'authenticated', 'public.job_hunter_source_cursors',
       'select, insert, update, delete') $$,
  'nor the cursor store');

-- An empty result has to carry its reason ----------------------------------

select throws_ok(
  $$ insert into public.job_hunter_source_crawls (source_key, started_at, outcome)
     values ('remotive', now(), 'nothing_today') $$,
  '23514',
  null,
  'a crawl outcome must be one of the four that distinguish why it was empty');

select lives_ok(
  $$ insert into public.job_hunter_source_crawls
       (source_key, started_at, finished_at, outcome, fetched, new_to_corpus)
     values ('remotive', now(), now(), 'not_modified', 0, 0) $$,
  'an unchanged board records not_modified rather than an empty fetch');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm db:test`
Expected: FAIL — `relation "public.job_hunter_source_crawls" does not exist`

- [ ] **Step 3: Write minimal implementation**

Append to `supabase/migrations/29999999000000_job_hunter_source_registry.sql`:

```sql
-- The crawl ledger ---------------------------------------------------------
--
-- The shared, identity-free yield signal the scheduler bands on.
--
-- "Measured yield" split in two when #203 landed. Board health became shared
-- knowledge on job_hunter_ats_boards; eligible_jobs_seen and last_eligible_at
-- stayed per-user on job_hunter_ats_registry, and that migration's comment
-- says "#184 is built on it". That line is stale, for two reasons.
--
-- Mechanically: the crawl_source scheduler runs as the privileged owning
-- role with no auth.uid() and cannot read a per-user column. Threading a
-- user through to reach one is the mistake #174 and #175 spent four tickets
-- undoing.
--
-- And substantively: with one user, an aggregate of per-user eligibility IS
-- that user's search profile. The engine would learn to visit only the
-- boards matching the current job hunt, and would then present a narrowing
-- corpus as a quiet job market. #203 already warns that a shared row
-- carrying one user's yield misleads budgeting for everyone else; the same
-- argument applied to scheduling gives the same answer.
--
-- So the signal here is corpus novelty: how much of what a source returned
-- the corpus did not already have. Same measure for every user, scales with
-- jobs rather than with subscribers.
--
-- Shared *machinery* in the #183 sense, not shared knowledge: this is the
-- engine's own operational state, no user reads it, so row level security is
-- on with no policy and the grants are revoked as well. Neither half is
-- load-bearing alone.
create table public.job_hunter_source_crawls (
  id uuid primary key default gen_random_uuid(),
  source_key text not null,
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  outcome text not null check (outcome in (
    'fetched', 'not_modified', 'rate_limited', 'failed'
  )),
  fetched integer not null default 0 check (fetched >= 0),
  new_to_corpus integer not null default 0 check (new_to_corpus >= 0),
  changed integer not null default 0 check (changed >= 0),
  unchanged_by_hash integer not null default 0 check (unchanged_by_hash >= 0),
  requests integer not null default 0 check (requests >= 0),
  elapsed_ms integer not null default 0 check (elapsed_ms >= 0),
  error text not null default ''
);

comment on table public.job_hunter_source_crawls is
  'One row per crawl attempt, written unconditionally -- especially for the '
  'attempt that produced nothing. `outcome` is what stops a stalled source '
  'or a schedule that never fired from presenting as "nothing new today" '
  '(issue #184).';

-- The scheduler's only read: this source's recent crawls, newest first.
create index job_hunter_source_crawls_recent_idx
  on public.job_hunter_source_crawls (source_key, started_at desc);

-- The crawl cursor ---------------------------------------------------------
--
-- Held apart from job_hunter_sources because the two take different policy
-- shapes: the registry is knowledge a user may read, this is hot machinery
-- nobody reads. Folding them would force one shape onto both.
--
-- For most of the current portfolio -- remotive, arbeitnow, jobicy,
-- himalayas, remoteok, weworkremotely, lever, greenhouse, ashby -- there is
-- no pagination cursor to resume from: the whole board arrives in one GET.
-- For those the honest cursor is the HTTP validator, which is what makes the
-- *next* fetch conditional and therefore cheap. `high_water_at` serves the
-- sources that accept a since-style parameter. Inventing a synthetic cursor
-- for a source that has none would satisfy a checkbox and change no cost.
create table public.job_hunter_source_cursors (
  source_key text primary key,
  etag text not null default '',
  last_modified text not null default '',
  high_water_at timestamptz,
  updated_at timestamptz not null default now()
);

comment on table public.job_hunter_source_cursors is
  'Where each source resumes from: its HTTP cache validators, and a '
  'high-water timestamp for the sources that accept one (issue #184).';

alter table public.job_hunter_source_crawls enable row level security;
alter table public.job_hunter_source_cursors enable row level security;

revoke all on table public.job_hunter_source_crawls
  from public, anon, authenticated, service_role;
revoke all on table public.job_hunter_source_cursors
  from public, anon, authenticated, service_role;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm db:reset && pnpm db:test`
Expected: PASS — 17/17

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/29999999000000_job_hunter_source_registry.sql supabase/tests/pgtap/job_hunter_source_registry.sql
git commit -m "feat(job-hunter): record per-source crawl novelty and cursors (#184)"
```

---

### Task 6: Stop per-source schedules collapsing into one

`job_hunter_schedule_stage_enqueue` derives its `pg_cron` job name from the stage alone. `cron.schedule` replaces by name, so N per-source schedules install under one name and each silently replaces the last: exactly one source is ever visited, and it presents as a quiet job market rather than an error. The existing pgTAP asserts that literal name and therefore locks the defect in.

**Files:**
- Modify: `supabase/migrations/29999999000000_job_hunter_source_registry.sql` (append)
- Modify: `supabase/tests/pgtap/job_hunter_stage_queues.sql:155–166`
- Modify: `supabase/tests/pgtap/job_hunter_store_functions.sql:150–172`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `public.job_hunter_source_schedule_slug(p_key text) returns text`.
  - `public.job_hunter_schedule_stage_enqueue(p_stage text, p_schedule text, p_payload jsonb default '{}', p_schedule_key text default null) returns bigint` — job name is `job-hunter-enqueue-<stage>` when `p_schedule_key` is null, `job-hunter-enqueue-<stage>-<slug>` otherwise.

- [ ] **Step 1: Write the failing test**

In `supabase/tests/pgtap/job_hunter_stage_queues.sql`, replace the `-- pg_cron only enqueues` block (currently lines 155–166) with:

```sql
-- pg_cron only enqueues, and one schedule per source ------------------------

select lives_ok(
  $$ select public.job_hunter_schedule_stage_enqueue(
       'crawl_source', '* * * * *', '{"source_key":"remotive"}'::jsonb,
       'remotive') $$,
  'a stage enqueue can be scheduled for one source');
select lives_ok(
  $$ select public.job_hunter_schedule_stage_enqueue(
       'crawl_source', '0 * * * *', '{"source_key":"lever:acme"}'::jsonb,
       'lever:acme') $$,
  'and for a second source');

-- The regression this signature exists to prevent. cron.schedule replaces by
-- name, so a job name derived from the stage alone means every per-source
-- schedule overwrites the last and exactly one source is ever crawled -- with
-- no error anywhere, presenting as a quiet job market.
select is(
  (select count(*)::int from cron.job
    where jobname like 'job-hunter-enqueue-crawl-source-%'),
  2,
  'two sources produce two cron entries rather than replacing each other');
select is(
  (select jobname from cron.job
    where command like '%lever:acme%'),
  'job-hunter-enqueue-crawl-source-lever-acme',
  'a source key with punctuation becomes a usable job name');

select alike(
  (select command from cron.job
    where jobname = 'job-hunter-enqueue-crawl-source-remotive'),
  'select pgmq.send(%',
  'the scheduled command only sends a queue message');
select unalike(
  (select command from cron.job
    where jobname = 'job-hunter-enqueue-crawl-source-remotive'),
  '%job_hunter_merge_posting_batch%',
  'pg_cron performs no resolve_persist work');

-- Omitting the key keeps the stage-wide name, for a stage with one schedule.
select lives_ok(
  $$ select public.job_hunter_schedule_stage_enqueue(
       'recheck_freshness', '0 4 * * *', '{}'::jsonb) $$,
  'a stage with a single schedule needs no key');
select is(
  (select count(*)::int from cron.job
    where jobname = 'job-hunter-enqueue-recheck-freshness'),
  1,
  'and keeps the stage-wide job name');
```

Update this file's `select plan(N);` at the top by `+4` (six assertions replace four; count the file's current plan and adjust).

Also update the two `has_function_privilege` assertions at lines 104 and 109 to the new signature:

```sql
select is_empty(
  $$ select 1 where has_function_privilege('authenticated',
       'public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text)',
       'execute') $$,
  'scheduling an enqueue is not a function a user may call');
select is_empty(
  $$ select 1 where has_function_privilege('anon',
       'public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text)',
       'execute') $$,
  'nor may anon');
```

In `supabase/tests/pgtap/job_hunter_store_functions.sql`, add **one** entry to the expected-function array — `job_hunter_source_schedule_slug`, in alphabetical position between `job_hunter_set_job_markets` and `job_hunter_stage_queue_metrics`:

```sql
    'job_hunter_set_job_markets',
    'job_hunter_source_schedule_slug',
    'job_hunter_stage_queue_metrics',
```

and step the count word by one. Task 4 already took it from "twenty-seven" to **"twenty-eight"** by adding `job_hunter_posting_display_credit`, so this task takes it to **"twenty-nine"**. Task 10 adds `job_hunter_reschedule_sources` and takes it to "thirty". **Read the current word in the file rather than trusting this paragraph** — three tasks step the same counter and whichever runs last is right.

Neither function this task adds is `security definer`, so the definer inventories in `job_hunter_store_functions.sql` and `job_hunter_shared_writes.sql` do not change.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm db:test`
Expected: FAIL — `function public.job_hunter_schedule_stage_enqueue(unknown, unknown, jsonb, unknown) does not exist`

- [ ] **Step 3: Write minimal implementation**

Append to `supabase/migrations/29999999000000_job_hunter_source_registry.sql`:

```sql
-- One cron entry per source, not one per stage -----------------------------
--
-- #183 shipped job_hunter_schedule_stage_enqueue deriving its cron job name
-- from the stage alone:
--
--   cron.schedule('job-hunter-enqueue-' || replace(p_stage, '_', '-'), ...)
--
-- cron.schedule replaces by name. This ticket installs one schedule per
-- source, so under that name every schedule would overwrite the last and
-- exactly one source would ever be crawled. Nothing raises; the corpus just
-- stops growing, which reads as a quiet job market. The pgTAP asserting the
-- literal stage-derived name locked it in, so the fix is the signature and
-- the assertion together.
create or replace function public.job_hunter_source_schedule_slug(p_key text)
returns text
language sql
immutable
set search_path = ''
as $$
  -- Lower-cased, punctuation collapsed to single hyphens, trimmed. Long keys
  -- keep a hash tail so two that share a prefix cannot land on one job name
  -- after truncation -- which would reintroduce the collapse this fixes.
  select case
           when length(v.slug) <= 40 then v.slug
           else left(v.slug, 31) || '-' ||
                left(encode(sha256(convert_to(p_key, 'UTF8')), 'hex'), 8)
         end
    from (
      select trim(both '-' from
               regexp_replace(lower(coalesce(p_key, '')), '[^a-z0-9]+', '-', 'g')
             ) as slug
    ) v;
$$;

comment on function public.job_hunter_source_schedule_slug(text) is
  'A source key rendered as a pg_cron job-name fragment: lower case, '
  'punctuation collapsed, hashed tail past 40 characters so two long keys '
  'cannot collide (issue #184).';

drop function if exists public.job_hunter_schedule_stage_enqueue(text, text, jsonb);

create or replace function public.job_hunter_schedule_stage_enqueue(
  p_stage text,
  p_schedule text,
  p_payload jsonb default '{}'::jsonb,
  p_schedule_key text default null
)
returns bigint
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_queue_name text;
  v_job_name text;
  v_job_id bigint;
begin
  v_queue_name := case p_stage
    when 'crawl_source' then 'job_hunter_crawl_source'
    when 'resolve_persist' then 'job_hunter_resolve_persist'
    when 'extract_facets' then 'job_hunter_extract_facets'
    when 'recheck_freshness' then 'job_hunter_recheck_freshness'
    else null
  end;

  if v_queue_name is null then
    raise exception 'unknown job hunter stage: %', p_stage
      using errcode = '22023';
  end if;
  if jsonb_typeof(p_payload) <> 'object' then
    raise exception 'stage payload must be a JSON object'
      using errcode = '22023';
  end if;

  v_job_name := 'job-hunter-enqueue-' || replace(p_stage, '_', '-');
  if p_schedule_key is not null then
    if public.job_hunter_source_schedule_slug(p_schedule_key) = '' then
      raise exception 'schedule key % has no usable job-name form', p_schedule_key
        using errcode = '22023';
    end if;
    v_job_name := v_job_name || '-'
      || public.job_hunter_source_schedule_slug(p_schedule_key);
  end if;

  select cron.schedule(
    v_job_name,
    p_schedule,
    format('select pgmq.send(%L, %L::jsonb);', v_queue_name, p_payload::text)
  ) into v_job_id;
  return v_job_id;
end;
$$;

comment on function public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text) is
  'Store a pg_cron schedule whose whole command is one pgmq.send. The cron '
  'session enqueues due work and never performs stage work itself (#183). '
  'p_schedule_key names one schedule within a stage: cron.schedule replaces '
  'by name, so without it N per-source schedules collapse into one and '
  'exactly one source is ever visited (#184).';

revoke all on function
  public.job_hunter_schedule_stage_enqueue(text, text, jsonb, text)
  from public, anon, authenticated, service_role;
revoke all on function public.job_hunter_source_schedule_slug(text)
  from public, anon, authenticated, service_role;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm db:reset && pnpm db:test`
Expected: PASS — `job_hunter_stage_queues.sql` and `job_hunter_store_functions.sql` both green.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/29999999000000_job_hunter_source_registry.sql supabase/tests/pgtap/job_hunter_stage_queues.sql supabase/tests/pgtap/job_hunter_store_functions.sql
git commit -m "fix(job-hunter): stop per-source cron schedules replacing each other (#184)"
```

---

### Task 7: The search budget becomes a platform ledger

The owner's decision on #184: the quota is a property of the API key, not of a person. This moves the table, its uniqueness rule, its index, and its position in the enforced private-data inventory, and carries this month's already-spent count across.

**Files:**
- Modify: `supabase/migrations/29999999000000_job_hunter_source_registry.sql` (append)
- Modify: `supabase/tests/pgtap/job_hunter_isolation.sql` (remove `job_hunter_search_api_usage` from the table list at **line 74** and its seed `when` arm at **lines 172–173**)
- Modify: `supabase/tests/pgtap/job_hunter_write_idempotency.sql` — **two** places, not one: **lines 22–23** and **lines 51–52**
- Modify: `apps/job-hunter/tests/conftest.py` — the cleanup list entry is at **line 165**
- Modify: `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py` — `_migrate_search_api_usage` (around line 657) and its call site (around line 213) write to the table this task drops
- Modify: `docs/positioning.md:138` — names the table in a list of per-user tables

> **Do a fresh sweep before you start:** `grep -rln job_hunter_search_api_usage --include='*.py' --include='*.sql' --include='*.md' . | grep -v node_modules`. Dropping a table is only complete when nothing references it. The two entries above were found that way and were missing from the first draft of this plan — there may be more by the time this task runs.

> Line numbers re-derived from `origin/main` at `a975945` (the #179 merge), which moved several of them. Verify with `grep -n job_hunter_search_api_usage` before editing rather than trusting these — #179's own hunks shifted this file and another merge may shift it again.
- Modify: `supabase/tests/pgtap/job_hunter_source_registry.sql` (append; raise `plan(17)` to `plan(22)`)

**Interfaces:**
- Consumes: nothing.
- Produces: `public.job_hunter_platform_search_usage (id uuid, provider text, occurred_at timestamptz, created_at timestamptz)`, unique on `(provider, occurred_at)`, with `runner_select` / `runner_insert` / `runner_update` policies on the `job_hunter_runner` JWT claim and no delete policy.

- [ ] **Step 1: Write the failing test**

Append to `supabase/tests/pgtap/job_hunter_source_registry.sql` and change `plan(17)` to `plan(22)`:

```sql
-- The search allowance is a property of the key, not of a person -----------

select has_table('public', 'job_hunter_platform_search_usage',
  'the platform search ledger exists');
select hasnt_table('public', 'job_hunter_search_api_usage',
  'and the per-user table it replaces is gone');

select col_is_unique(
  'public', 'job_hunter_platform_search_usage', array['provider', 'occurred_at'],
  'the platform ledger converges a retried write on (provider, occurred_at)');

select is_empty(
  $$ select 1 from pg_policy
      where polrelid = 'public.job_hunter_platform_search_usage'::regclass
        and polcmd = 'd' $$,
  'a ledger that can be rewritten is not a ledger: no delete policy');

select is(
  (select count(*)::int from pg_policy
    where polrelid = 'public.job_hunter_platform_search_usage'::regclass),
  3,
  'select, insert and update only, all on the runner claim');
```

In `supabase/tests/pgtap/job_hunter_isolation.sql`, delete the line `  'job_hunter_search_api_usage',` from the `pg_temp.job_hunter_tables` view and delete its `when 'job_hunter_search_api_usage' then ... ` branch from `pg_temp.job_hunter_seed_row`. Adjust that file's `plan(N)` down by however many assertions per table it makes (check the multiplier at the top of the file).

In `supabase/tests/pgtap/job_hunter_write_idempotency.sql` there are **two** references, and both change. Lines 51–52:

```sql
select col_is_unique(
  'public', 'job_hunter_platform_search_usage', array['provider', 'occurred_at'],
  'platform_search_usage enforces unique (provider, occurred_at)'
);
```

And lines 22–23, whose assertion message is the claim this ticket falsifies — it currently reads `'search_api_usage has a user-scoped natural key'`, and after this change the key is deliberately not user-scoped:

```sql
select has_index(
  'public', 'job_hunter_platform_search_usage',
  'job_hunter_platform_search_usage_provider_at_key',
  'platform_search_usage has a provider-scoped natural key, not a user-scoped one'
);
```

Name the constraint to match when you create it in the migration — add `constraint job_hunter_platform_search_usage_provider_at_key unique (provider, occurred_at)` rather than the bare `unique (provider, occurred_at)` shown earlier, so this assertion has a name to find.

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm db:test`
Expected: FAIL — `relation "public.job_hunter_platform_search_usage" does not exist`

- [ ] **Step 3: Write minimal implementation**

Append to `supabase/migrations/29999999000000_job_hunter_source_registry.sql`:

```sql
-- The external search allowance becomes a platform ledger ------------------
--
-- Owner's decision on #184. job_hunter_search_api_usage was per-user, but a
-- shared crawl runs as the privileged role with no user identity and then
-- the budget has no user to charge. A per-user ledger has exactly two
-- possible behaviours and both are wrong: charge one arbitrary user for
-- everyone's crawl, or fan the crawl out per user and burn the same cap N
-- times for identical results. The quota belongs to the API key -- Adzuna's
-- is 2,500 calls a month on the key -- so the budget scales with postings
-- rather than with subscribers, which is the same shape as shared
-- extraction.
--
-- Keeping both ledgers was explicitly rejected: two ledgers over one key is
-- a bug already live on the Gemini side, where two of them jointly authorise
-- about 160% of the key's real quota. Per-user search accounting returns if
-- and when a user-triggered search exists, and not before.
--
-- Shape copied from job_hunter_platform_ai_usage: the runner claim, and no
-- delete policy, because a ledger that can be rewritten is not a ledger.
create table public.job_hunter_platform_search_usage (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  occurred_at timestamptz not null,
  created_at timestamptz not null default now(),
  constraint job_hunter_platform_search_usage_provider_at_key
    unique (provider, occurred_at)
);

create index job_hunter_platform_search_usage_window_idx
  on public.job_hunter_platform_search_usage (provider, occurred_at desc);

comment on table public.job_hunter_platform_search_usage is
  'Consumption of the platform-owned external search keys (issue #184). '
  'Deliberately not keyed by user_id: the key has one allowance and the '
  'crawl it pays for belongs to no one user.';

alter table public.job_hunter_platform_search_usage enable row level security;

create policy runner_select on public.job_hunter_platform_search_usage
  for select to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
create policy runner_insert on public.job_hunter_platform_search_usage
  for insert to authenticated
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);
-- Update is what makes the ledger's upsert converge rather than fail on a
-- duplicate key; it is not an invitation to rewrite history.
create policy runner_update on public.job_hunter_platform_search_usage
  for update to authenticated
  using (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb)
  with check (coalesce((select auth.jwt() -> 'job_hunter_runner'), 'false'::jsonb) = 'true'::jsonb);

-- Carry this month's spend across.
--
-- The owner's call, and the reason: the Brave monthly cap is drawn against
-- the key, so calls already spent this month are real spend no matter whose
-- row recorded them. Starting the platform ledger empty would hand the
-- engine a fresh 1,000-query allowance on a key already drawn down.
--
-- Distinct users who happened to record the same occurred_at collapse to one
-- row, which is correct: the ledger counts calls against the key, and two
-- rows at one microsecond were one reservation being retried.
insert into public.job_hunter_platform_search_usage (provider, occurred_at, created_at)
select provider, occurred_at, min(created_at)
  from public.job_hunter_search_api_usage
 group by provider, occurred_at
on conflict (provider, occurred_at) do nothing;

drop table public.job_hunter_search_api_usage;
```

Then repoint the one-time SQLite import at the new table. `apps/job-hunter/scripts/migrate_sqlite_to_postgres.py` has `_migrate_search_api_usage`, which upserts rows carrying a `user_id` into `job_hunter_search_api_usage`. Change it to write `job_hunter_platform_search_usage` with `{"provider": …, "occurred_at": …}` and `on_conflict="provider,occurred_at"`, dropping the `user_id` it currently sends — the legacy SQLite database was single-user, so its rows collapse onto the provider key without loss. Update the module docstring's list of unconditionally-migrated tables (around line 52) and the counts key to match, and run `tests/test_migrate_sqlite_to_postgres.py` — it asserts on that script's behaviour and will go red otherwise.

Then correct `docs/positioning.md:138`, which lists the table among per-user tables. After this task it is not one.

Then, in the same task, remove the dropped table from the test fixture's cleanup walk — `apps/job-hunter/tests/conftest.py`: delete the line `    "job_hunter_search_api_usage",` from `_TABLES_CHILD_FIRST`, and remove `search_api_usage,` from the comment block above it at line 134.

This belongs here rather than in Task 8: the drop and the fixture that walks the table are one change. Split across two commits, the branch spends a commit with every integration test erroring on a table that no longer exists — and this task's verification runs only `supabase test db`, so it would not notice.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm db:reset && pnpm db:test`
Expected: PASS — all pgTAP files green, including `job_hunter_isolation.sql` with one fewer table.

- [ ] **Step 5: Commit**

```bash
git add supabase/migrations/29999999000000_job_hunter_source_registry.sql supabase/tests/pgtap/
git commit -m "feat(job-hunter): make the search allowance a platform ledger (#184)"
```

---

### Task 8: Point `search_budget.py` at the platform ledger — ABSORBED INTO TASK 7

> **This task no longer exists as a separate step.** Task 7's implementer did this work in the same commit as the table drop, and that was the right call: splitting a table drop from its only writer leaves the branch holding a commit where the ledger writes a table that does not exist. Same argument as the `conftest.py` cleanup-list edit. The steps below are kept as the record of what was required; do not dispatch them separately.
>
> Task 7 also absorbed a consequence neither task anticipated: removing `user_id` removed the test isolation that column was silently providing, because `conftest.py`'s cleanup walk deletes by user id and `SearchUsageLedger.count()` was row-level-security filtered. The general fix — a cleanup path for platform-owned tables with no `user_id` — landed there too.

**Files:**
- Modify: `apps/job-hunter/src/job_hunter/search_budget.py:34,36–75,231–240`
- Modify: `apps/job-hunter/tests/test_brave_budget.py:21,108`

> `conftest.py`'s cleanup list is edited in Task 7, alongside the drop it follows from — not here.

**Interfaces:**
- Consumes: `public.job_hunter_platform_search_usage`.
- Produces: `SearchUsageLedger.record(*, provider, occurred_at)` and `.count(*, provider, start_at, end_at)` with identical signatures to today — only the row shape changes, so `BraveRequestBudget` and every caller are untouched.

- [ ] **Step 1: Write the failing test**

In `apps/job-hunter/tests/test_brave_budget.py`, add:

```python
def test_the_ledger_writes_no_user_id():
    """The allowance belongs to the API key; a row naming a user is wrong."""
    client = _RecordingClient()
    ledger = SearchUsageLedger(client)
    ledger.record(provider="brave", occurred_at=datetime(2026, 9, 9, tzinfo=timezone.utc))
    table, rows, on_conflict = client.upserts[0]
    assert table == "job_hunter_platform_search_usage"
    assert on_conflict == "provider,occurred_at"
    assert "user_id" not in rows[0]
```

Add the recording double near the top of the file if one is not already present:

```python
class _RecordingClient:
    user_id = "11111111-0000-0000-0000-00000000000a"

    def __init__(self, rows=None):
        self.upserts = []
        self._rows = rows or []

    def upsert(self, table, rows, on_conflict=None):
        self.upserts.append((table, rows, on_conflict))

    def select(self, table, params=None):
        return self._rows
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm job-hunter:test -- tests/test_brave_budget.py::test_the_ledger_writes_no_user_id -v`
Expected: FAIL — `assert 'job_hunter_search_api_usage' == 'job_hunter_platform_search_usage'`

- [ ] **Step 3: Write minimal implementation**

(The `conftest.py` cleanup-list edit was made in Task 7. Verify it is already gone rather than editing it again.)

In `apps/job-hunter/src/job_hunter/search_budget.py`, change `_TABLE` and `record`:

```python
_TABLE = "job_hunter_platform_search_usage"
```

```python
    def record(self, *, provider: str, occurred_at: datetime) -> None:
        occurred_at = _normalize_utc(occurred_at)
        self._client.upsert(
            _TABLE,
            [
                {
                    "provider": provider,
                    "occurred_at": to_iso(occurred_at),
                }
            ],
            on_conflict="provider,occurred_at",
        )
```

Update the class docstring:

```python
class SearchUsageLedger:
    """Metered ledger for external search API requests, one row per request.

    Keyed on the provider and the moment, not on a user: the quota belongs
    to the API key, and under #184's shared crawl there is no user to charge
    (issue #184, owner's decision). The `(provider, occurred_at)` unique
    constraint is what makes `record`'s upsert converge instead of
    duplicating a retried write.
    """
```

Update the `reserve` docstring reference from `(user_id, provider, occurred_at)` to `(provider, occurred_at)` and from `job_hunter_search_api_usage` to `job_hunter_platform_search_usage`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm job-hunter:test -- tests/test_brave_budget.py -v`
Expected: PASS

Run the whole suite: `pnpm job-hunter:test`
Expected: PASS with **no** skips attributable to a missing stack — confirm the `SUPABASE_TEST_*` variables are exported by checking the summary line reports integration tests as run, not skipped.

- [ ] **Step 5: Commit**

```bash
git add apps/job-hunter/src/job_hunter/search_budget.py apps/job-hunter/tests/test_brave_budget.py
git commit -m "refactor(job-hunter): charge search calls to the key, not a user (#184)"
```

---

### Task 9: The `crawl_source` stage handler

One source, one message, one crawl. Mirrors `resolve_persist.py`: no user-scoped store, no matching, no scoring, no credentials, so a worker holding only the privileged connection can run it.

**Files:**
- Create: `apps/job-hunter/src/job_hunter/crawl_source.py`
- Create: `apps/job-hunter/tests/test_crawl_source.py`
- Modify: `apps/job-hunter/src/job_hunter/sources/__init__.py`

**Interfaces:**
- Consumes: `source_key_for` (Task 1), `NOT_MODIFIED` / `Validators` (Task 2), `job_hunter_source_crawls` / `job_hunter_source_cursors` (Task 5).
- Produces:
  - `CrawlOutcome` — `@dataclass(frozen=True)` with `source_key: str`, `outcome: str`, `fetched: int`, `new_to_corpus: int`, `changed: int`, `unchanged_by_hash: int`.
  - `CrawlSourceStage(database, *, build_source: Callable[[str], JobSource], persist: Callable[[list[Job]], PostingBatch], probe: Callable[[JobSource, Validators], object] | None = None)` — callable on a `QueueMessage`, returns `CrawlOutcome`. `persist` **must return** the `PostingBatch` from `resolve_persist`; its `newly_discovered` is what the scheduler bands on.
  - `description_hash(description: str) -> str` — `sha256` hex, matching `job_hunter_upsert_posting`'s `encode(sha256(convert_to(description, 'UTF8')), 'hex')`.

- [ ] **Step 1: Write the failing test**

Create `apps/job-hunter/tests/test_crawl_source.py`:

```python
from __future__ import annotations

import pytest

from job_hunter.crawl_source import CrawlSourceStage, description_hash
from job_hunter.models import Job
from job_hunter.stage_queue import PermanentStageFailure, QueueMessage, Stage


class _FakeCursor:
    def __init__(self, database):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._database.executed.append((sql, params))
        if "job_hunter_source_cursors" in sql and sql.strip().startswith("select"):
            self._rows = [(self._database.etag, "", None)]
        elif "description_hash" in sql:
            self._rows = list(self._database.known_hashes)
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    def __init__(self, database):
        self._database = database

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._database)


class _FakeDatabase:
    def __init__(self, *, etag="", known_hashes=()):
        self.etag = etag
        self.known_hashes = known_hashes
        self.executed: list = []

    def connection(self):
        return _FakeConnection(self)


def _job(source: str, source_job_id: str, description: str) -> Job:
    return Job(
        source=source,
        source_job_id=source_job_id,
        title="Engineer",
        company="Acme",
        location="Remote",
        url=f"https://example.test/{source_job_id}",
        description=description,
    )


class _StubSource:
    source_label = "remotive"
    source_key = "remotive"

    def __init__(self, jobs, *, raises=None):
        self._jobs = jobs
        self._raises = raises

    def discover(self):
        if self._raises is not None:
            raise self._raises
        yield from self._jobs


def _message(source_key="remotive") -> QueueMessage:
    return QueueMessage(
        stage=Stage.CRAWL_SOURCE, message_id=1, payload={"source_key": source_key}
    )


def test_a_listing_whose_hash_is_unchanged_never_reaches_persistence():
    """Criterion 3. The hash check must happen before staging, not inside the upsert."""
    unchanged = _job("remotive", "1", "same words")
    fresh = _job("remotive", "2", "new words")
    from job_hunter.normalize import job_fingerprint

    database = _FakeDatabase(
        known_hashes=[(job_fingerprint(unchanged), description_hash("same words"))]
    )
    persisted: list[list[Job]] = []

    def _persist(jobs):
        persisted.append(jobs)
        return None

    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([unchanged, fresh]),
        persist=_persist,
    )
    outcome = stage(_message())

    assert [job.source_job_id for job in persisted[0]] == ["2"]
    assert outcome.unchanged_by_hash == 1
    assert outcome.fetched == 2


def test_an_unchanged_board_records_not_modified_rather_than_an_empty_fetch():
    """Criterion 2, and the charter rule that an empty result carries its reason."""
    from job_hunter.http import NOT_MODIFIED

    seen: list = []
    database = _FakeDatabase(etag='"abc"')
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([]),
        persist=lambda jobs: seen.append(jobs),
        probe=lambda source, validators: NOT_MODIFIED,
    )
    outcome = stage(_message())

    assert outcome.outcome == "not_modified"
    assert outcome.fetched == 0
    assert seen == [], "a 304 must cost no downstream work at all"


def test_a_zero_job_fetch_is_not_reported_as_not_modified():
    """A board that answered in full and had nothing is a different fact."""
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    outcome = stage(_message())
    assert outcome.outcome == "fetched"
    assert outcome.fetched == 0


def test_new_to_corpus_comes_back_from_the_persist_call():
    """The scheduler bands on novelty, so the count must survive the round trip."""
    from job_hunter.resolve_persist import PostingBatch

    fresh = _job("remotive", "2", "new words")
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([fresh]),
        persist=lambda jobs: PostingBatch(
            posting_ids={"fp": "id"}, newly_discovered=1
        ),
    )
    outcome = stage(_message())
    assert outcome.new_to_corpus == 1
    assert outcome.changed == 1


def test_a_rate_limited_source_records_rate_limited_and_does_not_raise():
    """Criterion 5. This source stalls; nothing else may be affected."""
    import requests

    response = requests.Response()
    response.status_code = 429
    error = requests.HTTPError(response=response)

    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([], raises=error),
        persist=lambda jobs: None,
    )
    outcome = stage(_message())

    assert outcome.outcome == "rate_limited"


def test_a_failing_source_records_failed():
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _StubSource([], raises=RuntimeError("boom")),
        persist=lambda jobs: None,
    )
    assert stage(_message()).outcome == "failed"


def test_the_wrong_stage_is_a_permanent_failure():
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    wrong = QueueMessage(
        stage=Stage.RESOLVE_PERSIST, message_id=1, payload={"source_key": "remotive"}
    )
    with pytest.raises(PermanentStageFailure):
        stage(wrong)


def test_an_unexpected_payload_key_is_a_permanent_failure():
    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database, build_source=lambda key: _StubSource([]), persist=lambda jobs: None
    )
    message = QueueMessage(
        stage=Stage.CRAWL_SOURCE,
        message_id=1,
        payload={"source_key": "remotive", "user_id": "someone"},
    )
    with pytest.raises(PermanentStageFailure):
        stage(message)


def test_the_crawl_row_carries_what_the_crawl_cost():
    """`requests` is the cost half of the yield figure; always-zero is a lie."""

    class _CountingHttp:
        request_count = 0

    http = _CountingHttp()

    class _RequestingSource:
        source_label = "remotive"
        source_key = "remotive"

        def discover(self):
            http.request_count += 3
            yield from ()

    database = _FakeDatabase()
    stage = CrawlSourceStage(
        database,
        build_source=lambda key: _RequestingSource(),
        persist=lambda jobs: None,
        http=http,
    )
    assert stage(_message()).requests == 3


def test_description_hash_matches_the_sql_definition():
    """job_hunter_upsert_posting computes sha256 over the UTF-8 description."""
    assert description_hash("hello") == (
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm job-hunter:test -- tests/test_crawl_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'job_hunter.crawl_source'`

- [ ] **Step 3: Write minimal implementation**

Create `apps/job-hunter/src/job_hunter/crawl_source.py`:

```python
"""The privileged, user-free ``crawl_source`` stage (issue #184).

One source, one message. A source that is rate-limited or failing stalls
only itself, because it is a message on a queue rather than a step in a
shared run, and its cadence backs off without touching any other source's.

Imports nothing user-scoped -- no store, no matching, no scoring, no
credentials -- so a worker holding only the privileged ingestion connection
can run it, exactly as `resolve_persist.py` can (issue #183, constraint C1).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import requests

from .http import NOT_MODIFIED, Validators
from .normalize import job_fingerprint
from .stage_queue import PermanentStageFailure, QueueMessage, Stage

logger = logging.getLogger(__name__)


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class CrawlOutcome:
    """What one crawl of one source produced, with no user dimension."""

    source_key: str
    outcome: str
    fetched: int = 0
    new_to_corpus: int = 0
    changed: int = 0
    unchanged_by_hash: int = 0
    requests: int = 0
    elapsed_ms: int = 0
    error: str = ""


def description_hash(description: str) -> str:
    """Hash a description the way `job_hunter_upsert_posting` does.

    That function computes `encode(sha256(convert_to(description, 'UTF8')),
    'hex')`. Computing the same value here is the whole point of the
    short-circuit: the SQL hash is produced *during* the upsert, which is far
    too late to prevent the work the upsert is doing.
    """
    return hashlib.sha256((description or "").encode("utf-8")).hexdigest()


def _is_rate_limited(error: BaseException) -> bool:
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None) == 429


class CrawlSourceStage:
    """Crawl one source, drop what has not changed, stage the rest."""

    def __init__(
        self,
        database: _ConnectionLease,
        *,
        build_source: Callable[[str], Any],
        persist: Callable[[list], Any],
        probe: Callable[[Any, Validators], Any] | None = None,
        http: Any | None = None,
    ) -> None:
        self._database = database
        self._build_source = build_source
        self._persist = persist
        self._probe = probe
        # The cost half of the yield figure. `HttpClient` counts every attempt
        # it makes, retries included, so bracketing the drain attributes the
        # requests to this source the way `discovery.collect_candidates`
        # already does for the per-run statistics. Optional only because the
        # unit tests construct the stage without one; a real crawl always has
        # a client, and a `requests` column that is always zero would read as
        # measured while telling nobody anything.
        self._http = http

    def _requests_since(self, before: int) -> int:
        if self._http is None:
            return 0
        return max(0, getattr(self._http, "request_count", 0) - before)

    def __call__(self, message: QueueMessage) -> CrawlOutcome:
        source_key = self._source_key(message)
        started = time.monotonic()
        requests_before = getattr(self._http, "request_count", 0) if self._http else 0

        cursor = self._read_cursor(source_key)
        source = self._build_source(source_key)

        if self._probe is not None and self._probe(source, cursor) is NOT_MODIFIED:
            outcome = CrawlOutcome(
                source_key=source_key,
                outcome="not_modified",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            self._record(outcome)
            return outcome

        try:
            jobs = list(source.discover())
        except BaseException as error:  # noqa: BLE001 - classified, then recorded
            outcome = CrawlOutcome(
                source_key=source_key,
                outcome="rate_limited" if _is_rate_limited(error) else "failed",
                requests=self._requests_since(requests_before),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=str(error)[:500],
            )
            logger.warning(
                "crawl_source %s ended %s: %s", source_key, outcome.outcome, error
            )
            self._record(outcome)
            return outcome

        fresh, unchanged = self._drop_unchanged(jobs)
        # `persist` returns resolve_persist's PostingBatch, whose
        # `newly_discovered` is the only place the count of postings the
        # corpus did not already hold exists. The scheduler bands on exactly
        # that number, so it has to survive the round trip rather than being
        # inferred from `changed` -- a source re-advertising the same job with
        # an edited description is changed but not new, and a source that only
        # ever does that should not earn a faster band.
        batch = self._persist(fresh) if fresh else None

        outcome = CrawlOutcome(
            source_key=source_key,
            outcome="fetched",
            fetched=len(jobs),
            new_to_corpus=getattr(batch, "newly_discovered", 0) or 0,
            changed=len(fresh),
            unchanged_by_hash=unchanged,
            requests=self._requests_since(requests_before),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        self._record(outcome)
        return outcome

    def _drop_unchanged(self, jobs: list) -> tuple[list, int]:
        """Keep only the listings whose description the corpus does not have.

        This is acceptance criterion 3, and it has to happen here rather than
        in SQL: the description hash is computed inside
        `job_hunter_upsert_posting`, by which point the row has already been
        staged, copied and merged.
        """
        if not jobs:
            return [], 0
        by_fingerprint = {job_fingerprint(job): job for job in jobs}
        known = self._known_hashes(list(by_fingerprint))
        fresh = []
        unchanged = 0
        for fingerprint, job in by_fingerprint.items():
            if known.get(fingerprint) == description_hash(job.description):
                unchanged += 1
                continue
            fresh.append(job)
        return fresh, unchanged

    def _known_hashes(self, fingerprints: list[str]) -> dict[str, str]:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select fingerprint, description_hash "
                    "from public.job_hunter_postings "
                    "where fingerprint = any(%s)",
                    (fingerprints,),
                )
                rows = cursor.fetchall()
        return {fingerprint: hashed for fingerprint, hashed in rows}

    def _read_cursor(self, source_key: str) -> Validators:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select etag, last_modified, high_water_at "
                    "from public.job_hunter_source_cursors where source_key = %s",
                    (source_key,),
                )
                row = cursor.fetchone()
        if row is None:
            return Validators()
        return Validators(etag=row[0] or "", last_modified=row[1] or "")

    def _record(self, outcome: CrawlOutcome) -> None:
        """Write the crawl row unconditionally, including the empty ones.

        An empty result has to carry its reason: a stalled source and a
        schedule that never fired must not both present as "nothing new
        today". Failing to record is logged and never raised -- the crawl
        already happened, and losing its telemetry must not also lose its
        output.
        """
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_source_crawls "
                        "(source_key, finished_at, outcome, fetched, "
                        " new_to_corpus, changed, unchanged_by_hash, "
                        " requests, elapsed_ms, error) "
                        "values (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s)",
                        (
                            outcome.source_key,
                            outcome.outcome,
                            outcome.fetched,
                            outcome.new_to_corpus,
                            outcome.changed,
                            outcome.unchanged_by_hash,
                            outcome.requests,
                            outcome.elapsed_ms,
                            outcome.error,
                        ),
                    )
        except Exception:
            logger.exception(
                "could not record the crawl of %s; its cadence will not move",
                outcome.source_key,
            )

    @staticmethod
    def _source_key(message: QueueMessage) -> str:
        if message.stage is not Stage.CRAWL_SOURCE:
            raise PermanentStageFailure("crawl_source received the wrong stage")
        if set(message.payload) != {"source_key"}:
            raise PermanentStageFailure(
                "crawl_source payload must contain only source_key"
            )
        source_key = message.payload.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise PermanentStageFailure("crawl_source source_key must be a string")
        return source_key
```

In `apps/job-hunter/src/job_hunter/sources/__init__.py`, add below `build_sources` and to `__all__`:

```python
def build_source(
    settings: Settings,
    http,
    source_key: str,
    **kwargs,
) -> JobSource:
    """Build the one source `source_key` names.

    The `crawl_source` stage handles one source per message, so constructing
    the whole portfolio to reach one of them would make every crawl pay for
    every other source's setup -- including the Brave budget read that
    `build_sources` performs before it can decide whether to add a targeted
    search source at all.
    """
    from .base import source_key_for

    for source in build_sources(settings, http, **kwargs):
        if source_key_for(source) == source_key:
            return source
    raise KeyError(f"no source is configured for key {source_key!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm job-hunter:test -- tests/test_crawl_source.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Write the integration test against a real connection**

The fakes above verify the classification logic and nothing else. `_known_hashes`, `_read_cursor` and `_record` are three real queries against three real tables, and a fake answers all of them however the fake was written.

This is the failure #179 exposed and this plan must not repeat: until then, `conftest.py`'s `store` fixture was built with no `IngestionDatabase`, so every path behind `if self._ingestion is None` was dead in the whole suite while production always had `SUPABASE_DB_URL` set. The tested path and the shipped path were different paths, and two live cost defects shipped behind that gap. An optional dependency that production always supplies is not optional.

Append to `apps/job-hunter/tests/test_crawl_source.py`:

```python
@pytest.mark.integration
def test_the_stage_reads_and_writes_the_real_tables(store):
    """The three queries the fakes above cannot check.

    Guards the gap #179 exposed: a fixture that withholds the ingestion
    connection tests a configuration nobody deploys.
    """
    from job_hunter.normalize import job_fingerprint

    assert store._ingestion is not None, (
        "the store fixture must supply an ingestion connection; a fake here "
        "would test a configuration nobody runs"
    )

    # Use store.upsert_job rather than the `seed_postings` fixture #179 added.
    # seed_postings inserts a posting row directly over the privileged
    # connection, which leaves `description_hash` at its '' default --
    # the hash is computed inside `job_hunter_upsert_posting`. This test is
    # about the hash short-circuit, so it needs the path that populates it.

    existing = _job("remotive", "int-1", "unchanged body")
    store.upsert_job(existing)

    stage = CrawlSourceStage(
        store._ingestion,
        build_source=lambda key: _StubSource([existing, _job("remotive", "int-2", "new body")]),
        persist=lambda jobs: store.merge_posting_batch(jobs),
    )
    outcome = stage(_message())

    # The hash short-circuit resolved against a row that is really there.
    assert outcome.unchanged_by_hash == 1
    assert outcome.fetched == 2

    with store._ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select outcome, fetched, unchanged_by_hash "
                "from public.job_hunter_source_crawls where source_key = %s",
                ("remotive",),
            )
            rows = cursor.fetchall()

    assert rows == [("fetched", 2, 1)], "the crawl row is written, not just logged"


@pytest.mark.integration
def test_a_crawl_that_produced_nothing_still_leaves_a_row(store):
    """An empty result must carry its reason, in the table and not only in a log."""
    stage = CrawlSourceStage(
        store._ingestion,
        build_source=lambda key: _StubSource([], raises=RuntimeError("upstream down")),
        persist=lambda jobs: None,
    )
    stage(_message("arbeitnow"))

    with store._ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "select outcome, error from public.job_hunter_source_crawls "
                "where source_key = %s",
                ("arbeitnow",),
            )
            row = cursor.fetchone()

    assert row[0] == "failed"
    assert "upstream down" in row[1]
```

- [ ] **Step 6: Run the integration tests**

Run: `pnpm job-hunter:test -- tests/test_crawl_source.py -v`
Expected: PASS (13 tests). Confirm from the output that the two `integration` tests **ran** rather than skipped — a skip here reproduces exactly the gap this step exists to close. If they skip, the `SUPABASE_TEST_*` variables are not exported and the run proves nothing.

- [ ] **Step 7: Commit**

```bash
git add apps/job-hunter/src/job_hunter/crawl_source.py apps/job-hunter/src/job_hunter/sources/__init__.py apps/job-hunter/tests/test_crawl_source.py
git commit -m "feat(job-hunter): crawl one source per queue message (#184)"
```

---

### Task 10: Install the schedules, and correct what this made stale

**Files:**
- Modify: `supabase/migrations/29999999000000_job_hunter_source_registry.sql` (append)
- Modify: `supabase/migrations/20260909200000_job_hunter_ats_boards.sql` (comment only)
- Modify: `supabase/tests/pgtap/job_hunter_source_registry.sql` (append; raise `plan(22)` to `plan(25)`)
- Modify: `apps/job-hunter/AGENTS.md`

**Interfaces:**
- Consumes: `job_hunter_source_crawls`, `job_hunter_schedule_stage_enqueue(text, text, jsonb, text)`, `job_hunter_source_schedule_slug`.
- Produces: `public.job_hunter_reschedule_sources() returns integer` — the number of sources scheduled.

- [ ] **Step 1: Write the failing test**

Append to `supabase/tests/pgtap/job_hunter_source_registry.sql` and change `plan(22)` to `plan(25)`:

```sql
-- The scheduler installs one entry per source ------------------------------

insert into public.job_hunter_sources (source_key, kind) values ('arbeitnow', 'crawl')
  on conflict (source_key) do nothing;
update public.job_hunter_sources set enabled = false where source_key = 'example_licensed';

select is(
  public.job_hunter_reschedule_sources(),
  (select count(*)::int from public.job_hunter_sources where enabled),
  'every enabled source is scheduled and no disabled one is');

select is(
  (select count(distinct jobname)::int from cron.job
    where jobname like 'job-hunter-enqueue-crawl-source-%'),
  (select count(*)::int from public.job_hunter_sources where enabled),
  'one distinct cron entry per enabled source, not one shared entry');

select is_empty(
  $$ select 1 from cron.job
      where jobname = 'job-hunter-enqueue-crawl-source-example-licensed' $$,
  'a disabled source is unscheduled rather than left firing');
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm db:test`
Expected: FAIL — `function public.job_hunter_reschedule_sources() does not exist`

- [ ] **Step 3: Write minimal implementation**

Append to `supabase/migrations/29999999000000_job_hunter_source_registry.sql`:

```sql
-- The yield-driven scheduler -----------------------------------------------
--
-- Reads the crawl ledger, bands each enabled source on its recent novelty,
-- and installs one pg_cron entry per source through the fixed helper. No
-- operator sets these frequencies: a source earns a faster band by producing
-- material the corpus did not already have, and loses one by producing none.
-- Configuration may pin one source as an override; it is never the
-- mechanism.
--
-- The band ladder is mirrored in `source_schedule.py`, where the same policy
-- is unit-tested without a database. The two must agree; the Python side is
-- the one with the exhaustive tests.
create or replace function public.job_hunter_reschedule_sources()
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_bands int[] := array[15, 60, 360, 1440, 4320, 10080];
  v_source record;
  v_index int;
  v_minute int;
  v_hour int;
  v_schedule text;
  v_count int := 0;
begin
  -- Unschedule everything this function owns first, so a source that has
  -- been disabled or removed stops firing rather than being left behind by
  -- a loop that only ever adds.
  for v_source in
    select jobname from cron.job
     where jobname like 'job-hunter-enqueue-crawl-source-%'
  loop
    perform cron.unschedule(v_source.jobname);
  end loop;

  for v_source in
    select s.source_key,
           coalesce((
             select count(*)
               from (
                 select c.outcome, c.new_to_corpus + c.changed as novelty
                   from public.job_hunter_source_crawls c
                  where c.source_key = s.source_key
                  order by c.started_at desc
                  limit 6
               ) recent
              where recent.outcome in ('rate_limited', 'failed')
                 or recent.novelty = 0
           ), 0) as demotions,
           coalesce((
             select count(*)
               from (
                 select c.outcome, c.new_to_corpus + c.changed as novelty
                   from public.job_hunter_source_crawls c
                  where c.source_key = s.source_key
                  order by c.started_at desc
                  limit 6
               ) recent
              where recent.outcome not in ('rate_limited', 'failed')
                and recent.novelty > 0
           ), 0) as promotions
      from public.job_hunter_sources s
     where s.enabled
  loop
    -- A source with no history starts in the middle of the ladder: fast
    -- enough to prove itself within a day, slow enough that eighteen unknown
    -- sources do not open at fifteen-minute intervals.
    v_index := greatest(0, least(
      array_length(v_bands, 1) - 1,
      3 + v_source.demotions - v_source.promotions
    ));

    -- A stable per-source offset, so sources sharing a band do not stampede.
    v_minute := abs(hashtext(v_source.source_key)) % 60;
    v_hour := abs(hashtext(v_source.source_key || ':hour')) % 24;

    v_schedule := case v_bands[v_index + 1]
      when 15 then format('%s-59/15 * * * *', v_minute % 15)
      when 60 then format('%s * * * *', v_minute)
      when 360 then format('%s %s-23/6 * * *', v_minute, v_hour % 6)
      when 1440 then format('%s %s * * *', v_minute, v_hour)
      when 4320 then format('%s %s 1-31/3 * *', v_minute, v_hour)
      else format('%s %s * * %s', v_minute, v_hour,
                  abs(hashtext(v_source.source_key || ':dow')) % 7)
    end;

    perform public.job_hunter_schedule_stage_enqueue(
      'crawl_source',
      v_schedule,
      jsonb_build_object('source_key', v_source.source_key),
      v_source.source_key
    );
    v_count := v_count + 1;
  end loop;

  return v_count;
end;
$$;

comment on function public.job_hunter_reschedule_sources() is
  'Install one pg_cron entry per enabled source, banded on measured corpus '
  'novelty rather than on an operator setting (issue #184). Unschedules '
  'first, so a disabled source stops firing rather than being left behind.';

revoke all on function public.job_hunter_reschedule_sources()
  from public, anon, authenticated, service_role;

-- The scheduler reschedules itself. One meta-entry, deliberately not
-- per-source: it reads the ledger for every source at once.
select cron.schedule(
  'job-hunter-reschedule-sources',
  '7 * * * *',
  'select public.job_hunter_reschedule_sources();'
);

select public.job_hunter_reschedule_sources();
```

In `supabase/migrations/20260909200000_job_hunter_ats_boards.sql`, correct the now-stale sentence in the header comment. Replace:

```
-- profile (`record_ats_eligible_jobs`), so it is per-user *yield*, not
-- board health -- #184 (yield-driven per-source crawl) is built on it. A
-- shared board carrying one user's yield would mislead budgeting for
-- every other user.
```

with:

```
-- profile (`record_ats_eligible_jobs`), so it is per-user *yield*, not
-- board health. A shared board carrying one user's yield would mislead
-- budgeting for every other user.
--
-- Corrected by #184: that ticket does NOT build on this column. Its
-- scheduler runs as the privileged role with no auth.uid() and cannot read
-- a per-user column at all, and with one user an aggregate of this column
-- is that user's search profile -- the engine would learn to visit only the
-- boards matching the current job hunt. #184 records corpus novelty in
-- job_hunter_source_crawls instead, which is shared by construction.
```

In `apps/job-hunter/AGENTS.md`, extend the paragraph that begins "Since #183 that same privileged side owns the four pgmq stage queues" with:

```
   #184 installs the first schedules on that machinery. `job_hunter_sources` is the
   shared source registry — a source's kind (`crawl` or `licensed`) and any
   `display_credit` it obliges a surface to show, resolved by
   `job_hunter_posting_display_credit` with no `user_id` in the path.
   `job_hunter_source_crawls` records what each crawl fetched and how much of it was
   new, and `job_hunter_reschedule_sources` bands each source on that novelty and
   installs one `pg_cron` entry per source. `job_hunter_schedule_stage_enqueue` gained a
   `p_schedule_key` argument for exactly that reason: `cron.schedule` replaces by name,
   so a stage-derived job name would collapse every per-source schedule into one and
   crawl a single source forever without raising anything. The external search allowance
   moved to `job_hunter_platform_search_usage`, keyed on the provider — the quota belongs
   to the API key, not to a person. `crawl_source.py` is the stage handler and, like
   `resolve_persist.py`, imports nothing user-scoped.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pnpm db:reset && pnpm db:test`
Expected: PASS — 25/25 in `job_hunter_source_registry.sql`, every other pgTAP file still green.

Run the full Python suite: `pnpm job-hunter:test`
Expected: PASS, with integration tests running rather than skipping.

- [ ] **Step 5: Make the Python ladder earn its place**

`source_schedule.py` (Task 3) has no production consumer — the scheduler is PL/pgSQL because it must run under `pg_cron` with no worker process, so the ladder is necessarily encoded twice. Two copies of a policy with nothing holding them together drift silently, and the Python copy would otherwise be dead code.

This test is what makes the Python module the executable specification of the SQL rather than an unused duplicate.

Append to `apps/job-hunter/tests/test_source_schedule.py`:

```python
import re
from pathlib import Path


def _migration_text() -> str:
    root = Path(__file__).resolve().parents[3]
    matches = sorted(root.glob("supabase/migrations/*_job_hunter_source_registry.sql"))
    assert matches, "the source registry migration is missing"
    return matches[-1].read_text(encoding="utf-8")


def test_the_sql_ladder_matches_the_python_one():
    """Two copies of one policy drift unless something holds them together."""
    sql = _migration_text()
    declared = re.search(r"v_bands\s+int\[\]\s*:=\s*array\[([^\]]+)\]", sql)
    assert declared, "job_hunter_reschedule_sources declares no band array"
    sql_bands = tuple(int(value.strip()) for value in declared.group(1).split(","))
    assert sql_bands == BANDS


def test_every_python_cron_rendering_appears_in_the_sql():
    """The format strings differ in syntax; the shapes they produce must not."""
    sql = _migration_text()
    for index, minutes in enumerate(BANDS):
        rendered = cron_expression(index, source_key="remotive")
        fields = rendered.split()
        # Compare the shape of the day/month/weekday fields, which is where a
        # cadence actually lives -- the minute and hour are per-source offsets.
        shape = " ".join(fields[2:])
        assert shape in sql, (
            f"band {minutes} renders day/month/weekday {shape!r}, "
            "which job_hunter_reschedule_sources does not produce"
        )
```

- [ ] **Step 6: Run the agreement test**

Run: `pnpm job-hunter:test -- tests/test_source_schedule.py -v`
Expected: PASS. If it fails, the SQL and Python ladders disagree — fix the SQL, not the test: `source_schedule.py` is the side with the exhaustive coverage.

- [ ] **Step 7: Commit**

```bash
git add supabase/migrations/ supabase/tests/pgtap/job_hunter_source_registry.sql apps/job-hunter/AGENTS.md apps/job-hunter/tests/test_source_schedule.py
git commit -m "feat(job-hunter): schedule each source on its measured yield (#184)"
```

---

## Before opening the pull request

- [ ] Allocate the migration timestamp yourself, at PR-open, by re-running this enumeration — do not ask for it, and do not use a number derived earlier than this moment:

```bash
git fetch origin -q
# every migration filename on main, on every remote branch, and on every local branch
{ for b in $(git branch -r --format='%(refname:short)' | grep -v HEAD) \
           $(git branch --format='%(refname:short)'); do
    git ls-tree -r --name-only "$b" supabase/migrations/ 2>/dev/null | sed 's|.*/||'
  done; } | sort -u | tail -5
gh pr list --state open --json number,headRefName   # a PR branch you have not fetched
```

  Take the next `YYYYMMDDHHMMSS` slot above everything that returns, in **UTC** (`date -u`) — the local date can be a day ahead of UTC and a timestamp from the wrong one sorts wrong. Then rename `29999999000000_job_hunter_source_registry.sql` to `<timestamp>_job_hunter_source_registry.sql` and update every reference to it, including `AGENTS.md`.

  The rule about not reaching for "the next number after the highest on `main`" is about placeholders chosen at *dispatch*, when other agents are choosing simultaneously and none of you can see the others. At PR-open, with the enumeration above returning nothing in flight, the next slot is correct by construction — that is what "issued in merge order" means. What makes it correct is that the enumeration ran *now*, not that a human said the number.

  The only part that is genuinely not yours to decide: if that enumeration shows another branch ready to merge at the same time, the merge *order* between you is the board's call. Ask then, and only then.
- [ ] Confirm #179 has merged and this branch is rebased onto it.
- [ ] Run `pnpm job-hunter:test` with the `SUPABASE_TEST_*` variables exported and confirm from the summary that integration tests **ran**. A green run with hundreds of skips verifies nothing.
- [ ] Run `pnpm db:reset && pnpm db:test` on a clean stack, so the migration is proved from empty rather than against a hand-applied one.
- [ ] Post the completed status to the coordination log, and to `career-platform-9c` in the six-field format.
- [ ] In the pull-request body, state plainly which acceptance criteria are met in substance rather than in shape: criterion 1's "cursor" is the HTTP validator for the nine sources that have no pagination cursor, and criterion 6 is measured from `job_hunter_source_crawls`' own `fetched` versus `new_to_corpus` rather than against the 14,014 partial count.

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: the shared-novelty decision to Tasks 5 and 10; `display_credit` to Task 4; the platform ledger to Tasks 7 and 8; the validator-as-cursor to Tasks 2 and 5; criterion 6's self-measurement to Task 5 and the pull-request checklist; the `job_hunter_schedule_stage_enqueue` fix to Task 6; the stage handler to Task 9; the stale #203 comment to Task 10.

**Defect found and fixed in review.** The first draft recorded `changed` and left `new_to_corpus` at zero, which would have made the scheduler band on the wrong number: a source that re-advertises the same jobs with edited descriptions is *changed* but not *new*, and would have earned a faster band forever. `persist` now returns `resolve_persist`'s `PostingBatch` and Task 9 reads `newly_discovered` off it, with `test_new_to_corpus_comes_back_from_the_persist_call` holding the contract.

**Second defect found and fixed.** The `not_modified` test constructed an unused stub whose `discover` raised `StopIteration` inside a generator — which Python converts to a `RuntimeError`, so the test would have passed for the wrong reason. Replaced with an assertion that a 304 causes no `persist` call at all, plus a companion test proving a genuine zero-job fetch is recorded as `fetched` and not as `not_modified`. Those two are the same distinction acceptance criterion 2 and the charter's empty-result rule both turn on.

**Type consistency.** `source_key_for` (Task 1), `NOT_MODIFIED` / `Validators` (Task 2), `BANDS` / `next_band` / `cron_expression` (Task 3), `description_hash` / `CrawlOutcome` / `CrawlSourceStage` (Task 9) are each defined once and referenced under those exact names. The band ladder `(15, 60, 360, 1440, 4320, 10080)` and its six cron renderings appear in both `source_schedule.py` and `job_hunter_reschedule_sources`; Task 10's comment names that duplication and which side is authoritative.

**Placeholder scan.** No "TBD", no "handle edge cases", no "similar to Task N". Every code step carries the code.

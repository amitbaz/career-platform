# ATS Rejection Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator reverse a wrongly rejected learned ATS board by adding it to a `learned_ats_allowlist` in `config/search.yml`, with the board recovered and rescanned on the next run.

**Architecture:** A new config list mirrors the existing `learned_ats_denylist` and is normalized through the same `ats_board_key` form, so the two can be compared directly; a key in both fails the config load. `LearnedAtsSource` receives the allowlist and uses it at three points: it heals already-rejected allowlisted boards at the start of `discover()` (via a new `JobStore.clear_ats_board_rejection`), it skips the denylist rejection branch, and it keeps a board whose aggregator verdict says reject — logging the verdict it overrode. Nothing about detection's signal, its threshold, or `upsert_ats_board`'s guard changes.

**Tech Stack:** Python 3.12, SQLite (`var/job_hunter.sqlite3`), pytest, PyYAML.

**Spec:** `apps/job-hunter/docs/superpowers/specs/2026-09-06-ats-rejection-recovery-design.md`

## Global Constraints

- All paths below are relative to `apps/job-hunter/`. Run every command from that directory.
- Use the app's own virtualenv: `source .venv/bin/activate` before running pytest. A stale global install at `~/job-hunter-bot` can hijack `python -m pytest`.
- Install with `pip install -e '.[test,webhook]'` — the full suite imports flask.
- Red-green-refactor: the failing test is written and *run* before the implementation, every task.
- The allowlist is an override, never the mechanism. Detection must keep working with the list empty, and no threshold, phrase list, or minimum sample size may change in this plan.
- The allowlist governs aggregator/denylist verdicts only. `record_ats_scan_failure`, `paused_until` backoff and stale-404 deactivation stay in force for an allowlisted board.
- Board keys are always compared in `ats_board_key(provider, board_identifier)` form: lowercased provider, lowercased identifier, joined by `:`.
- Commit style: `job-hunter: <what changed>` (app-scoped prefix in place of a type is fine when the change is local to the app). End every commit message with `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.
- `git` in this worktree must be invoked as `/usr/bin/git` — the repo's rtk hook is refused inside a worktree-isolated session.

---

## File Structure

| File | Responsibility | Change |
| --- | --- | --- |
| `src/job_hunter/models.py` | `PolicySettings.learned_ats_allowlist` field | Modify (~line 202, beside `learned_ats_denylist`) |
| `src/job_hunter/config.py` | Parse and normalize the allowlist; reject a deny/allow conflict | Modify (`_parse_learned_ats_denylist` neighbourhood, ~line 175; `load_settings` policy construction, ~line 98) |
| `config/search.yml` | Operator surface: the allowlist key and its comment | Modify (~line 81, beside the denylist) |
| `src/job_hunter/store.py` | `clear_ats_board_rejection` — the inverse of `reject_ats_board` | Modify (add after `reject_ats_board`, ~line 1480) |
| `src/job_hunter/sources/learned_ats.py` | Healing, denylist skip, verdict override | Modify (`__init__`, `discover`, `_aggregator_rejection`) |
| `src/job_hunter/sources/__init__.py` | Wire the policy list into `LearnedAtsSource` | Modify (~line 224) |
| `src/job_hunter/aggregator_detection.py` | Module docstring: the allowlist is the second override | Modify (docstring only) |
| `tests/test_config.py` | Allowlist parsing and conflict tests | Modify |
| `tests/test_store.py` | `clear_ats_board_rejection` tests | Modify |
| `tests/test_learned_ats_source.py` | Allowlist behaviour and #17 regression | Modify |

---

### Task 1: Config surface — `learned_ats_allowlist`

**Files:**
- Modify: `src/job_hunter/models.py:202`
- Modify: `src/job_hunter/config.py:98`, `src/job_hunter/config.py:175`
- Modify: `config/search.yml:81`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `ats_board_key(provider, board_identifier) -> str` from `job_hunter.normalize` (already imported in `config.py`).
- Produces: `PolicySettings.learned_ats_allowlist: list[str]` — normalized `"<provider>:<board>"` keys, default `[]`. Task 3 reads it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`, after the existing `learned_ats_denylist` tests (~line 372). The `_write_config` shape is copied from `test_load_settings_reads_learned_ats_denylist` — repeated in full rather than factored out, so each test reads on its own.

```python
def _minimal_config_body(extra: str = "") -> str:
    return (
        "timezone: Europe/Berlin\nscheduled_hour: 9\n"
        "thresholds:\n  package: 75\n  possible: 65\nsalary_floor_eur: 90000\n"
        "target_titles: []\npositive_keywords: []\nblocked_title_keywords: []\n"
        "search_queries: []\nats:\n  ashby: []\n  lever: []\n  greenhouse: []\n"
        + extra
    )


def _set_required_env(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("CANDIDATE_PROFILE_B64", base64.b64encode(b"profile").decode())
    monkeypatch.setenv(
        "COVER_LETTER_TEMPLATE_B64", base64.b64encode(b"template").decode()
    )
    monkeypatch.setenv("JOB_HUNTER_DRY_RUN", "1")


def test_load_settings_defaults_learned_ats_allowlist_to_no_entries(
    monkeypatch, tmp_path: Path
):
    cfg = tmp_path / "search.yml"
    cfg.write_text(_minimal_config_body())
    _set_required_env(monkeypatch)

    settings = load_settings(cfg)

    assert settings.policy.learned_ats_allowlist == []


def test_load_settings_reads_and_normalizes_learned_ats_allowlist(
    monkeypatch, tmp_path: Path
):
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        _minimal_config_body("learned_ats_allowlist:\n  - ' Lever:ClientCo '\n")
    )
    _set_required_env(monkeypatch)

    settings = load_settings(cfg)

    assert settings.policy.learned_ats_allowlist == ["lever:clientco"]


def test_load_settings_treats_empty_learned_ats_allowlist_key_as_no_entries(
    monkeypatch, tmp_path: Path
):
    # The state left behind by commenting out the list's only entry.
    cfg = tmp_path / "search.yml"
    cfg.write_text(_minimal_config_body("learned_ats_allowlist:\n"))
    _set_required_env(monkeypatch)

    settings = load_settings(cfg)

    assert settings.policy.learned_ats_allowlist == []


def test_load_settings_rejects_malformed_learned_ats_allowlist_entry(
    monkeypatch, tmp_path: Path
):
    cfg = tmp_path / "search.yml"
    cfg.write_text(_minimal_config_body("learned_ats_allowlist:\n  - clientco\n"))
    _set_required_env(monkeypatch)

    with pytest.raises(ValueError, match="learned_ats_allowlist"):
        load_settings(cfg)


def test_load_settings_rejects_a_board_in_both_ats_lists(monkeypatch, tmp_path: Path):
    # The two lists express opposite operator intent; honouring either one
    # silently would hide an editing mistake in the only operator surface.
    cfg = tmp_path / "search.yml"
    cfg.write_text(
        _minimal_config_body(
            "learned_ats_denylist:\n  - lever:clientco\n"
            "learned_ats_allowlist:\n  - Lever:ClientCo\n"
        )
    )
    _set_required_env(monkeypatch)

    with pytest.raises(ValueError, match="lever:clientco"):
        load_settings(cfg)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_config.py -k learned_ats_allowlist -q`
Expected: FAIL — `AttributeError: 'PolicySettings' object has no attribute 'learned_ats_allowlist'` on the first three, and the two `pytest.raises` tests failing because no `ValueError` is raised.

- [ ] **Step 3: Add the model field**

In `src/job_hunter/models.py`, directly below `learned_ats_denylist`:

```python
    #: Normalized `ats_board_key` values that aggregator detection may never
    #: reject -- the inverse of `learned_ats_denylist`, and the operator's
    #: only way to reverse a rejection. See aggregator_detection.py.
    learned_ats_allowlist: list[str] = field(default_factory=list)
```

- [ ] **Step 4: Parse the list and reject conflicts**

In `src/job_hunter/config.py`, replace `_parse_learned_ats_denylist` with a shared parser plus two thin callers, and add the conflict check. The denylist's existing behaviour and error text must not change.

```python
def _parse_ats_board_key_list(data: dict, key: str) -> list[str]:
    """Normalize one `<provider>:<board>` policy list once, for every consumer.

    A bare `<key>:` (the state left behind by commenting out its only entry)
    parses as None, which must read as an empty list rather than aborting
    the run.
    """
    entries = data.get(key) or []
    if not isinstance(entries, list):
        raise ValueError(f"{key} must be a list")

    board_keys: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, str) or entry.count(":") != 1:
            raise ValueError(f"{key}[{index}] must be a \"<provider>:<board>\" string")
        provider, board_identifier = entry.split(":")
        if not provider.strip() or not board_identifier.strip():
            raise ValueError(f"{key}[{index}] must be a \"<provider>:<board>\" string")
        board_keys.append(ats_board_key(provider, board_identifier))
    return board_keys


def _parse_learned_ats_denylist(data: dict) -> list[str]:
    """Boards that must be kept out of the learned ATS registry."""
    return _parse_ats_board_key_list(data, "learned_ats_denylist")


def _parse_learned_ats_allowlist(data: dict) -> list[str]:
    """Boards that aggregator detection may never reject.

    Both lists normalize to the same key form, so a board named by both is a
    contradiction the operator has to resolve: there is no correct way to
    honour "always reject" and "never reject" for one board, and preferring
    either silently would hide the edit that caused it.
    """
    allowlist = _parse_ats_board_key_list(data, "learned_ats_allowlist")
    denylist = set(_parse_learned_ats_denylist(data))
    for board_key in allowlist:
        if board_key in denylist:
            raise ValueError(
                f"{board_key} is in both learned_ats_denylist and "
                "learned_ats_allowlist; remove it from one"
            )
    return allowlist
```

Then in `load_settings`'s policy construction, directly below `learned_ats_denylist=_parse_learned_ats_denylist(data),`:

```python
        learned_ats_allowlist=_parse_learned_ats_allowlist(data),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_config.py -q`
Expected: PASS — the new tests and every existing denylist test.

- [ ] **Step 6: Document the key in the operator config**

In `config/search.yml`, directly below the `learned_ats_denylist` block (which ends with `  - lever:jobgether`):

```yaml
# "<provider>:<board>" boards aggregator detection may never reject -- the way
# to reverse a rejection you judge to be wrong. A board listed here is
# un-rejected and rescanned on the next run. A board may not be in both lists.
learned_ats_allowlist: []
```

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add src/job_hunter/models.py src/job_hunter/config.py config/search.yml tests/test_config.py
/usr/bin/git commit -m "job-hunter: add a learned_ats_allowlist policy list

Refs #63

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Store — clearing a rejection

**Files:**
- Modify: `src/job_hunter/store.py:1457-1480` (add the new method after `reject_ats_board`)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `JobStore.clear_ats_board_rejection(provider: str, board_identifier: str) -> None`. Task 3 calls it.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_store.py`, after `test_ats_rejected_board_is_not_resurrected_by_rediscovery` (~line 655).

```python
def test_clear_ats_board_rejection_makes_a_rejected_board_due_again():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="clientco")
    store.reject_ats_board("lever", "clientco", "aggregator: 98% third-party", now)

    store.clear_ats_board_rejection("lever", "clientco")

    assert store.list_rejected_ats_boards() == []
    due = store.list_due_ats_boards(now)
    assert [e.board_identifier for e in due] == ["clientco"]
    assert due[0].rejected_reason is None
    assert due[0].active is True


def test_clear_ats_board_rejection_matches_the_stored_provider_case_insensitively():
    # Callers hold normalized ats_board_key values ("lever:jobgether"), while
    # the row was written from whatever case discovery saw.
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="ClientCo")
    store.reject_ats_board("lever", "ClientCo", "aggregator: 98% third-party", now)

    store.clear_ats_board_rejection("Lever", "clientco")

    assert store.list_rejected_ats_boards() == []
    assert [e.board_identifier for e in store.list_due_ats_boards(now)] == ["ClientCo"]


def test_clear_ats_board_rejection_is_a_no_op_for_a_board_that_was_never_rejected():
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="healthy-co")

    store.clear_ats_board_rejection("lever", "healthy-co")
    store.clear_ats_board_rejection("lever", "never-registered")

    assert [e.board_identifier for e in store.list_due_ats_boards(now)] == ["healthy-co"]


def test_clear_ats_board_rejection_does_not_revive_a_health_deactivated_board():
    # A board deactivated by repeated 404s is broken, not misjudged. Clearing
    # a rejection it never had must not put it back in the rotation.
    store = JobStore(":memory:")
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="dead-co")
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", "dead-co", now + timedelta(hours=25 * i), permanent=True
        )

    store.clear_ats_board_rejection("lever", "dead-co")

    assert store.list_due_ats_boards(now + timedelta(days=30)) == []
```

`datetime`, `timedelta`, `timezone` and `JobStore` are already imported at the top of `tests/test_store.py`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_store.py -k clear_ats_board_rejection -q`
Expected: FAIL — `AttributeError: 'JobStore' object has no attribute 'clear_ats_board_rejection'`.

- [ ] **Step 3: Implement the method**

In `src/job_hunter/store.py`, directly after `reject_ats_board`:

```python
    def clear_ats_board_rejection(self, provider: str, board_identifier: str) -> None:
        """Reverse a rejection, putting the board back in the due rotation.

        The inverse of `reject_ats_board`, and the only code path that clears
        `rejected_reason`. Used when an operator names a board in
        `learned_ats_allowlist`, having judged its rejection wrong.

        Scoped to rejected rows on purpose: a board deactivated by repeated
        404s carries no `rejected_reason`, and reviving it here would confuse
        "wrongly judged" with "broken", which health backoff owns. Clearing a
        board that was never rejected is a no-op.
        """
        with self._conn:
            self._conn.execute(
                """
                UPDATE ats_registry SET
                    active = 1,
                    rejected_reason = NULL
                WHERE lower(provider) = lower(?)
                  AND lower(board_identifier) = lower(?)
                  AND rejected_reason IS NOT NULL
                """,
                (provider.strip(), board_identifier.strip()),
            )
```

`last_checked_at` is deliberately untouched: healing is not a scan, and rewriting it would misreport when the board was last checked.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_store.py -q`
Expected: PASS — the four new tests plus every existing `ats_registry` test.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add src/job_hunter/store.py tests/test_store.py
/usr/bin/git commit -m "job-hunter: add JobStore.clear_ats_board_rejection

Refs #63

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `LearnedAtsSource` honours the allowlist

**Files:**
- Modify: `src/job_hunter/sources/learned_ats.py` (`__init__`, `discover`, `_aggregator_rejection`)
- Modify: `src/job_hunter/sources/__init__.py:224`
- Modify: `src/job_hunter/aggregator_detection.py` (module docstring)
- Test: `tests/test_learned_ats_source.py`

**Interfaces:**
- Consumes: `PolicySettings.learned_ats_allowlist: list[str]` (Task 1); `JobStore.clear_ats_board_rejection(provider, board_identifier)` (Task 2).
- Produces: `LearnedAtsSource(..., denylist: frozenset[str] = frozenset(), allowlist: frozenset[str] = frozenset())`, and `LearnedAtsStats.boards_recovered: int = 0`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_learned_ats_source.py`, after `test_learned_ats_source_denylisted_board_does_not_consume_a_scan_slot`. The module's `RoutingHttp`, `_lever_postings`, `_seed_board` and `_JOBGETHER_PHRASING` helpers already exist at the top of the file.

```python
def test_learned_ats_source_keeps_an_allowlisted_board_detection_would_reject():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "clientco")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={"lever.co": _lever_postings(10, "clientco", _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:clientco"}),
    )
    jobs = source.discover()

    assert len(jobs) == 10
    assert source.stats.boards_rejected == 0
    assert source.stats.boards_successful == 1
    assert store.list_rejected_ats_boards() == []


def test_learned_ats_source_logs_the_verdict_it_overrode(caplog):
    # The operator overrode a verdict, so the verdict must stay visible --
    # otherwise the allowlist entry can never be shown to be unnecessary.
    store = JobStore(":memory:")
    _seed_board(store, "lever", "clientco")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={"lever.co": _lever_postings(10, "clientco", _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:clientco"}),
    )
    with caplog.at_level(logging.INFO):
        source.discover()

    kept = [r.getMessage() for r in caplog.records if "learned_ats_allowlist" in r.getMessage()]
    assert len(kept) == 1
    assert "lever:clientco" in kept[0]
    assert "third_party_listing" in kept[0]


def test_learned_ats_source_heals_an_already_rejected_allowlisted_board():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "clientco")
    rejected_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board(
        "lever", "clientco", "third_party_listing: 9/10 postings (90%)", rejected_at
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={"lever.co": _lever_postings(10, "clientco", _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:clientco"}),
    )
    jobs = source.discover()

    # Recovered and rescanned within the same run -- editing the config is
    # the whole recovery procedure.
    assert len(jobs) == 10
    assert source.stats.boards_recovered == 1
    assert source.stats.boards_successful == 1
    assert store.list_rejected_ats_boards() == []
    assert [e.board_identifier for e in store.list_due_ats_boards(now)] == ["clientco"]


def test_learned_ats_source_logs_the_reason_it_cleared_when_healing(caplog):
    store = JobStore(":memory:")
    _seed_board(store, "lever", "clientco")
    rejected_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board(
        "lever", "clientco", "third_party_listing: 9/10 postings (90%)", rejected_at
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={"lever.co": _lever_postings(10, "clientco", _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:clientco"}),
    )
    with caplog.at_level(logging.INFO):
        source.discover()

    recovered = [r.getMessage() for r in caplog.records if "recovered" in r.getMessage()]
    assert len(recovered) == 1
    assert "lever:clientco" in recovered[0]
    assert "9/10 postings (90%)" in recovered[0]


def test_learned_ats_source_healing_ignores_a_board_that_is_not_allowlisted():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "jobgether")
    rejected_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board(
        "lever", "jobgether", "third_party_listing: 9/10 postings (90%)", rejected_at
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={"lever.co": _lever_postings(10, "jobgether")})

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:someone-else"}),
    )
    jobs = source.discover()

    assert jobs == []
    assert http.calls == []
    assert source.stats.boards_recovered == 0
    assert [e.board_identifier for e in store.list_rejected_ats_boards()] == ["jobgether"]


def test_learned_ats_source_allowlist_matches_the_board_key_case_insensitively():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "ClientCo")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={"lever.co": _lever_postings(10, "ClientCo", _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:clientco"}),
    )
    jobs = source.discover()

    assert len(jobs) == 10
    assert source.stats.boards_rejected == 0


def test_learned_ats_source_allowlist_wins_over_the_denylist_branch():
    # The config load refuses a board named by both lists, so this can only
    # be reached by constructing the source directly -- the guard keeps the
    # invariant local to the code that depends on it.
    store = JobStore(":memory:")
    _seed_board(store, "lever", "clientco")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={"lever.co": _lever_postings(10, "clientco")})

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({"lever:clientco"}),
        allowlist=frozenset({"lever:clientco"}),
    )
    jobs = source.discover()

    assert len(jobs) == 10
    assert source.stats.boards_rejected == 0


def test_learned_ats_source_still_rejects_an_aggregator_that_is_not_allowlisted():
    # Regression on #17: an empty or unrelated allowlist changes nothing.
    store = JobStore(":memory:")
    _seed_board(store, "lever", "jobgether")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={"lever.co": _lever_postings(10, "jobgether", _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:someone-else"}),
    )
    jobs = source.discover()

    assert jobs == []
    assert source.stats.boards_rejected == 1
    assert [e.board_identifier for e in store.list_rejected_ats_boards()] == ["jobgether"]


def test_learned_ats_source_allowlist_does_not_override_health_backoff():
    # An allowlisted board that 404s is broken, not misjudged: health
    # deactivation is not a verdict the allowlist may reverse.
    store = JobStore(":memory:")
    _seed_board(store, "lever", "clientco")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(not_found_urls={"lever.co"})

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:clientco"}),
    )
    jobs = source.discover()

    assert jobs == []
    assert source.stats.boards_failed == 1
    assert store.list_due_ats_boards(now) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `source .venv/bin/activate && pytest tests/test_learned_ats_source.py -k allowlist -q`
Expected: FAIL — `TypeError: LearnedAtsSource.__init__() got an unexpected keyword argument 'allowlist'`.

- [ ] **Step 3: Add the allowlist to the source's construction and stats**

In `src/job_hunter/sources/learned_ats.py`, extend `LearnedAtsStats`:

```python
@dataclass(slots=True)
class LearnedAtsStats:
    boards_scanned: int = 0
    boards_successful: int = 0
    boards_failed: int = 0
    jobs_raw: int = 0
    boards_rejected: int = 0
    boards_recovered: int = 0
```

and `LearnedAtsSource.__init__`, after the `denylist` parameter:

```python
        denylist: frozenset[str] = frozenset(),
        allowlist: frozenset[str] = frozenset(),
    ) -> None:
```

with, beside `self._denylist = denylist`:

```python
        self._allowlist = allowlist
```

- [ ] **Step 4: Heal allowlisted boards before reading the due list**

In `discover()`, replace the first two statements:

```python
        checked_at = self._now()
        due = self._store.list_due_ats_boards(checked_at)
```

with:

```python
        checked_at = self._now()
        # Recover before reading the due list, so a board the operator
        # un-rejected is scanned in the same run that recovered it --
        # editing config/search.yml is the whole recovery procedure.
        self._recover_allowlisted_boards()
        due = self._store.list_due_ats_boards(checked_at)
```

and add the method beside `_reject_board`:

```python
    def _recover_allowlisted_boards(self) -> None:
        """Clear the rejection on every allowlisted board that carries one.

        The stored reason is the only record of what the operator overrode
        and the healing write destroys it, so each recovery is logged with
        the reason it cleared before clearing it.
        """
        if not self._allowlist:
            return
        for entry in self._store.list_rejected_ats_boards():
            board_key = ats_board_key(entry.provider, entry.board_identifier)
            if board_key not in self._allowlist:
                continue
            try:
                self._store.clear_ats_board_rejection(
                    entry.provider, entry.board_identifier
                )
            except Exception:
                logger.warning(
                    "learned ATS rejection recovery failed for %s",
                    board_key,
                    exc_info=True,
                )
                continue
            self.stats.boards_recovered += 1
            logger.info(
                "learned ATS board recovered by learned_ats_allowlist: %s "
                "(cleared rejection: %s)",
                board_key,
                entry.rejected_reason,
            )
```

A failed healing write is a warning, not an abort, matching how `_reject_board` and the health writes already isolate per-board store failures: the board stays rejected and the next run retries it.

- [ ] **Step 5: Exempt allowlisted boards from the denylist branch**

In `discover()`, change the denylist loop's condition:

```python
        for entry in due:
            board_key = ats_board_key(entry.provider, entry.board_identifier)
            if board_key in self._denylist and board_key not in self._allowlist:
                self._reject_board(
                    entry, checked_at, f"configured in learned_ats_denylist ({board_key})"
                )
            else:
                remaining.append(entry)
```

- [ ] **Step 6: Override a rejecting verdict for an allowlisted board**

In `_aggregator_rejection`, replace the `if verdict.rejected:` branch:

```python
        if verdict.rejected:
            board_key = ats_board_key(entry.provider, entry.board_identifier)
            if board_key in self._allowlist:
                # Detection still runs, and the overridden verdict is logged:
                # an override nobody can see is an override nobody can ever
                # show to be unnecessary.
                logger.info(
                    "learned ATS board kept by learned_ats_allowlist: %s "
                    "(despite %s)",
                    board_key,
                    verdict.reason,
                )
                return None
            return verdict.reason
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `source .venv/bin/activate && pytest tests/test_learned_ats_source.py -q`
Expected: PASS — the nine new tests plus every existing one, including the #17 rejection tests.

- [ ] **Step 8: Wire the policy list through source construction**

In `src/job_hunter/sources/__init__.py`, in the `LearnedAtsSource(...)` call, below the `denylist=` argument:

```python
                allowlist=frozenset(settings.policy.learned_ats_allowlist),
```

- [ ] **Step 9: Update the detection module docstring**

In `src/job_hunter/aggregator_detection.py`, replace the paragraph beginning "This detection is the mechanism":

```python
This detection is the mechanism, and it needs no operator configuration.
Two lists in `config/search.yml` are overrides only, never the mechanism:
`learned_ats_denylist` is an instant kill for a board these signals miss,
enforced in `ats_registry.harvest_ats_board` (refusing admission) and
`sources/learned_ats.LearnedAtsSource` (rejecting an already-registered
board before scanning it); `learned_ats_allowlist` is its inverse, naming
boards that may never be rejected, and is the operator's only way to
reverse a verdict. An allowlisted board is still evaluated and its
overridden verdict still logged, so an entry that has become unnecessary
stays visible. A board may not appear in both lists.
```

- [ ] **Step 10: Run the wider suite touching these modules**

Run: `source .venv/bin/activate && pytest tests/test_learned_ats_source.py tests/test_sources.py tests/test_aggregator_detection.py tests/test_ats_registry.py tests/test_pipeline.py -q`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
/usr/bin/git add src/job_hunter/sources/learned_ats.py src/job_hunter/sources/__init__.py src/job_hunter/aggregator_detection.py tests/test_learned_ats_source.py
/usr/bin/git commit -m "job-hunter: recover allowlisted ATS boards from a wrong rejection

Refs #63

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Full-suite verification and PR

**Files:**
- No source changes. Fix whatever the full suite surfaces, in the file that owns it.

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: a PR closing #63.

- [ ] **Step 1: Run the full suite**

Run: `source .venv/bin/activate && pytest -q`
Expected: PASS, no new failures against the pre-change baseline. Never claim the work is done without this output in hand.

- [ ] **Step 2: Confirm the operator path end to end by reading the diff**

Run: `/usr/bin/git diff main...HEAD --stat`
Check by eye that the chain is whole: `config/search.yml` key → `_parse_learned_ats_allowlist` → `PolicySettings.learned_ats_allowlist` → `build_sources` → `LearnedAtsSource(allowlist=...)` → healing plus both skip points. A break anywhere in that chain leaves the tests green and the feature dead in production.

- [ ] **Step 3: Push and open the PR**

```bash
/usr/bin/git push -u origin feat/job-hunter-ats-rejection-recovery
```

PR body: what changed, the three enforcement points, the deny/allow conflict rule, and `Closes #63`. Note explicitly that nothing needs applying outside git — no migration, no secret, no re-registration; `rejected_reason` already exists on `ats_registry` and no schema change is involved.

---

## Notes for the executor

- `_ATS_REGISTRY_REJECTION_COLUMNS` in `store.py:294` already adds `rejected_reason` to existing databases. This plan adds no column and needs no migration.
- The spec lists "README / AGENTS.md wherever policy keys are enumerated" as documentation. Neither file enumerates `learned_ats_denylist` today (`grep -rn learned_ats_denylist README.md AGENTS.md docs/*.md` is empty), so there is nothing to update there and no task covers it. Re-run that grep before concluding the same.
- The board keys in tests use `clientco` rather than `jobgether` wherever the board is meant to be a wrongly rejected legitimate employer. `jobgether` is the real aggregator and stays the fixture for cases that must still reject.
- If the full suite surfaces a failure in a test that asserts on the `ats_registry` log line, check whether it counts log records rather than matching them — the two new log lines are additions to a stream some pipeline tests read.

import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS
from job_hunter.gmail_models import ExtractedJob
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import Evaluation, Job, Material
from job_hunter.store import JobStore
from job_hunter.store_mapping import from_iso


def make_job(*, fingerprint: str = "default", **overrides) -> Job:
    """Build a `Job` whose computed fingerprint is deterministic on `fingerprint`.

    `Job` carries no literal fingerprint field -- `job_fingerprint()` derives
    one from `source_job_id` or `url`. Embedding the requested value in the
    URL makes two `make_job()` calls with the same `fingerprint` collide on
    the same job, exactly as passing a literal fingerprint would.
    """
    fields = {
        "source": "test",
        "title": "Engineer",
        "url": f"https://example.test/jobs/{fingerprint}",
    }
    fields.update(overrides)
    return Job(**fields)


def _evaluation(job_id, **overrides):
    defaults = dict(
        job_id=job_id,
        total_score=90,
        scores={},
        decision="high_priority",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="m",
    )
    defaults.update(overrides)
    return Evaluation(**defaults)


def _drop_raw_model_score_column(db_path):
    """Simulate a database written before `raw_model_score` existed.

    Drops the column outright (rather than just zeroing its values) so that
    reopening the store genuinely exercises the add-column-then-backfill
    migration path in `_add_missing_columns` / `_backfill_raw_model_score`.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if sqlite3.sqlite_version_info >= (3, 35, 0):
            conn.execute("ALTER TABLE evaluations DROP COLUMN raw_model_score")
        else:
            columns = [
                row["name"]
                for row in conn.execute("PRAGMA table_info(evaluations)")
                if row["name"] != "raw_model_score"
            ]
            column_list = ", ".join(columns)
            conn.execute(
                f"CREATE TABLE evaluations_legacy AS SELECT {column_list} FROM evaluations"
            )
            conn.execute("DROP TABLE evaluations")
            conn.execute("ALTER TABLE evaluations_legacy RENAME TO evaluations")
        conn.commit()
    finally:
        conn.close()


def _create_r1_jobs_only_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '',
            source_job_id TEXT,
            url TEXT NOT NULL DEFAULT '',
            company TEXT NOT NULL DEFAULT '',
            title TEXT NOT NULL DEFAULT '',
            location TEXT NOT NULL DEFAULT '',
            remote INTEGER,
            description TEXT NOT NULL DEFAULT '',
            description_hash TEXT NOT NULL DEFAULT '',
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new'
        )
        """
    )
    conn.execute(
        """
        INSERT INTO jobs
            (fingerprint, source, url, company, title, description_hash,
             first_seen_at, last_seen_at)
        VALUES ('legacy', 'gmail:linkedin', 'https://example.test/job',
                'Acme', 'Frontend Engineer', '',
                '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')
        """
    )
    conn.commit()
    conn.close()


def test_r2_schema_upgrades_legacy_jobs_table(tmp_path):
    db = tmp_path / "state.sqlite3"
    _create_r1_jobs_only_db(db)

    store = JobStore(db)

    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(jobs)")}
    assert "canonical_url" in columns
    assert "ats_provider" in columns
    assert "ats_board" in columns
    assert "ats_job_id" in columns
    assert "market_id" in columns
    assert store.count_jobs() == 1
    tables = {
        row["name"]
        for row in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "job_sources" in tables
    assert "company_watch" in tables


def test_evaluations_table_has_market_id_column():
    store = JobStore(":memory:")
    columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(evaluations)")}
    assert "market_id" in columns


def test_gemini_usage_rows_persist_success_without_prompt_or_response_content():
    store = JobStore(":memory:")

    store.record_gemini_usage(
        occurred_at="2026-09-01T08:00:00+00:00",
        run_id="run-1",
        model="gemini-3.6-flash",
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=120,
        prompt_tokens=100,
        output_tokens=20,
        thinking_tokens=5,
        cached_tokens=10,
        total_tokens=125,
    )

    rows = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00",
        "2026-09-02T00:00:00+00:00",
        model="gemini-3.6-flash",
        run_id="run-1",
    )

    assert len(rows) == 1
    assert dict(rows[0]) == {
        "id": 1,
        "occurred_at": "2026-09-01T08:00:00+00:00",
        "run_id": "run-1",
        "model": "gemini-3.6-flash",
        "purpose": "job_evaluation",
        "status": "success",
        "estimated_input_tokens": 120,
        "prompt_tokens": 100,
        "output_tokens": 20,
        "thinking_tokens": 5,
        "cached_tokens": 10,
        "total_tokens": 125,
        "http_status": None,
        "error_code": None,
    }
    assert "prompt" not in rows[0].keys()
    assert "response" not in rows[0].keys()


def test_gemini_usage_rows_persist_429_attempt():
    store = JobStore(":memory:")

    store.record_gemini_usage(
        occurred_at="2026-09-01T08:00:00+00:00",
        run_id="run-1",
        model="gemini-3.6-flash",
        purpose="cover_letter",
        status="quota_429",
        estimated_input_tokens=80,
        http_status=429,
        error_code="RESOURCE_EXHAUSTED",
    )

    rows = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00"
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "quota_429"
    assert rows[0]["http_status"] == 429
    assert rows[0]["error_code"] == "RESOURCE_EXHAUSTED"


def test_gemini_pause_round_trip_and_clear():
    store = JobStore(":memory:")

    store.set_gemini_pause(
        "gemini-3.6-flash",
        "2026-09-01T08:01:30+00:00",
        "rate_limit",
    )

    pause = store.get_gemini_pause("gemini-3.6-flash")

    assert pause is not None
    assert pause["paused_until"] == "2026-09-01T08:01:30+00:00"
    assert pause["reason"] == "rate_limit"
    assert pause["updated_at"]

    store.clear_gemini_pause("gemini-3.6-flash")

    assert store.get_gemini_pause("gemini-3.6-flash") is None


def test_candidate_context_cache_round_trip_serializes_json_inside_store():
    store = JobStore(":memory:")
    context = {"summary": "Frontend engineer", "technical_skills": ["Python"]}

    store.save_candidate_context(
        cache_key="profile:model:v1",
        profile_hash="profile-hash",
        model="gemini-3.6-flash",
        schema_version="v1",
        context=context,
    )

    cached = store.get_candidate_context("profile:model:v1")

    assert cached is not None
    assert cached.cache_key == "profile:model:v1"
    assert cached.profile_hash == "profile-hash"
    assert cached.model == "gemini-3.6-flash"
    assert cached.schema_version == "v1"
    assert cached.context == context


def test_pending_ai_work_is_idempotent_and_updates_its_timestamp(monkeypatch):
    store = JobStore(":memory:")
    job_id, _, _ = store.upsert_job(Job(source="manual", title="Frontend Engineer"))
    timestamps = iter(
        ["2026-09-01T08:00:00+00:00", "2026-09-01T08:01:00+00:00"]
    )
    monkeypatch.setattr("job_hunter.store._now_iso", lambda: next(timestamps))

    store.enqueue_ai_work("cover_letter", job_id)
    store.enqueue_ai_work("cover_letter", job_id)

    pending = store.list_pending_ai_work("cover_letter")

    assert len(pending) == 1
    assert pending[0]["job_id"] == job_id
    assert pending[0]["created_at"] == "2026-09-01T08:00:00+00:00"
    assert pending[0]["updated_at"] == "2026-09-01T08:01:00+00:00"

    store.complete_ai_work("cover_letter", job_id)

    assert store.list_pending_ai_work("cover_letter") == []


# ------------------------------------------------------------------
# Company watch
# ------------------------------------------------------------------


def _watch(store, **overrides):
    """Upsert one company watch target, defaulting every required argument."""
    fields = dict(
        company_name="Acme",
        careers_url="",
        ats_provider="greenhouse",
        ats_identifier="acme",
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    fields.update(overrides)
    return store.upsert_company_watch(**fields)


def _watch_rows(client):
    return client.select("job_hunter_company_watch", params={"select": "id"})


def test_company_watch_upsert_deduplicates_normalized_company_name(
    store, supabase_client
):
    first_id = _watch(store, company_name="Acme GmbH")
    second_id = _watch(store, company_name="ACME")

    assert second_id == first_id
    assert store.get_company_watch("acme")["promotion_source"] == "manual"
    assert len(_watch_rows(supabase_client)) == 1


def test_company_watch_upsert_converges_when_another_writer_inserts_first(
    store, supabase_client, monkeypatch
):
    """A row inserted between our read and our write must not become a second row.

    Replaces the SQLite original's two-connection barrier test, which
    synchronized two `sqlite3` connections at the legacy SELECT. There is no
    connection to synchronize against PostgREST, so the race is staged
    directly: the competing row is written from inside the store's own read,
    which is exactly the window the original test opened. Convergence is held
    up by `unique (user_id, normalized_company_name)` and the
    merge-duplicates upsert, not by application care.
    """
    normalized = normalize_company_name("Acme GmbH")
    competitor: list[str] = []
    original_select = supabase_client.select

    def select_then_race(table, **kwargs):
        rows = original_select(table, **kwargs)
        if table == "job_hunter_company_watch" and not competitor:
            written = supabase_client.upsert(
                "job_hunter_company_watch",
                [
                    {
                        "user_id": supabase_client.user_id,
                        "company_name": "Acme GmbH",
                        "normalized_company_name": normalized,
                        "careers_url": "https://acme.test/careers",
                        "promotion_source": "automatic",
                        "confidence": 0.1,
                        "first_seen_at": "2026-09-06T00:00:00+00:00",
                        "updated_at": "2026-09-06T00:00:00+00:00",
                    }
                ],
                on_conflict="user_id,normalized_company_name",
            )
            competitor.append(written[0]["id"])
        return rows

    monkeypatch.setattr(supabase_client, "select", select_then_race)

    watch_id = _watch(store, company_name="Acme GmbH")

    assert competitor, "the staged competing write never ran"
    assert watch_id == competitor[0]
    assert len(_watch_rows(supabase_client)) == 1


def test_automatic_generic_url_cannot_replace_manual_greenhouse_target(store):
    _watch(store, company_name="Acme", promotion_source="manual", confidence=1.0)

    _watch(
        store,
        company_name="Acme GmbH",
        careers_url="https://acme.test/careers",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="automatic",
        confidence=1.0,
    )

    row = store.get_company_watch("ACME")
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert row["careers_url"] == ""
    assert row["promotion_source"] == "manual"


def test_supported_ats_target_upgrades_automatic_generic_entry(store):
    _watch(
        store,
        company_name="Acme",
        careers_url="https://acme.test/careers",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="automatic",
        confidence=0.4,
    )

    _watch(
        store,
        company_name="Acme GmbH",
        promotion_source="automatic",
        confidence=0.9,
    )

    row = store.get_company_watch("Acme")
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert row["careers_url"] == ""
    assert row["confidence"] == 0.9


def test_equal_strength_target_replaces_only_at_higher_confidence(store):
    _watch(
        store,
        company_name="Beta",
        careers_url="https://beta.test/careers",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="manual",
        confidence=0.8,
    )

    _watch(
        store,
        company_name="Beta",
        careers_url="https://beta.test/jobs",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="automatic",
        confidence=0.8,
    )
    assert store.get_company_watch("Beta")["careers_url"] == "https://beta.test/careers"

    _watch(
        store,
        company_name="Beta",
        careers_url="https://beta.test/jobs",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="automatic",
        confidence=0.9,
    )

    row = store.get_company_watch("Beta")
    assert row["careers_url"] == "https://beta.test/jobs"
    assert row["confidence"] == 0.9
    assert row["promotion_source"] == "manual"


def test_get_company_watch_is_none_for_unknown_and_unnormalizable_names(store):
    _watch(store, company_name="Acme")

    assert store.get_company_watch("Nobody") is None
    assert store.get_company_watch("   ") is None


def test_upsert_company_watch_rejects_a_name_that_normalizes_to_nothing(store):
    with pytest.raises(ValueError, match="must normalize to a non-empty value"):
        _watch(store, company_name="   ")


def test_list_due_company_watches_excludes_paused_and_inactive_targets(
    store, supabase_client
):
    unpaused_id = _watch(store, company_name="Acme")
    expired_id = _watch(store, company_name="Beta")
    paused_id = _watch(store, company_name="Gamma")
    inactive_id = _watch(store, company_name="Delta")
    supabase_client.update(
        "job_hunter_company_watch",
        {"paused_until": "2026-08-31T11:59:59+00:00"},
        params={"id": f"eq.{expired_id}"},
    )
    supabase_client.update(
        "job_hunter_company_watch",
        {"paused_until": "2026-08-31T12:00:01+00:00"},
        params={"id": f"eq.{paused_id}"},
    )
    supabase_client.update(
        "job_hunter_company_watch",
        {"active": False},
        params={"id": f"eq.{inactive_id}"},
    )

    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    assert [row["id"] for row in store.list_due_company_watches(now)] == [
        unpaused_id,
        expired_id,
    ]


def test_due_watch_compares_equivalent_offset_instants(store, supabase_client):
    """The due filter compares instants, not the strings they were written as.

    SQLite compared `julianday(paused_until) <= julianday(now)`; the port
    hands PostgREST `paused_until.lte.<iso>` against a `timestamptz` column.
    A pause written at `+02:00` and a `now` given at `-04:00` name the same
    instant, so the watch is due -- a string comparison would say otherwise.
    """
    watch_id = _watch(store)
    supabase_client.update(
        "job_hunter_company_watch",
        {"paused_until": "2026-08-31T14:00:00+02:00"},
        params={"id": f"eq.{watch_id}"},
    )
    same_instant = datetime(2026, 8, 31, 8, 0, tzinfo=timezone(timedelta(hours=-4)))

    assert [row["id"] for row in store.list_due_company_watches(same_instant)] == [
        watch_id
    ]


def test_first_two_watch_failures_remain_due(store):
    watch_id = _watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 2
    assert row["paused_until"] is None
    assert [due["id"] for due in store.list_due_company_watches(now)] == [watch_id]


def test_third_watch_failure_pauses_for_24_hours(store):
    watch_id = _watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    for _ in range(3):
        store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 3
    assert from_iso(row["paused_until"]) == datetime(
        2026, 9, 1, 12, 0, tzinfo=timezone.utc
    )
    assert store.list_due_company_watches(now) == []


def test_watch_failure_pause_is_24_elapsed_hours_across_dst(store):
    watch_id = _watch(store)
    before_spring_forward = datetime(
        2026, 3, 28, 12, 0, tzinfo=ZoneInfo("Europe/Berlin")
    )

    for _ in range(3):
        store.record_watch_failure(watch_id, before_spring_forward)

    row = store.get_company_watch("Acme")
    assert from_iso(row["paused_until"]) == datetime(
        2026, 3, 29, 11, 0, tzinfo=timezone.utc
    )


def test_watch_success_clears_failures_and_pause_and_stamps_health(store):
    watch_id = _watch(store)
    failed_at = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    succeeded_at = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)
    for _ in range(3):
        store.record_watch_failure(watch_id, failed_at)

    store.record_watch_success(watch_id, succeeded_at)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 0
    assert row["paused_until"] is None
    assert from_iso(row["last_successful_check_at"]) == succeeded_at
    assert from_iso(row["last_verified_at"]) == succeeded_at
    assert row["promotion_source"] == "manual"


def test_watch_success_normalizes_a_non_utc_offset(store):
    watch_id = _watch(store)
    now = datetime(2026, 8, 31, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    store.record_watch_success(watch_id, now)

    row = store.get_company_watch("Acme")
    assert from_iso(row["last_successful_check_at"]) == datetime(
        2026, 8, 31, 12, 0, tzinfo=timezone.utc
    )


@pytest.mark.parametrize(
    "method_name",
    [
        "list_due_company_watches",
        "record_watch_success",
        "record_watch_failure",
        "list_due_ats_boards",
        "reject_ats_board",
        "record_ats_scan_success",
        "record_ats_scan_failure",
        "record_ats_eligible_job",
    ],
)
def test_discovery_state_time_methods_reject_naive_datetimes(store, method_name):
    """A naive datetime is ambiguous, so it is refused rather than guessed.

    `store_mapping.to_iso` deliberately treats a naive datetime as already
    being UTC, which is right for a value the store itself stamped but wrong
    for one a caller passed in: a caller's naive local time would be silently
    recorded as UTC. These eight methods take the instant from their caller,
    so each one rejects it first (as the SQLite original's `_normalize_utc`
    did).
    """
    naive = datetime(2026, 8, 31, 12, 0)
    arguments = {
        "list_due_company_watches": (naive,),
        "record_watch_success": ("00000000-0000-0000-0000-000000000000", naive),
        "record_watch_failure": ("00000000-0000-0000-0000-000000000000", naive),
        "list_due_ats_boards": (naive,),
        "reject_ats_board": ("lever", "acme", "reason", naive),
        "record_ats_scan_success": ("lever", "acme", naive, 0),
        "record_ats_scan_failure": ("lever", "acme", naive),
        "record_ats_eligible_job": ("lever", "acme", naive),
    }[method_name]

    with pytest.raises(ValueError, match="timezone-aware"):
        getattr(store, method_name)(*arguments)


# ------------------------------------------------------------------
# ATS registry
# ------------------------------------------------------------------


def test_ats_registry_upsert_is_provider_board_unique(store):
    created = store.upsert_ats_board(
        provider="ashby",
        board_identifier="omnea",
        company_name="Omnea",
        market_hint="london",
    )
    repeated = store.upsert_ats_board(
        provider="ashby",
        board_identifier="omnea",
        company_name="Omnea Ltd",
        market_hint="london",
    )

    assert created is True
    assert repeated is False
    assert store.count_ats_boards() == 1


def test_ats_registry_upsert_does_not_wipe_metadata_with_blank_values(store):
    store.upsert_ats_board(
        provider="ashby",
        board_identifier="omnea",
        company_name="Omnea",
        market_hint="london",
    )
    store.upsert_ats_board(
        provider="ashby",
        board_identifier="omnea",
        company_name="",
        market_hint="",
    )

    due = store.list_due_ats_boards(datetime.now(timezone.utc))
    entry = next(e for e in due if e.board_identifier == "omnea")
    assert entry.company_name == "Omnea"
    assert entry.market_hint == "london"


def test_ats_registry_rejects_unsupported_provider(store):
    with pytest.raises(ValueError, match="unsupported ATS provider"):
        store.upsert_ats_board(provider="workday", board_identifier="x")


def test_ats_failure_pauses_board_without_rediscovery_bypassing_pause(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="acme")

    store.record_ats_scan_failure("lever", "acme", now)
    store.upsert_ats_board(provider="lever", board_identifier="acme")

    assert store.list_due_ats_boards(now + timedelta(hours=1)) == []
    due = store.list_due_ats_boards(now + timedelta(hours=25))
    assert [(entry.provider, entry.board_identifier) for entry in due] == [
        ("lever", "acme")
    ]


def test_ats_scan_success_records_job_count_and_resets_failures(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="greenhouse", board_identifier="acme")

    store.record_ats_scan_failure("greenhouse", "acme", now)
    later = now + timedelta(hours=1)
    store.record_ats_scan_success("greenhouse", "acme", later, job_count=7)

    due = store.list_due_ats_boards(later)
    assert len(due) == 1
    entry = due[0]
    assert entry.last_job_count == 7
    assert from_iso(entry.last_success_at) == later
    assert entry.consecutive_failures == 0
    assert entry.paused_until is None


def test_ats_permanent_failure_deactivates_board_after_three_in_a_row(store):
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


def test_ats_permanent_failure_stays_active_below_threshold(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="maybe-dead")

    store.record_ats_scan_failure("lever", "maybe-dead", now, permanent=True)
    store.record_ats_scan_failure(
        "lever", "maybe-dead", now + timedelta(hours=25), permanent=True
    )

    due = store.list_due_ats_boards(now + timedelta(hours=50))
    assert [e.board_identifier for e in due] == ["maybe-dead"]
    assert due[0].consecutive_failures == 2


def test_ats_transient_failure_never_deactivates_board(store):
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


def test_ats_mixed_transient_then_permanent_failure_deactivates_board(store):
    # consecutive_failures is one shared counter incremented by both
    # transient and permanent failures, so two transient failures followed
    # by a single permanent one reaches the threshold on that 404 alone —
    # not after three permanent failures in a row. This pins the documented
    # (if slightly surprising) real behavior of record_ats_scan_failure.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="mixed-co")

    store.record_ats_scan_failure("lever", "mixed-co", now, permanent=False)
    store.record_ats_scan_failure(
        "lever", "mixed-co", now + timedelta(hours=25), permanent=False
    )
    store.record_ats_scan_failure(
        "lever", "mixed-co", now + timedelta(hours=25 * 2), permanent=True
    )

    assert store.list_due_ats_boards(now + timedelta(days=30)) == []


def test_ats_deactivated_board_reactivates_on_rediscovery(store):
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


def test_reject_ats_board_deactivates_and_records_reason(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="jobgether")

    store.reject_ats_board("lever", "jobgether", "aggregator: 98% third-party", now)

    assert store.list_due_ats_boards(now) == []
    rejected = store.list_rejected_ats_boards()
    assert [e.board_identifier for e in rejected] == ["jobgether"]
    assert rejected[0].rejected_reason == "aggregator: 98% third-party"
    assert rejected[0].active is False


def test_list_rejected_ats_boards_excludes_healthy_and_health_paused_boards(store):
    # Only a board rejected for cause carries a reason -- a board merely
    # deactivated by repeated 404s must not show up as rejected.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="healthy-co")
    store.upsert_ats_board(provider="lever", board_identifier="dead-co")
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", "dead-co", now + timedelta(hours=25 * i), permanent=True
        )

    assert store.list_rejected_ats_boards() == []


def test_ats_rejected_board_is_not_resurrected_by_rediscovery(store):
    # Unlike a health-based deactivation (see
    # test_ats_deactivated_board_reactivates_on_rediscovery), a board
    # rejected for cause must stay rejected even when a freshly discovered
    # job points at it again -- upsert_ats_board must not flip active back
    # to true once rejected_reason is set.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="jobgether")
    store.reject_ats_board("lever", "jobgether", "aggregator: 98% third-party", now)

    store.upsert_ats_board(provider="lever", board_identifier="jobgether")

    assert store.list_due_ats_boards(now + timedelta(days=30)) == []


def test_clear_ats_board_rejection_makes_a_rejected_board_due_again(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="clientco")
    store.reject_ats_board("lever", "clientco", "aggregator: 98% third-party", now)

    store.clear_ats_board_rejection("lever", "clientco")

    assert store.list_rejected_ats_boards() == []
    due = store.list_due_ats_boards(now)
    assert [e.board_identifier for e in due] == ["clientco"]
    assert due[0].rejected_reason is None
    assert due[0].active is True


def test_clear_ats_board_rejection_matches_the_stored_provider_case_insensitively(store):
    # Callers hold normalized ats_board_key values ("lever:jobgether"), while
    # the row was written from whatever case discovery saw.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="ClientCo")
    store.reject_ats_board("lever", "ClientCo", "aggregator: 98% third-party", now)

    store.clear_ats_board_rejection("Lever", "clientco")

    assert store.list_rejected_ats_boards() == []
    assert [e.board_identifier for e in store.list_due_ats_boards(now)] == ["ClientCo"]


def test_clear_ats_board_rejection_treats_underscores_as_literal_text(store):
    """`_` is a single-character wildcard to `ilike`, so matching can't use it.

    Two boards whose identifiers differ only where one has an underscore
    would both match an `ilike` filter built from either name. Clearing one
    rejection must leave the other rejected.
    """
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="client_co")
    store.upsert_ats_board(provider="lever", board_identifier="clientxco")
    store.reject_ats_board("lever", "client_co", "aggregator", now)
    store.reject_ats_board("lever", "clientxco", "aggregator", now)

    store.clear_ats_board_rejection("lever", "client_co")

    assert [e.board_identifier for e in store.list_rejected_ats_boards()] == [
        "clientxco"
    ]
    assert [e.board_identifier for e in store.list_due_ats_boards(now)] == ["client_co"]


def test_clear_ats_board_rejection_is_a_no_op_for_a_board_that_was_never_rejected(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="healthy-co")

    store.clear_ats_board_rejection("lever", "healthy-co")
    store.clear_ats_board_rejection("lever", "never-registered")

    assert [e.board_identifier for e in store.list_due_ats_boards(now)] == ["healthy-co"]


def test_clear_ats_board_rejection_does_not_revive_a_health_deactivated_board(store):
    # A board deactivated by repeated 404s is broken, not misjudged. Clearing
    # a rejection it never had must not put it back in the rotation.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="dead-co")
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", "dead-co", now + timedelta(hours=25 * i), permanent=True
        )

    store.clear_ats_board_rejection("lever", "dead-co")

    assert store.list_due_ats_boards(now + timedelta(days=30)) == []


def test_record_ats_eligible_job_counts_every_sighting(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="acme")

    store.record_ats_eligible_job("lever", "acme", now)
    later = now + timedelta(hours=2)
    store.record_ats_eligible_job("lever", "acme", later)

    entry = store.list_due_ats_boards(later)[0]
    assert entry.eligible_jobs_seen == 2
    assert from_iso(entry.last_eligible_at) == later


def test_list_due_ats_boards_orders_by_provider_then_board(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    store.upsert_ats_board(provider="lever", board_identifier="zeta")
    store.upsert_ats_board(provider="ashby", board_identifier="beta")
    store.upsert_ats_board(provider="ashby", board_identifier="alpha")

    due = store.list_due_ats_boards(now)

    assert [(e.provider, e.board_identifier) for e in due] == [
        ("ashby", "alpha"),
        ("ashby", "beta"),
        ("lever", "zeta"),
    ]


def test_record_job_source_is_idempotent(store):
    job_id, _, _ = store.upsert_job(
        Job(source="yc", title="Frontend Engineer", company="Acme", url="https://yc.test/job/123")
    )

    store.record_job_source(
        job_id,
        source="yc",
        source_job_id="123",
        source_url="https://yc.test/job/123",
    )
    store.record_job_source(
        job_id,
        source="yc",
        source_job_id="123",
        source_url="https://yc.test/job/123",
    )

    rows = store.list_job_sources(job_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "yc"
    assert rows[0]["source_job_id"] == "123"


def test_provenance_without_source_id_uses_canonical_url(store):
    job_id, _, _ = store.upsert_job(
        Job(source="yc", title="Frontend Engineer", company="Acme")
    )

    store.record_job_source(
        job_id,
        source="yc",
        source_job_id=None,
        source_url="https://yc.test/job/123?utm_source=digest",
    )
    store.record_job_source(
        job_id,
        source="yc",
        source_job_id=None,
        source_url="https://yc.test/job/123",
    )

    rows = store.list_job_sources(job_id)
    assert len(rows) == 1
    assert rows[0]["identity_key"] == "url:https://yc.test/job/123"
    # The second call resolved to the same identity via a differently-decorated
    # raw URL; the original source_url (and every other non-last_seen_at column)
    # must survive, matching the SQLite original's DO UPDATE SET last_seen_at only.
    assert rows[0]["source_url"] == "https://yc.test/job/123?utm_source=digest"


def test_strong_lookups_find_the_single_matching_job(store):
    job_id, _, _ = store.upsert_job(
        Job(
            source="greenhouse",
            source_job_id="posting-1",
            title="Senior Frontend Engineer",
            company="Acme GmbH",
            location="Berlin, Germany",
            url="https://boards.greenhouse.io/acme/jobs/posting-1?gh_src=feed",
            canonical_url="https://boards.greenhouse.io/acme/jobs/posting-1",
            ats_provider="greenhouse",
            ats_board="acme",
            ats_job_id="posting-1",
        )
    )

    assert store.find_job_by_canonical_url(
        "https://boards.greenhouse.io/acme/jobs/posting-1?utm_source=email"
    ) == job_id
    assert store.find_job_by_ats("greenhouse", "acme", "posting-1") == job_id
    assert store.find_job_by_identity("ACME", "senior frontend engineer", "Berlin") == job_id


def test_find_job_by_canonical_url_matches_percent_encoded_query_values(store):
    # job_hunter_canonicalize_url (the SQL authority) and normalize.canonicalize_url
    # (Python) disagree on percent-encoding: Python's parse_qsl/urlencode round trip
    # turns "%20" into "+", SQL leaves the raw text alone. upsert_job stores
    # canonical_url via the SQL function (no explicit canonical_url is given here,
    # so it's derived from url), so the lookup must canonicalize the same way or a
    # URL that differs only in encoding won't resolve to the job that owns it.
    job_id, _, _ = store.upsert_job(
        Job(
            source="ashby",
            title="Frontend Engineer",
            url="https://jobs.ashbyhq.com/acme/xyz?q=a%20b",
        )
    )

    assert store.find_job_by_canonical_url(
        "https://jobs.ashbyhq.com/acme/xyz?q=a%20b"
    ) == job_id


def test_unresolved_rediscovery_retains_existing_canonical_and_ats_metadata(store):
    job_id, _, _ = store.upsert_job(
        Job(
            source="greenhouse",
            source_job_id="posting-1",
            title="Senior Frontend Engineer",
            company="Acme GmbH",
            location="Berlin, Germany",
            url="https://boards.greenhouse.io/acme/jobs/posting-1?gh_src=feed",
            canonical_url="https://boards.greenhouse.io/acme/jobs/posting-1",
            ats_provider="greenhouse",
            ats_board="acme",
            ats_job_id="posting-1",
        )
    )

    store.upsert_job(
        Job(
            source="greenhouse",
            source_job_id="posting-1",
            title="Senior Frontend Engineer",
            company="Acme GmbH",
            location="Berlin, Germany",
        )
    )

    assert store.find_job_by_canonical_url(
        "https://boards.greenhouse.io/acme/jobs/posting-1?utm_source=email"
    ) == job_id
    assert store.find_job_by_ats("greenhouse", "acme", "posting-1") == job_id


def test_identity_lookup_rejects_ambiguous_matches(store):
    for source_job_id in ("1", "2"):
        store.upsert_job(
            Job(
                source="source",
                source_job_id=source_job_id,
                title="Frontend Engineer",
                company="Acme",
                location="Berlin",
            )
        )

    assert store.find_job_by_identity("Acme", "Frontend Engineer", "Berlin") is None


def test_upsert_dedupes_and_detects_description_change(store):
    job = Job(source="lever", source_job_id="1", title="Senior Product Engineer", description="React")
    job_id, is_new, changed = store.upsert_job(job)
    assert (is_new, changed) == (True, False)

    same_id, is_new, changed = store.upsert_job(job)
    assert same_id == job_id
    assert (is_new, changed) == (False, False)

    job.description = "React TypeScript"
    same_id, is_new, changed = store.upsert_job(job)
    assert (is_new, changed) == (False, True)


def test_upsert_job_reports_description_change(store):
    job = make_job(fingerprint="fp-1", description="first")
    job_id, is_new, changed = store.upsert_job(job)
    assert is_new is True and changed is False

    same_id, is_new, changed = store.upsert_job(
        make_job(fingerprint="fp-1", description="second")
    )
    assert same_id == job_id
    assert is_new is False
    assert changed is True


def test_needs_evaluation_new_job(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    assert store.needs_evaluation(job_id) is True


def test_needs_evaluation_false_after_rediscovering_unchanged_job(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer", description="React")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    assert store.needs_evaluation(job_id) is False

    # Simulate a later run rediscovering the same, unchanged job.
    same_id, is_new, changed = store.upsert_job(job)
    assert (is_new, changed) == (False, False)
    assert store.needs_evaluation(same_id) is False


def test_needs_evaluation_true_after_description_changes_post_evaluation(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer", description="React")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    assert store.needs_evaluation(job_id) is False

    job.description = "React and TypeScript"
    store.upsert_job(job)
    assert store.needs_evaluation(job_id) is True


def test_needs_evaluation_true_after_content_confidence_changes_post_evaluation(store):
    job = Job(
        source="x", source_job_id="1", title="Senior Product Engineer",
        description="React", content_confidence=AGGREGATOR_TEXT,
    )
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, content_confidence=AGGREGATOR_TEXT))
    assert store.needs_evaluation(job_id) is False

    # Description text stays byte-identical, but the job's tier upgrades
    # (e.g. duplicate postings merge and the surviving row's tier improves).
    job.content_confidence = OFFICIAL_ATS
    same_id, _is_new, description_changed = store.upsert_job(job)
    assert description_changed is False
    assert store.needs_evaluation(same_id) is True


def test_count_and_delivery(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    assert store.count_jobs() == 1
    assert store.has_delivery(job_id) is False
    store.mark_delivered(job_id, "telegram_message")
    assert store.has_delivery(job_id) is True


def test_has_delivery_filters_by_type(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)

    assert store.has_delivery(job_id, "telegram_message") is False
    assert store.has_delivery(job_id, "telegram_document") is False

    store.mark_delivered(job_id, "telegram_message")

    assert store.has_delivery(job_id, "telegram_message") is True
    assert store.has_delivery(job_id, "telegram_document") is False
    assert store.has_delivery(job_id) is True


def test_pending_delivery_job_ids_excludes_score_sixty_possible_match(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=60, decision="possible_match"))

    assert store.pending_delivery_job_ids() == []


def test_pending_delivery_job_ids_excludes_score_sixty_ready_match(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=60, decision="high_priority"))

    assert store.pending_delivery_job_ids() == []


def test_pending_delivery_job_ids_keeps_score_sixty_one_possible_match_until_message_sent(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=61, decision="possible_match"))

    assert store.pending_delivery_job_ids() == [job_id]

    store.mark_delivered(job_id, "telegram_message")
    assert store.pending_delivery_job_ids() == []


def test_pending_delivery_job_ids_keeps_score_sixty_one_ready_match_until_message_sent(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=61, decision="package_match"))

    assert store.pending_delivery_job_ids() == [job_id]

    store.mark_delivered(job_id, "telegram_message")
    assert store.pending_delivery_job_ids() == []


def test_pending_delivery_job_ids_excludes_ready_match_with_message_but_no_document(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=61, decision="high_priority"))
    store.mark_delivered(job_id, "telegram_message")

    assert store.pending_delivery_job_ids() == []


def test_get_evaluation_and_material_roundtrip(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer", company="Acme", description="React")
    job_id, _, _ = store.upsert_job(job)

    assert store.get_evaluation(job_id) is None
    assert store.get_material(job_id) is None

    store.save_evaluation(job_id, _evaluation(job_id, hard_blockers=["visa"], strengths=["React"]))
    evaluation = store.get_evaluation(job_id)
    assert evaluation is not None
    assert evaluation.decision == "high_priority"
    assert evaluation.hard_blockers == ["visa"]
    assert evaluation.strengths == ["React"]

    from job_hunter.models import Material

    store.save_material(job_id, Material(job_id=job_id, cover_letter_text="Dear Hiring Team,"))
    material = store.get_material(job_id)
    assert material is not None
    assert material.cover_letter_text == "Dear Hiring Team,"

    fetched_job = store.get_job(job_id)
    assert fetched_job is not None
    assert fetched_job.company == "Acme"


def test_job_market_round_trip(store):
    job_id, _, _ = store.upsert_logical_job(
        Job(source="x", title="Senior Frontend Engineer", location="London")
    )

    assert store.get_job(job_id).market_id is None

    store.set_job_market(job_id, "london")
    assert store.get_job(job_id).market_id == "london"


def test_set_job_market_treats_none_as_unset(store):
    job_id, _, _ = store.upsert_logical_job(
        Job(source="x", title="Senior Frontend Engineer", location="London")
    )
    store.set_job_market(job_id, "london")

    store.set_job_market(job_id, None)
    assert store.get_job(job_id).market_id is None


def test_evaluation_market_id_round_trip(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer", company="Acme")
    job_id, _, _ = store.upsert_job(job)

    store.save_evaluation(job_id, _evaluation(job_id, market_id="london"))
    evaluation = store.get_evaluation(job_id)
    assert evaluation is not None
    assert evaluation.market_id == "london"


def test_gmail_persistence_schema_minimizes_private_email_data(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")

    tables = {
        row["name"]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {
        "gmail_sync_state",
        "gmail_messages",
        "inbound_job_candidates",
        "application_events",
        "review_deliveries",
    } <= tables

    for table in ("gmail_messages", "inbound_job_candidates", "application_events"):
        columns = {
            row["name"] for row in store._conn.execute(f"PRAGMA table_info({table})")
        }
        assert "body" not in columns
        assert "email_body" not in columns


def test_gmail_message_and_sync_state_roundtrip(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")

    assert store.has_processed_gmail_message("m1") is False
    store.record_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="alerts@example.com",
        subject="Frontend roles",
        occurred_at="2026-08-31T10:00:00+00:00",
        classification="JOB_ALERT",
        confidence=0.98,
        rationale="sender rule",
    )
    assert store.has_processed_gmail_message("m1") is True

    assert store.get_gmail_sync_state("primary") is None
    store.save_gmail_sync_state(
        account_id="primary",
        history_id="h1",
        last_successful_sync_at="2026-08-31T10:05:00+00:00",
        backfill_completed_at="2026-08-31T10:05:00+00:00",
    )
    state = store.get_gmail_sync_state("primary")
    assert state is not None
    assert dict(state)["history_id"] == "h1"
    assert dict(state)["backfill_completed_at"] == "2026-08-31T10:05:00+00:00"


def test_inbound_candidate_source_message_and_key_are_idempotent(tmp_path, monkeypatch):
    store = JobStore(tmp_path / "state.sqlite3")
    timestamps = iter(("2026-08-31T10:00:00+00:00", "2026-08-31T10:01:00+00:00"))
    monkeypatch.setattr("job_hunter.store._now_iso", lambda: next(timestamps))
    job = ExtractedJob(
        source_platform="linkedin",
        source_job_id="job-1",
        url="https://jobs.example.com/1",
        company="Acme",
        title="Frontend Engineer",
    )

    first = store.stage_inbound_job("m1", "linkedin:job-1", job)
    second = store.stage_inbound_job("m1", "linkedin:job-1", job)

    row = store._conn.execute(
        "SELECT id, created_at, last_seen_at FROM inbound_job_candidates"
    ).fetchone()
    assert second == first == row["id"]
    assert row["created_at"] == "2026-08-31T10:00:00+00:00"
    assert row["last_seen_at"] == "2026-08-31T10:01:00+00:00"


def test_application_event_source_message_is_idempotent(tmp_path):
    store = JobStore(tmp_path / "db.sqlite3")
    first = store.save_application_event(
        job_id=None,
        event_type="REVIEW_NEEDED",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="m1",
        source_thread_id="t1",
        confidence=0.4,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="ambiguous",
    )
    second = store.save_application_event(
        job_id=None,
        event_type="REVIEW_NEEDED",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="m1",
        source_thread_id="t1",
        confidence=0.4,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="ambiguous",
    )
    assert second == first


def test_current_application_state_derives_the_latest_eligible_event(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(
        Job(source="manual", source_job_id="1", title="Frontend Engineer")
    )
    store.save_application_event(
        job_id=job_id,
        event_type="OFFER",
        occurred_at="2026-08-01T10:00:00+00:00",
        source_message_id="m1",
        source_thread_id="t1",
        confidence=0.95,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="offer",
    )
    store.save_application_event(
        job_id=job_id,
        event_type="APPLIED",
        occurred_at="2026-08-02T10:00:00+00:00",
        source_message_id="m2",
        source_thread_id="t1",
        confidence=0.95,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="application",
    )

    assert store.current_application_state(job_id) == "APPLIED"


def test_pending_reviews_include_subject_and_are_marked_delivered(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job_id, _, _ = store.upsert_job(
        Job(source="manual", source_job_id="1", title="Frontend Engineer")
    )
    store.record_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="recruiter@example.com",
        subject="A role to discuss",
        occurred_at="2026-08-31T10:00:00+00:00",
        classification="REVIEW_NEEDED",
        confidence=0.4,
        rationale="ambiguous",
    )
    event_id = store.save_application_event(
        job_id=job_id,
        event_type="REVIEW_NEEDED",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="m1",
        source_thread_id="t1",
        confidence=0.4,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="ambiguous",
    )

    assert [row["id"] for row in store.list_application_events(job_id)] == [event_id]
    pending = store.pending_review_events()
    assert [(row["id"], row["subject"]) for row in pending] == [
        (event_id, "A role to discuss")
    ]

    store.mark_review_delivered([event_id], "telegram-1")
    assert store.pending_review_events() == []


def test_candidate_not_emitted_when_any_job_has_same_canonical_url(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    store.stage_inbound_job(
        "m1",
        "linkedin:1",
        ExtractedJob(
            source_platform="linkedin",
            source_job_id="1",
            url="https://jobs.example.com/role?utm_source=linkedin",
            company="Different Company",
            title="Different Title",
        ),
    )
    store.upsert_job(
        Job(
            source="public",
            source_job_id="public-1",
            url="https://jobs.example.com/role",
            company="Acme",
            title="Frontend Engineer",
        )
    )

    assert store.list_unmaterialized_inbound_jobs() == []


def test_candidate_not_emitted_when_gmail_source_and_candidate_key_match(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    store.stage_inbound_job(
        "m1",
        "candidate-key-1",
        ExtractedJob(
            source_platform="talentboard",
            url="https://email.example/jobs/one",
            company="Email Company",
            title="Email Role",
        ),
    )
    store.upsert_job(
        Job(
            source="gmail:talentboard",
            source_job_id="candidate-key-1",
            url="https://materialized.example/jobs/different",
            company="Materialized Company",
            title="Materialized Role",
        )
    )

    assert store.list_unmaterialized_inbound_jobs() == []


def test_candidate_not_emitted_when_url_missing_but_identity_matches(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    store.stage_inbound_job(
        "m1",
        "linkedin:1",
        ExtractedJob(
            source_platform="linkedin",
            source_job_id="1",
            company="  ACME  ",
            title="Senior   Frontend Engineer",
            location="Berlin",
        ),
    )
    store.upsert_job(
        Job(
            source="public",
            source_job_id="public-1",
            url="https://jobs.example.com/role",
            company="Acme",
            title="senior frontend engineer",
            location=" berlin ",
        )
    )

    assert store.list_unmaterialized_inbound_jobs() == []


def test_candidate_emitted_when_no_existing_job_matches(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    candidate_id = store.stage_inbound_job(
        "m1",
        "linkedin:1",
        ExtractedJob(
            source_platform="linkedin",
            source_job_id="1",
            url="https://jobs.example.com/role",
            company="Acme",
            title="Frontend Engineer",
        ),
    )

    rows = store.list_unmaterialized_inbound_jobs()
    assert [(row["id"], row["source_candidate_key"]) for row in rows] == [
        (candidate_id, "linkedin:1")
    ]


def test_same_canonical_job_from_two_sources_uses_one_job_id(store):
    first = Job(
        source="gmail:linkedin",
        title="Senior Frontend Engineer",
        company="Acme",
        url="https://linkedin.test/1",
        original_url="https://linkedin.test/1",
        canonical_url="https://jobs.lever.co/acme/abc",
        ats_provider="lever",
        ats_board="acme",
        ats_job_id="abc",
    )
    second = Job(
        source="yc",
        title="Senior Frontend Engineer",
        company="Acme GmbH",
        url="https://yc.test/2",
        original_url="https://yc.test/2",
        canonical_url="https://jobs.lever.co/acme/abc",
        ats_provider="lever",
        ats_board="acme",
        ats_job_id="abc",
    )

    first_id, _, _ = store.upsert_logical_job(first)
    second_id, _, _ = store.upsert_logical_job(second)

    assert first_id == second_id
    assert {row["source"] for row in store.list_job_sources(first_id)} == {
        "gmail:linkedin",
        "yc",
    }


def test_different_titles_at_same_company_do_not_merge(store):
    first_id, _, _ = store.upsert_logical_job(
        Job(source="a", title="Senior Frontend Engineer", company="Acme", location="Berlin")
    )
    second_id, _, _ = store.upsert_logical_job(
        Job(source="b", title="Staff Frontend Engineer", company="Acme", location="Berlin")
    )
    assert first_id != second_id


def test_same_title_at_different_companies_does_not_merge(store):
    first_id, _, _ = store.upsert_logical_job(
        Job(source="a", title="Senior Frontend Engineer", company="Acme", location="Berlin")
    )
    second_id, _, _ = store.upsert_logical_job(
        Job(source="b", title="Senior Frontend Engineer", company="Beta", location="Berlin")
    )
    assert first_id != second_id


def test_merge_jobs_preserves_associations_provenance_and_richer_fields(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    plain_id, _, _ = store.upsert_job(
        Job(source="gmail:linkedin", source_job_id="1", title="Frontend Engineer")
    )
    history_id, _, _ = store.upsert_job(
        Job(
            source="yc",
            source_job_id="2",
            title="Frontend Engineer",
            company="Acme",
            description="A detailed React role description",
            canonical_url="https://jobs.lever.co/acme/abc",
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        )
    )
    store.record_job_source(
        plain_id,
        source="gmail:linkedin",
        source_job_id="1",
        source_url="https://linkedin.test/1",
    )
    store.record_job_source(
        history_id,
        source="yc",
        source_job_id="2",
        source_url="https://yc.test/2",
    )
    store.save_application_event(
        job_id=history_id,
        event_type="APPLIED",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="merge-message",
        source_thread_id=None,
        confidence=1.0,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="application confirmation",
    )
    store.save_evaluation(history_id, _evaluation(history_id))
    store.save_material(
        history_id,
        Material(job_id=history_id, cover_letter_text="Tailored letter"),
    )
    store.mark_delivered(history_id, "telegram_message", "delivery-1")
    now = "2026-08-31T10:00:00+00:00"
    store._conn.execute(
        """
        INSERT INTO company_watch
            (company_name, normalized_company_name, discovered_from_job_id,
             promotion_source, confidence, first_seen_at, created_at, updated_at)
        VALUES ('Acme', 'acme', ?, 'automatic', 1.0, ?, ?, ?)
        """,
        (history_id, now, now, now),
    )
    store._conn.commit()

    assert store.merge_jobs(plain_id, history_id) == history_id

    merged = store._conn.execute("SELECT * FROM jobs WHERE id = ?", (history_id,)).fetchone()
    assert merged["company"] == "Acme"
    assert merged["description"] == "A detailed React role description"
    assert merged["url"] == "https://jobs.lever.co/acme/abc"
    assert merged["ats_provider"] == "lever"
    assert store.count_jobs() == 1
    assert {row["source"] for row in store.list_job_sources(history_id)} == {
        "gmail:linkedin",
        "yc",
    }
    for table in ("application_events", "evaluations", "materials", "deliveries"):
        row = store._conn.execute(f"SELECT job_id FROM {table}").fetchone()
        assert row["job_id"] == history_id
    watch = store._conn.execute("SELECT discovered_from_job_id FROM company_watch").fetchone()
    assert watch["discovered_from_job_id"] == history_id


def test_late_canonical_merge_keeps_application_history_job_and_all_associations(
    tmp_path,
):
    store = JobStore(tmp_path / "state.sqlite3")
    legacy_url = "https://aggregator.test/jobs/acme-frontend"
    canonical_url = "https://jobs.lever.co/acme/abc"
    legacy_id, _, _ = store.upsert_job(
        Job(
            source="aggregator",
            source_job_id="legacy-1",
            title="Senior Frontend Engineer",
            company="Acme GmbH",
            location="Berlin",
            url=legacy_url,
            description="React and TypeScript role",
        )
    )
    store.record_job_source(
        legacy_id,
        source="aggregator",
        source_job_id="legacy-1",
        source_url=legacy_url,
    )
    store.save_evaluation(legacy_id, _evaluation(legacy_id))
    store.save_material(
        legacy_id,
        Material(job_id=legacy_id, cover_letter_text="Tailored letter"),
    )
    store.mark_delivered(legacy_id, "telegram_message", "delivery-1")
    store.save_application_event(
        job_id=legacy_id,
        event_type="INTERVIEW",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="late-canonical-interview",
        source_thread_id=None,
        confidence=1.0,
        company="Acme",
        role_title="Senior Frontend Engineer",
        rationale="interview invitation",
    )

    canonical_id, _, _ = store.upsert_job(
        Job(
            source="lever",
            source_job_id="abc",
            title="senior frontend engineer",
            company="ACME",
            location="Berlin",
            url=canonical_url,
            canonical_url=canonical_url,
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        )
    )
    store.record_job_source(
        canonical_id,
        source="lever",
        source_job_id="abc",
        source_url=canonical_url,
    )

    survivor_id, is_new, _description_changed = store.upsert_logical_job(
        Job(
            source="aggregator",
            source_job_id="legacy-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="Berlin",
            url=canonical_url,
            original_url=legacy_url,
            canonical_url=canonical_url,
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
            description="React and TypeScript role",
        )
    )

    assert canonical_id != legacy_id
    assert survivor_id == legacy_id
    assert is_new is False
    assert store.count_jobs() == 1
    assert store.get_evaluation(survivor_id) is not None
    assert store.get_material(survivor_id) is not None
    assert store.has_delivery(survivor_id, "telegram_message")
    assert store.current_application_state(survivor_id) == "INTERVIEW"
    assert store.get_job(survivor_id).url == canonical_url
    assert {row["source"] for row in store.list_job_sources(survivor_id)} == {
        "aggregator",
        "lever",
    }


def test_late_canonical_upsert_enriches_single_existing_job_in_place(store):
    legacy_url = "https://aggregator.test/jobs/acme-frontend"
    canonical_url = "https://jobs.lever.co/acme/abc"
    existing_id, _, _ = store.upsert_job(
        Job(
            source="aggregator",
            source_job_id="legacy-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="Berlin",
            url=legacy_url,
        )
    )

    job_id, is_new, _description_changed = store.upsert_logical_job(
        Job(
            source="aggregator",
            source_job_id="legacy-1",
            title="Senior Frontend Engineer",
            company="Acme GmbH",
            location="Berlin",
            url=canonical_url,
            original_url=legacy_url,
            canonical_url=canonical_url,
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        )
    )

    assert job_id == existing_id
    assert is_new is False
    assert store.count_jobs() == 1
    assert store.get_job(existing_id).url == canonical_url


def test_logical_upsert_merges_all_exact_matches_into_global_history_survivor(
    tmp_path,
):
    store = JobStore(tmp_path / "state.sqlite3")
    canonical_url = "https://jobs.lever.co/acme/abc"
    rows = [
        Job(
            source="canonical-evaluation",
            source_job_id="canonical-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="New York",
            url=canonical_url,
            canonical_url=canonical_url,
        ),
        Job(
            source="canonical-material",
            source_job_id="canonical-2",
            title="senior frontend engineer",
            company="ACME GmbH",
            location="Berlin",
            url=canonical_url,
            canonical_url=canonical_url,
        ),
        Job(
            source="ats-application",
            source_job_id="ats-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="London",
            url="https://aggregator.test/jobs/ats-1",
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        ),
        Job(
            source="ats-delivery",
            source_job_id="ats-2",
            title="Senior Frontend Engineer",
            company="Acme",
            location="Berlin",
            url="https://aggregator.test/jobs/ats-2",
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        ),
    ]
    job_ids = []
    for job in rows:
        job_id, _, _ = store.upsert_job(job)
        job_ids.append(job_id)
        store.record_job_source(
            job_id,
            source=job.source,
            source_job_id=job.source_job_id,
            source_url=job.url,
        )

    assert store.find_job_by_canonical_url(canonical_url) is None
    assert store.find_job_by_ats("lever", "acme", "abc") is None
    assert store.find_job_by_identity(
        "Acme", "Senior Frontend Engineer", "Berlin"
    ) is None

    evaluation_id, material_id, application_id, delivery_id = job_ids
    store.save_evaluation(evaluation_id, _evaluation(evaluation_id))
    store.save_material(
        material_id,
        Material(job_id=material_id, cover_letter_text="Existing material"),
    )
    store.save_application_event(
        job_id=application_id,
        event_type="INTERVIEW",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="all-match-application",
        source_thread_id=None,
        confidence=1.0,
        company="Acme",
        role_title="Senior Frontend Engineer",
        rationale="interview invitation",
    )
    store.mark_delivered(delivery_id, "telegram_message", "delivery-1")

    survivor_id, is_new, _description_changed = store.upsert_logical_job(
        Job(
            source="ats-delivery",
            source_job_id="ats-2",
            title="Senior Frontend Engineer",
            company="Acme",
            location="Berlin",
            url=canonical_url,
            original_url="https://aggregator.test/jobs/ats-2",
            canonical_url=canonical_url,
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        )
    )

    assert survivor_id == application_id
    assert is_new is False
    assert store.count_jobs() == 1
    assert store.get_job(survivor_id).url == canonical_url
    assert store.get_evaluation(survivor_id) is not None
    assert store.get_material(survivor_id) is not None
    assert store.has_delivery(survivor_id, "telegram_message")
    assert store.current_application_state(survivor_id) == "INTERVIEW"
    assert {row["source"] for row in store.list_job_sources(survivor_id)} == {
        "canonical-evaluation",
        "canonical-material",
        "ats-application",
        "ats-delivery",
    }
    for table in ("evaluations", "materials", "application_events", "deliveries"):
        associated_ids = {
            row["job_id"] for row in store._conn.execute(f"SELECT job_id FROM {table}")
        }
        assert associated_ids == {survivor_id}


def test_missing_location_does_not_merge_incompatible_role_locations(store):
    berlin_id, _, _ = store.upsert_job(
        Job(
            source="aggregator",
            source_job_id="berlin-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="Berlin",
            url="https://aggregator.test/jobs/berlin-1",
        )
    )
    berlin_duplicate_id, _, _ = store.upsert_job(
        Job(
            source="second",
            source_job_id="berlin-2",
            title="senior frontend engineer",
            company="ACME GmbH",
            location="Berlin, Germany",
            url="https://second.test/jobs/berlin-2",
        )
    )
    new_york_id, _, _ = store.upsert_job(
        Job(
            source="third",
            source_job_id="new-york-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="New York",
            url="https://third.test/jobs/new-york-1",
        )
    )

    berlin_survivor, _, _ = store.upsert_logical_job(
        Job(
            source="aggregator",
            source_job_id="berlin-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="Berlin",
            url="https://aggregator.test/jobs/berlin-1",
        )
    )

    assert berlin_survivor == berlin_id
    assert store.count_jobs() == 2
    assert store.get_job(berlin_duplicate_id) is None
    assert store.get_job(new_york_id) is not None

    missing_location_id, _, _ = store.upsert_logical_job(
        Job(
            source="aggregator",
            source_job_id="berlin-1",
            title="Senior Frontend Engineer",
            company="Acme",
            location="",
            url="https://aggregator.test/jobs/berlin-1",
        )
    )

    assert missing_location_id == berlin_id
    assert store.count_jobs() == 2
    assert store.get_job(new_york_id) is not None


def test_merge_survivor_prefers_other_history_over_age_and_lower_id(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    older_id, _, _ = store.upsert_job(
        Job(source="older", source_job_id="1", title="Frontend Engineer")
    )
    history_id, _, _ = store.upsert_job(
        Job(source="history", source_job_id="2", title="Frontend Engineer")
    )
    store.save_material(
        history_id,
        Material(job_id=history_id, cover_letter_text="Existing material"),
    )

    assert store.merge_jobs(older_id, history_id) == history_id
    assert store.get_job(older_id) is None
    assert store.get_material(history_id) is not None


def test_merge_survivor_prefers_application_events_over_other_history(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    other_history_id, _, _ = store.upsert_job(
        Job(source="history", source_job_id="1", title="Frontend Engineer")
    )
    application_id, _, _ = store.upsert_job(
        Job(source="application", source_job_id="2", title="Frontend Engineer")
    )
    store.save_evaluation(other_history_id, _evaluation(other_history_id))
    store.save_material(
        other_history_id,
        Material(job_id=other_history_id, cover_letter_text="Existing material"),
    )
    store.mark_delivered(other_history_id, "telegram_message", "delivery-1")
    store.save_application_event(
        job_id=application_id,
        event_type="INTERVIEW",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="application-priority",
        source_thread_id=None,
        confidence=1.0,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="interview invitation",
    )

    assert store.merge_jobs(other_history_id, application_id) == application_id
    assert store.current_application_state(application_id) == "INTERVIEW"
    assert store.get_evaluation(application_id) is not None
    assert store.get_material(application_id) is not None
    assert store.has_delivery(application_id, "telegram_message")


def test_merge_survivor_prefers_older_first_seen_over_lower_id(store, supabase_client):
    # Ids are random uuids now, so "the older job" and "the lower-sorting id"
    # coincide about half the time by chance. Sort the two actual ids first
    # and deliberately give the age advantage to the HIGHER-sorting one, so
    # the assertion below can only hold if age genuinely beats the id
    # tie-break -- a survivor rule that wrongly checked id before age would
    # fail this, whereas it would pass a version of this test that left age
    # and id pointing the same way by luck.
    first_id, _, _ = store.upsert_job(
        Job(source="a", source_job_id="1", title="Frontend Engineer")
    )
    second_id, _, _ = store.upsert_job(
        Job(source="b", source_job_id="2", title="Frontend Engineer")
    )
    lower_id, higher_id = sorted((first_id, second_id))
    supabase_client.update(
        "job_hunter_jobs",
        {"first_seen_at": "2026-08-31T10:00:00+00:00"},
        params={"id": f"eq.{lower_id}"},
    )
    supabase_client.update(
        "job_hunter_jobs",
        {"first_seen_at": "2026-08-30T10:00:00+00:00"},
        params={"id": f"eq.{higher_id}"},
    )

    assert store.merge_jobs(lower_id, higher_id) == higher_id
    assert store.get_job(lower_id) is None
    assert store.get_job(higher_id) is not None


def test_merge_survivor_uses_lower_id_when_history_and_age_are_equal(store, supabase_client):
    # Ids are random uuids now, not sequential autoincrement integers, so
    # which of the two literally sorts lower can't be fixed by construction
    # order -- it's computed from the ids actually assigned, below.
    first_id, _, _ = store.upsert_job(
        Job(source="first", source_job_id="1", title="Frontend Engineer")
    )
    second_id, _, _ = store.upsert_job(
        Job(source="second", source_job_id="2", title="Frontend Engineer")
    )
    first_seen_at = "2026-08-31T10:00:00+00:00"
    supabase_client.update(
        "job_hunter_jobs",
        {"first_seen_at": first_seen_at},
        params={"id": f"in.({first_id},{second_id})"},
    )
    lower_id, higher_id = sorted((first_id, second_id))

    # Passing the higher id as the nominal survivor proves the tie-break
    # (lower id wins) overrides the caller's argument order.
    assert store.merge_jobs(higher_id, lower_id) == lower_id
    assert store.get_job(lower_id) is not None
    assert store.get_job(higher_id) is None


def test_merge_job_sources_preserves_seen_bounds_on_identity_conflict(store, supabase_client):
    survivor_id, _, _ = store.upsert_job(
        Job(source="first", source_job_id="1", title="Frontend Engineer")
    )
    duplicate_id, _, _ = store.upsert_job(
        Job(source="second", source_job_id="2", title="Frontend Engineer")
    )
    for job_id in (survivor_id, duplicate_id):
        store.record_job_source(
            job_id,
            source="shared",
            source_job_id="same-id",
            source_url="https://source.test/jobs/same-id",
        )
    supabase_client.update(
        "job_hunter_job_sources",
        {
            "first_seen_at": "2026-08-10T00:00:00+00:00",
            "last_seen_at": "2026-08-20T00:00:00+00:00",
        },
        params={"job_id": f"eq.{survivor_id}"},
    )
    supabase_client.update(
        "job_hunter_job_sources",
        {
            "first_seen_at": "2026-08-01T00:00:00+00:00",
            "last_seen_at": "2026-08-31T00:00:00+00:00",
        },
        params={"job_id": f"eq.{duplicate_id}"},
    )

    merged_id = store.merge_jobs(survivor_id, duplicate_id)

    sources = store.list_job_sources(merged_id)
    assert len(sources) == 1
    assert sources[0]["first_seen_at"] == "2026-08-01T00:00:00+00:00"
    assert sources[0]["last_seen_at"] == "2026-08-31T00:00:00+00:00"


def test_logical_upsert_reports_description_change_caused_by_merge(store):
    canonical_url = "https://jobs.lever.co/acme/abc"
    survivor_id, _, _ = store.upsert_job(
        Job(
            source="lever",
            source_job_id="abc",
            title="Senior Frontend Engineer",
            company="Acme",
            url=canonical_url,
            canonical_url=canonical_url,
            description="Short description",
        )
    )
    duplicate_description = "A much richer React and TypeScript role description"
    duplicate_id, _, _ = store.upsert_job(
        Job(
            source="yc",
            source_job_id="yc-1",
            title="Senior Frontend Engineer",
            company="Acme",
            url="https://yc.test/jobs/1",
            description=duplicate_description,
        )
    )

    job_id, is_new, description_changed = store.upsert_logical_job(
        Job(
            source="yc",
            source_job_id="yc-1",
            title="Senior Frontend Engineer",
            company="Acme",
            url="https://yc.test/jobs/1",
            original_url="https://yc.test/jobs/1",
            canonical_url=canonical_url,
            description=duplicate_description,
        )
    )

    assert duplicate_id != survivor_id
    assert job_id == survivor_id
    assert is_new is False
    assert description_changed is True
    assert store.count_jobs() == 1
    merged = store.get_job(survivor_id)
    assert merged.description == duplicate_description


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


def test_save_evaluation_persists_evaluation_confidence_not_jobs_row(store):
    # The jobs row can legitimately hold a different (e.g. stronger) tier than
    # the in-memory job that evaluate_job's gating logic actually acted on.
    # The persisted snapshot must reflect what drove the gating decision, not
    # whatever happens to be in the jobs table at save time.
    job_id, _, _ = store.upsert_job(
        Job(source="ashby", title="Eng", description="JD", content_confidence=OFFICIAL_ATS)
    )
    evaluation = Evaluation(
        job_id=job_id, total_score=60, scores={}, decision="possible_match",
        hard_blockers=[], strengths=[], gaps=[], salary_note="", location_note="",
        rationale="", model="test", content_confidence=AGGREGATOR_TEXT,
        requirements={},
    )
    store.save_evaluation(job_id, evaluation)
    saved = store.get_evaluation(job_id)
    assert saved.content_confidence == AGGREGATOR_TEXT


def test_evaluation_raw_model_score_round_trip(store):
    job_id, _, _ = store.upsert_job(Job(source="x", source_job_id="1", title="Analyst", company="Acme"))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=64, raw_model_score=89))
    loaded = store.get_evaluation(job_id)
    assert loaded.total_score == 64
    assert loaded.raw_model_score == 89


def test_evaluations_with_identical_evaluated_at_converge_on_one_deterministic_row(
    store, supabase_client, monkeypatch
):
    """"Latest evaluation" now means newest `evaluated_at`, not highest id
    (ids are random uuids, so max-id is meaningless). The naive worry is that
    two evaluations sharing the same `evaluated_at` would leave `get_evaluation`
    picking an arbitrary one of two rows.

    That scenario cannot actually arise: Task 1 put a
    `unique (user_id, job_id, evaluated_at)` constraint on this table
    specifically so a retried write converges instead of duplicating, and
    `save_evaluation` always writes through `upsert` against that constraint.
    So two saves for the same job that land on the same `evaluated_at` --
    forced here by freezing the clock, since `save_evaluation` always stamps
    "now" itself -- never produce two competing rows to choose between; the
    second save's `merge-duplicates` upsert overwrites the first row in
    place. This test proves that convergence: exactly one row exists
    afterwards, and `get_evaluation` deterministically reflects the second
    (last-written) call, not an arbitrary pick between two rows.
    """
    import job_hunter.postgres_store as postgres_store_module

    job_id, _, _ = store.upsert_job(
        Job(source="x", source_job_id="1", title="Senior Product Engineer")
    )

    frozen_instant = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_instant if tz is None else frozen_instant.astimezone(tz)

    monkeypatch.setattr(postgres_store_module, "datetime", _FrozenDatetime)

    store.save_evaluation(
        job_id, _evaluation(job_id, decision="possible_match", total_score=61)
    )
    store.save_evaluation(
        job_id, _evaluation(job_id, decision="high_priority", total_score=95)
    )

    rows = supabase_client.select(
        "job_hunter_evaluations",
        params={"job_id": f"eq.{job_id}", "select": "id,decision,total_score,evaluated_at"},
    )
    assert len(rows) == 1
    assert rows[0]["evaluated_at"] == frozen_instant.isoformat()

    evaluation = store.get_evaluation(job_id)
    assert evaluation is not None
    assert evaluation.decision == "high_priority"
    assert evaluation.total_score == 95


def test_get_evaluation_returns_the_newer_evaluation_when_evaluated_at_differs(store):
    """Pins the ordering itself, not just the merge case above.

    The convergence test above (frozen clock, identical `evaluated_at`)
    only proves two same-instant saves can't produce two rows to choose
    between. It says nothing about which row `get_evaluation` picks when
    two rows for the same job genuinely coexist -- the normal case, since
    `save_evaluation` stamps a fresh `evaluated_at` on every real call.

    This test uses the real clock deliberately: two back-to-back
    `save_evaluation` calls land on distinct microsecond `evaluated_at`
    values, so the unique `(user_id, job_id, evaluated_at)` constraint does
    *not* merge them -- two distinct rows exist. Asserting on `decision`
    (a field that differs between the two writes, not on row identity)
    pins which row `_LATEST_EVALUATION_ORDER`'s `evaluated_at.desc` selects
    and why: flipping that to `.asc`, or dropping the `order` param
    entirely, makes `get_evaluation` return the first (`possible_match`)
    write instead of the second, failing the `decision == "high_priority"`
    assertion below.
    """
    job_id, _, _ = store.upsert_job(
        Job(source="x", source_job_id="1", title="Senior Product Engineer")
    )

    store.save_evaluation(
        job_id, _evaluation(job_id, decision="possible_match", total_score=61)
    )
    store.save_evaluation(
        job_id, _evaluation(job_id, decision="high_priority", total_score=95)
    )

    evaluation = store.get_evaluation(job_id)
    assert evaluation is not None
    assert evaluation.decision == "high_priority"
    assert evaluation.total_score == 95


def test_get_material_returns_the_newer_material_when_generated_at_differs(store):
    """Same ordering concern as the evaluation test above, for
    `job_hunter_materials`'s `(user_id, job_id, generated_at)` constraint
    and `_LATEST_MATERIAL_ORDER`.

    Two real (unfrozen) `save_material` calls land on distinct
    `generated_at` instants, so two rows exist rather than one merged row.
    Asserting on `cover_letter_text` (which differs between the two
    writes) pins which row `get_material` selects: flipping
    `_LATEST_MATERIAL_ORDER`'s `generated_at.desc` to `.asc`, or dropping
    the `order` param, makes `get_material` return the first ("v1") text
    instead of the second, failing the `cover_letter_text == "... v2"`
    assertion below.
    """
    job_id, _, _ = store.upsert_job(
        Job(source="x", source_job_id="1", title="Senior Product Engineer")
    )

    store.save_material(job_id, Material(job_id=job_id, cover_letter_text="Dear Hiring Team, v1"))
    store.save_material(job_id, Material(job_id=job_id, cover_letter_text="Dear Hiring Team, v2"))

    material = store.get_material(job_id)
    assert material is not None
    assert material.cover_letter_text == "Dear Hiring Team, v2"


def test_legacy_evaluation_rows_backfill_raw_model_score(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    store = JobStore(db_path)
    job_id, _, _ = store.upsert_job(Job(source="x", source_job_id="1", title="Analyst", company="Acme"))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=77, raw_model_score=77))
    store._conn.close()

    # Simulate a database from before `raw_model_score` existed: the column
    # is genuinely absent, so reopening must add it and then backfill it.
    _drop_raw_model_score_column(db_path)

    reopened = JobStore(db_path)
    columns = {row["name"] for row in reopened._conn.execute("PRAGMA table_info(evaluations)")}
    assert "raw_model_score" in columns
    assert reopened.get_evaluation(job_id).raw_model_score == 77


def test_legacy_evaluation_row_with_genuinely_zero_score_stays_zero(tmp_path):
    db_path = tmp_path / "state.sqlite3"
    store = JobStore(db_path)
    job_id, _, _ = store.upsert_job(Job(source="x", source_job_id="1", title="Analyst", company="Acme"))
    store.save_evaluation(job_id, _evaluation(job_id, total_score=0, raw_model_score=0))
    store._conn.close()

    # Same missing-column simulation as above, but for a row whose true raw
    # score is 0 rather than merely defaulted to 0 — the backfill must leave
    # it at 0, not mistake it for an un-migrated row and rewrite it wrongly.
    _drop_raw_model_score_column(db_path)

    reopened = JobStore(db_path)
    evaluation = reopened.get_evaluation(job_id)
    assert evaluation.total_score == 0
    assert evaluation.raw_model_score == 0


def test_backfill_ats_identity_fills_rows_from_their_urls(store, supabase_client):
    job_id, _, _ = store.upsert_job(
        Job(
            source="lever",
            source_job_id="abc-123",
            title="Backend Engineer",
            company="Acme",
            url="https://jobs.lever.co/acme/abc-123",
        )
    )

    assert store.backfill_ats_identity() == 1

    row = supabase_client.select(
        "job_hunter_jobs",
        params={
            "id": f"eq.{job_id}",
            "select": "ats_provider,ats_board,ats_job_id",
        },
    )[0]
    assert (row["ats_provider"], row["ats_board"], row["ats_job_id"]) == (
        "lever",
        "acme",
        "abc-123",
    )


def test_backfill_ats_identity_leaves_non_ats_and_already_attributed_rows_alone(store, supabase_client):
    store.upsert_job(
        Job(source="hackernews", source_job_id="1", title="Backend Engineer", url="https://acme.test/jobs/1")
    )
    attributed_id, _, _ = store.upsert_job(
        Job(
            source="ashby",
            source_job_id="2",
            title="Backend Engineer",
            url="https://jobs.ashbyhq.com/other/xyz",
            ats_provider="ashby",
            ats_board="acme",
            ats_job_id="xyz",
        )
    )

    assert store.backfill_ats_identity() == 0

    row = supabase_client.select(
        "job_hunter_jobs",
        params={"id": f"eq.{attributed_id}", "select": "ats_board"},
    )[0]
    assert row["ats_board"] == "acme"


def test_backfill_ats_identity_attributes_job_boards_greenhouse_rows(store, supabase_client):
    # Rows discovered before the Greenhouse adapter attributed its own postings
    # kept a job-boards.greenhouse.io URL and no identity, which left them off
    # the strongest dedup key. The URL alone is enough to attribute them.
    job_id, _, _ = store.upsert_job(
        Job(
            source="greenhouse",
            source_job_id="456",
            title="Backend Engineer",
            company="Acme",
            url="https://job-boards.greenhouse.io/acme/jobs/456",
        )
    )

    assert store.backfill_ats_identity() == 1

    row = supabase_client.select(
        "job_hunter_jobs",
        params={
            "id": f"eq.{job_id}",
            "select": "ats_provider,ats_board,ats_job_id",
        },
    )[0]
    assert (row["ats_provider"], row["ats_board"], row["ats_job_id"]) == (
        "greenhouse",
        "acme",
        "456",
    )


def test_backfill_ats_identity_is_idempotent(store):
    store.upsert_job(
        Job(
            source="greenhouse",
            source_job_id="456",
            title="Backend Engineer",
            url="https://boards.greenhouse.io/acme/jobs/456",
        )
    )

    assert store.backfill_ats_identity() == 1
    assert store.backfill_ats_identity() == 0

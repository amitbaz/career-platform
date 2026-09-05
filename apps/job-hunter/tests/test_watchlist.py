from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from job_hunter.models import CompanyWatchSeed, Evaluation, Job
from job_hunter.store import JobStore
from job_hunter.watchlist import (
    promote_company,
    should_auto_promote,
    sync_manual_watch_seeds,
)


def _evaluation(decision, **overrides):
    values = dict(
        job_id=1,
        total_score=80,
        scores={},
        decision=decision,
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="test",
    )
    values.update(overrides)
    return Evaluation(**values)


@pytest.mark.parametrize(
    "decision,expected",
    [
        ("high_priority", True),
        ("package_match", True),
        ("possible_match", False),
        ("skip", False),
        ("blocked", False),
    ],
)
def test_auto_promotion_uses_final_decision(decision, expected):
    assert should_auto_promote(_evaluation(decision)) is expected


def test_auto_promotion_rejects_hard_blockers():
    evaluation = _evaluation("high_priority", hard_blockers=["work authorization"])

    assert should_auto_promote(evaluation) is False


def test_auto_promotion_rejects_failed_evaluation():
    evaluation = _evaluation("high_priority", status="failed")

    assert should_auto_promote(evaluation) is False


def test_syncing_manual_greenhouse_seed_is_idempotent(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    seed = CompanyWatchSeed(
        company_name="Acme GmbH",
        ats_provider="greenhouse",
        ats_identifier="acme",
    )

    sync_manual_watch_seeds(store, [seed])
    sync_manual_watch_seeds(store, [seed])

    row = store.get_company_watch("ACME")
    assert row is not None
    assert row["company_name"] == "Acme GmbH"
    assert row["promotion_source"] == "manual"
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert store._conn.execute("SELECT COUNT(*) FROM company_watch").fetchone()[0] == 1


def test_syncing_manual_generic_careers_seed(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    seed = CompanyWatchSeed(
        company_name="Beta",
        careers_url="https://beta.test/careers",
    )

    sync_manual_watch_seeds(store, [seed])

    row = store.get_company_watch("Beta")
    assert row is not None
    assert row["careers_url"] == "https://beta.test/careers"
    assert row["promotion_source"] == "manual"


def test_automatic_promotion_prefers_supported_ats_metadata(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(
        source="greenhouse",
        title="Frontend Engineer",
        company="Acme",
        canonical_url="https://boards.greenhouse.io/acme/jobs/123",
        ats_provider="greenhouse",
        ats_board="acme",
        ats_job_id="123",
    )
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("package_match"),
        package_threshold=75,
    )

    assert watch_id is not None
    row = store.get_company_watch("Acme")
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert row["careers_url"] == ""
    assert row["promotion_source"] == "automatic"


def test_automatic_promotion_uses_canonical_url_without_supported_ats(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(
        source="public",
        title="Frontend Engineer",
        company="Beta",
        canonical_url="https://beta.test/careers/frontend-engineer",
    )
    job_id, _, _ = store.upsert_job(job)

    promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    row = store.get_company_watch("Beta")
    assert row["careers_url"] == "https://beta.test/careers/frontend-engineer"
    assert row["ats_provider"] is None
    assert row["ats_identifier"] is None


def test_automatic_promotion_uses_canonical_url_for_whitespace_ats_board(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(
        source="greenhouse",
        title="Frontend Engineer",
        company="Beta",
        canonical_url="https://beta.test/careers/frontend-engineer",
        ats_provider="greenhouse",
        ats_board="   ",
    )
    job_id, _, _ = store.upsert_job(job)

    promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    row = store.get_company_watch("Beta")
    assert row["careers_url"] == "https://beta.test/careers/frontend-engineer"
    assert row["ats_provider"] is None
    assert row["ats_identifier"] is None


def test_automatic_promotion_stores_company_only_without_usable_endpoint(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(source="public", title="Frontend Engineer", company="No Endpoint GmbH")
    job_id, _, _ = store.upsert_job(job)

    promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    row = store.get_company_watch("No Endpoint")
    assert row is not None
    assert row["careers_url"] == ""
    assert row["ats_provider"] is None
    assert row["ats_identifier"] is None


def test_automatic_promotion_rejects_empty_company_identity(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(source="public", title="Frontend Engineer", company="GmbH")
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    assert watch_id is None
    assert store._conn.execute("SELECT COUNT(*) FROM company_watch").fetchone()[0] == 0


def test_automatic_promotion_rejects_inconsistent_score_below_configured_threshold(
    tmp_path,
):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(source="public", title="Frontend Engineer", company="Acme")
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("package_match", total_score=74),
        package_threshold=75,
    )

    assert watch_id is None
    assert store.get_company_watch("Acme") is None


def test_automatic_promotion_rejects_non_promotable_decision(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    job = Job(source="public", title="Frontend Engineer", company="Acme")
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("possible_match"),
        package_threshold=65,
    )

    assert watch_id is None
    assert store.get_company_watch("Acme") is None


def _manual_watch(store, company_name="Acme"):
    return store.upsert_company_watch(
        company_name=company_name,
        careers_url=f"https://{company_name.lower()}.test/careers",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )


def test_due_watches_include_unpaused_and_expired_active_rows(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    unpaused_id = _manual_watch(store, "Acme")
    expired_id = _manual_watch(store, "Beta")
    paused_id = _manual_watch(store, "Gamma")
    inactive_id = _manual_watch(store, "Delta")
    store._conn.execute(
        "UPDATE company_watch SET paused_until = ? WHERE id = ?",
        ("2026-08-31T11:59:59+00:00", expired_id),
    )
    store._conn.execute(
        "UPDATE company_watch SET paused_until = ? WHERE id = ?",
        ("2026-08-31T12:00:01+00:00", paused_id),
    )
    store._conn.execute(
        "UPDATE company_watch SET active = 0 WHERE id = ?",
        (inactive_id,),
    )

    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    assert [row["id"] for row in store.list_due_company_watches(now)] == [
        unpaused_id,
        expired_id,
    ]


def test_first_two_failures_remain_due(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 2
    assert row["paused_until"] is None
    assert [row["id"] for row in store.list_due_company_watches(now)] == [watch_id]


def test_third_failure_pauses_for_24_hours(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 3
    assert row["paused_until"] == "2026-09-01T12:00:00+00:00"
    assert store.list_due_company_watches(now) == []


def test_failed_retry_after_pause_expiry_pauses_for_another_24_hours(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    first_check = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    retry = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for _ in range(3):
        store.record_watch_failure(watch_id, first_check)

    assert [row["id"] for row in store.list_due_company_watches(retry)] == [watch_id]

    store.record_watch_failure(watch_id, retry)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 4
    assert row["paused_until"] == "2026-09-02T12:00:00+00:00"


def test_success_clears_failures_and_pause_and_updates_health_timestamps(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    failed_at = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    succeeded_at = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)
    for _ in range(3):
        store.record_watch_failure(watch_id, failed_at)

    store.record_watch_success(watch_id, succeeded_at)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 0
    assert row["paused_until"] is None
    assert row["last_successful_check_at"] == "2026-09-01T13:30:00+00:00"
    assert row["last_verified_at"] == "2026-09-01T13:30:00+00:00"
    assert row["promotion_source"] == "manual"


def test_success_timestamps_are_normalized_to_utc(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    now = datetime(
        2026,
        8,
        31,
        14,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )

    store.record_watch_success(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["last_successful_check_at"] == "2026-08-31T12:00:00+00:00"
    assert row["last_verified_at"] == "2026-08-31T12:00:00+00:00"


def test_due_watch_compares_equivalent_offset_instants(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    store._conn.execute(
        "UPDATE company_watch SET paused_until = ? WHERE id = ?",
        ("2026-08-31T14:00:00+02:00", watch_id),
    )
    same_instant = datetime(
        2026,
        8,
        31,
        8,
        0,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    assert [row["id"] for row in store.list_due_company_watches(same_instant)] == [
        watch_id
    ]


def test_failure_pause_is_24_elapsed_hours_across_dst(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    before_spring_forward = datetime(
        2026,
        3,
        28,
        12,
        0,
        tzinfo=ZoneInfo("Europe/Berlin"),
    )

    for _ in range(3):
        store.record_watch_failure(watch_id, before_spring_forward)

    row = store.get_company_watch("Acme")
    assert row["paused_until"] == "2026-03-29T11:00:00+00:00"


@pytest.mark.parametrize(
    "method_name",
    [
        "list_due_company_watches",
        "record_watch_success",
        "record_watch_failure",
    ],
)
def test_watch_time_methods_reject_naive_datetimes(tmp_path, method_name):
    store = JobStore(tmp_path / "state.sqlite3")
    watch_id = _manual_watch(store)
    naive = datetime(2026, 8, 31, 12, 0)
    method = getattr(store, method_name)
    arguments = (
        (naive,)
        if method_name == "list_due_company_watches"
        else (watch_id, naive)
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        method(*arguments)

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from job_hunter.models import CompanyWatchSeed, Evaluation, Job
from job_hunter.watchlist import (
    promote_company,
    should_auto_promote,
    sync_manual_watch_seeds,
)


def _company_name(label: str = "Acme") -> str:
    """A company name no other test or concurrent run shares.

    Since #204 an automatic promotion writes the shared
    `job_hunter_company_watch_health`, keyed on the company entity
    (`job_hunter_companies`, #198) -- neither table is cleaned between
    tests or scoped to a user, so a test asserting an exact endpoint,
    confidence or strength for a bare "Acme" watch can otherwise read
    whatever an unrelated earlier test (in this file or any other) already
    promoted it to.
    """
    return f"{label} {uuid.uuid4().hex[:12]}"


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


def test_syncing_manual_greenhouse_seed_is_idempotent(store, tmp_path):
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
    assert len(store.client.select("job_hunter_company_watch", params={"select": "id"})) == 1


def test_syncing_manual_generic_careers_seed(store, tmp_path):
    seed = CompanyWatchSeed(
        company_name="Beta",
        careers_url="https://beta.test/careers",
    )

    sync_manual_watch_seeds(store, [seed])

    row = store.get_company_watch("Beta")
    assert row is not None
    assert row["careers_url"] == "https://beta.test/careers"
    assert row["promotion_source"] == "manual"


def test_automatic_promotion_prefers_supported_ats_metadata(store, tmp_path):
    company = _company_name()
    job = Job(
        source="greenhouse",
        title="Frontend Engineer",
        company=company,
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
    row = store.get_company_watch(company)
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert row["careers_url"] == ""
    assert row["promotion_source"] == "automatic"


def test_automatic_promotion_uses_canonical_url_without_supported_ats(store, tmp_path):
    company = _company_name("Beta")
    job = Job(
        source="public",
        title="Frontend Engineer",
        company=company,
        canonical_url="https://beta.test/careers/frontend-engineer",
    )
    job_id, _, _ = store.upsert_job(job)

    promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    row = store.get_company_watch(company)
    assert row["careers_url"] == "https://beta.test/careers/frontend-engineer"
    assert row["ats_provider"] is None
    assert row["ats_identifier"] is None


def test_automatic_promotion_uses_canonical_url_for_whitespace_ats_board(store, tmp_path):
    company = _company_name("Beta")
    job = Job(
        source="greenhouse",
        title="Frontend Engineer",
        company=company,
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

    row = store.get_company_watch(company)
    assert row["careers_url"] == "https://beta.test/careers/frontend-engineer"
    assert row["ats_provider"] is None
    assert row["ats_identifier"] is None


def test_automatic_promotion_stores_company_only_without_usable_endpoint(store, tmp_path):
    company = _company_name("No Endpoint")
    job = Job(source="public", title="Frontend Engineer", company=f"{company} GmbH")
    job_id, _, _ = store.upsert_job(job)

    promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    row = store.get_company_watch(company)
    assert row is not None
    assert row["careers_url"] == ""
    assert row["ats_provider"] is None
    assert row["ats_identifier"] is None


def test_automatic_promotion_rejects_empty_company_identity(store, tmp_path):
    job = Job(source="public", title="Frontend Engineer", company="GmbH")
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("high_priority"),
    )

    assert watch_id is None
    assert store.client.select("job_hunter_company_watch", params={"select": "id"}) == []


def test_automatic_promotion_rejects_inconsistent_score_below_configured_threshold(
    store,
    tmp_path,
):
    company = _company_name()
    job = Job(source="public", title="Frontend Engineer", company=company)
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("package_match", total_score=74),
        package_threshold=75,
    )

    assert watch_id is None
    assert store.get_company_watch(company) is None


def test_automatic_promotion_rejects_non_promotable_decision(store, tmp_path):
    company = _company_name()
    job = Job(source="public", title="Frontend Engineer", company=company)
    job_id, _, _ = store.upsert_job(job)

    watch_id = promote_company(
        store,
        job_id=job_id,
        job=job,
        evaluation=_evaluation("possible_match"),
        package_threshold=65,
    )

    assert watch_id is None
    assert store.get_company_watch(company) is None


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


def test_due_watches_include_unpaused_and_expired_active_rows(store, tmp_path):
    unpaused_id = _manual_watch(store, "Acme")
    expired_id = _manual_watch(store, "Beta")
    paused_id = _manual_watch(store, "Gamma")
    inactive_id = _manual_watch(store, "Delta")
    store.client.update(
        "job_hunter_company_watch",
        {"paused_until": "2026-08-31T11:59:59+00:00"},
        params={"id": f"eq.{expired_id}"},
    )
    store.client.update(
        "job_hunter_company_watch",
        {"paused_until": "2026-08-31T12:00:01+00:00"},
        params={"id": f"eq.{paused_id}"},
    )
    store.client.update(
        "job_hunter_company_watch",
        {"active": False},
        params={"id": f"eq.{inactive_id}"},
    )

    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    # Filtered to this test's own ids rather than asserted as the whole
    # list: since #204 the due list also unions the shared automatic pool,
    # which other tests in this session may have left active, due rows in.
    seeded_ids = {unpaused_id, expired_id, paused_id, inactive_id}
    due_ids = [
        row["id"]
        for row in store.list_due_company_watches(now)
        if row["id"] in seeded_ids
    ]
    assert due_ids == [unpaused_id, expired_id]


def test_first_two_failures_remain_due(store, tmp_path):
    watch_id = _manual_watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 2
    assert row["paused_until"] is None
    due_ids = [row["id"] for row in store.list_due_company_watches(now)]
    assert watch_id in due_ids


def test_third_failure_pauses_for_24_hours(store, tmp_path):
    watch_id = _manual_watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 3
    assert row["paused_until"] == "2026-09-01T12:00:00+00:00"
    # Not asserted empty: since #204 the due list also unions the shared
    # automatic pool, which other tests in this session may have left due
    # rows in. This watch's own absence is still the property under test.
    due_ids = [row["id"] for row in store.list_due_company_watches(now)]
    assert watch_id not in due_ids


def test_failed_retry_after_pause_expiry_pauses_for_another_24_hours(store, tmp_path):
    watch_id = _manual_watch(store)
    first_check = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    retry = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    for _ in range(3):
        store.record_watch_failure(watch_id, first_check)

    due_ids = [row["id"] for row in store.list_due_company_watches(retry)]
    assert watch_id in due_ids

    store.record_watch_failure(watch_id, retry)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 4
    assert row["paused_until"] == "2026-09-02T12:00:00+00:00"


def test_success_clears_failures_and_pause_and_updates_health_timestamps(store, tmp_path):
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


def test_success_timestamps_are_normalized_to_utc(store, tmp_path):
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


def test_due_watch_compares_equivalent_offset_instants(store, tmp_path):
    watch_id = _manual_watch(store)
    store.client.update(
        "job_hunter_company_watch",
        {"paused_until": "2026-08-31T14:00:00+02:00"},
        params={"id": f"eq.{watch_id}"},
    )
    same_instant = datetime(
        2026,
        8,
        31,
        8,
        0,
        tzinfo=timezone(timedelta(hours=-4)),
    )

    due_ids = [row["id"] for row in store.list_due_company_watches(same_instant)]
    assert watch_id in due_ids


def test_failure_pause_is_24_elapsed_hours_across_dst(store, tmp_path):
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
def test_watch_time_methods_reject_naive_datetimes(store, tmp_path, method_name):
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

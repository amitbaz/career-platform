import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS
from job_hunter.gmail_models import ExtractedJob
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import Evaluation, Job, Material
from job_hunter.search_profile import SearchProfile, SearchProfileMarket
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


def test_ai_usage_rows_persist_success_without_prompt_or_response_content(store):
    store.record_ai_usage(
        occurred_at="2026-09-01T08:00:00+00:00",
        provider="gemini",
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

    rows = store.ai_usage_rows(
        "2026-09-01T00:00:00+00:00",
        "2026-09-02T00:00:00+00:00",
        provider="gemini",
        model="gemini-3.6-flash",
    )

    assert len(rows) == 1
    row = dict(rows[0])
    row.pop("id")
    assert row == {
        "occurred_at": "2026-09-01T08:00:00+00:00",
        "run_id": "unknown",
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
    # This only proves ai_usage_rows' own select projects no prompt/response
    # column -- it is not a claim about the job_hunter_ai_usage table itself.
    # The table-level privacy invariant (no prompt/response column can be
    # added without a conscious decision) lives in
    # supabase/tests/pgtap/job_hunter_gmail_privacy.sql.
    assert "prompt" not in rows[0].keys()
    assert "response" not in rows[0].keys()


def test_ai_usage_rows_persist_429_attempt(store):
    store.record_ai_usage(
        occurred_at="2026-09-01T08:00:00+00:00",
        provider="gemini",
        model="gemini-3.6-flash",
        purpose="cover_letter",
        status="quota_429",
        estimated_input_tokens=80,
        http_status=429,
        error_code="RESOURCE_EXHAUSTED",
    )

    rows = store.ai_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", provider="gemini"
    )

    assert len(rows) == 1
    assert rows[0]["status"] == "quota_429"
    assert rows[0]["http_status"] == 429
    assert rows[0]["error_code"] == "RESOURCE_EXHAUSTED"


def test_ai_usage_rows_respects_half_open_time_range(store):
    store.record_ai_usage(
        occurred_at="2026-09-01T00:00:00+00:00",
        provider="gemini",
        model="gemini-3.6-flash",
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=1,
    )
    store.record_ai_usage(
        occurred_at="2026-09-02T00:00:00+00:00",
        provider="gemini",
        model="gemini-3.6-flash",
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=1,
    )

    rows = store.ai_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", provider="gemini"
    )

    assert [row["occurred_at"] for row in rows] == ["2026-09-01T00:00:00+00:00"]


def test_record_ai_usage_writes_the_run_id_sentinel(store):
    """`run_id` is NOT NULL but no longer carries meaning (#73).

    The column stays as an optional annotation; every row now takes the
    migration's own backfill sentinel, and no quota decision reads it.
    """
    store.record_ai_usage(
        occurred_at="2026-09-01T08:00:00+00:00",
        provider="gemini",
        model="gemini-3.6-flash",
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=10,
    )

    rows = store.ai_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", provider="gemini"
    )

    assert len(rows) == 1
    assert rows[0]["run_id"] == "unknown"


def test_ai_pause_round_trip_and_clear(store):
    store.set_ai_pause(
        "gemini",
        "gemini-3.6-flash",
        "2026-09-01T08:01:30+00:00",
        "rate_limit",
    )

    pause = store.get_ai_pause("gemini", "gemini-3.6-flash")

    assert pause is not None
    assert pause["paused_until"] == "2026-09-01T08:01:30+00:00"
    assert pause["reason"] == "rate_limit"
    assert pause["updated_at"]

    store.clear_ai_pause("gemini", "gemini-3.6-flash")

    assert store.get_ai_pause("gemini", "gemini-3.6-flash") is None


def test_ai_pause_upsert_converges_on_repeated_writes(store):
    """A retried POST on a transient 5xx must update the one row, not duplicate it."""
    store.set_ai_pause("gemini", "gemini-3.6-flash", "2026-09-01T08:01:30+00:00", "rate_limit")
    store.set_ai_pause("gemini", "gemini-3.6-flash", "2026-09-01T09:00:00+00:00", "daily_quota")

    pause = store.get_ai_pause("gemini", "gemini-3.6-flash")
    assert pause["paused_until"] == "2026-09-01T09:00:00+00:00"
    assert pause["reason"] == "daily_quota"

    rows = store._client.select(
        "job_hunter_ai_quota_state",
        params={"model": "eq.gemini-3.6-flash", "select": "id"},
    )
    assert len(rows) == 1


def test_candidate_context_cache_round_trip_serializes_json_inside_store(store):
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


def test_candidate_context_cache_is_none_for_unknown_key(store):
    assert store.get_candidate_context("does-not-exist") is None


def test_pending_ai_work_is_idempotent_and_updates_its_timestamp(store):
    job_id, _, _ = store.upsert_job(make_job(fingerprint="ai-work"))

    store.enqueue_ai_work("cover_letter", job_id)
    first = store.list_pending_ai_work("cover_letter")
    store.enqueue_ai_work("cover_letter", job_id)
    second = store.list_pending_ai_work("cover_letter")

    assert len(second) == 1
    assert second[0]["job_id"] == job_id
    assert second[0]["created_at"] == first[0]["created_at"]
    assert from_iso(second[0]["updated_at"]) > from_iso(first[0]["updated_at"])
    assert second[0]["updated_at"] != second[0]["created_at"]

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


def _shared_watch_health(client, identity: str):
    """The shared watch-health row for a normalized company identity, if any.

    Reads are open to any authenticated user (#204), so this is a plain
    select regardless of which store promoted the row.
    """
    rows = client.select(
        "job_hunter_company_watch_health",
        params={
            "select": "*,company:job_hunter_companies!inner(display_name)",
            "company.identity": f"eq.{identity}",
            "limit": "1",
        },
    )
    return rows[0] if rows else None


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


# Since #204 automatic promotion no longer shares a row with a manual watch
# -- it writes the shared job_hunter_company_watch_health instead. The
# strength/confidence ranking (supported ATS beats generic URL beats
# company-only; equal strength requires greater confidence to replace) is
# unchanged, but it now only ever compares two writes within the *same*
# pool. The three tests below replace the old cross-source ones: one proves
# the ranking still upgrades a repeated manual watch, one proves it still
# upgrades a repeated automatic promotion (now shared), and one proves the
# two pools no longer interact at all.


def test_manual_watch_ranking_upgrades_on_stronger_repeat_upsert(store):
    """A later manual upsert for the same company can still upgrade the first."""
    _watch(
        store,
        company_name="Acme",
        careers_url="https://acme.test/careers",
        ats_provider=None,
        ats_identifier=None,
        confidence=0.4,
    )

    _watch(store, company_name="Acme GmbH", confidence=0.9)

    row = store.get_company_watch("Acme")
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert row["careers_url"] == ""
    assert row["confidence"] == 0.9
    assert row["promotion_source"] == "manual"


def test_automatic_promotion_shares_one_row_per_company(store, supabase_client):
    """Two automatic promotions for the same employer converge on one shared row.

    Nothing lands on the per-user table at all: automatic promotion writes
    `job_hunter_company_watch_health` exclusively.
    """
    company = f"Acme Shared {uuid.uuid4().hex}"
    identity = normalize_company_name(company)

    _watch(
        store,
        company_name=company,
        careers_url="https://acme-shared.test/careers",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="automatic",
        confidence=0.9,
    )
    _watch(store, company_name=company, promotion_source="automatic", confidence=0.1)

    row = _shared_watch_health(supabase_client, identity)
    assert row is not None
    assert row["ats_provider"] == "greenhouse"
    assert row["ats_identifier"] == "acme"
    assert row["confidence"] == 0.1
    assert _watch_rows(supabase_client) == []


def test_manual_and_automatic_watches_for_same_company_stay_separate(
    store, supabase_client
):
    """Manual watches stay the user's own and are unaffected by automatic discovery.

    A company that is both manually watched and automatically promoted is
    checked from both places -- see the migration's note -- rather than
    merged into one row: a stronger automatic endpoint must never silently
    upgrade a user's manual watch.
    """
    company = f"Acme Both {uuid.uuid4().hex}"
    identity = normalize_company_name(company)

    manual_id = _watch(
        store,
        company_name=company,
        careers_url="",
        ats_provider=None,
        ats_identifier=None,
        promotion_source="manual",
        confidence=1.0,
    )
    _watch(store, company_name=company, promotion_source="automatic", confidence=1.0)

    manual_row = store.get_company_watch(company)
    assert manual_row["id"] == manual_id
    assert manual_row["promotion_source"] == "manual"
    assert manual_row["ats_provider"] is None
    assert manual_row["careers_url"] == ""

    shared_row = _shared_watch_health(supabase_client, identity)
    assert shared_row is not None
    assert shared_row["ats_provider"] == "greenhouse"

    due_ids = {
        row["id"] for row in store.list_due_company_watches(datetime.now(timezone.utc))
    }
    assert manual_id in due_ids
    assert shared_row["id"] in due_ids


def test_second_user_inherits_shared_watch_without_reverifying(
    store, other_store, supabase_client
):
    """A second user's run sees an automatic promotion it never paid for.

    `other_store` shares the same ingestion connection but a different
    per-user client (#204's whole point): it never calls
    `upsert_company_watch` itself and still finds the watch due, and its
    `get_company_watch` reads the same shared endpoint.
    """
    company = f"Acme Inherited {uuid.uuid4().hex}"

    watch_id = _watch(store, company_name=company, promotion_source="automatic")

    due_ids = {
        row["id"]
        for row in other_store.list_due_company_watches(datetime.now(timezone.utc))
    }
    assert watch_id in due_ids
    assert other_store.get_company_watch(company)["ats_provider"] == "greenhouse"


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
    # Stamp explicit, well-separated created_at values instead of relying on
    # four separate now() inserts landing in a particular order.
    supabase_client.update(
        "job_hunter_company_watch",
        {"created_at": "2026-08-30T10:00:00+00:00"},
        params={"id": f"eq.{unpaused_id}"},
    )
    supabase_client.update(
        "job_hunter_company_watch",
        {
            "created_at": "2026-08-30T11:00:00+00:00",
            "paused_until": "2026-08-31T11:59:59+00:00",
        },
        params={"id": f"eq.{expired_id}"},
    )
    supabase_client.update(
        "job_hunter_company_watch",
        {
            "created_at": "2026-08-30T12:00:00+00:00",
            "paused_until": "2026-08-31T12:00:01+00:00",
        },
        params={"id": f"eq.{paused_id}"},
    )
    supabase_client.update(
        "job_hunter_company_watch",
        {"created_at": "2026-08-30T13:00:00+00:00", "active": False},
        params={"id": f"eq.{inactive_id}"},
    )

    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    # Filtered to this test's own ids rather than asserted as the whole
    # list: since #204 the due list also unions the shared automatic pool,
    # which other tests in this session may have left active, due rows in.
    # Filtering (rather than dropping the check) still proves the pause/
    # active filtering *and* the insertion-order tie-break among these four.
    seeded_ids = {unpaused_id, expired_id, paused_id, inactive_id}
    due_ids = [
        row["id"]
        for row in store.list_due_company_watches(now)
        if row["id"] in seeded_ids
    ]
    assert due_ids == [unpaused_id, expired_id]


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

    due_ids = [row["id"] for row in store.list_due_company_watches(same_instant)]
    assert watch_id in due_ids


def test_record_watch_success_advances_updated_at(store, supabase_client):
    watch_id = _watch(store)
    before = supabase_client.select(
        "job_hunter_company_watch",
        params={"id": f"eq.{watch_id}", "select": "updated_at"},
    )[0]["updated_at"]

    store.record_watch_success(
        watch_id, datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    )

    after = store.get_company_watch("Acme")["updated_at"]
    assert from_iso(after) > from_iso(before)


def test_first_two_watch_failures_remain_due(store):
    watch_id = _watch(store)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    store.record_watch_failure(watch_id, now)
    store.record_watch_failure(watch_id, now)

    row = store.get_company_watch("Acme")
    assert row["consecutive_failures"] == 2
    assert row["paused_until"] is None
    due_ids = [due["id"] for due in store.list_due_company_watches(now)]
    assert watch_id in due_ids


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
    # Not asserted empty: since #204 the due list also unions the shared
    # automatic pool, which other tests in this session may have left due
    # rows in. This watch's own absence is still the property under test.
    due_ids = [due["id"] for due in store.list_due_company_watches(now)]
    assert watch_id not in due_ids


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
        "record_ats_eligible_jobs",
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
        "record_ats_eligible_jobs": ([("lever", "acme")], naive),
    }[method_name]

    with pytest.raises(ValueError, match="timezone-aware"):
        getattr(store, method_name)(*arguments)


# ------------------------------------------------------------------
# ATS registry
#
# job_hunter_ats_boards is shared (issue #203): unlike a per-user table, it
# carries no user_id and no delete policy, so nothing here cleans it up
# between tests -- exactly the constraint job_hunter_postings and
# job_hunter_companies already live with (see conftest.py's
# `_postings_unique_to_this_test`). Every board identifier below is
# suffixed unique per test with `_board_id`, and every assertion reads only
# the boards this test itself created, never the whole shared table.
# ------------------------------------------------------------------


def _board_id(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:8]}"


def _due(store, now, *board_identifiers):
    wanted = set(board_identifiers)
    return [
        e for e in store.list_due_ats_boards(now) if e.board_identifier in wanted
    ]


def _rejected(store, *board_identifiers):
    wanted = set(board_identifiers)
    return [
        e
        for e in store.list_rejected_ats_boards()
        if e.board_identifier in wanted
    ]


def test_ats_registry_upsert_is_provider_board_unique(store):
    board = _board_id("omnea")
    created = store.upsert_ats_board(
        provider="ashby",
        board_identifier=board,
        company_name="Omnea",
        market_hint="london",
    )
    repeated = store.upsert_ats_board(
        provider="ashby",
        board_identifier=board,
        company_name="Omnea Ltd",
        market_hint="london",
    )

    assert created is True
    assert repeated is False
    assert len(_due(store, datetime.now(timezone.utc), board)) == 1


def test_ats_registry_upsert_does_not_wipe_metadata_with_blank_values(store):
    board = _board_id("omnea")
    store.upsert_ats_board(
        provider="ashby",
        board_identifier=board,
        company_name="Omnea",
        market_hint="london",
    )
    store.upsert_ats_board(
        provider="ashby",
        board_identifier=board,
        company_name="",
        market_hint="",
    )

    entry = _due(store, datetime.now(timezone.utc), board)[0]
    assert entry.company_name == "Omnea"
    assert entry.market_hint == "london"


def test_ats_registry_rejects_unsupported_provider(store):
    with pytest.raises(ValueError, match="unsupported ATS provider"):
        store.upsert_ats_board(provider="workday", board_identifier="x")


def test_ats_failure_pauses_board_without_rediscovery_bypassing_pause(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("acme")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    store.record_ats_scan_failure("lever", board, now)
    store.upsert_ats_board(provider="lever", board_identifier=board)

    assert _due(store, now + timedelta(hours=1), board) == []
    due = _due(store, now + timedelta(hours=25), board)
    assert [(entry.provider, entry.board_identifier) for entry in due] == [
        ("lever", board)
    ]


def test_ats_scan_success_records_job_count_and_resets_failures(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("acme")
    store.upsert_ats_board(provider="greenhouse", board_identifier=board)

    store.record_ats_scan_failure("greenhouse", board, now)
    later = now + timedelta(hours=1)
    store.record_ats_scan_success("greenhouse", board, later, job_count=7)

    due = _due(store, later, board)
    assert len(due) == 1
    entry = due[0]
    assert entry.last_job_count == 7
    assert from_iso(entry.last_success_at) == later
    assert entry.consecutive_failures == 0
    assert entry.paused_until is None


def test_ats_permanent_failure_deactivates_board_after_three_in_a_row(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("dead-co")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    for i in range(3):
        store.record_ats_scan_failure(
            "lever", board, now + timedelta(hours=25 * i), permanent=True
        )

    # Deactivated boards are never returned by list_due_ats_boards, even
    # once any pause would have expired.
    much_later = now + timedelta(days=30)
    assert _due(store, much_later, board) == []


def test_ats_permanent_failure_stays_active_below_threshold(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("maybe-dead")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    store.record_ats_scan_failure("lever", board, now, permanent=True)
    store.record_ats_scan_failure(
        "lever", board, now + timedelta(hours=25), permanent=True
    )

    due = _due(store, now + timedelta(hours=50), board)
    assert [e.board_identifier for e in due] == [board]
    assert due[0].consecutive_failures == 2


def test_ats_transient_failure_never_deactivates_board(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("flaky")
    store.upsert_ats_board(provider="greenhouse", board_identifier=board)

    for i in range(10):
        store.record_ats_scan_failure(
            "greenhouse", board, now + timedelta(hours=25 * i)
        )

    due = _due(store, now + timedelta(days=30), board)
    assert due[0].board_identifier == board
    assert due[0].consecutive_failures == 10
    assert due[0].active is True


def test_ats_mixed_transient_then_permanent_failure_deactivates_board(store):
    # consecutive_failures is one shared counter incremented by both
    # transient and permanent failures, so two transient failures followed
    # by a single permanent one reaches the threshold on that 404 alone —
    # not after three permanent failures in a row. This pins the documented
    # (if slightly surprising) real behavior of record_ats_scan_failure.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("mixed-co")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    store.record_ats_scan_failure("lever", board, now, permanent=False)
    store.record_ats_scan_failure(
        "lever", board, now + timedelta(hours=25), permanent=False
    )
    store.record_ats_scan_failure(
        "lever", board, now + timedelta(hours=25 * 2), permanent=True
    )

    assert _due(store, now + timedelta(days=30), board) == []


def test_ats_deactivated_board_reactivates_on_rediscovery(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("reborn-co")
    store.upsert_ats_board(provider="lever", board_identifier=board)
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", board, now + timedelta(hours=25 * i), permanent=True
        )
    assert _due(store, now + timedelta(days=30), board) == []

    # The board resurfaces in a freshly discovered job pointing at the same
    # provider/board — ordinary rediscovery reactivates it (existing
    # upsert_ats_board behavior), but the still-unexpired pause still holds.
    store.upsert_ats_board(provider="lever", board_identifier=board)
    last_pause_start = now + timedelta(hours=25 * 2)
    still_paused_check = last_pause_start + timedelta(hours=1)
    assert _due(store, still_paused_check, board) == []
    after_pause = last_pause_start + timedelta(hours=25)
    due = _due(store, after_pause, board)
    assert [e.board_identifier for e in due] == [board]


def test_reject_ats_board_deactivates_and_records_reason(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("jobgether")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    store.reject_ats_board("lever", board, "aggregator: 98% third-party", now)

    assert _due(store, now, board) == []
    rejected = _rejected(store, board)
    assert [e.board_identifier for e in rejected] == [board]
    assert rejected[0].rejected_reason == "aggregator: 98% third-party"
    assert rejected[0].active is False


def test_list_rejected_ats_boards_excludes_healthy_and_health_paused_boards(store):
    # Only a board rejected for cause carries a reason -- a board merely
    # deactivated by repeated 404s must not show up as rejected.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    healthy = _board_id("healthy-co")
    dead = _board_id("dead-co")
    store.upsert_ats_board(provider="lever", board_identifier=healthy)
    store.upsert_ats_board(provider="lever", board_identifier=dead)
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", dead, now + timedelta(hours=25 * i), permanent=True
        )

    assert _rejected(store, healthy, dead) == []


def test_ats_rejected_board_is_not_resurrected_by_rediscovery(store):
    # Unlike a health-based deactivation (see
    # test_ats_deactivated_board_reactivates_on_rediscovery), a board
    # rejected for cause must stay rejected even when a freshly discovered
    # job points at it again -- upsert_ats_board must not flip active back
    # to true once rejected_reason is set.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("jobgether")
    store.upsert_ats_board(provider="lever", board_identifier=board)
    store.reject_ats_board("lever", board, "aggregator: 98% third-party", now)

    store.upsert_ats_board(provider="lever", board_identifier=board)

    assert _due(store, now + timedelta(days=30), board) == []


def test_clear_ats_board_rejection_makes_a_rejected_board_due_again(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("clientco")
    store.upsert_ats_board(provider="lever", board_identifier=board)
    store.reject_ats_board("lever", board, "aggregator: 98% third-party", now)

    store.clear_ats_board_rejection("lever", board)

    assert _rejected(store, board) == []
    due = _due(store, now, board)
    assert [e.board_identifier for e in due] == [board]
    assert due[0].rejected_reason is None
    assert due[0].active is True


def test_clear_ats_board_rejection_matches_the_stored_provider_case_insensitively(store):
    # Callers hold normalized ats_board_key values ("lever:jobgether"), while
    # the row was written from whatever case discovery saw.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("ClientCo")
    store.upsert_ats_board(provider="lever", board_identifier=board)
    store.reject_ats_board("lever", board, "aggregator: 98% third-party", now)

    store.clear_ats_board_rejection("Lever", board.lower())

    assert _rejected(store, board) == []
    assert [e.board_identifier for e in _due(store, now, board)] == [board]


def test_clear_ats_board_rejection_treats_underscores_as_literal_text(store):
    """`_` is a single-character wildcard to `ilike`, so matching can't use it.

    Two boards whose identifiers differ only where one has an underscore
    would both match an `ilike` filter built from either name. Clearing one
    rejection must leave the other rejected.
    """
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    suffix = uuid.uuid4().hex[:8]
    with_underscore = f"client_co-{suffix}"
    with_x = f"clientxco-{suffix}"
    store.upsert_ats_board(provider="lever", board_identifier=with_underscore)
    store.upsert_ats_board(provider="lever", board_identifier=with_x)
    store.reject_ats_board("lever", with_underscore, "aggregator", now)
    store.reject_ats_board("lever", with_x, "aggregator", now)

    store.clear_ats_board_rejection("lever", with_underscore)

    assert [
        e.board_identifier for e in _rejected(store, with_underscore, with_x)
    ] == [with_x]
    assert [e.board_identifier for e in _due(store, now, with_underscore, with_x)] == [
        with_underscore
    ]


def test_clear_ats_board_rejection_is_a_no_op_for_a_board_that_was_never_rejected(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("healthy-co")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    store.clear_ats_board_rejection("lever", board)
    store.clear_ats_board_rejection("lever", "never-registered")

    assert [e.board_identifier for e in _due(store, now, board)] == [board]


def test_clear_ats_board_rejection_does_not_revive_a_health_deactivated_board(store):
    # A board deactivated by repeated 404s is broken, not misjudged. Clearing
    # a rejection it never had must not put it back in the rotation.
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("dead-co")
    store.upsert_ats_board(provider="lever", board_identifier=board)
    for i in range(3):
        store.record_ats_scan_failure(
            "lever", board, now + timedelta(hours=25 * i), permanent=True
        )

    store.clear_ats_board_rejection("lever", board)

    assert _due(store, now + timedelta(days=30), board) == []


def test_record_ats_eligible_jobs_counts_every_sighting(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("acme")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    store.record_ats_eligible_jobs([("lever", board)], now)
    later = now + timedelta(hours=2)
    store.record_ats_eligible_jobs([("lever", board)], later)

    entry = _due(store, later, board)[0]
    assert entry.eligible_jobs_seen == 2
    assert from_iso(entry.last_eligible_at) == later


def test_record_ats_eligible_jobs_counts_a_board_once_per_job(store):
    """Many jobs on one board are one row update carrying their count.

    The collapse is what keeps the request count off the job count, and
    getting it wrong the other way -- one increment per board rather than
    per job -- would quietly undercount every busy board.
    """
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    acme = _board_id("acme")
    globex = _board_id("globex")
    store.upsert_ats_board(provider="lever", board_identifier=acme)
    store.upsert_ats_board(provider="ashby", board_identifier=globex)

    updated = store.record_ats_eligible_jobs(
        [("lever", acme), ("lever", acme), ("lever", acme), ("ashby", globex)],
        now,
    )

    assert updated == 2
    seen = {
        (entry.provider, entry.board_identifier): entry.eligible_jobs_seen
        for entry in _due(store, now, acme, globex)
    }
    assert seen == {("lever", acme): 3, ("ashby", globex): 1}


def test_record_ats_eligible_jobs_ignores_a_board_it_does_not_know(store):
    """An unregistered board is left alone, exactly as the per-job version did."""
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    board = _board_id("acme")
    store.upsert_ats_board(provider="lever", board_identifier=board)

    updated = store.record_ats_eligible_jobs(
        [("lever", board), ("lever", "never-registered")], now
    )

    assert updated == 1
    assert _due(store, now, board)[0].eligible_jobs_seen == 1


def test_record_ats_eligible_jobs_with_nothing_to_record_writes_nothing(store):
    """No eligible job on any board means no round trip at all."""
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)

    assert store.record_ats_eligible_jobs([], now) == 0


def test_list_due_ats_boards_orders_by_provider_then_board(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    suffix = uuid.uuid4().hex[:8]
    zeta, beta, alpha = (f"zeta-{suffix}", f"beta-{suffix}", f"alpha-{suffix}")
    store.upsert_ats_board(provider="lever", board_identifier=zeta)
    store.upsert_ats_board(provider="ashby", board_identifier=beta)
    store.upsert_ats_board(provider="ashby", board_identifier=alpha)

    due = _due(store, now, zeta, beta, alpha)

    assert [(e.provider, e.board_identifier) for e in due] == [
        ("ashby", alpha),
        ("ashby", beta),
        ("lever", zeta),
    ]


def test_list_rejected_ats_boards_orders_by_provider_then_board(store):
    now = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
    suffix = uuid.uuid4().hex[:8]
    zeta, beta, alpha = (f"zeta-{suffix}", f"beta-{suffix}", f"alpha-{suffix}")
    store.upsert_ats_board(provider="lever", board_identifier=zeta)
    store.upsert_ats_board(provider="ashby", board_identifier=beta)
    store.upsert_ats_board(provider="ashby", board_identifier=alpha)
    store.reject_ats_board("lever", zeta, "aggregator", now)
    store.reject_ats_board("ashby", beta, "aggregator", now)
    store.reject_ats_board("ashby", alpha, "aggregator", now)

    rejected = _rejected(store, zeta, beta, alpha)

    assert [(e.provider, e.board_identifier) for e in rejected] == [
        ("ashby", alpha),
        ("ashby", beta),
        ("lever", zeta),
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
    # A unique fingerprint per run: the posting this resolves to is global
    # and cannot be deleted, so a fixed one would answer from the text an
    # earlier run left on it (#177, 20260909100000).
    job = Job(
        source="x",
        source_job_id=f"desc-changes-{uuid.uuid4()}",
        title="Senior Product Engineer",
        description="React",
    )
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id))
    assert store.needs_evaluation(job_id) is False

    job.description = "React and TypeScript"
    store.upsert_job(job)
    assert store.needs_evaluation(job_id) is True


def test_needs_evaluation_true_after_content_confidence_changes_post_evaluation(store):
    job = Job(
        source="x", source_job_id=f"conf-changes-{uuid.uuid4()}",
        title="Senior Product Engineer",
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


#: An arbitrary delivery floor for the tests below. Scores are written
#: relative to it (`_FLOOR - 1` is withheld, `_FLOOR` is delivered) so the
#: inclusive boundary is visible without hunting for a bare literal.
_FLOOR = 61


def test_pending_delivery_job_ids_excludes_a_possible_match_below_the_floor(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=_FLOOR - 1, decision="possible_match"))

    assert store.pending_delivery_job_ids(_FLOOR) == []


def test_pending_delivery_job_ids_excludes_a_ready_match_below_the_floor(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=_FLOOR - 1, decision="high_priority"))

    assert store.pending_delivery_job_ids(_FLOOR) == []


def test_pending_delivery_job_ids_keeps_a_possible_match_at_the_floor_until_message_sent(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=_FLOOR, decision="possible_match"))

    assert store.pending_delivery_job_ids(_FLOOR) == [job_id]

    store.mark_delivered(job_id, "telegram_message")
    assert store.pending_delivery_job_ids(_FLOOR) == []


def test_pending_delivery_job_ids_uses_an_inclusive_match_score_floor(store):
    below = Job(source="x", source_job_id="below", title="Senior Product Engineer")
    at_floor = Job(source="x", source_job_id="at-floor", title="Senior Product Engineer")
    below_id, _, _ = store.upsert_job(below)
    at_floor_id, _, _ = store.upsert_job(at_floor)
    store.save_evaluation(
        below_id,
        _evaluation(below_id, total_score=49, decision="possible_match"),
    )
    store.save_evaluation(
        at_floor_id,
        _evaluation(at_floor_id, total_score=50, decision="possible_match"),
    )

    assert store.pending_delivery_job_ids(match_score_floor=50) == [at_floor_id]


def test_pending_delivery_job_ids_keeps_a_ready_match_at_the_floor_until_message_sent(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=_FLOOR, decision="package_match"))

    assert store.pending_delivery_job_ids(_FLOOR) == [job_id]

    store.mark_delivered(job_id, "telegram_message")
    assert store.pending_delivery_job_ids(_FLOOR) == []


def test_pending_delivery_job_ids_excludes_a_ready_match_with_message_but_no_document(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer")
    job_id, _, _ = store.upsert_job(job)
    store.save_evaluation(job_id, _evaluation(job_id, total_score=_FLOOR, decision="high_priority"))
    store.mark_delivered(job_id, "telegram_message")

    assert store.pending_delivery_job_ids(_FLOOR) == []


def test_get_evaluation_and_material_roundtrip(store):
    # `company` is read off the posting now, so this needs a fingerprint no
    # other test shares -- see the note in apps/job-hunter/AGENTS.md.
    job = Job(
        source="x", source_job_id=f"roundtrip-{uuid.uuid4()}",
        title="Senior Product Engineer", company="Acme", description="React",
    )
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


def _job_status(store, job_id: str) -> str:
    rows = store.client.select(
        "job_hunter_jobs", params={"id": f"eq.{job_id}", "select": "status"}
    )
    return rows[0]["status"]


def test_set_job_statuses_persists_both_statuses_in_one_batch(store):
    rejected_id, _, _ = store.upsert_logical_job(
        Job(source="x", source_job_id="r1", title="Senior Frontend Engineer")
    )
    closed_id, _, _ = store.upsert_logical_job(
        Job(source="x", source_job_id="c1", title="Senior Backend Engineer")
    )

    store.set_job_statuses([(rejected_id, "rejected"), (closed_id, "closed")])

    assert _job_status(store, rejected_id) == "rejected"
    assert _job_status(store, closed_id) == "closed"


def test_set_job_statuses_is_a_no_op_for_an_empty_list(store):
    # Must not raise -- discovery calls this unconditionally every run, even
    # when nothing was rejected.
    store.set_job_statuses([])


def test_set_job_statuses_keeps_every_url_filter_under_the_proxy_limit(
    store, monkeypatch
):
    """The `id=in.(...)` filter rides in the query string, so it must be chunked
    by `_URL_FILTER_CHUNK_SIZE`, not the body-sized `_ID_ARRAY_CHUNK_SIZE`.

    A normal run rejects thousands of jobs. At 1,000 uuids per request the
    request line is ~36 KB, roughly 4.5x the ~8 KB limit typical of proxies
    in front of PostgREST -- and a 414 is not in `_RETRY_STATUS_CODES`, so
    it would raise straight out of `collect_candidates` after all the
    batching had already succeeded.

    Written against the generated filters rather than the database: 2,500
    real rows would make this slow without making it stronger, and the
    invariant under test is the shape of the request, not the write.
    """
    from job_hunter import postgres_store as module

    filters: list[str] = []

    def capturing_update(table, payload, params=None, **kwargs):
        filters.append(params["id"])
        return []

    monkeypatch.setattr(store._client, "update", capturing_update)

    job_ids = [f"{index:08d}-0000-4000-8000-000000000000" for index in range(2500)]
    store.set_job_statuses([(job_id, "rejected") for job_id in job_ids])

    # Chunking must actually be happening, or the length assertion below
    # would pass trivially on a single short request.
    assert len(filters) > 1
    assert sum(f.count(",") + 1 for f in filters) == len(job_ids)
    for id_filter in filters:
        # 8 KB is the whole request line's budget, and the filter is only
        # part of it, so leave the path, the other params and the headers
        # room by staying well inside it.
        assert len(id_filter) < 8192, (
            f"an id=in.(...) filter of {len(id_filter)} bytes will 414 -- "
            "set_job_statuses is chunking by the body-sized constant again"
        )
    assert all(
        f.count(",") + 1 <= module._URL_FILTER_CHUNK_SIZE for f in filters
    )


def test_set_job_statuses_rejects_an_invalid_status(store):
    job_id, _, _ = store.upsert_logical_job(
        Job(source="x", source_job_id="bad1", title="Senior Frontend Engineer")
    )
    with pytest.raises(ValueError):
        store.set_job_statuses([(job_id, "new")])


def test_evaluation_market_id_round_trip(store):
    job = Job(source="x", source_job_id="1", title="Senior Product Engineer", company="Acme")
    job_id, _, _ = store.upsert_job(job)

    store.save_evaluation(job_id, _evaluation(job_id, market_id="london"))
    evaluation = store.get_evaluation(job_id)
    assert evaluation is not None
    assert evaluation.market_id == "london"


def test_gmail_message_and_sync_state_roundtrip(store):
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


def test_gmail_sync_state_upsert_advances_history_and_updated_at(store):
    store.save_gmail_sync_state(
        account_id="primary",
        history_id="h1",
        last_successful_sync_at="2026-08-31T10:05:00+00:00",
        backfill_completed_at=None,
    )
    first = store.get_gmail_sync_state("primary")

    store.save_gmail_sync_state(
        account_id="primary",
        history_id="h2",
        last_successful_sync_at="2026-08-31T11:05:00+00:00",
        backfill_completed_at="2026-08-31T11:05:00+00:00",
    )
    second = store.get_gmail_sync_state("primary")

    assert second["history_id"] == "h2"
    assert second["backfill_completed_at"] == "2026-08-31T11:05:00+00:00"
    assert from_iso(second["updated_at"]) > from_iso(first["updated_at"])


def test_record_gmail_message_does_not_overwrite_an_already_processed_message(store):
    """`INSERT OR IGNORE` semantics: a repeat call must not reclassify the message."""
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

    store.record_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="alerts@example.com",
        subject="Frontend roles",
        occurred_at="2026-08-31T10:00:00+00:00",
        classification="REVIEW_NEEDED",
        confidence=0.1,
        rationale="reclassified",
    )

    events = store._client.select(
        "job_hunter_gmail_messages", params={"message_id": "eq.m1"}
    )
    assert len(events) == 1
    assert events[0]["classification"] == "JOB_ALERT"
    assert events[0]["confidence"] == 0.98


def test_inbound_candidate_source_message_and_key_are_idempotent(store):
    job = ExtractedJob(
        source_platform="linkedin",
        source_job_id="job-1",
        url="https://jobs.example.com/1",
        company="Acme",
        title="Frontend Engineer",
    )

    first = store.stage_inbound_job("m1", "linkedin:job-1", job)
    first_rows = store._client.select(
        "job_hunter_inbound_job_candidates",
        params={"select": "id,created_at,last_seen_at"},
    )
    first_last_seen_at = first_rows[0]["last_seen_at"]

    second = store.stage_inbound_job("m1", "linkedin:job-1", job)

    rows = store._client.select(
        "job_hunter_inbound_job_candidates",
        params={"select": "id,created_at,last_seen_at"},
    )
    assert len(rows) == 1
    assert second == first == rows[0]["id"]
    assert from_iso(rows[0]["last_seen_at"]) > from_iso(first_last_seen_at)


def test_application_event_source_message_is_idempotent(store):
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


def test_current_application_state_derives_the_latest_eligible_event(store):
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


def test_pending_reviews_include_subject_and_are_marked_delivered(store):
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


def test_pending_review_events_excludes_high_confidence_auto_matched_events(store):
    """A confident, matched non-REVIEW_NEEDED event needs no human review."""
    job_id, _, _ = store.upsert_job(Job(source="manual", title="Frontend Engineer"))
    store.record_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="recruiter@example.com",
        subject="You applied",
        occurred_at="2026-08-31T10:00:00+00:00",
        classification="APPLIED",
        confidence=0.95,
        rationale="confident",
    )
    store.save_application_event(
        job_id=job_id,
        event_type="APPLIED",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="m1",
        source_thread_id="t1",
        confidence=0.95,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="confident",
    )

    assert store.pending_review_events() == []


def test_release_legacy_gmail_semantic_failures_clears_only_the_legacy_rationale(store):
    from job_hunter.gmail_models import LEGACY_SEMANTIC_FAILURE_RATIONALE

    store.record_gmail_message(
        message_id="legacy",
        thread_id="t1",
        sender="alerts@example.com",
        subject="Unclear",
        occurred_at="2026-08-31T10:00:00+00:00",
        classification="REVIEW_NEEDED",
        confidence=0.0,
        rationale=LEGACY_SEMANTIC_FAILURE_RATIONALE,
    )
    legacy_event_id = store.save_application_event(
        job_id=None,
        event_type="REVIEW_NEEDED",
        occurred_at="2026-08-31T10:00:00+00:00",
        source_message_id="legacy",
        source_thread_id="t1",
        confidence=0.0,
        company="",
        role_title="",
        rationale=LEGACY_SEMANTIC_FAILURE_RATIONALE,
    )
    store.mark_review_delivered([legacy_event_id], "telegram-1")

    store.record_gmail_message(
        message_id="genuine",
        thread_id="t2",
        sender="recruiter@example.com",
        subject="Discuss role",
        occurred_at="2026-08-31T11:00:00+00:00",
        classification="REVIEW_NEEDED",
        confidence=0.4,
        rationale="genuinely ambiguous",
    )
    genuine_event_id = store.save_application_event(
        job_id=None,
        event_type="REVIEW_NEEDED",
        occurred_at="2026-08-31T11:00:00+00:00",
        source_message_id="genuine",
        source_thread_id="t2",
        confidence=0.4,
        company="Acme",
        role_title="Frontend Engineer",
        rationale="genuinely ambiguous",
    )

    removed = store.release_legacy_gmail_semantic_failures()

    assert removed == 1
    assert store.has_processed_gmail_message("legacy") is False
    assert store.has_processed_gmail_message("genuine") is True
    assert store._client.select(
        "job_hunter_review_deliveries", params={"event_id": f"eq.{legacy_event_id}"}
    ) == []
    remaining_ids = {
        row["id"]
        for row in store._client.select(
            "job_hunter_application_events", params={"select": "id"}
        )
    }
    assert remaining_ids == {genuine_event_id}


def test_release_legacy_gmail_semantic_failures_is_a_no_op_when_none_exist(store):
    assert store.release_legacy_gmail_semantic_failures() == 0


def test_release_legacy_gmail_semantic_failures_chunks_large_id_lists(store, monkeypatch):
    """A backlog bigger than one chunk must still be fully released.

    Lowers the real `_URL_FILTER_CHUNK_SIZE` module constant used by
    `release_legacy_gmail_semantic_failures` (not a test-only stand-in) so a
    handful of rows are enough to force more than one `in.(...)` batch on
    both the read and the deletes.
    """
    from job_hunter import postgres_store
    from job_hunter.gmail_models import LEGACY_SEMANTIC_FAILURE_RATIONALE

    monkeypatch.setattr(postgres_store, "_URL_FILTER_CHUNK_SIZE", 2)

    legacy_count = 5
    event_ids = []
    for i in range(legacy_count):
        message_id = f"legacy-{i}"
        store.record_gmail_message(
            message_id=message_id,
            thread_id=f"t{i}",
            sender="alerts@example.com",
            subject="Unclear",
            occurred_at="2026-08-31T10:00:00+00:00",
            classification="REVIEW_NEEDED",
            confidence=0.0,
            rationale=LEGACY_SEMANTIC_FAILURE_RATIONALE,
        )
        event_id = store.save_application_event(
            job_id=None,
            event_type="REVIEW_NEEDED",
            occurred_at="2026-08-31T10:00:00+00:00",
            source_message_id=message_id,
            source_thread_id=f"t{i}",
            confidence=0.0,
            company="",
            role_title="",
            rationale=LEGACY_SEMANTIC_FAILURE_RATIONALE,
        )
        store.mark_review_delivered([event_id], "telegram-1")
        event_ids.append(event_id)

    removed = store.release_legacy_gmail_semantic_failures()

    assert removed == legacy_count
    for i in range(legacy_count):
        assert store.has_processed_gmail_message(f"legacy-{i}") is False
    remaining_ids = {
        row["id"]
        for row in store._client.select(
            "job_hunter_application_events", params={"select": "id"}
        )
    }
    assert remaining_ids.isdisjoint(event_ids)
    for event_id in event_ids:
        assert (
            store._client.select(
                "job_hunter_review_deliveries", params={"event_id": f"eq.{event_id}"}
            )
            == []
        )


def test_candidate_remains_eligible_when_matching_job_has_no_evaluation(store):
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

    assert [row["source_candidate_key"] for row in store.list_eligible_inbound_jobs()] == ["linkedin:1"]


def test_candidate_remains_eligible_when_gmail_job_has_no_evaluation(store):
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

    assert [row["source_candidate_key"] for row in store.list_eligible_inbound_jobs()] == ["candidate-key-1"]


def test_candidate_remains_eligible_when_identity_match_has_no_evaluation(store):
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

    assert [row["source_candidate_key"] for row in store.list_eligible_inbound_jobs()] == ["linkedin:1"]


def test_candidate_emitted_when_no_existing_job_matches(store):
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

    rows = store.list_eligible_inbound_jobs()
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


def test_merge_jobs_preserves_associations_provenance_and_richer_fields(store):
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
    store.upsert_company_watch(
        company_name="Acme",
        careers_url="",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=history_id,
        promotion_source="automatic",
        confidence=1.0,
    )

    assert store.merge_jobs(plain_id, history_id) == history_id

    # The advertisement's own columns live on the posting since #178, so read
    # the posting the surviving membership row points at -- the merge must
    # carry ats_provider across.
    merged = store.client.select(
        "job_hunter_jobs",
        params={
            "id": f"eq.{history_id}",
            "select": (
                "posting:job_hunter_postings"
                "(company,description,url,ats_provider)"
            ),
        },
    )[0]["posting"]
    assert merged["company"] == "Acme"
    assert merged["description"] == "A detailed React role description"
    assert merged["url"] == "https://jobs.lever.co/acme/abc"
    assert merged["ats_provider"] == "lever"
    assert store.count_jobs() == 1
    assert {row["source"] for row in store.list_job_sources(history_id)} == {
        "gmail:linkedin",
        "yc",
    }
    assert store.current_application_state(history_id) == "APPLIED"
    assert store.get_evaluation(history_id) is not None
    assert store.get_material(history_id) is not None
    assert store.has_delivery(history_id, "telegram_message")
    watch = store.get_company_watch("Acme")
    assert watch["discovered_from_job_id"] == history_id


def test_late_canonical_merge_keeps_application_history_job_and_all_associations(
    store,
):
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
    store,
):
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
    # The link is the surviving posting's since #178, and it is the same link
    # the merged job row used to carry: the posting merge applies
    # job_hunter_merge_jobs' URL rule, so a canonical URL wins outright when
    # either side carries an ATS identity.
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


def test_merge_survivor_prefers_other_history_over_age_and_lower_id(store):
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


def test_merge_survivor_prefers_application_events_over_other_history(store):
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
    # Both fingerprints are unique per run: the merged row's description is
    # read off the posting it ends up pointing at (AGENTS.md).
    run = uuid.uuid4()
    canonical_url = f"https://jobs.lever.co/acme/{run}"
    survivor_id, _, _ = store.upsert_job(
        Job(
            source="lever",
            source_job_id=f"abc-{run}",
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
            source_job_id=f"yc-{run}",
            title="Senior Frontend Engineer",
            company="Acme",
            url="https://yc.test/jobs/1",
            description=duplicate_description,
        )
    )

    job_id, is_new, description_changed = store.upsert_logical_job(
        Job(
            source="yc",
            source_job_id=f"yc-{run}",
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
    # No `url` and no `source_job_id`, so the fingerprint falls back to
    # company|title|location -- unique here for the reason in AGENTS.md.
    job = Job(
        source="ashby", title="Eng", company=f"Acme {uuid.uuid4()}",
        description="full JD", content_confidence=OFFICIAL_ATS,
    )

    job_id, _, _ = store.upsert_logical_job(job)

    stored = store.get_job(job_id)
    assert stored.content_confidence == OFFICIAL_ATS


def test_upsert_logical_job_upgrades_description_by_confidence_not_length(store):
    # Neither payload carries a `url`, so `job_fingerprint` falls back to
    # company|title|location -- which is what the posting is keyed by. A
    # unique company keeps this test off the posting the next one uses, and
    # off whichever one either of them left behind on an earlier run (#177).
    company = f"Acme {uuid.uuid4()}"
    weak = Job(
        source="hackernews", title="Eng", company=company, location="Remote",
        canonical_url="https://jobs.example.com/acme/1",
        description="a" * 300, content_confidence=AGGREGATOR_TEXT,
    )
    job_id, _, _ = store.upsert_logical_job(weak)

    strong = Job(
        source="ashby", title="Eng", company=company, location="Remote",
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
    # Unique per run, for the reason on the test above.
    company = f"Acme {uuid.uuid4()}"
    strong = Job(
        source="ashby", title="Eng", company=company, location="Remote",
        canonical_url="https://jobs.example.com/acme/2",
        description="authoritative JD text", content_confidence=OFFICIAL_ATS,
    )
    job_id, _, _ = store.upsert_logical_job(strong)

    weak = Job(
        source="hackernews", title="Eng", company=company, location="Remote",
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


@pytest.mark.parametrize("total_score,raw_model_score", [(64, 89), (64, 0)])
def test_evaluation_raw_model_score_round_trip(store, total_score, raw_model_score):
    """The (64, 0) case guards against `row.get("raw_model_score") or
    row["total_score"]` in `store_mapping.py`, which would silently turn a
    genuinely-zero `raw_model_score` into `total_score` (64) -- a mutation
    the (64, 89) case alone cannot catch, since `or` only misbehaves on a
    falsy left-hand value, and would pass unnoticed if `total_score` were
    also 0.
    """
    job_id, _, _ = store.upsert_job(Job(source="x", source_job_id="1", title="Analyst", company="Acme"))
    store.save_evaluation(
        job_id, _evaluation(job_id, total_score=total_score, raw_model_score=raw_model_score)
    )
    loaded = store.get_evaluation(job_id)
    assert loaded.total_score == total_score
    assert loaded.raw_model_score == raw_model_score


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

    # The identity is the advertisement's, so it is read back off the posting
    # the job row is a membership of (#178).
    row = supabase_client.select(
        "job_hunter_jobs",
        params={
            "id": f"eq.{job_id}",
            "select": "posting:job_hunter_postings(ats_provider,ats_board,ats_job_id)",
        },
    )[0]["posting"]
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
        params={
            "id": f"eq.{attributed_id}",
            "select": "posting:job_hunter_postings(ats_board)",
        },
    )[0]["posting"]
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
            "select": "posting:job_hunter_postings(ats_provider,ats_board,ats_job_id)",
        },
    )[0]["posting"]
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


# ---------------------------------------------------------------------------
# Search profile
# ---------------------------------------------------------------------------


def _search_profile(**overrides) -> SearchProfile:
    defaults = dict(
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        thresholds={"package": 75, "possible": 65},
        salary_floor_eur=90000,
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
        markets=[],
    )
    defaults.update(overrides)
    return SearchProfile(**defaults)


def _search_profile_market(market_id: str, **overrides) -> SearchProfileMarket:
    defaults = dict(
        market_id=market_id,
        query_share=0.5,
        currency="EUR",
        gross_base_floor=90000,
        remote_policy="preferred",
        relocation_policy="selective",
        sponsorship_policy="not_required",
    )
    defaults.update(overrides)
    return SearchProfileMarket(**defaults)


def test_get_search_profile_returns_none_for_a_fresh_user(store):
    assert store.get_search_profile() is None


def test_save_then_get_search_profile_round_trips_with_markets_in_declared_order(
    store,
):
    declared_order = ["germany_eu", "israel_remote", "us_nyc_sf"]
    profile = _search_profile(
        target_titles=["senior product engineer"],
        markets=[_search_profile_market(market_id) for market_id in declared_order],
    )

    profile_id = store.save_search_profile(profile)

    result = store.get_search_profile()
    assert result is not None
    profile_row, market_rows = result
    assert profile_row["id"] == profile_id
    assert profile_row["timezone"] == "Europe/Berlin"
    assert profile_row["target_titles"] == ["senior product engineer"]
    assert [row["market_id"] for row in market_rows] == declared_order


def test_save_search_profile_replaces_markets_rather_than_accumulating(store):
    store.save_search_profile(
        _search_profile(markets=[_search_profile_market("germany_eu")])
    )

    second_profile_id = store.save_search_profile(
        _search_profile(markets=[_search_profile_market("israel_remote")])
    )

    _, market_rows = store.get_search_profile()
    assert [row["market_id"] for row in market_rows] == ["israel_remote"]
    # Same user -> same profile row (upsert on user_id), not a second one.
    assert store.get_search_profile()[0]["id"] == second_profile_id


# Merge redirects (#145) ----------------------------------------------------------
# `merge_jobs` deletes the duplicate row, so any id captured before the merge
# names nothing afterwards. These cover the record that says where it went and
# the writes that follow it.


def test_resolve_merged_job_id_is_none_for_a_job_that_was_never_merged(store):
    job_id, _, _ = store.upsert_job(make_job(fingerprint="live"))

    assert store.resolve_merged_job_id(job_id) is None


def test_resolve_merged_job_id_points_at_the_survivor(store):
    duplicate_id, _, _ = store.upsert_job(make_job(fingerprint="duplicate"))
    survivor_id, _, _ = store.upsert_job(make_job(fingerprint="survivor"))
    store.save_evaluation(survivor_id, _evaluation(survivor_id))

    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id
    assert store.resolve_merged_job_id(duplicate_id) == survivor_id


def test_resolve_merged_job_id_follows_a_survivor_that_is_merged_again(store):
    first_id, _, _ = store.upsert_job(make_job(fingerprint="first"))
    second_id, _, _ = store.upsert_job(make_job(fingerprint="second"))
    third_id, _, _ = store.upsert_job(make_job(fingerprint="third"))
    # History decides the survivor, so give each merge's intended survivor an
    # evaluation first: first -> second, then second -> third.
    store.save_evaluation(second_id, _evaluation(second_id))
    assert store.merge_jobs(second_id, first_id) == second_id
    # Both rows carry an evaluation now, so pin the second merge's survivor
    # with the one signal that outranks that: application-event history.
    store.save_application_event(
        job_id=third_id,
        event_type="APPLIED",
        occurred_at="2026-09-08T10:00:00+00:00",
        source_message_id="m-third",
        source_thread_id="t-third",
        confidence=1.0,
        company="Third GmbH",
        role_title="Engineer",
        rationale="applied",
    )
    assert store.merge_jobs(third_id, second_id) == third_id

    # Redirects are flattened as they are written, so the first job resolves
    # straight to the row that is actually left rather than to a dead one.
    assert store.resolve_merged_job_id(first_id) == third_id
    assert store.resolve_merged_job_id(second_id) == third_id


def test_save_evaluation_follows_a_job_merged_away_since_selection(store):
    duplicate_id, _, _ = store.upsert_job(make_job(fingerprint="stale"))
    survivor_id, _, _ = store.upsert_job(make_job(fingerprint="kept"))
    store.save_evaluation(survivor_id, _evaluation(survivor_id, total_score=55))
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id

    written_to = store.save_evaluation(duplicate_id, _evaluation(duplicate_id, total_score=90))

    assert written_to == survivor_id
    assert store.get_evaluation(survivor_id).total_score == 90


def test_save_evaluation_raises_when_the_job_id_is_not_a_merged_one(store):
    from job_hunter.supabase_client import SupabaseRequestError

    unknown_id = "00000000-0000-0000-0000-0000000000ff"

    # A foreign key violation with no redirect behind it is a real error and
    # must stay one: silently swallowing it would lose the evaluation.
    with pytest.raises(SupabaseRequestError):
        store.save_evaluation(unknown_id, _evaluation(unknown_id))


def test_mark_delivered_follows_a_job_merged_away_since_delivery(store):
    duplicate_id, _, _ = store.upsert_job(make_job(fingerprint="delivered-stale"))
    survivor_id, _, _ = store.upsert_job(make_job(fingerprint="delivered-kept"))
    store.save_evaluation(survivor_id, _evaluation(survivor_id))
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id

    store.mark_delivered(duplicate_id, "telegram_message", "msg-1")

    assert store.has_delivery(survivor_id, "telegram_message")


def test_enqueue_ai_work_follows_a_job_merged_away_since_selection(store):
    duplicate_id, _, _ = store.upsert_job(make_job(fingerprint="deferred-stale"))
    survivor_id, _, _ = store.upsert_job(make_job(fingerprint="deferred-kept"))
    store.save_evaluation(survivor_id, _evaluation(survivor_id))
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id

    # A deferral that raised would drop the job rather than retry it tomorrow.
    store.enqueue_ai_work("job_evaluation", duplicate_id)

    queued = [row["job_id"] for row in store.list_pending_ai_work("job_evaluation")]
    assert queued == [survivor_id]


def test_mark_delivered_returns_the_job_id_it_wrote_against(store):
    duplicate_id, _, _ = store.upsert_job(make_job(fingerprint="returned-stale"))
    survivor_id, _, _ = store.upsert_job(make_job(fingerprint="returned-kept"))
    store.save_evaluation(survivor_id, _evaluation(survivor_id))
    assert store.merge_jobs(survivor_id, duplicate_id) == survivor_id

    # The caller reads the job back to attribute the delivery to its source,
    # and has to be given an id that still names a row.
    assert store.mark_delivered(duplicate_id, "telegram_message", "msg-1") == survivor_id
    assert store.mark_delivered(survivor_id, "telegram_document", "doc-1") == survivor_id

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from job_hunter.gemini_usage import (
    GeminiBudgetExceeded,
    GeminiQuotaPaused,
    GeminiUsageTracker,
    _pacific_day_bounds,
    estimate_input_tokens,
)
from job_hunter.models import GeminiQuotaSettings
from job_hunter.store import JobStore

MODEL = "gemini-3.6-flash"
NOW = datetime(2026, 9, 1, 20, 0, 0, tzinfo=timezone.utc)  # 2026-09-01T20:00 UTC = 13:00 PDT


def _quota(**overrides) -> GeminiQuotaSettings:
    defaults = dict(rpm=10, tpm=1000, rpd=100)
    defaults.update(overrides)
    return GeminiQuotaSettings(**defaults)


@pytest.fixture
def store() -> JobStore:
    return JobStore(":memory:")


@pytest.fixture
def tracker(store: JobStore) -> GeminiUsageTracker:
    return GeminiUsageTracker(store, _quota(), MODEL, run_id="run-1")


def _record_attempts(
    store: JobStore,
    count: int,
    *,
    purpose: str = "gmail_semantic",
    occurred_at: datetime = NOW,
    status: str = "success",
    estimated_input_tokens: int = 10,
    prompt_tokens: int | None = None,
    spacing_seconds: int = 120,
) -> None:
    """Record `count` attempts strictly before `occurred_at`, spaced apart.

    The default spacing (120s) keeps rows outside any 60-second rolling
    window relative to `occurred_at`, so tests that exercise the daily/core
    reserve ceiling aren't incidentally also tripping the RPM ceiling. Tests
    that specifically target RPM/TPM pass a tight `spacing_seconds=1`.
    """
    for i in range(count):
        store.record_gemini_usage(
            occurred_at=(occurred_at - timedelta(seconds=spacing_seconds * (i + 1))).isoformat(),
            run_id="run-1",
            model=MODEL,
            purpose=purpose,
            status=status,
            estimated_input_tokens=estimated_input_tokens,
            prompt_tokens=prompt_tokens,
        )


# ---------------------------------------------------------------------------
# Step 0: GeminiQuotaSettings validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["rpm", "tpm", "rpd", "rate_pause_seconds"])
@pytest.mark.parametrize("bad_value", [0, -1])
def test_gemini_quota_settings_rejects_non_positive_fields(field_name, bad_value):
    kwargs = dict(rpm=10, tpm=1000, rpd=100, rate_pause_seconds=90)
    kwargs[field_name] = bad_value
    with pytest.raises(ValueError, match=field_name):
        GeminiQuotaSettings(**kwargs)


def test_gemini_quota_settings_accepts_all_positive_fields():
    # Regression guard: a valid construction must not raise.
    GeminiQuotaSettings(rpm=10, tpm=1000, rpd=100, rate_pause_seconds=1)


# ---------------------------------------------------------------------------
# Step 1: token estimation and Pacific-day tests
# ---------------------------------------------------------------------------


def test_estimate_input_tokens_is_conservative_character_estimate():
    assert estimate_input_tokens("x" * 10) == 4


def test_estimate_input_tokens_never_returns_zero_for_nonempty_prompt():
    assert estimate_input_tokens("x") == 1


def test_daily_rows_reset_on_pacific_midnight_not_utc_or_berlin(store, tracker):
    # 2026-09-02T06:59:59 UTC is 2026-09-01T23:59:59 PDT (still "yesterday" in Pacific).
    before_midnight = datetime(2026, 9, 2, 6, 59, 59, tzinfo=timezone.utc)
    # 2026-09-02T07:00:01 UTC is 2026-09-02T00:00:01 PDT (just after Pacific midnight).
    after_midnight = datetime(2026, 9, 2, 7, 0, 1, tzinfo=timezone.utc)

    _record_attempts(store, 1, purpose="job_evaluation", occurred_at=before_midnight)

    snap_before = tracker.snapshot(before_midnight)
    assert snap_before.requests_today == 1

    snap_after = tracker.snapshot(after_midnight)
    assert snap_after.requests_today == 0

    # A UTC-midnight or Berlin-midnight boundary would have already rolled over
    # by 07:00:01 UTC; only the Pacific boundary (07:00 UTC in PDT) explains this.


def test_pacific_day_spans_23_hours_on_spring_forward_and_rolls_over_correctly(
    store, tracker
):
    # 2026-03-08 is the US spring-forward day: Pacific clocks jump from 02:00
    # PST straight to 03:00 PDT, so the *local calendar day* is only 23 real
    # hours long (07:00 UTC on Mar 8 to 07:00 UTC on Mar 9).
    moment_in_day = datetime(2026, 3, 8, 12, 0, 0, tzinfo=timezone.utc)
    start, end = _pacific_day_bounds(moment_in_day)
    assert start == datetime(2026, 3, 8, 8, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 3, 9, 7, 0, 0, tzinfo=timezone.utc)
    assert end - start == timedelta(hours=23)

    _record_attempts(
        store, 1, purpose="job_evaluation", occurred_at=moment_in_day, spacing_seconds=1
    )

    assert tracker.snapshot(moment_in_day).requests_today == 1

    # A day naively assumed to be a fixed 24h would still call this "today";
    # the true (23h) Pacific day has already rolled over by the real boundary.
    just_after_true_midnight = end + timedelta(seconds=1)
    assert tracker.snapshot(just_after_true_midnight).requests_today == 0


def test_pacific_day_spans_25_hours_on_fall_back_with_repeated_local_hour(
    store, tracker
):
    # 2026-11-01 is the US fall-back day: Pacific clocks repeat 01:00-02:00
    # local (first as PDT, then as PST), so the local calendar day is 25 real
    # hours long (07:00 UTC on Nov 1 to 08:00 UTC on Nov 2).
    moment_in_day = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    start, end = _pacific_day_bounds(moment_in_day)
    assert start == datetime(2026, 11, 1, 7, 0, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 2, 8, 0, 0, tzinfo=timezone.utc)
    assert end - start == timedelta(hours=25)

    # 01:30 local occurs twice: first as PDT (UTC-7), then as PST (UTC-8).
    first_pass_0130_pdt = datetime(2026, 11, 1, 8, 30, 0, tzinfo=timezone.utc)
    second_pass_0130_pst = datetime(2026, 11, 1, 9, 30, 0, tzinfo=timezone.utc)
    for occurred_at in (first_pass_0130_pdt, second_pass_0130_pst):
        store.record_gemini_usage(
            occurred_at=occurred_at.isoformat(),
            run_id="run-1",
            model=MODEL,
            purpose="job_evaluation",
            status="success",
            estimated_input_tokens=10,
        )

    # Both passes through the repeated hour land in the same Pacific day.
    assert tracker.snapshot(moment_in_day).requests_today == 2

    # A day naively assumed to be a fixed 24h would already have rolled over
    # by now; the true (25h) Pacific day has not.
    still_same_day = start + timedelta(hours=24, minutes=30)
    assert tracker.snapshot(still_same_day).requests_today == 2

    just_after_true_midnight = end + timedelta(seconds=1)
    assert tracker.snapshot(just_after_true_midnight).requests_today == 0


# ---------------------------------------------------------------------------
# Step 2: 80% ceiling and core-reserve tests
# ---------------------------------------------------------------------------


def test_non_core_cannot_consume_core_reserve(store, tracker):
    _record_attempts(store, 60, purpose="gmail_semantic")

    with pytest.raises(GeminiBudgetExceeded):
        tracker.preflight("gmail_semantic", "email", NOW)

    tracker.preflight("job_evaluation", "job", NOW)


def test_core_purpose_blocked_once_ceiling_fully_consumed(store, tracker):
    _record_attempts(store, 80, purpose="job_evaluation")

    with pytest.raises(GeminiBudgetExceeded):
        tracker.preflight("job_evaluation", "job", NOW)


def test_preflight_blocks_at_rpm_ceiling(store, tracker):
    # rpm=10, ceiling_ratio=0.8 -> rpm ceiling of 8 requests per rolling minute.
    _record_attempts(
        store, 8, purpose="job_evaluation", occurred_at=NOW, spacing_seconds=1
    )

    with pytest.raises(GeminiBudgetExceeded):
        tracker.preflight("job_evaluation", "job", NOW)


def test_preflight_allows_once_rolling_minute_window_expires(store, tracker):
    stale = NOW - timedelta(seconds=61)
    _record_attempts(store, 8, purpose="job_evaluation", occurred_at=stale, spacing_seconds=1)

    tracker.preflight("job_evaluation", "job", NOW)


def test_preflight_blocks_at_estimated_tpm_ceiling(store, tracker):
    # tpm=1000, ceiling_ratio=0.8 -> tpm ceiling of 800 tokens per rolling minute.
    _record_attempts(
        store,
        1,
        purpose="job_evaluation",
        occurred_at=NOW,
        estimated_input_tokens=795,
        spacing_seconds=1,
    )

    with pytest.raises(GeminiBudgetExceeded):
        # "x" * 30 -> 10 estimated tokens; 795 + 10 > 800.
        tracker.preflight("job_evaluation", "x" * 30, NOW)


def test_preflight_allows_within_estimated_tpm_ceiling(store, tracker):
    _record_attempts(
        store,
        1,
        purpose="job_evaluation",
        occurred_at=NOW,
        estimated_input_tokens=780,
        spacing_seconds=1,
    )

    tracker.preflight("job_evaluation", "x" * 30, NOW)


def test_blocked_budget_rows_do_not_count_toward_rpd(store, tracker):
    _record_attempts(store, 80, purpose="job_evaluation", status="blocked_budget")

    tracker.preflight("job_evaluation", "job", NOW)


def test_preflight_records_blocked_budget_exactly_once(store, tracker):
    _record_attempts(store, 60, purpose="gmail_semantic")

    with pytest.raises(GeminiBudgetExceeded):
        tracker.preflight("gmail_semantic", "email", NOW)

    rows = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", model=MODEL
    )
    blocked = [r for r in rows if r["status"] == "blocked_budget"]
    assert len(blocked) == 1
    assert blocked[0]["purpose"] == "gmail_semantic"


# ---------------------------------------------------------------------------
# Step 3: pause-state tests
# ---------------------------------------------------------------------------


def test_record_429_rate_limit_pauses_for_configured_seconds_with_no_provider_attempt(
    store, tracker
):
    tracker.record_429("job_evaluation", "job", NOW, kind="rate_limit")

    with pytest.raises(GeminiQuotaPaused):
        tracker.preflight("job_evaluation", "job", NOW + timedelta(seconds=1))

    # The pause has expired; no provider attempt is blocked any more.
    tracker.preflight("job_evaluation", "job", NOW + timedelta(seconds=91))


def test_record_429_daily_quota_pauses_until_next_pacific_midnight(store, tracker):
    tracker.record_429("job_evaluation", "job", NOW, kind="daily_quota")

    with pytest.raises(GeminiQuotaPaused):
        tracker.preflight("job_evaluation", "job", NOW + timedelta(hours=1))

    # Just after the next Pacific midnight the pause must have expired.
    next_pacific_midnight = datetime(2026, 9, 2, 7, 0, 1, tzinfo=timezone.utc)
    tracker.preflight("job_evaluation", "job", next_pacific_midnight)


def test_record_429_unknown_kind_pauses_conservatively_for_rate_pause_seconds(
    store, tracker
):
    tracker.record_429("job_evaluation", "job", NOW, kind="unknown")

    with pytest.raises(GeminiQuotaPaused):
        tracker.preflight("job_evaluation", "job", NOW + timedelta(seconds=1))

    tracker.preflight("job_evaluation", "job", NOW + timedelta(seconds=91))


def test_pause_blocks_before_any_budget_check_and_records_no_ledger_row(store, tracker):
    tracker.record_429("job_evaluation", "job", NOW, kind="rate_limit")

    rows_before = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", model=MODEL
    )
    count_before = len(rows_before)

    with pytest.raises(GeminiQuotaPaused):
        tracker.preflight("job_evaluation", "job", NOW + timedelta(seconds=1))

    rows_after = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", model=MODEL
    )
    assert len(rows_after) == count_before


def test_record_429_returns_the_persisted_pause_pair(store, tracker):
    """A caller must be able to raise GeminiQuotaPaused directly from this
    return value instead of re-querying store state via a second preflight()
    call (see gemini.py's 429 handling)."""
    paused_until, reason = tracker.record_429("job_evaluation", "job", NOW, kind="daily_quota")

    pause = store.get_gemini_pause(MODEL)
    assert (paused_until, reason) == (pause["paused_until"], pause["reason"])


def test_record_429_return_value_survives_a_later_expired_or_overwritten_pause(
    store, tracker
):
    """Regression: record_429's return must reflect what it itself computed
    and wrote at call time, not whatever the store happens to hold later.

    This used to matter because the old call site re-ran preflight() right
    after record_429() using the *same* `now`; with `rate_pause_seconds=0`
    (now rejected by GeminiQuotaSettings.__post_init__) that made the freshly
    persisted pause already look expired, and the exception surfaced as a
    bare GeminiError instead of GeminiQuotaPaused. `rate_pause_seconds=0` is
    no longer constructible, so this test proves the same failure mode is
    closed by construction: tampering with the store after the fact cannot
    change what record_429 already returned.
    """
    paused_until, reason = tracker.record_429("job_evaluation", "job", NOW, kind="rate_limit")

    store.set_gemini_pause(MODEL, (NOW - timedelta(seconds=1)).isoformat(), "rate_limit")

    assert reason == "rate_limit"
    assert datetime.fromisoformat(paused_until) > NOW


def test_record_429_writes_exactly_one_row_regardless_of_daily_ceiling(store):
    """Regression: record_429 must never trip the daily budget check on the
    quota_429 row it is about to write. Previously, calling preflight() again
    right after record_429() counted that just-written row and, with a tight
    rpd ceiling, raised GeminiBudgetExceeded plus a spurious extra
    blocked_budget row for a call that indisputably reached Google. record_429
    itself never calls preflight, so this must hold no matter how full the
    ceiling is.
    """
    tight_quota = _quota(rpd=2)
    tracker = GeminiUsageTracker(store, tight_quota, MODEL, run_id="run-1")

    tracker.record_429("job_evaluation", "job", NOW, kind="unknown")

    rows = store.gemini_usage_rows(
        "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00", model=MODEL
    )
    assert [row["status"] for row in rows] == ["quota_429"]


def test_preflight_rejects_naive_datetime(tracker):
    with pytest.raises(ValueError):
        tracker.preflight("job_evaluation", "job", datetime(2026, 9, 1, 12, 0, 0))


# ---------------------------------------------------------------------------
# Step 5: snapshot tests
# ---------------------------------------------------------------------------


def test_snapshot_computes_exact_provider_percentages(store, tracker):
    _record_attempts(
        store,
        5,
        purpose="job_evaluation",
        occurred_at=NOW,
        estimated_input_tokens=50,
        spacing_seconds=1,
    )

    summary = tracker.snapshot(NOW)

    assert summary.requests_today == 5
    assert summary.rpd_percent == pytest.approx(5 / 100 * 100)
    assert summary.rpm_peak_percent == pytest.approx(5 / 10 * 100)
    assert summary.tpm_peak_percent == pytest.approx(250 / 1000 * 100)
    assert summary.purpose_counts == {"job_evaluation": 5}


def test_snapshot_uses_exact_usage_metadata_over_estimate(store, tracker):
    store.record_gemini_usage(
        occurred_at=NOW.isoformat(),
        run_id="run-1",
        model=MODEL,
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=999,
        prompt_tokens=42,
        output_tokens=8,
        thinking_tokens=2,
        cached_tokens=1,
        total_tokens=53,
    )

    summary = tracker.snapshot(NOW)

    assert summary.input_tokens_today == 42
    assert summary.output_tokens_today == 8
    assert summary.thinking_tokens_today == 2
    assert summary.cached_tokens_today == 1


def test_snapshot_falls_back_to_estimate_when_no_usage_metadata(store, tracker):
    store.record_gemini_usage(
        occurred_at=NOW.isoformat(),
        run_id="run-1",
        model=MODEL,
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=17,
    )

    summary = tracker.snapshot(NOW)

    assert summary.input_tokens_today == 17
    assert summary.output_tokens_today == 0
    # No usageMetadata at all means no total_tokens either; the reconstruction
    # falls back to input(estimate) + output(0) + thinking(0), same as above.
    assert summary.total_tokens_today == 17


def test_snapshot_uses_authoritative_total_tokens_not_a_naive_component_sum(store, tracker):
    """`cachedContentTokenCount` is a subset of `promptTokenCount`, not additional

    to it, so Google's own `totalTokenCount` for one call with
    promptTokenCount=1000 (400 of which were cached), candidatesTokenCount=200,
    and thoughtsTokenCount=50 is 1250 -- never `1000 + 200 + 50 + 400 = 1650`.
    """
    store.record_gemini_usage(
        occurred_at=NOW.isoformat(),
        run_id="run-1",
        model=MODEL,
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=999,
        prompt_tokens=1000,
        output_tokens=200,
        thinking_tokens=50,
        cached_tokens=400,
        total_tokens=1250,
    )

    summary = tracker.snapshot(NOW)

    assert summary.total_tokens_today == 1250
    assert (
        summary.input_tokens_today
        + summary.output_tokens_today
        + summary.thinking_tokens_today
        + summary.cached_tokens_today
    ) == 1650


def test_snapshot_excludes_blocked_budget_rows_from_provider_percentages(store, tracker):
    _record_attempts(store, 80, purpose="job_evaluation", status="blocked_budget")

    summary = tracker.snapshot(NOW)

    assert summary.requests_today == 0
    assert summary.rpd_percent == 0
    assert summary.rpm_peak_percent == 0


def test_snapshot_reports_internal_budget_and_provider_pause_flags(store, tracker):
    summary = tracker.snapshot(NOW)
    assert summary.internal_budget_exhausted is False
    assert summary.provider_paused is False

    tracker.record_429("job_evaluation", "job", NOW, kind="rate_limit")
    paused_summary = tracker.snapshot(NOW + timedelta(seconds=1))
    assert paused_summary.provider_paused is True

    expired_summary = tracker.snapshot(NOW + timedelta(seconds=91))
    assert expired_summary.provider_paused is False


def test_snapshot_filters_by_run_id(store, tracker):
    store.record_gemini_usage(
        occurred_at=NOW.isoformat(),
        run_id="other-run",
        model=MODEL,
        purpose="job_evaluation",
        status="success",
        estimated_input_tokens=10,
    )

    summary = tracker.snapshot(NOW, run_id="run-1")

    assert summary.requests_today == 0

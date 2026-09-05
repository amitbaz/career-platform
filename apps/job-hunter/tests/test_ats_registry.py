from datetime import datetime, timedelta, timezone

import pytest

from job_hunter.ats_registry import (
    extract_ats_reference,
    harvest_ats_board,
    select_ats_boards,
)
from job_hunter.models import AtsRegistryEntry, Job
from job_hunter.store import JobStore


@pytest.mark.parametrize(
    ("url", "provider", "board"),
    [
        ("https://jobs.ashbyhq.com/omnea/123", "ashby", "omnea"),
        ("https://jobs.lever.co/acme/abc", "lever", "acme"),
        ("https://boards.greenhouse.io/brex/jobs/999", "greenhouse", "brex"),
    ],
)
def test_extract_ats_reference_from_supported_url(url, provider, board):
    ref = extract_ats_reference(Job(source="feed", title="x", url=url))
    assert ref is not None
    assert (ref.provider, ref.board) == (provider, board)


def test_extract_ats_reference_returns_none_for_unsupported_url():
    ref = extract_ats_reference(
        Job(source="feed", title="x", url="https://example.com/jobs/1")
    )
    assert ref is None


def test_extract_ats_reference_prefers_populated_ats_fields():
    job = Job(
        source="feed",
        title="x",
        url="https://example.com/jobs/1",
        canonical_url="https://jobs.lever.co/other/def",
        ats_provider="greenhouse",
        ats_board="acme",
        ats_job_id="999",
    )
    ref = extract_ats_reference(job)
    assert (ref.provider, ref.board, ref.job_id) == ("greenhouse", "acme", "999")


def test_extract_ats_reference_falls_back_to_canonical_url_before_url():
    job = Job(
        source="feed",
        title="x",
        url="https://example.com/jobs/1",
        canonical_url="https://jobs.lever.co/acme/abc",
    )
    ref = extract_ats_reference(job)
    assert (ref.provider, ref.board, ref.job_id) == ("lever", "acme", "abc")


def test_extract_ats_reference_falls_back_to_url_before_original_url():
    job = Job(
        source="feed",
        title="x",
        url="https://jobs.ashbyhq.com/acme/xyz",
        original_url="https://jobs.lever.co/other/def",
    )
    ref = extract_ats_reference(job)
    assert (ref.provider, ref.board, ref.job_id) == ("ashby", "acme", "xyz")


def test_extract_ats_reference_falls_back_to_original_url_last():
    job = Job(
        source="feed",
        title="x",
        url="https://example.com/jobs/1",
        original_url="https://jobs.lever.co/acme/abc",
    )
    ref = extract_ats_reference(job)
    assert (ref.provider, ref.board, ref.job_id) == ("lever", "acme", "abc")


def test_harvest_ats_board_persists_supported_reference():
    store = JobStore(":memory:")
    job = Job(
        source="feed",
        title="x",
        company="Omnea",
        market_hint="london",
        url="https://jobs.ashbyhq.com/omnea/123",
    )

    created = harvest_ats_board(store, job)

    assert created is True
    assert store.count_ats_boards() == 1


def test_harvest_ats_board_returns_false_for_unsupported_url():
    store = JobStore(":memory:")
    job = Job(source="feed", title="x", url="https://example.com/jobs/1")

    created = harvest_ats_board(store, job)

    assert created is False
    assert store.count_ats_boards() == 0


def test_harvest_ats_board_uses_market_hint_precedence():
    store = JobStore(":memory:")
    job = Job(
        source="feed",
        title="x",
        company="Omnea",
        market_id="berlin",
        url="https://jobs.ashbyhq.com/omnea/123",
    )

    harvest_ats_board(store, job, market_hint="london")

    due = store.list_due_ats_boards(datetime.now(timezone.utc))
    assert due[0].market_hint == "london"


def _entry(
    provider,
    board_identifier,
    *,
    market_hint="",
    last_checked_at=None,
    last_eligible_at=None,
):
    return AtsRegistryEntry(
        provider=provider,
        board_identifier=board_identifier,
        company_name="",
        market_hint=market_hint,
        first_seen_at="2026-01-01T00:00:00+00:00",
        last_seen_at="2026-01-01T00:00:00+00:00",
        last_checked_at=last_checked_at,
        last_success_at=None,
        last_eligible_at=last_eligible_at,
        last_job_count=0,
        eligible_jobs_seen=0,
        consecutive_failures=0,
        active=True,
        paused_until=None,
    )


def test_select_ats_boards_applies_documented_priority_order():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)

    recently_eligible = _entry(
        "greenhouse",
        "recent",
        market_hint="berlin",
        last_eligible_at=(now - timedelta(days=5)).isoformat(),
        last_checked_at=(now - timedelta(days=1)).isoformat(),
    )
    market_priority = _entry(
        "ashby",
        "priority-market",
        market_hint="london",
        last_checked_at=(now - timedelta(days=2)).isoformat(),
    )
    never_checked = _entry(
        "lever",
        "never-checked",
        market_hint="berlin",
        last_checked_at=None,
    )
    oldest_checked = _entry(
        "lever",
        "oldest-checked",
        market_hint="berlin",
        last_checked_at=(now - timedelta(days=10)).isoformat(),
    )

    entries = [market_priority, oldest_checked, recently_eligible, never_checked]
    market_order = ["berlin", "london"]

    selected = select_ats_boards(entries, market_order, 3, now)

    assert [(entry.provider, entry.board_identifier) for entry in selected] == [
        ("greenhouse", "recent"),
        ("lever", "never-checked"),
        ("lever", "oldest-checked"),
    ]


def test_select_ats_boards_orders_by_oldest_checked_first_when_earlier_levels_tie():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    # Both tie on level 1 (no last_eligible_at), level 2 (same market), and
    # level 3 (both previously checked). Lexical order (level 5) would put
    # "board-a" before "board-b" -- the opposite of the correct level-4
    # order -- so this only passes if last_checked_at is applied first.
    checked_recently = _entry(
        "lever",
        "board-a",
        market_hint="berlin",
        last_checked_at=(now - timedelta(days=1)).isoformat(),
    )
    checked_long_ago = _entry(
        "lever",
        "board-b",
        market_hint="berlin",
        last_checked_at=(now - timedelta(days=10)).isoformat(),
    )

    selected = select_ats_boards([checked_recently, checked_long_ago], ["berlin"], 2, now)

    assert [(entry.provider, entry.board_identifier) for entry in selected] == [
        ("lever", "board-b"),
        ("lever", "board-a"),
    ]


def test_select_ats_boards_tie_breaks_lexically_on_provider_and_board():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    a = _entry("ashby", "zzz", market_hint="berlin")
    b = _entry("ashby", "aaa", market_hint="berlin")

    selected = select_ats_boards([a, b], ["berlin"], 2, now)

    assert [(entry.provider, entry.board_identifier) for entry in selected] == [
        ("ashby", "aaa"),
        ("ashby", "zzz"),
    ]


def test_select_ats_boards_respects_limit():
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    entries = [_entry("lever", f"board-{i}") for i in range(5)]

    selected = select_ats_boards(entries, [], 2, now)

    assert len(selected) == 2

from datetime import datetime, timedelta, timezone

import pytest

from job_hunter.ats_registry import (
    ats_board_reference,
    extract_ats_reference,
    select_ats_boards,
)
from job_hunter.models import AtsRegistryEntry, Job


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


def test_ats_board_reference_registers_a_supported_reference(store):
    job = Job(
        source="feed",
        title="x",
        company="Omnea",
        market_hint="london",
        url="https://jobs.ashbyhq.com/omnea/123",
    )

    reference = ats_board_reference(job)

    assert reference == ("ashby", "omnea", "Omnea", "london")
    assert store.upsert_ats_boards([reference]) == 1
    assert store.count_ats_boards() == 1


def test_ats_board_reference_is_none_for_unsupported_url(store):
    job = Job(source="feed", title="x", url="https://example.com/jobs/1")

    assert ats_board_reference(job) is None
    assert store.count_ats_boards() == 0


def test_ats_board_reference_refuses_denylisted_board(store):
    job = Job(
        source="feed",
        title="x",
        company="Jobgether",
        url="https://jobs.lever.co/jobgether/123",
    )

    assert ats_board_reference(job, denylist=frozenset({"lever:jobgether"})) is None
    assert store.count_ats_boards() == 0


def test_ats_board_reference_denylist_match_is_case_insensitive(store):
    # A manual_company_watch seed can carry an unnormalized provider, and
    # upsert_ats_board would store it lowercased -- creating the very row
    # the denylist exists to prevent.
    job = Job(
        source="feed",
        title="x",
        company="Jobgether",
        ats_provider="Lever",
        ats_board="JobGether",
        ats_job_id="1",
    )

    assert ats_board_reference(job, denylist=frozenset({"lever:jobgether"})) is None
    assert store.count_ats_boards() == 0


def test_ats_board_reference_admits_board_not_on_denylist(store):
    job = Job(
        source="feed",
        title="x",
        company="Omnea",
        url="https://jobs.ashbyhq.com/omnea/123",
    )

    reference = ats_board_reference(job, denylist=frozenset({"lever:jobgether"}))

    assert reference is not None
    assert store.upsert_ats_boards([reference]) == 1
    assert store.count_ats_boards() == 1


def test_ats_board_reference_uses_market_hint_precedence(store):
    job = Job(
        source="feed",
        title="x",
        company="Omnea",
        market_id="berlin",
        url="https://jobs.ashbyhq.com/omnea/123",
    )

    reference = ats_board_reference(job, market_hint="london")

    assert reference is not None
    store.upsert_ats_boards([reference])

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

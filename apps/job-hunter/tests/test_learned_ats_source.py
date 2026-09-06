import logging
from datetime import datetime, timedelta, timezone

import requests

from job_hunter.sources.learned_ats import LearnedAtsSource, LearnedAtsStats
from job_hunter.store import JobStore


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _http_error(status_code: int) -> requests.HTTPError:
    return requests.HTTPError(f"status {status_code}", response=_Response(status_code))


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


_JOBGETHER_PHRASING = (
    "This position is listed on behalf of a partner company, who manages "
    "all applications and next steps."
)


def _lever_postings(count, board, phrasing="", start_id=1):
    return [
        {
            "id": str(start_id + i),
            "text": "Senior Product Engineer",
            "categories": {"location": "Remote"},
            "hostedUrl": f"https://jobs.lever.co/{board}/{start_id + i}",
            "descriptionPlain": f"Great role. {phrasing} Apply now.",
            "workplaceType": "remote",
        }
        for i in range(count)
    ]


def _seed_board(store, provider, board_identifier, market_hint="berlin"):
    store.upsert_ats_board(
        provider=provider,
        board_identifier=board_identifier,
        company_name=board_identifier,
        market_hint=market_hint,
    )


def test_learned_ats_source_scans_due_boards_through_native_adapters():
    store = JobStore(":memory:")
    _seed_board(store, "ashby", "acme-ashby")
    _seed_board(store, "lever", "acme-lever")
    _seed_board(store, "greenhouse", "acme-greenhouse")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    http = RoutingHttp(
        responses={
            "ashbyhq.com": {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Senior Product Engineer",
                        "location": "Remote",
                        "jobUrl": "https://jobs.ashbyhq.com/acme-ashby/1",
                        "descriptionPlain": "React",
                        "isRemote": True,
                    }
                ]
            },
            "lever.co": [
                {
                    "id": "2",
                    "text": "Senior Product Engineer",
                    "categories": {"location": "Remote"},
                    "hostedUrl": "https://jobs.lever.co/acme-lever/2",
                    "descriptionPlain": "React",
                    "workplaceType": "remote",
                }
            ],
            "greenhouse.io": {
                "jobs": [
                    {
                        "id": 3,
                        "title": "Senior Product Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": "https://boards.greenhouse.io/acme-greenhouse/3",
                        "content": "React",
                    }
                ]
            },
        }
    )

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()

    assert sorted(job.source for job in jobs) == ["ashby", "greenhouse", "lever"]
    assert source.stats == LearnedAtsStats(
        boards_scanned=3, boards_successful=3, boards_failed=0, jobs_raw=3
    )


def test_learned_ats_source_isolates_a_failing_board_from_a_healthy_one():
    store = JobStore(":memory:")
    _seed_board(store, "ashby", "broken-ashby")
    _seed_board(store, "lever", "healthy-lever")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    http = RoutingHttp(
        responses={
            "lever.co": [
                {
                    "id": "2",
                    "text": "Senior Product Engineer",
                    "categories": {"location": "Remote"},
                    "hostedUrl": "https://jobs.lever.co/healthy-lever/2",
                    "descriptionPlain": "React",
                    "workplaceType": "remote",
                }
            ],
        },
        fail_urls={"ashbyhq.com"},
    )

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()

    assert [job.source_job_id for job in jobs] == ["2"]
    assert jobs[0].source == "lever"
    assert source.stats.boards_scanned == 2
    assert source.stats.boards_successful == 1
    assert source.stats.boards_failed == 1
    assert source.stats.jobs_raw == 1

    later = now + timedelta(hours=25)
    entries = {entry.board_identifier: entry for entry in store.list_due_ats_boards(later)}
    assert entries["broken-ashby"].consecutive_failures == 1
    assert entries["broken-ashby"].last_success_at is None
    assert entries["healthy-lever"].consecutive_failures == 0
    assert entries["healthy-lever"].last_success_at == now.isoformat()
    assert entries["healthy-lever"].last_job_count == 1


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


def test_learned_ats_source_never_deactivates_board_after_repeated_transient_errors():
    # Mirror image of test_learned_ats_source_deactivates_board_after_repeated_404s:
    # repeated *transient* (non-404) errors must never deactivate a board.
    # This is the test that would fail if `permanent` were ever hardcoded or
    # inverted for the unexpected-error path instead of being threaded
    # through as `is_stale_board_error(exc)`.
    store = JobStore(":memory:")
    _seed_board(store, "greenhouse", "flaky-co")
    base = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(fail_urls={"flaky-co"})

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
    assert due_identifiers == {"flaky-co"}


def test_learned_ats_source_rejects_jobgether_shaped_board_and_drops_its_jobs():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "jobgether")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(2, "jobgether") + _lever_postings(
        6, "jobgether", _JOBGETHER_PHRASING, start_id=3
    )
    http = RoutingHttp(responses={"lever.co": postings})

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()

    assert jobs == []
    assert source.stats.boards_rejected == 1
    assert source.stats.boards_successful == 0
    assert source.stats.jobs_raw == 0

    later = now + timedelta(days=30)
    assert store.list_due_ats_boards(later) == []


def test_learned_ats_source_keeps_scanning_veeva_shaped_board_with_no_markers():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "veeva")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(21, "veeva")
    http = RoutingHttp(responses={"lever.co": postings})

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()

    assert len(jobs) == 21
    assert source.stats.boards_successful == 1
    assert source.stats.boards_rejected == 0

    due = {e.board_identifier for e in store.list_due_ats_boards(now)}
    assert due == {"veeva"}


def test_learned_ats_source_survives_one_stray_third_party_posting():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "mostly-clean")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(19, "mostly-clean") + _lever_postings(
        1, "mostly-clean", _JOBGETHER_PHRASING, start_id=20
    )
    http = RoutingHttp(responses={"lever.co": postings})

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()

    assert len(jobs) == 20
    assert source.stats.boards_rejected == 0
    due = {e.board_identifier for e in store.list_due_ats_boards(now)}
    assert due == {"mostly-clean"}


def test_learned_ats_source_refuses_denylisted_board_without_scanning():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "jobgether")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={"lever.co": _lever_postings(10, "jobgether")})

    source = LearnedAtsSource(
        store,
        http,
        limit=10,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({"lever:jobgether"}),
    )
    jobs = source.discover()

    assert jobs == []
    assert http.calls == []
    assert source.stats.boards_rejected == 1
    assert source.stats.boards_scanned == 0

    assert store.list_due_ats_boards(now) == []


def test_learned_ats_source_does_not_resurrect_a_previously_rejected_board():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "jobgether")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board("lever", "jobgether", "aggregator: prior run", now)

    # A fresh job pointing at the same board rediscovers it, the way
    # collect_candidates does on every run.
    store.upsert_ats_board(provider="lever", board_identifier="jobgether")

    later = now + timedelta(days=1)
    assert store.list_due_ats_boards(later) == []

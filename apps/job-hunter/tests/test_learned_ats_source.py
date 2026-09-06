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


def test_learned_ats_source_never_rescans_a_board_it_rejected_on_an_earlier_run():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "jobgether")
    base = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(8, "jobgether", _JOBGETHER_PHRASING)
    http = RoutingHttp(responses={"lever.co": postings})

    first = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: base
    )
    first.discover()
    assert first.stats.boards_rejected == 1
    calls_after_first_run = len(http.calls)

    # Rediscovery through an unrelated job re-upserts the board, the way
    # collect_candidates does on every run.
    store.upsert_ats_board(provider="lever", board_identifier="jobgether")

    later = base + timedelta(days=30)
    second = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: later
    )
    jobs = second.discover()

    assert jobs == []
    assert second.stats.boards_scanned == 0
    assert http.calls[calls_after_first_run:] == []


def test_learned_ats_source_survives_a_posting_with_no_description():
    # An ATS returning an explicit null body yields Job.description None;
    # detection must not turn that into a source-wide crash.
    store = JobStore(":memory:")
    _seed_board(store, "greenhouse", "null-body-co")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={
            "greenhouse.io": {
                "jobs": [
                    {
                        "id": i,
                        "title": "Senior Product Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": f"https://boards.greenhouse.io/null-body-co/{i}",
                        "content": None,
                    }
                    for i in range(6)
                ]
            }
        }
    )

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()

    assert len(jobs) == 6
    assert source.stats.boards_rejected == 0
    assert source.stats.boards_successful == 1


def test_learned_ats_source_matches_denylist_entry_case_insensitively():
    store = JobStore(":memory:")
    _seed_board(store, "lever", "JobGether")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={"lever.co": _lever_postings(10, "JobGether")})

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


def test_learned_ats_source_denylisted_board_does_not_consume_a_scan_slot():
    # With limit=1, a denylisted board must not be the one board the run
    # spends its single slot on -- the legitimate board still gets scanned.
    store = JobStore(":memory:")
    _seed_board(store, "lever", "aaa-denylisted")
    _seed_board(store, "lever", "zzz-legit")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={"zzz-legit": _lever_postings(3, "zzz-legit")})

    source = LearnedAtsSource(
        store,
        http,
        limit=1,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({"lever:aaa-denylisted"}),
    )
    jobs = source.discover()

    assert len(jobs) == 3
    assert source.stats.boards_scanned == 1
    assert source.stats.boards_rejected == 1


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
    # An allowlisted board that 404s is paused by health backoff, not
    # rejected as an aggregator: a single permanent failure below the
    # strike threshold pauses the board for 24h rather than deactivating
    # it, and health backoff is not a verdict the allowlist may reverse.
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

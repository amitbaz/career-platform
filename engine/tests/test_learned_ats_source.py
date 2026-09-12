import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests

from engine.sources.learned_ats import LearnedAtsSource, LearnedAtsStats


@pytest.fixture(autouse=True)
def _ats_board_salt(monkeypatch):
    """Give each test its own namespace of board identifiers.

    `job_hunter_ats_boards` is shared and has no delete policy (#203), so a
    board an earlier test seeded is still there -- active, or rejected --
    for every later test in this session, the same problem
    `_postings_unique_to_this_test` (conftest.py:217) solves for postings.
    Salting every board identifier this file writes gives each test rows no
    other test can collide with, including a concurrent run in another
    worktree against the same shared local stack.

    This does not, by itself, stop `LearnedAtsSource.discover()` from also
    *scanning* a stray board left due by another test -- discover() reads
    the whole due list, not just this test's namespace. Each test below
    that is exposed to that (most of them, since they assert on `jobs`,
    `stats` or `http.calls`, not a specific identity) filters its request
    log and its result to URLs containing its own salt, making
    "nothing else got scanned" a claim about this test's namespace rather
    than a claim about the whole shared table -- which is what #203
    actually changed: that completeness guarantee used to be free because
    per-user state was exclusive, and now it is not.
    """
    from engine.postgres_store import PostgresJobStore

    salt = uuid.uuid4().hex[:8]
    real_list_due = PostgresJobStore.list_due_ats_boards

    def list_due_for_this_test(store, *args, **kwargs):
        return [
            entry
            for entry in real_list_due(store, *args, **kwargs)
            if salt in entry.board_identifier
        ]

    monkeypatch.setattr(
        PostgresJobStore,
        "list_due_ats_boards",
        list_due_for_this_test,
    )
    return salt


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
    # select_ats_boards ranks a recently-eligible board ahead of everything
    # else, so marking it eligible right now (issue #203's yield tracking,
    # exercised for its own sake elsewhere) is what actually keeps this
    # test's boards at the front of a due list a whole session's worth of
    # other tests' boards has been accumulating into -- a large `limit`
    # alone only guarantees inclusion, not who gets scanned or in what
    # order, and some of these tests depend on both.
    store.record_ats_eligible_jobs([(provider, board_identifier)], datetime.now(timezone.utc))


def _own_jobs(jobs, salt):
    """This test's own jobs, by the salt embedded in every seeded board's URL."""
    return [job for job in jobs if salt in (job.url or "")]


def _own_calls(http, salt):
    """This test's own HTTP calls, by the same salt."""
    return [url for url in http.calls if salt in url]


def _own_due(store, now, salt):
    return [e for e in store.list_due_ats_boards(now) if salt in e.board_identifier]


def _own_rejected(store, salt):
    return [e for e in store.list_rejected_ats_boards() if salt in e.board_identifier]


def test_learned_ats_source_scans_due_boards_through_native_adapters(store, _ats_board_salt):
    salt = _ats_board_salt
    ashby, lever, greenhouse = f"acme-ashby-{salt}", f"acme-lever-{salt}", f"acme-greenhouse-{salt}"
    _seed_board(store, "ashby", ashby)
    _seed_board(store, "lever", lever)
    _seed_board(store, "greenhouse", greenhouse)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    http = RoutingHttp(
        responses={
            ashby: {
                "jobs": [
                    {
                        "id": 1,
                        "title": "Senior Product Engineer",
                        "location": "Remote",
                        "jobUrl": f"https://jobs.ashbyhq.com/{ashby}/1",
                        "descriptionPlain": "React",
                        "isRemote": True,
                    }
                ]
            },
            lever: [
                {
                    "id": "2",
                    "text": "Senior Product Engineer",
                    "categories": {"location": "Remote"},
                    "hostedUrl": f"https://jobs.lever.co/{lever}/2",
                    "descriptionPlain": "React",
                    "workplaceType": "remote",
                }
            ],
            greenhouse: {
                "jobs": [
                    {
                        "id": 3,
                        "title": "Senior Product Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": f"https://boards.greenhouse.io/{greenhouse}/3",
                        "content": "React",
                    }
                ]
            },
        }
    )

    # limit == this test's own board count: select_ats_boards ranks
    # never-checked boards first, so this test's fresh boards fill the
    # limit before any already-scanned straggler from an earlier test.
    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert sorted(job.source for job in jobs) == ["ashby", "greenhouse", "lever"]
    # boards_scanned/boards_failed are session-wide counters that also see
    # any other due board this run's large `limit` pulls in (see
    # `_ats_board_salt`'s docstring); only what this test's own boards
    # produced is asserted here.
    assert source.stats.boards_successful >= 3
    assert source.stats.jobs_raw >= 3


def test_learned_ats_source_isolates_a_failing_board_from_a_healthy_one(store, _ats_board_salt):
    salt = _ats_board_salt
    broken, healthy = f"broken-ashby-{salt}", f"healthy-lever-{salt}"
    _seed_board(store, "ashby", broken)
    _seed_board(store, "lever", healthy)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    http = RoutingHttp(
        responses={
            healthy: [
                {
                    "id": "2",
                    "text": "Senior Product Engineer",
                    "categories": {"location": "Remote"},
                    "hostedUrl": f"https://jobs.lever.co/{healthy}/2",
                    "descriptionPlain": "React",
                    "workplaceType": "remote",
                }
            ],
        },
        fail_urls={broken},
    )

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert [job.source_job_id for job in jobs] == ["2"]
    assert jobs[0].source == "lever"
    assert source.stats.boards_successful >= 1
    assert source.stats.boards_failed >= 1
    assert source.stats.jobs_raw >= 1

    later = now + timedelta(hours=25)
    entries = {entry.board_identifier: entry for entry in _own_due(store, later, salt)}
    assert entries[broken].consecutive_failures == 1
    assert entries[broken].last_success_at is None
    assert entries[healthy].consecutive_failures == 0
    assert entries[healthy].last_success_at == now.isoformat()
    assert entries[healthy].last_job_count == 1


def test_learned_ats_source_404_logs_compact_and_marks_board_permanent(store, caplog, _ats_board_salt):
    salt = _ats_board_salt
    dead = f"dead-co-{salt}"
    _seed_board(store, "lever", dead)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(not_found_urls={dead})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    with caplog.at_level(logging.INFO):
        jobs = list(source.discover())

    assert _own_jobs(jobs, salt) == []
    assert source.stats.boards_failed >= 1

    # No full traceback anywhere for an expected 404: neither the adapter's
    # nor LearnedAtsSource's own log record carries exc_info.
    relevant = [r for r in caplog.records if dead in r.getMessage()]
    assert relevant
    assert all(r.exc_info is None for r in relevant)

    entries = {e.board_identifier: e for e in _own_due(store, now + timedelta(hours=25), salt)}
    assert entries[dead].consecutive_failures == 1


def test_learned_ats_source_unexpected_error_logs_exactly_one_full_traceback(store, caplog, _ats_board_salt):
    salt = _ats_board_salt
    flaky = f"flaky-co-{salt}"
    _seed_board(store, "greenhouse", flaky)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(fail_urls={"greenhouse.io"})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    with caplog.at_level(logging.WARNING):
        list(source.discover())

    traceback_records = [
        r for r in caplog.records if flaky in r.getMessage() and r.exc_info is not None
    ]
    assert len(traceback_records) == 1


def test_learned_ats_source_deactivates_board_after_repeated_404s(store, _ats_board_salt):
    salt = _ats_board_salt
    dead, healthy = f"dead-co-{salt}", f"healthy-co-{salt}"
    _seed_board(store, "lever", dead)
    _seed_board(store, "lever", healthy)
    base = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={
            healthy: [
                {
                    "id": "1",
                    "text": "Engineer",
                    "categories": {"location": "Remote"},
                    "hostedUrl": f"https://jobs.lever.co/{healthy}/1",
                    "descriptionPlain": "x",
                    "workplaceType": "remote",
                }
            ],
        },
        not_found_urls={dead},
    )

    for i in range(3):
        checked_at = base + timedelta(hours=25 * i)
        source = LearnedAtsSource(
            store, http, limit=500, market_order=["berlin"], now=lambda t=checked_at: t
        )
        list(source.discover())

    final_check = base + timedelta(hours=25 * 3)
    due_identifiers = {e.board_identifier for e in _own_due(store, final_check, salt)}
    assert due_identifiers == {healthy}


def test_learned_ats_source_never_deactivates_board_after_repeated_transient_errors(store, _ats_board_salt):
    # Mirror image of test_learned_ats_source_deactivates_board_after_repeated_404s:
    # repeated *transient* (non-404) errors must never deactivate a board.
    # This is the test that would fail if `permanent` were ever hardcoded or
    # inverted for the unexpected-error path instead of being threaded
    # through as `is_stale_board_error(exc)`.
    salt = _ats_board_salt
    flaky = f"flaky-co-{salt}"
    _seed_board(store, "greenhouse", flaky)
    base = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(fail_urls={flaky})

    for i in range(3):
        checked_at = base + timedelta(hours=25 * i)
        source = LearnedAtsSource(
            store, http, limit=500, market_order=["berlin"], now=lambda t=checked_at: t
        )
        list(source.discover())

    final_check = base + timedelta(hours=25 * 3)
    due_identifiers = {e.board_identifier for e in _own_due(store, final_check, salt)}
    assert due_identifiers == {flaky}


def test_learned_ats_source_rejects_jobgether_shaped_board_and_drops_its_jobs(store, _ats_board_salt):
    salt = _ats_board_salt
    jobgether = f"jobgether-{salt}"
    _seed_board(store, "lever", jobgether)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(2, jobgether) + _lever_postings(
        6, jobgether, _JOBGETHER_PHRASING, start_id=3
    )
    http = RoutingHttp(responses={jobgether: postings})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = list(source.discover())

    assert jobs == []
    assert source.stats.boards_rejected == 1
    assert source.stats.boards_successful == 0
    assert source.stats.jobs_raw == 0

    later = now + timedelta(days=30)
    assert _own_due(store, later, salt) == []


def test_learned_ats_source_keeps_scanning_veeva_shaped_board_with_no_markers(store, _ats_board_salt):
    salt = _ats_board_salt
    veeva = f"veeva-{salt}"
    _seed_board(store, "lever", veeva)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(21, veeva)
    http = RoutingHttp(responses={veeva: postings})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = list(source.discover())

    assert len(jobs) == 21
    assert source.stats.boards_successful == 1
    assert source.stats.boards_rejected == 0

    due = {e.board_identifier for e in _own_due(store, now, salt)}
    assert due == {veeva}


def test_learned_ats_source_survives_one_stray_third_party_posting(store, _ats_board_salt):
    salt = _ats_board_salt
    mostly_clean = f"mostly-clean-{salt}"
    _seed_board(store, "lever", mostly_clean)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(19, mostly_clean) + _lever_postings(
        1, mostly_clean, _JOBGETHER_PHRASING, start_id=20
    )
    http = RoutingHttp(responses={mostly_clean: postings})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = list(source.discover())

    assert len(jobs) == 20
    assert source.stats.boards_rejected == 0
    due = {e.board_identifier for e in _own_due(store, now, salt)}
    assert due == {mostly_clean}


def test_learned_ats_source_refuses_denylisted_board_without_scanning(store, _ats_board_salt):
    salt = _ats_board_salt
    jobgether = f"jobgether-{salt}"
    _seed_board(store, "lever", jobgether)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={jobgether: _lever_postings(10, jobgether)})

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({f"lever:{jobgether}"}),
    )
    jobs = list(source.discover())

    assert _own_jobs(jobs, salt) == []
    assert _own_calls(http, salt) == []
    assert source.stats.boards_rejected == 1

    # A denylist rejection is this operator's policy, not evidence about the
    # board, so (since #203) it is never persisted to the shared registry --
    # the board stays "due" there and is re-excluded from config on every
    # run instead.
    assert [e.board_identifier for e in _own_due(store, now, salt)] == [jobgether]
    assert _own_rejected(store, salt) == []


def test_learned_ats_source_never_rescans_a_board_it_rejected_on_an_earlier_run(store, _ats_board_salt):
    salt = _ats_board_salt
    jobgether = f"jobgether-{salt}"
    _seed_board(store, "lever", jobgether)
    base = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    postings = _lever_postings(8, jobgether, _JOBGETHER_PHRASING)
    http = RoutingHttp(responses={jobgether: postings})

    first = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: base
    )
    list(first.discover())
    assert first.stats.boards_rejected == 1
    own_calls_after_first_run = len(_own_calls(http, salt))

    # Rediscovery through an unrelated job re-upserts the board, the way
    # collect_candidates does on every run.
    store.upsert_ats_board(provider="lever", board_identifier=jobgether)

    later = base + timedelta(days=30)
    second = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: later
    )
    jobs = list(second.discover())

    assert _own_jobs(jobs, salt) == []
    assert _own_calls(http, salt)[own_calls_after_first_run:] == []


def test_learned_ats_source_survives_a_posting_with_no_description(store, _ats_board_salt):
    # An ATS returning an explicit null body yields Job.description None;
    # detection must not turn that into a source-wide crash.
    salt = _ats_board_salt
    null_body = f"null-body-co-{salt}"
    _seed_board(store, "greenhouse", null_body)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={
            null_body: {
                "jobs": [
                    {
                        "id": i,
                        "title": "Senior Product Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": f"https://boards.greenhouse.io/{null_body}/{i}",
                        "content": None,
                    }
                    for i in range(6)
                ]
            }
        }
    )

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert len(jobs) == 6
    assert source.stats.boards_rejected == 0
    assert source.stats.boards_successful == 1


def test_learned_ats_source_matches_denylist_entry_case_insensitively(store, _ats_board_salt):
    salt = _ats_board_salt
    jobgether = f"JobGether-{salt}"
    _seed_board(store, "lever", jobgether)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={jobgether: _lever_postings(10, jobgether)})

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({f"lever:{jobgether.lower()}"}),
    )
    jobs = list(source.discover())

    assert _own_jobs(jobs, salt) == []
    assert _own_calls(http, salt) == []
    assert source.stats.boards_rejected == 1


def test_learned_ats_source_denylisted_board_does_not_consume_a_scan_slot(store, _ats_board_salt):
    # With limit=500, a denylisted board must not be the one board the run
    # spends its single slot on -- the legitimate board still gets scanned.
    salt = _ats_board_salt
    denylisted, legit = f"aaa-denylisted-{salt}", f"zzz-legit-{salt}"
    _seed_board(store, "lever", denylisted)
    _seed_board(store, "lever", legit)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={legit: _lever_postings(3, legit)})

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({f"lever:{denylisted}"}),
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert len(jobs) == 3
    assert source.stats.boards_rejected == 1


def test_learned_ats_source_keeps_an_allowlisted_board_detection_would_reject(store, _ats_board_salt):
    salt = _ats_board_salt
    clientco = f"clientco-{salt}"
    _seed_board(store, "lever", clientco)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={clientco: _lever_postings(10, clientco, _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({f"lever:{clientco}"}),
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert len(jobs) == 10
    assert source.stats.boards_rejected == 0
    assert source.stats.boards_successful == 1
    assert _own_rejected(store, salt) == []


def test_learned_ats_source_logs_the_verdict_it_overrode(store, caplog, _ats_board_salt):
    # The operator overrode a verdict, so the verdict must stay visible --
    # otherwise the allowlist entry can never be shown to be unnecessary.
    salt = _ats_board_salt
    clientco = f"clientco-{salt}"
    _seed_board(store, "lever", clientco)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={clientco: _lever_postings(10, clientco, _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({f"lever:{clientco}"}),
    )
    with caplog.at_level(logging.INFO):
        list(source.discover())

    kept = [r.getMessage() for r in caplog.records if clientco in r.getMessage()]
    assert len(kept) == 1
    assert f"lever:{clientco}" in kept[0]
    assert "third_party_listing" in kept[0]


def test_learned_ats_source_heals_an_already_rejected_allowlisted_board(store, _ats_board_salt):
    salt = _ats_board_salt
    clientco = f"clientco-{salt}"
    _seed_board(store, "lever", clientco)
    rejected_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board(
        "lever", clientco, "third_party_listing: 9/10 postings (90%)", rejected_at
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={clientco: _lever_postings(10, clientco, _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({f"lever:{clientco}"}),
    )
    jobs = _own_jobs(list(source.discover()), salt)

    # Recovered and rescanned within the same run -- editing the config is
    # the whole recovery procedure.
    assert len(jobs) == 10
    assert source.stats.boards_recovered == 1
    assert source.stats.boards_successful == 1
    assert _own_rejected(store, salt) == []
    assert [e.board_identifier for e in _own_due(store, now, salt)] == [clientco]


def test_learned_ats_source_logs_the_reason_it_cleared_when_healing(store, caplog, _ats_board_salt):
    salt = _ats_board_salt
    clientco = f"clientco-{salt}"
    _seed_board(store, "lever", clientco)
    rejected_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board(
        "lever", clientco, "third_party_listing: 9/10 postings (90%)", rejected_at
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={clientco: _lever_postings(10, clientco, _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({f"lever:{clientco}"}),
    )
    with caplog.at_level(logging.INFO):
        list(source.discover())

    recovered = [r.getMessage() for r in caplog.records if "recovered" in r.getMessage() and clientco in r.getMessage()]
    assert len(recovered) == 1
    assert f"lever:{clientco}" in recovered[0]
    assert "9/10 postings (90%)" in recovered[0]


def test_learned_ats_source_healing_ignores_a_board_that_is_not_allowlisted(store, _ats_board_salt):
    salt = _ats_board_salt
    jobgether = f"jobgether-{salt}"
    _seed_board(store, "lever", jobgether)
    rejected_at = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
    store.reject_ats_board(
        "lever", jobgether, "third_party_listing: 9/10 postings (90%)", rejected_at
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={jobgether: _lever_postings(10, jobgether)})

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:someone-else"}),
    )
    jobs = list(source.discover())

    assert _own_jobs(jobs, salt) == []
    assert _own_calls(http, salt) == []
    assert source.stats.boards_recovered == 0
    assert [e.board_identifier for e in _own_rejected(store, salt)] == [jobgether]


def test_learned_ats_source_allowlist_matches_the_board_key_case_insensitively(store, _ats_board_salt):
    salt = _ats_board_salt
    clientco = f"ClientCo-{salt}"
    _seed_board(store, "lever", clientco)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={clientco: _lever_postings(10, clientco, _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({f"lever:{clientco.lower()}"}),
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert len(jobs) == 10
    assert source.stats.boards_rejected == 0


def test_learned_ats_source_allowlist_wins_over_the_denylist_branch(store, _ats_board_salt):
    # The config load refuses a board named by both lists, so this can only
    # be reached by constructing the source directly -- the guard keeps the
    # invariant local to the code that depends on it.
    salt = _ats_board_salt
    clientco = f"clientco-{salt}"
    _seed_board(store, "lever", clientco)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={clientco: _lever_postings(10, clientco)})

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        denylist=frozenset({f"lever:{clientco}"}),
        allowlist=frozenset({f"lever:{clientco}"}),
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert len(jobs) == 10
    assert source.stats.boards_rejected == 0


def test_learned_ats_source_still_rejects_an_aggregator_that_is_not_allowlisted(store, _ats_board_salt):
    # Regression on #17: an empty or unrelated allowlist changes nothing.
    salt = _ats_board_salt
    jobgether = f"jobgether-{salt}"
    _seed_board(store, "lever", jobgether)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(
        responses={jobgether: _lever_postings(10, jobgether, _JOBGETHER_PHRASING)}
    )

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({"lever:someone-else"}),
    )
    jobs = list(source.discover())

    assert _own_jobs(jobs, salt) == []
    assert source.stats.boards_rejected == 1
    assert [e.board_identifier for e in _own_rejected(store, salt)] == [jobgether]


def test_learned_ats_source_allowlist_does_not_override_health_backoff(store, _ats_board_salt):
    # An allowlisted board that 404s is paused by health backoff, not
    # rejected as an aggregator: a single permanent failure below the
    # strike threshold pauses the board for 24h rather than deactivating
    # it, and health backoff is not a verdict the allowlist may reverse.
    salt = _ats_board_salt
    clientco = f"clientco-{salt}"
    _seed_board(store, "lever", clientco)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(not_found_urls={clientco})

    source = LearnedAtsSource(
        store,
        http,
        limit=500,
        market_order=["berlin"],
        now=lambda: now,
        allowlist=frozenset({f"lever:{clientco}"}),
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert jobs == []
    assert source.stats.boards_failed >= 1
    assert _own_due(store, now, salt) == []


def test_a_board_abandoned_part_way_is_not_recorded_as_a_successful_scan(store, _ats_board_salt):
    """Stopping mid-board must not claim a harvest that was never delivered.

    The per-source time budget cuts between the postings a board yields, so a
    board's bookkeeping can no longer be written before its postings are
    handed over: doing that stamps `last_checked_at`, demoting the board in
    the oldest-first ranking, and records a job count nothing received.
    """
    salt = _ats_board_salt
    big_lever = f"big-lever-{salt}"
    _seed_board(store, "lever", big_lever)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={big_lever: _lever_postings(5, big_lever)})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = source.discover()
    taken = [next(jobs), next(jobs)]
    jobs.close()

    assert len(taken) == 2
    # Only what was actually handed over is counted, and the board is not
    # claimed as a completed scan.
    assert source.stats.jobs_raw == 2
    assert source.stats.boards_successful == 0

    later = now + timedelta(hours=25)
    entries = {
        entry.board_identifier: entry for entry in _own_due(store, later, salt)
    }
    # Still due: an unfinished scan must not look like a fresh one.
    assert entries[big_lever].last_success_at is None


def test_a_fully_drained_board_is_still_recorded_as_a_successful_scan(store, _ats_board_salt):
    salt = _ats_board_salt
    big_lever = f"big-lever-{salt}"
    _seed_board(store, "lever", big_lever)
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = RoutingHttp(responses={big_lever: _lever_postings(5, big_lever)})

    source = LearnedAtsSource(
        store, http, limit=500, market_order=["berlin"], now=lambda: now
    )
    jobs = _own_jobs(list(source.discover()), salt)

    assert len(jobs) == 5
    assert source.stats.boards_successful == 1

    later = now + timedelta(hours=25)
    entries = {
        entry.board_identifier: entry for entry in _own_due(store, later, salt)
    }
    assert entries[big_lever].last_success_at is not None

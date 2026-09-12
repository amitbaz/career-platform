"""Every job source hands jobs back incrementally rather than in one list.

These tests pin the property a per-source time budget depends on: a source
must be stoppable between the units of work it already iterates over (a feed
page, an ATS board, a watched company, a search query, a listing's detail
fetch), and the jobs it produced before that point must already be in the
caller's hands.

They deliberately assert on *when* network and store work happens, not only
on what a source returns, because a source that builds the whole list eagerly
and then returns an iterator over it would pass every content assertion while
being exactly as unbounded as before.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from datetime import datetime, timezone

import pytest

import job_hunter.sources as sources_package
from job_hunter.discovery import DiscoveryStats, _iter_source_jobs
from job_hunter.models import AtsRegistryEntry, Job
from job_hunter.search_backend import SearchHit, SearchResponse
from job_hunter.sources.arbeitnow import ArbeitnowSource
from job_hunter.sources.base import JobSource
from job_hunter.sources.company_watch import CompanyWatchSource
from job_hunter.sources.learned_ats import LearnedAtsSource
from job_hunter.sources.targeted_search import TargetedSearchSource
from job_hunter.sources.wellfound import WellfoundListing, WellfoundSource
from job_hunter.sources.yc import YCSource


def _discovering_classes() -> list[type]:
    """Return every class in the sources package that implements `discover`."""
    found: list[type] = []
    for module_info in pkgutil.iter_modules(sources_package.__path__):
        module = importlib.import_module(
            f"{sources_package.__name__}.{module_info.name}"
        )
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__ or obj is JobSource:
                continue
            if callable(getattr(obj, "discover", None)):
                found.append(obj)
    return sorted(found, key=lambda cls: (cls.__module__, cls.__name__))


@pytest.mark.parametrize(
    "source_cls", _discovering_classes(), ids=lambda cls: cls.__name__
)
def test_every_source_produces_jobs_incrementally(source_cls):
    assert inspect.isgeneratorfunction(source_cls.discover), (
        f"{source_cls.__name__}.discover still returns a fully built list; "
        "a per-source budget cannot cut it off partway"
    )


def test_the_source_protocol_is_satisfied_by_a_generator_source():
    """The protocol must accept an incremental source, not just a list one."""

    class Incremental:
        def discover(self):
            yield Job(source="x", title="Senior Product Engineer")

    source: JobSource = Incremental()
    assert [job.title for job in source.discover()] == ["Senior Product Engineer"]


# --- feed sources ------------------------------------------------------------


class _RecordingJsonHttp:
    def __init__(self, pages: dict[str, object]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def get_json(self, url, params=None, **kwargs):
        self.calls.append(url)
        return self._pages[url]


def _arbeitnow_page(slugs: list[str], next_url: str | None) -> dict:
    return {
        "data": [
            {
                "slug": slug,
                "title": "Senior Product Engineer",
                "company_name": "Acme",
                "location": "Remote",
                "url": f"https://www.arbeitnow.com/jobs/{slug}",
                "description": "React",
                "remote": True,
            }
            for slug in slugs
        ],
        "links": {"next": next_url},
    }


def test_arbeitnow_does_not_fetch_the_next_page_until_the_first_is_consumed():
    first = "https://www.arbeitnow.com/api/job-board-api"
    second = f"{first}?page=2"
    http = _RecordingJsonHttp(
        {
            first: _arbeitnow_page(["a", "b"], second),
            second: _arbeitnow_page(["c"], None),
        }
    )

    jobs = ArbeitnowSource(http).discover()
    assert http.calls == [], "no request may be made before the first job is pulled"

    assert next(jobs).source_job_id == "a"
    assert http.calls == [first]
    assert next(jobs).source_job_id == "b"
    assert http.calls == [first], "page two fetched before page one was drained"

    assert next(jobs).source_job_id == "c"
    assert http.calls == [first, second]
    with pytest.raises(StopIteration):
        next(jobs)


def test_abandoning_arbeitnow_partway_leaves_the_remaining_pages_unfetched():
    """The whole point of the prefactor: stopping a source keeps what it gave."""
    first = "https://www.arbeitnow.com/api/job-board-api"
    second = f"{first}?page=2"
    http = _RecordingJsonHttp(
        {
            first: _arbeitnow_page(["a", "b"], second),
            second: _arbeitnow_page(["c"], None),
        }
    )

    jobs = ArbeitnowSource(http).discover()
    kept = [next(jobs)]
    jobs.close()

    assert [job.source_job_id for job in kept] == ["a"]
    assert http.calls == [first]


# --- page-walking sources ----------------------------------------------------


class _RecordingPageHttp:
    def __init__(self, pages: dict[str, str]) -> None:
        self._pages = pages
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResponse(self._pages[url])


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


_YC_PAGE_ONE = (
    '<a class="ycdc-card" href="/companies/acme/jobs/1-senior-product-engineer"'
    ' data-title="Senior Product Engineer" data-company="Acme"'
    ' data-location="Remote"></a>'
)
_YC_PAGE_TWO = (
    '<a class="ycdc-card" href="/companies/globex/jobs/2-staff-engineer"'
    ' data-title="Staff Engineer" data-company="Globex"'
    ' data-location="Remote"></a>'
)


def test_yc_does_not_fetch_the_second_page_until_the_first_is_consumed():
    pages = {
        "https://www.ycombinator.com/jobs?page=1": _YC_PAGE_ONE,
        "https://www.ycombinator.com/jobs?page=2": _YC_PAGE_TWO,
    }
    http = _RecordingPageHttp(pages)

    jobs = YCSource(http, list(pages)).discover()
    assert http.calls == []

    assert next(jobs).company == "Acme"
    assert http.calls == ["https://www.ycombinator.com/jobs?page=1"]

    assert next(jobs).company == "Globex"
    assert http.calls == list(pages)


_WELLFOUND_LISTING_HTML = """
<a href="/jobs/1001-frontend-engineer">Frontend Engineer</a>
<a href="/jobs/1002-full-stack-engineer">Full Stack Engineer</a>
"""

_WELLFOUND_DETAIL_HTML = """
<html><head><title>Frontend Engineer at Omnea • London | Wellfound</title></head>
<body><h1>Frontend Engineer</h1><p>Remote Work Policy Remote only</p></body></html>
"""


def test_wellfound_does_not_fetch_the_second_detail_before_the_first_job_is_used():
    listing_url = "https://wellfound.com/role/l/frontend-engineer/london"
    http = _RecordingPageHttp(
        {
            listing_url: _WELLFOUND_LISTING_HTML,
            "https://wellfound.com/jobs/1001-frontend-engineer": _WELLFOUND_DETAIL_HTML,
            "https://wellfound.com/jobs/1002-full-stack-engineer": _WELLFOUND_DETAIL_HTML,
        }
    )
    listings = [WellfoundListing(url=listing_url, market_id="london")]

    jobs = WellfoundSource(http, listings).discover()
    assert http.calls == []

    assert next(jobs).source_job_id == "1001"
    assert http.calls == [
        listing_url,
        "https://wellfound.com/jobs/1001-frontend-engineer",
    ]

    assert next(jobs).source_job_id == "1002"
    assert http.calls[-1] == "https://wellfound.com/jobs/1002-full-stack-engineer"


# --- targeted search ---------------------------------------------------------


class _RecordingBackend:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, text):
        self.queries.append(text)
        return SearchResponse(
            backend="fake",
            hits=[SearchHit(title=f"{text} hit", url=f"https://example.test/{text}")],
        )


def test_targeted_search_does_not_run_the_next_query_until_the_first_is_consumed():
    backend = _RecordingBackend()
    source = TargetedSearchSource(backend, ["one", "two"])

    jobs = source.discover()
    assert backend.queries == []

    assert next(jobs).title == "one hit"
    assert backend.queries == ["one"]

    assert next(jobs).title == "two hit"
    assert backend.queries == ["one", "two"]


# --- store-backed aggregating sources ----------------------------------------


def _registry_entry(provider: str, board: str) -> AtsRegistryEntry:
    return AtsRegistryEntry(
        provider=provider,
        board_identifier=board,
        company_name=board,
        market_hint="berlin",
        first_seen_at="2026-08-01T00:00:00+00:00",
        last_seen_at="2026-08-01T00:00:00+00:00",
        last_checked_at=None,
        last_success_at=None,
        last_eligible_at=None,
        last_job_count=0,
        eligible_jobs_seen=0,
        consecutive_failures=0,
        active=True,
        paused_until=None,
    )


class _FakeAtsStore:
    def __init__(self, entries: list[AtsRegistryEntry]) -> None:
        self._entries = entries
        self.successes: list[tuple[str, str, int]] = []

    def list_due_ats_boards(self, checked_at):
        return list(self._entries)

    def list_rejected_ats_boards(self):
        return []

    def record_ats_scan_success(self, provider, board, checked_at, job_count):
        self.successes.append((provider, board, job_count))


def _lever_payload(board: str, job_id: str) -> list[dict]:
    return [
        {
            "id": job_id,
            "text": "Senior Product Engineer",
            "categories": {"location": "Remote"},
            "hostedUrl": f"https://jobs.lever.co/{board}/{job_id}",
            "descriptionPlain": "React and TypeScript, hired directly by us.",
            "workplaceType": "remote",
        }
    ]


class _BoardRoutingHttp:
    def __init__(self, payloads: dict[str, list[dict]]) -> None:
        self._payloads = payloads
        self.boards: list[str] = []

    def get_json(self, url, **kwargs):
        for board, payload in self._payloads.items():
            if f"/{board}?" in url:
                self.boards.append(board)
                return payload
        raise AssertionError(f"no fake payload configured for {url}")


def test_learned_ats_does_not_scan_the_second_board_until_the_first_is_consumed():
    store = _FakeAtsStore(
        [_registry_entry("lever", "acme"), _registry_entry("lever", "globex")]
    )
    http = _BoardRoutingHttp(
        {"acme": _lever_payload("acme", "1"), "globex": _lever_payload("globex", "2")}
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    jobs = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    ).discover()
    assert http.boards == []

    assert next(jobs).source_job_id == "1"
    assert http.boards == ["acme"], "the second board was scanned too early"

    assert next(jobs).source_job_id == "2"
    assert http.boards == ["acme", "globex"]


def test_learned_ats_still_records_a_board_as_scanned_before_moving_on():
    """A board stays the unit of work: its health write happens per board."""
    store = _FakeAtsStore(
        [_registry_entry("lever", "acme"), _registry_entry("lever", "globex")]
    )
    http = _BoardRoutingHttp(
        {"acme": _lever_payload("acme", "1"), "globex": _lever_payload("globex", "2")}
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    source = LearnedAtsSource(
        store, http, limit=10, market_order=["berlin"], now=lambda: now
    )
    assert [job.source_job_id for job in source.discover()] == ["1", "2"]
    assert store.successes == [("lever", "acme", 1), ("lever", "globex", 1)]
    assert source.stats.boards_successful == 2
    assert source.stats.jobs_raw == 2


class _FakeWatchStore:
    def __init__(self, watches: list[dict]) -> None:
        self._watches = watches
        self.successes: list[int] = []

    def list_due_company_watches(self, checked_at):
        return list(self._watches)

    def record_watch_success(self, watch_id, checked_at):
        self.successes.append(watch_id)


def _watch_row(watch_id: int, company: str, board: str) -> dict:
    return {
        "id": watch_id,
        "company_name": company,
        "careers_url": "",
        "ats_provider": "lever",
        "ats_identifier": board,
    }


def test_company_watch_does_not_check_the_second_watch_until_the_first_is_consumed():
    store = _FakeWatchStore(
        [_watch_row(1, "Acme", "acme"), _watch_row(2, "Globex", "globex")]
    )
    http = _BoardRoutingHttp(
        {"acme": _lever_payload("acme", "1"), "globex": _lever_payload("globex", "2")}
    )
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    jobs = CompanyWatchSource(store, http, now=lambda: now).discover()
    assert http.boards == []

    first = next(jobs)
    assert first.source == "watch:lever"
    assert http.boards == ["acme"], "the second watch was checked too early"

    assert next(jobs).source_job_id == "2"
    assert http.boards == ["acme", "globex"]
    # Watch 1's success is recorded only once its postings have been handed
    # over -- which is what advancing into watch 2 proves. Watch 2's is not
    # recorded yet: its own posting is still parked at the `yield`, and a
    # caller that stops here (the per-source time budget does) must leave that
    # watch looking unchecked rather than freshly checked.
    assert store.successes == [1]

    assert list(jobs) == []
    assert store.successes == [1, 2]


# --- discovery's per-source failure isolation --------------------------------


class _RaisesAfterOneJob:
    def discover(self):
        yield Job(source="flaky", source_job_id="1", title="Senior Product Engineer")
        raise RuntimeError("source died partway")


class _RaisesImmediately:
    def discover(self):
        raise RuntimeError("source is down")
        yield  # pragma: no cover - makes discover a generator function


class _CountingHttp:
    """Stands in for the shared client's request counter."""

    def __init__(self) -> None:
        self.request_count = 0


def _drain(source, http=None, stats=None, clock=None):
    """Call `_iter_source_jobs` with the cost plumbing tests rarely care about."""
    return _iter_source_jobs(
        source,
        http if http is not None else _CountingHttp(),
        stats if stats is not None else DiscoveryStats(),
        "test-source",
        clock if clock is not None else (lambda: 0.0),
    )


def test_iter_source_jobs_logs_and_stops_a_source_that_raises_partway(caplog):
    with caplog.at_level("ERROR"):
        jobs = list(_drain(_RaisesAfterOneJob()))

    assert [job.source_job_id for job in jobs] == ["1"]
    assert "source discovery failed" in caplog.text


def test_iter_source_jobs_logs_and_stops_a_source_that_raises_before_yielding(caplog):
    with caplog.at_level("ERROR"):
        assert list(_drain(_RaisesImmediately())) == []

    assert "source discovery failed" in caplog.text


def test_iter_source_jobs_does_not_swallow_the_callers_own_failure():
    """Isolation covers the source's work, never the caller's handling of a job."""
    with pytest.raises(ValueError, match="handling blew up"):
        for _ in _drain(_RaisesAfterOneJob()):
            raise ValueError("handling blew up")


# --- cost accounting survives the move into the iteration --------------------


class _SlowSource:
    """Spends a tick and a request per job, plus one to start up."""

    source_label = "slow"

    def __init__(self, http, ticks) -> None:
        self._http = http
        self._ticks = ticks

    def discover(self):
        for index in range(2):
            self._http.request_count += 1
            self._ticks.advance(1.0)
            yield Job(source="slow", source_job_id=str(index), title="Engineer")


class _Ticks:
    def __init__(self) -> None:
        self.now = 0.0

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def test_cost_is_charged_per_step_not_to_the_discover_call():
    """The work moved into the iteration, so the measurement had to move too."""
    ticks = _Ticks()
    http = _CountingHttp()
    stats = DiscoveryStats()

    jobs = list(_iter_source_jobs(_SlowSource(http, ticks), http, stats, "slow", ticks))

    assert [job.source_job_id for job in jobs] == ["0", "1"]
    assert stats.elapsed_by_source["slow"] == 2.0
    assert stats.requests_by_source["slow"] == 2


def test_cost_excludes_the_callers_own_per_job_handling():
    ticks = _Ticks()
    http = _CountingHttp()
    stats = DiscoveryStats()

    for _ in _iter_source_jobs(_SlowSource(http, ticks), http, stats, "slow", ticks):
        # The caller's handling is not the source's cost.
        ticks.advance(10.0)

    assert stats.elapsed_by_source["slow"] == 2.0


def test_cost_is_reported_for_a_source_that_yields_nothing():
    ticks = _Ticks()
    http = _CountingHttp()
    stats = DiscoveryStats()

    class _Empty:
        def discover(self):
            return iter(())

    assert list(_iter_source_jobs(_Empty(), http, stats, "empty", ticks)) == []
    assert stats.elapsed_by_source == {"empty": 0.0}
    assert stats.requests_by_source == {"empty": 0}


def test_cost_is_recorded_when_the_caller_abandons_the_source_partway():
    """A budget stopping a source must still see what that source spent."""
    ticks = _Ticks()
    http = _CountingHttp()
    stats = DiscoveryStats()

    jobs = _iter_source_jobs(_SlowSource(http, ticks), http, stats, "slow", ticks)
    next(jobs)
    jobs.close()

    assert stats.elapsed_by_source["slow"] == 1.0
    assert stats.requests_by_source["slow"] == 1


def test_cost_is_recorded_for_a_source_that_raises_partway():
    ticks = _Ticks()
    http = _CountingHttp()
    stats = DiscoveryStats()

    class _SpendsThenRaises:
        def discover(self):
            http.request_count += 1
            ticks.advance(3.0)
            yield Job(source="flaky", source_job_id="1", title="Engineer")
            http.request_count += 1
            ticks.advance(4.0)
            raise RuntimeError("died partway")

    jobs = list(_iter_source_jobs(_SpendsThenRaises(), http, stats, "flaky", ticks))

    assert [job.source_job_id for job in jobs] == ["1"]
    assert stats.elapsed_by_source["flaky"] == 7.0
    assert stats.requests_by_source["flaky"] == 2


from job_hunter.sources.base import source_key_for


class _LabelOnly:
    source_label = "remotive"

    def discover(self):
        yield from ()


class _KeyedBoard:
    source_label = "lever:acme"
    source_key = "lever:acme"

    def discover(self):
        yield from ()


def test_source_key_falls_back_to_the_metrics_label():
    """An adapter that never heard of source_key still has one."""
    assert source_key_for(_LabelOnly()) == "remotive"


def test_source_key_is_used_when_the_adapter_declares_one():
    assert source_key_for(_KeyedBoard()) == "lever:acme"

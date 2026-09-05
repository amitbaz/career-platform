"""Tests for DuckDuckGoSource market hint propagation and stats tracking."""

from dataclasses import dataclass

from job_hunter.models import SearchQuery
from job_hunter.sources.duckduckgo import DuckDuckGoSource


@dataclass
class FakeResponse:
    text: str
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, html: str) -> None:
        self._html = html
        self.requests: list[dict] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requests.append({"url": url, "kwargs": kwargs})
        return FakeResponse(text=self._html)


_RESULT_HTML = '<a class="result__a" href="https://example.test/job">Senior FE</a>'


def test_duckduckgo_preserves_query_market_hint():
    source = DuckDuckGoSource(
        FakeHttp(_RESULT_HTML),
        [SearchQuery('"senior frontend engineer" London', "london")],
    )
    jobs = source.discover()
    assert len(jobs) == 1
    assert jobs[0].market_hint == "london"
    assert source.stats.attempted_by_market == {"london": 1}
    assert source.stats.succeeded_by_market == {"london": 1}


def test_duckduckgo_legacy_string_queries_have_no_market_hint():
    source = DuckDuckGoSource(
        FakeHttp(_RESULT_HTML),
        ['"senior frontend engineer" remote'],
    )
    jobs = source.discover()
    assert len(jobs) == 1
    assert jobs[0].market_hint is None
    assert source.stats.planned_by_market == {"legacy": 1}
    assert source.stats.attempted_by_market == {"legacy": 1}
    assert source.stats.succeeded_by_market == {"legacy": 1}


def test_duckduckgo_stats_track_multiple_markets():
    source = DuckDuckGoSource(
        FakeHttp(_RESULT_HTML),
        [
            SearchQuery("query1", "germany_eu"),
            SearchQuery("query2", "germany_eu"),
            SearchQuery("query3", "london"),
        ],
    )
    source.discover()
    assert source.stats.planned_by_market == {"germany_eu": 2, "london": 1}
    assert source.stats.attempted_by_market == {"germany_eu": 2, "london": 1}
    assert source.stats.succeeded_by_market == {"germany_eu": 2, "london": 1}

from datetime import datetime, timezone

import job_hunter.search_budget as search_budget
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.models import Job, SearchQuery
from job_hunter.pipeline import _targeted_canonical_candidates
from job_hunter.search_budget import (
    SearchUsageLedger,
    brave_queries_available_today,
    split_queries_for_brave,
)


UTC = timezone.utc


def test_brave_budget_spreads_250_monthly_queries_and_blocks_same_day_reruns(tmp_path):
    ledger = SearchUsageLedger(tmp_path / "state.sqlite3")
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    # 250 remaining across Sep 2-30 => ceil(250 / 29) = 9 for today.
    assert brave_queries_available_today(ledger, monthly_limit=250, now=now) == 9

    for minute in range(9):
        ledger.record(
            provider="brave",
            occurred_at=now.replace(minute=minute),
        )

    # A manual rerun on the same day must not spend another 9 calls.
    assert brave_queries_available_today(ledger, monthly_limit=250, now=now) == 0

    # The next day gets a fresh share of the remaining monthly allowance.
    tomorrow = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    assert brave_queries_available_today(ledger, monthly_limit=250, now=tomorrow) == 9


def test_brave_budget_daily_target_does_not_shrink_as_today_is_consumed(tmp_path):
    ledger = SearchUsageLedger(tmp_path / "state.sqlite3")
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)

    assert brave_queries_available_today(ledger, monthly_limit=1000, now=now) == 34

    for minute in range(10):
        ledger.record(provider="brave", occurred_at=now.replace(minute=minute))

    # The day started with a 34-query share. Using 10 should leave 24, rather
    # than recalculating the daily target downward after each request.
    assert brave_queries_available_today(ledger, monthly_limit=1000, now=now) == 24


def test_brave_budget_never_exceeds_monthly_limit(tmp_path):
    ledger = SearchUsageLedger(tmp_path / "state.sqlite3")
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

    for index in range(250):
        ledger.record(
            provider="brave",
            occurred_at=datetime(2026, 9, 1, index % 24, index % 60, tzinfo=UTC),
        )

    assert brave_queries_available_today(ledger, monthly_limit=250, now=now) == 0


def test_brave_request_budget_hard_cap_is_shared_across_consumers(tmp_path):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    db_path = tmp_path / "state.sqlite3"
    discovery_budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(db_path), monthly_limit=3, now=lambda: now
    )
    canonical_budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(db_path), monthly_limit=3, now=lambda: now
    )

    assert discovery_budget.reserve() is True
    assert discovery_budget.reserve() is True
    assert canonical_budget.reserve() is True
    assert canonical_budget.reserve() is False

    ledger = SearchUsageLedger(db_path)
    month_start = datetime(2026, 9, 1, tzinfo=UTC)
    next_month = datetime(2026, 10, 1, tzinfo=UTC)
    assert ledger.count(provider="brave", start_at=month_start, end_at=next_month) == 3


def test_brave_discovery_priority_is_soft_within_shared_daily_allowance(tmp_path):
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(tmp_path / "state.sqlite3"),
        monthly_limit=10,
        discovery_share=0.8,
        now=lambda: now,
    )

    assert budget.discovery_allowance() == 8

    for _ in range(8):
        assert budget.reserve() is True

    # The discovery split is not a second hard budget: canonical work can use
    # the rest of the same persisted daily/monthly allowance.
    assert budget.reserve() is True
    assert budget.reserve() is True
    assert budget.reserve() is False


def test_brave_query_selection_round_robins_across_markets():
    queries = [
        SearchQuery("germany-1", "germany_eu"),
        SearchQuery("germany-2", "germany_eu"),
        SearchQuery("germany-3", "germany_eu"),
        SearchQuery("israel-1", "israel_remote"),
        SearchQuery("israel-2", "israel_remote"),
        SearchQuery("london-1", "london"),
        SearchQuery("singapore-1", "singapore"),
        SearchQuery("us-1", "us_nyc_sf"),
        SearchQuery("secondary-1", "secondary_eu_relocation"),
    ]

    brave, fallback = split_queries_for_brave(queries, limit=8)

    assert [query.market_id for query in brave] == [
        "germany_eu",
        "israel_remote",
        "london",
        "singapore",
        "us_nyc_sf",
        "secondary_eu_relocation",
        "germany_eu",
        "israel_remote",
    ]
    assert [query.text for query in fallback] == ["germany-3"]


class _Response:
    status_code = 200

    def __init__(self, text: str = "", payload=None):
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Http:
    def __init__(self):
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "api.search.brave.com" in url:
            return _Response(
                payload={
                    "web": {
                        "results": [
                            {
                                "title": "Founding Software Engineer",
                                "url": "https://jobs.ashbyhq.com/hera/123",
                            }
                        ]
                    }
                }
            )
        return _Response(
            '<a class="result__a" href="https://jobs.ashbyhq.com/hera/123">Founding Software Engineer</a>'
        )


def test_canonical_lookup_uses_shared_brave_budget_then_falls_back_to_ddg(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "configured-and-budgeted")
    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    ledger = SearchUsageLedger(tmp_path / "state.sqlite3")
    budget = search_budget.BraveRequestBudget(
        ledger, monthly_limit=1, now=lambda: now
    )
    http = _Http()
    job = Job(
        source="test",
        company="Hera",
        title="Founding Software Engineer",
        url="https://example.com/job",
    )

    _targeted_canonical_candidates(http, job, CircuitBreaker(5), budget)
    _targeted_canonical_candidates(http, job, CircuitBreaker(5), budget)

    brave_calls = [url for url in http.urls if "api.search.brave.com" in url]
    ddg_calls = [url for url in http.urls if "duckduckgo.com" in url]
    assert len(brave_calls) == 1
    assert len(ddg_calls) == 1

    month_start = datetime(2026, 9, 1, tzinfo=UTC)
    next_month = datetime(2026, 10, 1, tzinfo=UTC)
    assert ledger.count(provider="brave", start_at=month_start, end_at=next_month) == 1

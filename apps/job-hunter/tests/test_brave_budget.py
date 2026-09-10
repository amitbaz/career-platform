import itertools
from datetime import datetime, timedelta, timezone

import job_hunter.search_budget as search_budget
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.models import Job, SearchQuery
from job_hunter.pipeline import _targeted_canonical_candidates
from job_hunter.search_budget import (
    SearchUsageLedger,
    brave_queries_available_today,
    split_queries_for_brave,
)

# job_hunter_platform_search_usage has no user_id (issue #184), so nothing
# about a row here marks it as this test's -- two tests sharing provider=
# "brave" and a calendar month would have an earlier test's writes count
# against a later test's cap. Issue #236: each ledger-touching test below
# takes `brave_ledger_window`, which hands it a (year, month) no other test
# in the run is using (see `_brave_ledger_windows` in conftest.py), so
# nothing about running concurrently under xdist can make two tests share
# rows in the first place.

UTC = timezone.utc


def _distinct_instants(base: datetime):
    """A `now` callable that advances by a microsecond on every call.

    `job_hunter_platform_search_usage` carries a `(provider, occurred_at)`
    unique constraint (issue #184) so a retried write converges
    instead of duplicating -- necessary for idempotency, but it also means
    two distinct reservations that land on the exact same `occurred_at`
    collapse into one row. In production that never happens: the default
    `now` is a fresh `datetime.now(timezone.utc)` at each call, which is
    practically always microsecond-distinct. A test that freezes `now` to one
    fixed instant across several `reserve()` calls (as the SQLite-era ledger,
    with no such constraint, tolerated) needs this instead, to model
    several real, slightly-separated calls without coupling assertions to
    wall-clock timing.
    """
    counter = itertools.count()
    return lambda: base + timedelta(microseconds=next(counter))


def test_brave_budget_spreads_250_monthly_queries_and_blocks_same_day_reruns(
    supabase_client, brave_ledger_window
):
    year, month, _, _ = brave_ledger_window
    ledger = SearchUsageLedger(supabase_client)
    now = datetime(year, month, 2, 12, 0, tzinfo=UTC)

    # 250 remaining across day 2-30 of a 30-day month => ceil(250 / 29) = 9
    # for today.
    assert brave_queries_available_today(ledger, monthly_limit=250, now=now) == 9

    for minute in range(9):
        ledger.record(
            provider="brave",
            occurred_at=now.replace(minute=minute),
        )

    # A manual rerun on the same day must not spend another 9 calls.
    assert brave_queries_available_today(ledger, monthly_limit=250, now=now) == 0

    # The next day gets a fresh share of the remaining monthly allowance.
    tomorrow = datetime(year, month, 3, 12, 0, tzinfo=UTC)
    assert brave_queries_available_today(ledger, monthly_limit=250, now=tomorrow) == 9


def test_brave_budget_daily_target_does_not_shrink_as_today_is_consumed(
    supabase_client, brave_ledger_window
):
    year, month, _, _ = brave_ledger_window
    ledger = SearchUsageLedger(supabase_client)
    now = datetime(year, month, 1, 12, 0, tzinfo=UTC)

    assert brave_queries_available_today(ledger, monthly_limit=1000, now=now) == 34

    for minute in range(10):
        ledger.record(provider="brave", occurred_at=now.replace(minute=minute))

    # The day started with a 34-query share. Using 10 should leave 24, rather
    # than recalculating the daily target downward after each request.
    assert brave_queries_available_today(ledger, monthly_limit=1000, now=now) == 24


def test_brave_budget_never_exceeds_monthly_limit(supabase_client, brave_ledger_window):
    year, month, _, _ = brave_ledger_window
    ledger = SearchUsageLedger(supabase_client)
    now = datetime(year, month, 30, 12, 0, tzinfo=UTC)

    for index in range(250):
        ledger.record(
            provider="brave",
            occurred_at=datetime(year, month, 1, tzinfo=UTC) + timedelta(minutes=index),
        )

    assert brave_queries_available_today(ledger, monthly_limit=250, now=now) == 0


def test_brave_request_budget_hard_cap_is_shared_across_consumers(
    supabase_client, brave_ledger_window
):
    year, month, month_start, next_month = brave_ledger_window
    now = _distinct_instants(datetime(year, month, 30, 12, 0, tzinfo=UTC))
    discovery_budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(supabase_client), monthly_limit=3, now=now
    )
    canonical_budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(supabase_client), monthly_limit=3, now=now
    )

    assert discovery_budget.reserve() is True
    assert discovery_budget.reserve() is True
    assert canonical_budget.reserve() is True
    assert canonical_budget.reserve() is False

    ledger = SearchUsageLedger(supabase_client)
    assert ledger.count(provider="brave", start_at=month_start, end_at=next_month) == 3


def test_brave_request_budget_stops_at_limit_with_frozen_clock(
    supabase_client, brave_ledger_window
):
    """Repeated `occurred_at` must not collapse reservations into one row.

    The unique key on `job_hunter_platform_search_usage` makes a retried write
    converge -- but `BraveRequestBudget.reserve()` guards against a
    genuinely frozen (or non-monotonic) clock by bumping into
    strictly-increasing territory itself. A single instance issuing every
    reservation at the exact same instant must still stop at the limit,
    exactly like the old SQLite autoincrement ledger did.
    """
    year, month, month_start, next_month = brave_ledger_window
    frozen = datetime(year, month, 30, 12, 0, tzinfo=UTC)
    budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(supabase_client), monthly_limit=3, now=lambda: frozen
    )

    assert budget.reserve() is True
    assert budget.reserve() is True
    assert budget.reserve() is True
    assert budget.reserve() is False

    ledger = SearchUsageLedger(supabase_client)
    assert ledger.count(provider="brave", start_at=month_start, end_at=next_month) == 3


def test_brave_discovery_priority_is_soft_within_shared_daily_allowance(
    supabase_client, brave_ledger_window
):
    year, month, _, _ = brave_ledger_window
    now = _distinct_instants(datetime(year, month, 30, 12, 0, tzinfo=UTC))
    budget = search_budget.BraveRequestBudget(
        SearchUsageLedger(supabase_client),
        monthly_limit=10,
        discovery_share=0.8,
        now=now,
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
    supabase_client, brave_ledger_window
):
    year, month, month_start, next_month = brave_ledger_window
    now = datetime(year, month, 30, 12, 0, tzinfo=UTC)
    ledger = SearchUsageLedger(supabase_client)
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

    _targeted_canonical_candidates(
        http, job, CircuitBreaker(5), "configured-and-budgeted", budget
    )
    _targeted_canonical_candidates(
        http, job, CircuitBreaker(5), "configured-and-budgeted", budget
    )

    brave_calls = [url for url in http.urls if "api.search.brave.com" in url]
    ddg_calls = [url for url in http.urls if "duckduckgo.com" in url]
    assert len(brave_calls) == 1
    assert len(ddg_calls) == 1

    assert ledger.count(provider="brave", start_at=month_start, end_at=next_month) == 1

from __future__ import annotations

import logging
import os
from datetime import date

from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.discovery_queries import generate_search_queries
from job_hunter.models import MarketPolicy, Settings
from job_hunter.search_backend import BraveSearchBackend
from job_hunter.search_budget import (
    BraveRequestBudget,
    SearchUsageLedger,
    brave_queries_available_today,
    split_queries_for_brave,
)
from job_hunter.store import JobStore

from .arbeitnow import ArbeitnowSource
from .ashby import AshbySource
from .base import JobSource
from .company_watch import CompanyWatchSource
from .devjobs import DevJobsSource
from .duckduckgo import DuckDuckGoSource
from .greenhouse import GreenhouseSource
from .gmail_staged import GmailStagedSource
from .hackernews import HackerNewsHiringSource
from .himalayas import HimalayasSource
from .jobicy import JobicySource
from .learned_ats import LearnedAtsSource
from .lever import LeverSource
from .remotive import RemotiveSource
from .remoteok import RemoteOKSource
from .targeted_search import TargetedSearchSource
from .weworkremotely import WeWorkRemotelySource
from .wellfound import _WELLFOUND_LISTINGS, WellfoundListing, WellfoundSource
from .yc import YCSource

logger = logging.getLogger(__name__)
_DEFAULT_BRAVE_MONTHLY_QUERY_LIMIT = 1000
_KNOWN_DIRECT_SOURCES = frozenset({"devjobs", "wellfound"})

__all__ = [
    "JobSource",
    "RemotiveSource",
    "ArbeitnowSource",
    "JobicySource",
    "HimalayasSource",
    "GmailStagedSource",
    "CompanyWatchSource",
    "DuckDuckGoSource",
    "TargetedSearchSource",
    "AshbySource",
    "LeverSource",
    "GreenhouseSource",
    "DevJobsSource",
    "RemoteOKSource",
    "WeWorkRemotelySource",
    "HackerNewsHiringSource",
    "YCSource",
    "LearnedAtsSource",
    "WellfoundListing",
    "WellfoundSource",
    "build_brave_budget",
    "build_sources",
]


def _brave_monthly_query_limit() -> int:
    raw = os.environ.get(
        "BRAVE_MONTHLY_QUERY_LIMIT",
        str(_DEFAULT_BRAVE_MONTHLY_QUERY_LIMIT),
    ).strip()
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "invalid BRAVE_MONTHLY_QUERY_LIMIT=%r; disabling Brave to fail safe",
            raw,
        )
        return 0
    if value <= 0:
        logger.warning(
            "non-positive BRAVE_MONTHLY_QUERY_LIMIT=%r; disabling Brave to fail safe",
            raw,
        )
        return 0
    return value


def build_brave_budget(settings: Settings) -> BraveRequestBudget | None:
    """Build the run's shared persisted Brave budget when Brave is configured."""
    if not os.environ.get("BRAVE_SEARCH_API_KEY"):
        return None
    return BraveRequestBudget(
        SearchUsageLedger(settings.db_path),
        monthly_limit=_brave_monthly_query_limit(),
    )


def _validate_direct_sources(markets: list[MarketPolicy]) -> None:
    """Reject a `direct_sources` entry with no corresponding adapter.

    A misconfigured entry (typo, unsupported name, or `wellfound` on a market
    with no configured Wellfound routes) would otherwise silently produce zero
    sources and zero log output, contradicting the constraint that a
    configured `direct_sources` entry must correspond to an instantiated
    adapter. Only enabled markets are checked.
    """
    for market in markets:
        if not market.enabled:
            continue
        for name in market.direct_sources or []:
            if name not in _KNOWN_DIRECT_SOURCES:
                raise ValueError(
                    f"market {market.id!r} direct_sources entry {name!r} does not "
                    f"correspond to any known adapter (known: {sorted(_KNOWN_DIRECT_SOURCES)})"
                )
            if name == "wellfound" and not _WELLFOUND_LISTINGS.get(market.id):
                raise ValueError(
                    f"market {market.id!r} lists 'wellfound' in direct_sources but "
                    "has no configured Wellfound routes"
                )


def build_sources(
    settings: Settings,
    http,
    *,
    store: JobStore | None = None,
    search_breaker: CircuitBreaker | None = None,
    query_date: date | None = None,
    brave_budget: BraveRequestBudget | None = None,
) -> list[JobSource]:
    _validate_direct_sources(settings.policy.markets)
    queries = generate_search_queries(settings.policy, query_date)
    brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    targeted_sources: list[JobSource] = []

    if brave_api_key:
        budget = brave_budget or build_brave_budget(settings)
        if budget is not None:
            available_today = budget.available_today()
            discovery_allowance = budget.discovery_allowance()
            brave_queries, deferred_queries = split_queries_for_brave(
                queries,
                limit=discovery_allowance,
            )

            logger.info(
                "Brave source-discovery budget: monthly_limit=%s available_today=%s "
                "discovery_allowance=%s selected=%s deferred=%s",
                budget.monthly_limit,
                available_today,
                discovery_allowance,
                len(brave_queries),
                len(deferred_queries),
            )

            if brave_queries:
                targeted_sources.append(
                    TargetedSearchSource(
                        BraveSearchBackend(
                            http,
                            brave_api_key,
                            on_attempt=budget.reserve,
                        ),
                        brave_queries,
                        breaker=search_breaker,
                    )
                )

    sources: list[JobSource] = [
        RemotiveSource(http),
        ArbeitnowSource(http),
        JobicySource(http),
        HimalayasSource(http),
        RemoteOKSource(http),
        WeWorkRemotelySource(http),
        HackerNewsHiringSource(http),
        *targeted_sources,
    ]
    if settings.policy.yc_job_pages:
        sources.append(YCSource(http, settings.policy.yc_job_pages))

    if any(
        "devjobs" in (market.direct_sources or [])
        for market in settings.policy.markets
        if market.enabled
    ):
        sources.append(DevJobsSource(http))

    wellfound_listings: list[WellfoundListing] = []
    for market in settings.policy.markets:
        if not market.enabled:
            continue
        if "wellfound" not in (market.direct_sources or []):
            continue
        wellfound_listings.extend(
            WellfoundListing(url=url, market_id=market.id)
            for url in _WELLFOUND_LISTINGS.get(market.id, [])
        )
    if wellfound_listings:
        sources.append(WellfoundSource(http, wellfound_listings))

    ats = settings.policy.ats or {}
    for board in ats.get("ashby", []) or []:
        sources.append(AshbySource(board, http))
    for site in ats.get("lever", []) or []:
        sources.append(LeverSource(site, http))
    for token in ats.get("greenhouse", []) or []:
        sources.append(GreenhouseSource(token, http))

    if store is not None and settings.policy.max_learned_ats_boards_per_run > 0:
        market_order = [
            market.id for market in settings.policy.markets if market.enabled
        ]
        sources.append(
            LearnedAtsSource(
                store,
                http,
                limit=settings.policy.max_learned_ats_boards_per_run,
                market_order=market_order,
            )
        )

    return sources

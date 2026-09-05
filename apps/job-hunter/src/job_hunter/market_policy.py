"""Attribute a job to the single primary market it best matches.

Evidence is scored from strongest to weakest and the highest-scoring enabled
market wins. A query-time hint (``Job.market_hint``) is the weakest signal and
must never override stronger, directly observed location evidence:

    400  explicit job.location match
    300  explicit remote country/region scope in location/description
    200  explicit sponsorship/relocation language tied to a market
    100  Job.market_hint
      0  no evidence -> fallback to the first enabled market in configured
         order, unless the job is explicitly non-remote (then no configured
         market is compatible and the job is left unattributed)
"""

from __future__ import annotations

import re

from job_hunter.models import Job, MarketPolicy, SearchPolicy
from job_hunter.normalize import normalize_text

_LOCATION_MATCH_SCORE = 400
_REMOTE_SCOPE_SCORE = 300
_SPONSORSHIP_RELOCATION_SCORE = 200
_QUERY_HINT_SCORE = 100

_SPONSORSHIP_KEYWORDS = ("sponsor", "sponsorship", "visa")
_RELOCATION_KEYWORDS = ("relocat",)


def market_by_id(policy: SearchPolicy, market_id: str) -> MarketPolicy | None:
    """Return the market with the given id, or None if it isn't configured."""
    for market in policy.markets:
        if market.id == market_id:
            return market
    return None


def salary_floor_for_job(job: Job, market: MarketPolicy) -> int:
    """Return the gross base salary floor that applies to a job in a market.

    A city-specific floor from ``market.salary.location_floors`` wins when the
    job's normalized location names that city; otherwise the market's overall
    ``gross_base_floor`` applies.
    """
    location_text = normalize_text(job.location or "")
    for city, floor in market.salary.location_floors.items():
        if _phrase_in_text(city, location_text):
            return floor
    return market.salary.gross_base_floor


def attribute_market(job: Job, markets: list[MarketPolicy]) -> str | None:
    """Return the id of the single market this job is best attributed to.

    Every enabled market is scored on the evidence tiers described in this
    module's docstring; the highest-scoring market wins, ties broken by
    configured order. When no market has any evidence at all, the job falls
    back to the first *compatible* enabled market in configured order: a job
    that is explicitly non-remote and matches no configured market's
    locations is compatible with none of them, so it stays unattributed and
    the caller applies the legacy non-remote blocker instead.
    """
    enabled_markets = [market for market in markets if market.enabled]
    if not enabled_markets:
        return None

    best_market_id: str | None = None
    best_score = 0
    for market in enabled_markets:
        score = _evidence_score(job, market)
        if score > best_score:
            best_score = score
            best_market_id = market.id

    if best_score > 0:
        return best_market_id

    # No market has any evidence, so no configured market's locations were
    # named anywhere (a location match would have scored). An explicitly
    # non-remote job in an unnamed place is not plausibly compatible with any
    # market, and silently attributing it to the first enabled one would let
    # it skip both the market work-mode rules (which only run for
    # remote-required markets) and the legacy non-remote hard blocker.
    # Ambiguous jobs (remote, or remote unknown) still fall back, so market
    # uncertainty alone never drops a job.
    if job.remote is False:
        return None

    return enabled_markets[0].id


def _evidence_score(job: Job, market: MarketPolicy) -> int:
    location_text = normalize_text(job.location or "")
    description_text = normalize_text(job.description or "")

    if _any_phrase_in_text(market.locations, location_text):
        return _LOCATION_MATCH_SCORE

    phrase_in_description = _any_phrase_in_text(market.locations, description_text)
    if phrase_in_description and job.remote:
        return _REMOTE_SCOPE_SCORE

    if phrase_in_description and _mentions_sponsorship_or_relocation(
        f"{location_text} {description_text}"
    ):
        return _SPONSORSHIP_RELOCATION_SCORE

    if job.market_hint and job.market_hint == market.id:
        return _QUERY_HINT_SCORE

    return 0


def _mentions_sponsorship_or_relocation(text: str) -> bool:
    keywords = _SPONSORSHIP_KEYWORDS + _RELOCATION_KEYWORDS
    return any(keyword in text for keyword in keywords)


def _any_phrase_in_text(phrases: list[str], text: str) -> bool:
    return any(_phrase_in_text(phrase, text) for phrase in phrases)


def _phrase_in_text(phrase: str, text: str) -> bool:
    normalized_phrase = normalize_text(phrase)
    if not normalized_phrase or not text:
        return False
    pattern = rf"\b{re.escape(normalized_phrase)}\b"
    return re.search(pattern, text) is not None

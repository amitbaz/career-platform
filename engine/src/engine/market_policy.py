"""Attribute a job to the single primary market it best matches.

Evidence is scored from strongest to weakest and the highest-scoring enabled
market wins. A query-time hint (``Job.market_hint``) is the weakest signal and
must never override stronger, directly observed location evidence:

   +500  the posting states it hires in a region this market is in -- added to
         the tiers below rather than replacing them, so when a posting names
         several regions the ordinary evidence still picks between the markets
         that are in scope
    400  explicit job.location match
    300  explicit remote country/region scope in location/description
    200  explicit sponsorship/relocation language tied to a market
    100  Job.market_hint
      0  no evidence -> fallback to the first enabled market in configured
         order, unless the job is explicitly non-remote (then no configured
         market is compatible and the job is left unattributed)

The top tier exists because a listing variant's location label is a single,
often-syndicated field, while the posting's own statement of who it will hire
is the employer speaking directly (issue #16). A role labelled "North America"
whose description says it is open to candidates in the US *and* Europe is a
valid European candidate, and a label alone must not veto that. The same
statement runs the other way: when a posting names its regions, a market in
none of them is out of the running entirely, so an incidental "our team spans
Europe" can no longer pull a US-only role into a European market.

Reading the posting's scope is `hiring_scope.determine_hiring_scope`, which
deliberately knows nothing about markets or candidates -- see that module.
"""

from __future__ import annotations

import re

from engine.hiring_scope import determine_hiring_scope, regions_for_locations
from engine.models import Job, MarketPolicy, SearchPolicy
from engine.normalize import normalize_text

_EXPLICIT_HIRING_SCOPE_SCORE = 500
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

    A posting that states its own hiring regions narrows the field first: a
    market in none of the stated regions is dropped before scoring, so it can
    win neither on evidence nor as the fallback. A market whose locations name
    no region the atlas knows is never dropped -- an unplaceable market is an
    absence of evidence, not a contradiction. It can still be *outscored* by an
    in-scope rival, since the bonus exceeds a location match; that is the cost
    of an atlas gap, and the reason `regions_for_locations` reads configured
    place names as permissively as it can.
    """
    enabled_markets = [market for market in markets if market.enabled]
    if not enabled_markets:
        return None

    scope = determine_hiring_scope(job)
    location_text = normalize_text(job.location or "")

    # (market, evidence score, evidence score + hiring-scope bonus)
    scored: list[tuple[MarketPolicy, int, int]] = []
    for market in enabled_markets:
        regions = regions_for_locations(tuple(market.locations))
        # A market the posting's stated regions exclude is dropped -- unless
        # the job's own location label names that market, in which case label
        # and text contradict each other and the scope should outrank the
        # label rather than erase it.
        if not scope.permits(regions) and not _any_phrase_in_text(
            market.locations, location_text
        ):
            continue
        evidence = _evidence_score(job, market)
        bonus = (
            _EXPLICIT_HIRING_SCOPE_SCORE
            if scope.is_explicit and scope.regions & regions
            else 0
        )
        scored.append((market, evidence, evidence + bonus))

    if not scored:
        # The posting's stated regions exclude every configured market. Being
        # unattributed is *weaker* filtering, not stronger: the caller falls
        # back to the legacy global prefilter, which drops every market rule
        # (salary floor, language, sponsorship, employment type) and only
        # blocks a job that is explicitly non-remote. So a remote job goes
        # back through the ordinary evidence path rather than out of the
        # market system entirely -- attributing it imperfectly still subjects
        # it to a market's checks.
        if job.remote is False:
            return None
        scored = [
            (market, evidence, evidence)
            for market, evidence in (
                (market, _evidence_score(job, market))
                for market in enabled_markets
            )
        ]

    # An explicitly non-remote job is attributed only on directly observed
    # evidence. A stated hiring region says where the employer will hire, not
    # that the work can be done from there remotely, so it must not by itself
    # pull such a job into a market and past the legacy non-remote blocker.
    if job.remote is False and all(evidence == 0 for _m, evidence, _t in scored):
        return None

    best_market_id: str | None = None
    best_score = 0
    for market, _evidence, total in scored:
        if total > best_score:
            best_score = total
            best_market_id = market.id

    if best_score > 0:
        return best_market_id

    # No market scored, so no configured market's locations were named
    # anywhere (a location match would have scored) and the posting stated no
    # region either. An explicitly non-remote job in an unnamed place is not
    # plausibly compatible with any market, and silently attributing it to the
    # first enabled one would let it skip both the market work-mode rules
    # (which only run for remote-required markets) and the legacy non-remote
    # hard blocker -- the guard above has already returned None for it.
    # Ambiguous jobs (remote, or remote unknown) still fall back, so market
    # uncertainty alone never drops a job.
    return scored[0][0].id


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

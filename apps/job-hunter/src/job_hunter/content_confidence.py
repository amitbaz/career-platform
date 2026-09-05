from __future__ import annotations

OFFICIAL_ATS = "official_ats"
CANONICAL_EMPLOYER_PAGE = "canonical_employer_page"
SOURCE_DETAIL_PAGE = "source_detail_page"
AGGREGATOR_TEXT = "aggregator_text"
PARTIAL_UNKNOWN = "partial_unknown"

# Ordered most trustworthy first. Index doubles as the rank used for merge
# comparisons, so appending a new tier here must go in the right position.
TIERS = [
    OFFICIAL_ATS,
    CANONICAL_EMPLOYER_PAGE,
    SOURCE_DETAIL_PAGE,
    AGGREGATOR_TEXT,
    PARTIAL_UNKNOWN,
]

# Sources not listed here (including empty/future/unrecognized ones) default
# to AGGREGATOR_TEXT: not trusted as authoritative, but not treated as if it
# had no content either, since a description string is actually present.
_SOURCE_TIER: dict[str, str] = {
    "ashby": OFFICIAL_ATS,
    "lever": OFFICIAL_ATS,
    "greenhouse": OFFICIAL_ATS,
    "wellfound": SOURCE_DETAIL_PAGE,
    "devjobs": SOURCE_DETAIL_PAGE,
    "arbeitnow": AGGREGATOR_TEXT,
    "himalayas": AGGREGATOR_TEXT,
    "jobicy": AGGREGATOR_TEXT,
    "remoteok": AGGREGATOR_TEXT,
    "remotive": AGGREGATOR_TEXT,
    "weworkremotely": AGGREGATOR_TEXT,
    "hackernews": AGGREGATOR_TEXT,
    "yc": PARTIAL_UNKNOWN,
    "targeted_search": PARTIAL_UNKNOWN,
    "duckduckgo": PARTIAL_UNKNOWN,
    "company_watch": CANONICAL_EMPLOYER_PAGE,
}

_DEFAULT_TIER = AGGREGATOR_TEXT


def tier_rank(tier: str) -> int:
    """Lower is more trustworthy. An unrecognized/empty tier ranks worst of all."""
    try:
        return TIERS.index(tier)
    except ValueError:
        return len(TIERS)


def infer_content_confidence(source: str, description: str) -> str:
    """Infer a description's trust tier from its source and content.

    Call this once per raw job, immediately after a source adapter returns
    it and before any cross-source merge, so the tier always describes the
    exact description text it travels with.
    """
    if not description or not description.strip():
        return PARTIAL_UNKNOWN
    normalized_source = (source or "").split(":", 1)[0]
    return _SOURCE_TIER.get(normalized_source, _DEFAULT_TIER)


def is_sufficient(tier: str) -> bool:
    """Whether this tier is trustworthy enough to support a confident decision."""
    return bool(tier) and tier != PARTIAL_UNKNOWN

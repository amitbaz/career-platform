from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from job_hunter import content_confidence
from job_hunter.ats_registry import harvest_ats_board
from job_hunter.canonical import (
    CanonicalResolver,
    fetch_authoritative_description,
    parse_supported_ats_url,
)
from job_hunter.fetching import enrich_job
from job_hunter.http import HttpClient
from job_hunter.job_identity import job_fallback_identity
from job_hunter.market_policy import attribute_market, market_by_id
from job_hunter.models import CandidatePreferences, Job, SearchPolicy
from job_hunter.normalize import canonicalize_url
from job_hunter.prefilter import prefilter_job
from job_hunter.ranking import rank_jobs, select_diverse_candidates
from job_hunter.store import JobStore

logger = logging.getLogger(__name__)

_ATS_HOSTS = ("jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io")

# How many candidates beyond max_jobs_per_run to keep in the canonical
# resolution shortlist, to absorb resolution failures and any reordering
# that happens once resolved data (e.g. source_quality) feeds back into
# ranking. Fixed rather than configurable -- see docs/superpowers/plans/
# 2026-09-03-bound-canonical-resolution.md for why.
_CANONICAL_SHORTLIST_MULTIPLIER = 2

# Bucket key for jobs that could not be tied to any configured market (or when
# no markets are configured at all). Kept distinct from real market ids so
# per-market observability never silently merges unattributed jobs into a
# real market's counters.
_UNATTRIBUTED = "unattributed"


@dataclass(slots=True)
class DiscoveryStats:
    raw: int = 0
    unique: int = 0
    canonical_resolved: int = 0
    canonical_unresolved: int = 0
    cross_source_duplicates: int = 0
    canonical_budget_exhausted: int = 0
    canonical_network_attempts: int = 0
    canonical_shortlist_limit: int = 0
    prefilter_rejected: int = 0
    profession_rejected: int = 0
    eligible: int = 0
    ats_boards_discovered: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    raw_by_market: dict[str, int] = field(default_factory=dict)
    unique_by_market: dict[str, int] = field(default_factory=dict)
    rejected_by_market: dict[str, int] = field(default_factory=dict)
    eligible_by_market: dict[str, int] = field(default_factory=dict)
    reattributed_by_market: dict[str, int] = field(default_factory=dict)
    unique_by_source: dict[str, int] = field(default_factory=dict)
    rejected_by_source: dict[str, int] = field(default_factory=dict)
    eligible_by_source: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryResult:
    eligible: list[tuple[int, Job]]
    rediscovered_job_ids: list[int]
    stats: DiscoveryStats


def candidate_url_key(job: Job) -> str | None:
    url = job.canonical_url or job.url
    if not url:
        return None
    return canonicalize_url(url)


def candidate_identity_key(job: Job) -> str:
    return job_fallback_identity(job.company, job.title, job.location) or ""


def candidate_ats_key(job: Job) -> tuple[str, str, str] | None:
    if not job.ats_provider or not job.ats_board or not job.ats_job_id:
        return None
    return (job.ats_provider, job.ats_board, job.ats_job_id)


def _is_ats_url(job: Job) -> bool:
    url = (job.url or "").lower()
    return any(host in url for host in _ATS_HOSTS)


def _richness_key(job: Job) -> tuple[bool, int, bool, bool, bool]:
    tier_score = len(content_confidence.TIERS) - 1 - content_confidence.tier_rank(
        job.content_confidence
    )
    return (
        _is_ats_url(job),
        tier_score,
        bool(job.company),
        bool(job.location),
        job.remote is not None,
    )


def _merge_fields(richer: Job, weaker: Job) -> Job:
    if not richer.title and weaker.title:
        richer.title = weaker.title
    if not richer.company and weaker.company:
        richer.company = weaker.company
    if not richer.location and weaker.location:
        richer.location = weaker.location
    if weaker.description and content_confidence.tier_rank(
        weaker.content_confidence
    ) < content_confidence.tier_rank(richer.content_confidence):
        richer.description = weaker.description
        richer.content_confidence = weaker.content_confidence
    if richer.remote is None and weaker.remote is not None:
        richer.remote = weaker.remote
    if not richer.url and weaker.url:
        richer.url = weaker.url
    if not richer.source_job_id and weaker.source_job_id:
        richer.source_job_id = weaker.source_job_id
    if not richer.original_url and weaker.original_url:
        richer.original_url = weaker.original_url
    if not richer.canonical_url and weaker.canonical_url:
        richer.canonical_url = weaker.canonical_url
    if not richer.ats_provider and weaker.ats_provider:
        richer.ats_provider = weaker.ats_provider
    if not richer.ats_board and weaker.ats_board:
        richer.ats_board = weaker.ats_board
    if not richer.ats_job_id and weaker.ats_job_id:
        richer.ats_job_id = weaker.ats_job_id
    return richer


def _dedupe(jobs: list[Job]) -> tuple[list[Job], int]:
    """
    Collapse in-run duplicates. Two jobs are the same candidate when their
    canonical URLs, ATS identities, or exact source-independent fallback
    identities match. Union-find lets one record bridge multiple strong keys.
    """
    n = len(jobs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    url_seen: dict[str, int] = {}
    ats_seen: dict[tuple[str, str, str], int] = {}
    identity_seen: dict[str, int] = {}

    for i, job in enumerate(jobs):
        url_key = candidate_url_key(job)
        if url_key:
            if url_key in url_seen:
                union(i, url_seen[url_key])
            else:
                url_seen[url_key] = i

        ats_key = candidate_ats_key(job)
        if ats_key:
            if ats_key in ats_seen:
                union(i, ats_seen[ats_key])
            else:
                ats_seen[ats_key] = i

        identity_key = candidate_identity_key(job)
        if identity_key:
            if identity_key in identity_seen:
                union(i, identity_seen[identity_key])
            else:
                identity_seen[identity_key] = i

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    ordered_roots = sorted(clusters, key=lambda root: min(clusters[root]))

    merged: list[Job] = []
    cross_source_duplicate_groups = 0
    for root in ordered_roots:
        cluster = clusters[root]
        if len({metric_source_label(jobs[index].source) for index in cluster}) > 1:
            cross_source_duplicate_groups += 1
        cluster_jobs = sorted(
            (jobs[i] for i in cluster), key=_richness_key, reverse=True
        )
        winner = cluster_jobs[0]
        for weaker in cluster_jobs[1:]:
            winner = _merge_fields(winner, weaker)
        merged.append(winner)

    return merged, cross_source_duplicate_groups


def metric_source_label(source: str) -> str:
    """Return a bounded source label suitable for metrics and logs."""
    if source.startswith("gmail:"):
        return "gmail"
    return source


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _harvest_ats_board_safely(
    store: JobStore, job: Job, market_hint: str | None = None
) -> bool:
    """Learn a job's ATS board without letting a registry write drop the job.

    Returns True only when the harvest created a new registry entry.
    """
    try:
        return harvest_ats_board(store, job, market_hint=market_hint)
    except Exception:
        logger.exception(
            "ATS board harvesting failed: source=%s", metric_source_label(job.source)
        )
        return False


def _record_reattribution(
    stats: DiscoveryStats,
    before: str | None,
    after: str | None,
) -> None:
    if before == after:
        return
    _bump(stats.reattributed_by_market, after or _UNATTRIBUTED)


def _cheap_market_attribution(job: Job, policy: SearchPolicy) -> str | None:
    """Attribute a market as cheaply as possible for raw-stage observability.

    A query-time hint is free; otherwise fall back to the same evidence-based
    attribution used later, using whatever fields the raw job already carries
    (this never performs network I/O, so it's still cheap at this stage).
    """
    if job.market_hint:
        return job.market_hint
    if not policy.markets:
        return None
    return attribute_market(job, policy.markets)


def _format_source_contribution(per_source: dict[str, int]) -> str:
    """Render compact source totals without including any job content."""
    metric_counts: dict[str, int] = {}
    for source, count in per_source.items():
        label = metric_source_label(source)
        metric_counts[label] = metric_counts.get(label, 0) + count
    if not metric_counts:
        return "none"
    return " ".join(
        f"{source}={count}" for source, count in sorted(metric_counts.items())
    )


def collect_candidates(
    sources: list,
    store: JobStore,
    http: HttpClient,
    policy: SearchPolicy,
    resolver: CanonicalResolver | None = None,
    preferences: CandidatePreferences | None = None,
) -> DiscoveryResult:
    """Discover, canonicalize, deduplicate, persist, and prefilter jobs.

    Source failures and unresolved canonical lookups remain non-fatal. When no
    resolver is supplied, candidates retain their source URLs and canonical
    counters stay at zero.
    """
    stats = DiscoveryStats()
    raw_jobs: list[Job] = []

    for source in sources:
        try:
            jobs = source.discover()
        except Exception:
            logger.exception("source discovery failed: %r", source)
            continue

        for job in jobs:
            stats.raw += 1
            stats.per_source[job.source] = stats.per_source.get(job.source, 0) + 1
            if job.url:
                job.original_url = job.original_url or job.url
            job.content_confidence = content_confidence.infer_content_confidence(
                job.source, job.description
            )
            raw_market_id = _cheap_market_attribution(job, policy)
            _bump(stats.raw_by_market, raw_market_id or _UNATTRIBUTED)
            raw_jobs.append(job)

    # Persist every source copy before collapsing the run so provenance is
    # retained even when only one representative continues to evaluation.
    for job in raw_jobs:
        store.upsert_logical_job(job)

    unique_jobs, stats.cross_source_duplicates = _dedupe(raw_jobs)
    stats.unique = len(unique_jobs)

    prefiltered: list[tuple[int, Job]] = []
    rediscovered_job_ids: list[int] = []

    for job in unique_jobs:
        observed_market_id = _cheap_market_attribution(job, policy)
        if _harvest_ats_board_safely(store, job, market_hint=observed_market_id):
            stats.ats_boards_discovered += 1
        if job.url and not job.description:
            enrich_job(job, http)

        job_id, _is_new, _description_changed = store.upsert_logical_job(job)

        job.market_id = attribute_market(job, policy.markets) if policy.markets else None
        _record_reattribution(stats, observed_market_id, job.market_id)
        if job.market_id:
            store.set_job_market(job_id, job.market_id)
        market_key = job.market_id or _UNATTRIBUTED
        source_label = metric_source_label(job.source)
        _bump(stats.unique_by_market, market_key)
        _bump(stats.unique_by_source, source_label)

        if not store.needs_evaluation(job_id):
            rediscovered_job_ids.append(job_id)
            continue

        market = market_by_id(policy, job.market_id) if job.market_id else None
        prefilter_result = prefilter_job(job, policy, market)
        if not prefilter_result.should_evaluate:
            if prefilter_result.reason_code == "off_target_profession":
                stats.profession_rejected += 1
            else:
                stats.prefilter_rejected += 1
            _bump(stats.rejected_by_market, market_key)
            _bump(stats.rejected_by_source, source_label)
            continue

        prefiltered.append((job_id, job))

    # Canonical resolution costs a page fetch plus a public search for jobs
    # not already on a supported ATS host, so that expensive path only runs
    # for the highest-ranked prefiltered candidates: the ones that could
    # realistically survive ranking/selection this run. The shortlist size
    # is whichever is smaller, the flat per-run ceiling or max_jobs_per_run
    # times the slack multiplier. Already-supported-ATS URLs resolve locally
    # at zero network cost, so they are never gated by this shortlist -- and
    # never consume a shortlist slot either, since they're filtered out
    # before the slot count is applied below.
    shortlisted_ids: set[int] = set()
    if resolver is not None and prefiltered:
        shortlist_limit = max(
            0,
            min(
                policy.max_canonical_resolutions_per_run,
                max(0, policy.max_jobs_per_run) * _CANONICAL_SHORTLIST_MULTIPLIER,
            ),
        )
        stats.canonical_shortlist_limit = shortlist_limit
        ranked_prefiltered = rank_jobs(prefiltered, policy, preferences)
        needing_resolution_ranked = [
            item
            for item in ranked_prefiltered
            if item[1].url and parse_supported_ats_url(item[1].url) is None
        ]
        # Mirror pipeline._select_candidates's own strategy here: a flat
        # top-N slice when there's no candidate profile to diversify by,
        # diversity-aware selection when there is. Final selection
        # guarantees every source a minimum_per_source floor regardless of
        # global rank, so a flat rank slice here could shortlist zero
        # candidates from a source that final selection still picks --
        # leaving those jobs unresolved even though they ship.
        try:
            if preferences is None:
                shortlist = needing_resolution_ranked[:shortlist_limit]
            else:
                shortlist = select_diverse_candidates(
                    needing_resolution_ranked,
                    limit=shortlist_limit,
                    minimum_per_source=policy.source_minimum_per_run,
                    max_share=policy.source_max_share,
                )
        except Exception:
            logger.exception(
                "canonical shortlist selection failed; falling back to global rank"
            )
            shortlist = needing_resolution_ranked[:shortlist_limit]
        shortlisted_ids = {item[0] for item in shortlist}

    eligible: list[tuple[int, Job]] = []
    eligible_job_ids: set[int] = set()

    for job_id, job in prefiltered:
        if resolver is not None and job.url:
            already_ats_url = parse_supported_ats_url(job.url) is not None
            if not already_ats_url and job_id not in shortlisted_ids:
                stats.canonical_budget_exhausted += 1
            else:
                if not already_ats_url:
                    stats.canonical_network_attempts += 1
                try:
                    resolution = resolver.resolve(job)
                except Exception:
                    logger.exception(
                        "canonical resolution failed: source=%s",
                        metric_source_label(job.source),
                    )
                    resolution = None
                if resolution is None:
                    stats.canonical_unresolved += 1
                else:
                    stats.canonical_resolved += 1
                    job.canonical_url = resolution.url
                    job.url = resolution.url
                    if resolution.ats is not None:
                        job.ats_provider = resolution.ats.provider
                        job.ats_board = resolution.ats.board
                        job.ats_job_id = resolution.ats.job_id
                        if _harvest_ats_board_safely(store, job):
                            stats.ats_boards_discovered += 1
                        if job.content_confidence != content_confidence.OFFICIAL_ATS:
                            authoritative = fetch_authoritative_description(
                                resolution.ats, resolution.url, http
                            )
                            if authoritative:
                                job.description = authoritative
                                job.content_confidence = content_confidence.OFFICIAL_ATS
                    # Canonical resolution can surface stronger, directly
                    # observed location evidence than the query-time hint that
                    # seeded the earlier attribution above, so re-run it
                    # before the final append. Attribution uncertainty alone
                    # (i.e. falling back to the first enabled market) must
                    # never drop a job -- only prefilter/eligibility do that.
                    previous_market_id = job.market_id
                    job.market_id = (
                        attribute_market(job, policy.markets) if policy.markets else None
                    )
                    _record_reattribution(stats, previous_market_id, job.market_id)
                    # Late canonicalization may consolidate stored rows; use
                    # the store's history-preserving survivor ID downstream.
                    job_id, _is_new, _description_changed = store.upsert_logical_job(job)
                    if job.market_id:
                        store.set_job_market(job_id, job.market_id)
                    if not store.needs_evaluation(job_id):
                        rediscovered_job_ids.append(job_id)
                        continue

        if job_id in eligible_job_ids:
            continue
        eligible_job_ids.add(job_id)
        eligible.append((job_id, job))
        _bump(stats.eligible_by_market, job.market_id or _UNATTRIBUTED)
        _bump(stats.eligible_by_source, metric_source_label(job.source))
        if job.ats_provider and job.ats_board:
            try:
                store.record_ats_eligible_job(
                    job.ats_provider, job.ats_board, datetime.now(timezone.utc)
                )
            except Exception:
                logger.exception(
                    "recording ATS-eligible job failed: source=%s",
                    metric_source_label(job.source),
                )

    stats.eligible = len(eligible)
    logger.info(
        "discovery source contribution: %s canonical_resolved=%s "
        "canonical_unresolved=%s canonical_budget_exhausted=%s "
        "canonical_network_attempts=%s canonical_shortlist_limit=%s "
        "cross_source_duplicates=%s",
        _format_source_contribution(stats.per_source),
        stats.canonical_resolved,
        stats.canonical_unresolved,
        stats.canonical_budget_exhausted,
        stats.canonical_network_attempts,
        stats.canonical_shortlist_limit,
        stats.cross_source_duplicates,
    )

    return DiscoveryResult(
        eligible=eligible,
        rediscovered_job_ids=rediscovered_job_ids,
        stats=stats,
    )

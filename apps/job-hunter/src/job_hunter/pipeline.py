from __future__ import annotations

import logging
import secrets
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.availability import UNVERIFIED
from job_hunter.candidate_context import get_candidate_context
from job_hunter.canonical import CanonicalResolver, parse_supported_ats_url
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.cover_letter import generate_cover_letter
from job_hunter.discovery import (
    DiscoveryResult,
    DiscoveryStats,
    collect_candidates,
    metric_source_label,
)
from job_hunter.company_facets import (
    CompanyEvidence,
    CompanyFacetExtractionError,
    extract_company_facets,
)
from job_hunter.facets import FacetExtractionError, PostingFacts, extract_facets
from job_hunter.ai import (
    AI_PURPOSES,
    AIBudgetExceeded,
    AIProvider,
    AIQuotaPaused,
    AITemporaryCapacity,
    PlatformAllowanceExhausted,
    wait_out_capacity,
)
from job_hunter.ai.usage import AIUsageTracker
from job_hunter.http import HttpClient
from job_hunter.job_identity import normalize_company_name
from job_hunter.matching import MatchResult, match_jobs
from job_hunter.models import (
    AtsReference,
    CandidateContext,
    CompanyFacets,
    DigestItem,
    AIUsageSummary,
    Job,
    JobFacets,
    Material,
    NavigationCard,
    NavigationSession,
    ReviewItem,
    RunSummary,
    Settings,
)
from job_hunter.pdf import render_cover_letter_pdf
from job_hunter.ranking import rank_jobs, select_diverse_candidates
from job_hunter.source_cursors import SourceCursorStore, record_run_crawls
from job_hunter.search_backend import build_search_backend
from job_hunter.search_budget import BraveRequestBudget
from job_hunter.sources import (
    CompanyWatchSource,
    GmailStagedSource,
    LearnedAtsSource,
    TargetedSearchSource,
    build_brave_budget,
    build_sources,
)
from job_hunter.postgres_store import PostgresJobStore
from job_hunter.sources.learned_ats import LearnedAtsStats
from job_hunter.telegram import (
    TelegramClient,
    build_digest,
    build_ai_pause_warning,
    build_gmail_review_digest_chunks,
    select_deliverable_items,
)
from job_hunter.telegram_navigation import build_navigation_card, navigation_sort_key
from job_hunter.watchlist import promote_company, sync_manual_watch_seeds

logger = logging.getLogger(__name__)

_AVAILABILITY_WARNING = "⚠️ Availability not verified - check the posting before applying"
_READY_DECISIONS = {"high_priority", "package_match"}
#: The outcomes the daily offer limit counts: the offers themselves, ready to
#: apply or possible. A `skip` costs a model call but no delivery budget, and
#: so does a `blocked` job -- it reaches Telegram only under "Needs review /
#: blockers", which is a warning about a job, not an offer to act on.
_OFFER_DECISIONS = _READY_DECISIONS | {"possible_match"}
_NAVIGATION_SESSION_TTL = timedelta(days=30)
_SUPPORTED_WATCH_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})
_SEARCH_FAILURE_THRESHOLD = 5
#: How many times a shared-platform-key posting read waits out rolling
#: capacity before giving up. Bounded, unlike scoring's wait: the platform
#: key is shared with every other user's run, and none of them alone can
#: clear it, so unbounded patience here can hold two overlapping runs over
#: the ceiling for as long as they both keep waiting.
_READ_CAPACITY_WAITS = 3
_CANONICAL_SEARCH_SITES = (
    " OR ".join(f"site:{host}" for host in SUPPORTED_ATS_HOSTS) + " OR careers"
)


def _targeted_canonical_candidates(
    http: HttpClient,
    job: Job,
    breaker: CircuitBreaker,
    brave_api_key: str | None,
    brave_budget: BraveRequestBudget | None = None,
) -> list[Job]:
    """Run one bounded public search for the employer's original posting."""
    company = " ".join(job.company.replace('"', " ").split())
    title = " ".join(job.title.replace('"', " ").split())
    if not company or not title:
        return []

    query = f'"{company}" "{title}" ({_CANONICAL_SEARCH_SITES})'
    backend = build_search_backend(
        http,
        brave_api_key,
        enable_brave=brave_budget is not None,
        on_brave_attempt=brave_budget.reserve if brave_budget is not None else None,
    )
    # Materialised: this returns a list its callers measure and re-read, and
    # the search is one bounded query rather than an open-ended harvest.
    candidates = list(
        TargetedSearchSource(backend, [query], breaker=breaker).discover()
    )
    for candidate in candidates:
        ats = parse_supported_ats_url(candidate.url)
        if ats is not None and (
            normalize_company_name(ats.board) == normalize_company_name(job.company)
        ):
            candidate.company = ats.board
    return candidates


def _persisted_watch_target(
    store: PostgresJobStore, company_name: str
) -> AtsReference | None:
    """Return only a persisted, complete, supported ATS watch target."""
    watch = store.get_company_watch(company_name)
    if watch is None:
        return None

    provider = (watch["ats_provider"] or "").strip().lower()
    identifier = (watch["ats_identifier"] or "").strip()
    if provider not in _SUPPORTED_WATCH_ATS_PROVIDERS or not identifier:
        return None
    return AtsReference(provider=provider, board=identifier, job_id=None)


def _select_candidates(ranked, policy, preferences):
    if not ranked or policy.max_jobs_per_run <= 0:
        return []

    try:
        if preferences is None:
            return ranked[: policy.max_jobs_per_run]
        return select_diverse_candidates(
            ranked,
            limit=policy.max_jobs_per_run,
            minimum_per_source=policy.source_minimum_per_run,
            max_share=policy.source_max_share,
        )
    except Exception:
        logger.exception("shortlist selection failed; falling back to stable global ranking")
        return ranked[: policy.max_jobs_per_run]


def _source_counts(items) -> dict[str, int]:
    return dict(
        sorted(
            Counter(
                metric_source_label(job.source)
                for _job_id, job, _score in items
            ).items()
        )
    )


def _format_source_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    return " ".join(f"{source}={count}" for source, count in counts.items())


def _market_counts(items) -> dict[str, int]:
    """Count `(job_id, job, score)` items by `job.market_id`, ignoring unattributed ones."""
    counts: dict[str, int] = {}
    for _job_id, job, _score in items:
        if not job.market_id:
            continue
        counts[job.market_id] = counts.get(job.market_id, 0) + 1
    return counts


def _bump_market_count(counts: dict[str, int], market_id: str | None) -> None:
    if not market_id:
        return
    counts[market_id] = counts.get(market_id, 0) + 1


def _record_decision(
    decision_counts: dict[str, dict[str, int]], key: str | None, decision: str | None
) -> None:
    """Tally one fresh evaluation outcome under its key (market or source) for per-key logging."""
    if not key or not decision:
        return
    bucket = decision_counts.setdefault(key, {})
    bucket[decision] = bucket.get(decision, 0) + 1


def _bump_source_count(counts: dict[str, int], source_label: str) -> None:
    counts[source_label] = counts.get(source_label, 0) + 1


def _raw_counts_by_source(per_source: dict[str, int]) -> dict[str, int]:
    """Bound each raw job.source string down to its metric label before summing."""
    counts: dict[str, int] = {}
    for source, count in per_source.items():
        label = metric_source_label(source)
        counts[label] = counts.get(label, 0) + count
    return counts


def _learned_ats_stats(sources) -> LearnedAtsStats:
    """Return the run's LearnedAtsSource stats, or zeros when none ran this run."""
    for source in sources:
        if isinstance(source, LearnedAtsSource):
            return source.stats
    return LearnedAtsStats()


def _aggregate_targeted_search_stats(
    sources,
) -> tuple[dict[str, int], dict[str, int], dict[str, int], dict[str, int]]:
    """Sum targeted-search query/result counters by market after discovery."""
    planned: dict[str, int] = {}
    attempted: dict[str, int] = {}
    succeeded: dict[str, int] = {}
    results: dict[str, int] = {}
    for source in sources:
        if not isinstance(source, TargetedSearchSource):
            continue
        for market_id, count in source.stats.planned_by_market.items():
            planned[market_id] = planned.get(market_id, 0) + count
        for market_id, count in source.stats.attempted_by_market.items():
            attempted[market_id] = attempted.get(market_id, 0) + count
        for market_id, count in source.stats.succeeded_by_market.items():
            succeeded[market_id] = succeeded.get(market_id, 0) + count
        for market_id, count in source.stats.results_by_market.items():
            results[market_id] = results.get(market_id, 0) + count
    return planned, attempted, succeeded, results


def _log_market_metrics(
    settings: Settings,
    discovery,
    search_planned: dict[str, int],
    search_attempted: dict[str, int],
    search_succeeded: dict[str, int],
    search_results: dict[str, int],
    selected_by_market: dict[str, int],
    decision_counts: dict[str, dict[str, int]],
    delivered_by_market: dict[str, int],
) -> None:
    """Log one structured line per configured market using completed run state."""
    for market in settings.policy.markets:
        market_id = market.id
        decisions = decision_counts.get(market_id, {})
        logger.info(
            "market=%s queries_planned=%s queries_attempted=%s queries_succeeded=%s "
            "raw=%s unique=%s rejected=%s eligible=%s selected=%s high_priority=%s "
            "package_match=%s possible_match=%s skip=%s blocked=%s delivered=%s "
            "search_results=%s reattributed=%s",
            market_id,
            search_planned.get(market_id, 0),
            search_attempted.get(market_id, 0),
            search_succeeded.get(market_id, 0),
            discovery.stats.raw_by_market.get(market_id, 0),
            discovery.stats.unique_by_market.get(market_id, 0),
            discovery.stats.rejected_by_market.get(market_id, 0),
            discovery.stats.eligible_by_market.get(market_id, 0),
            selected_by_market.get(market_id, 0),
            decisions.get("high_priority", 0),
            decisions.get("package_match", 0),
            decisions.get("possible_match", 0),
            decisions.get("skip", 0),
            decisions.get("blocked", 0),
            delivered_by_market.get(market_id, 0),
            search_results.get(market_id, 0),
            discovery.stats.reattributed_by_market.get(market_id, 0),
        )


def _log_source_metrics(
    discovery,
    raw_by_source: dict[str, int],
    selected_by_source: dict[str, int],
    decision_counts_by_source: dict[str, dict[str, int]],
    delivered_by_source: dict[str, int],
) -> None:
    """Log one structured source_quality line per bounded source label seen this run."""
    sources = (
        set(raw_by_source)
        | set(discovery.stats.unique_by_source)
        | set(discovery.stats.rejected_by_source)
        | set(discovery.stats.eligible_by_source)
        | set(selected_by_source)
        | set(decision_counts_by_source)
        | set(delivered_by_source)
    )
    for source in sorted(sources):
        decisions = decision_counts_by_source.get(source, {})
        logger.info(
            "source_quality source=%s raw=%s unique=%s rejected=%s eligible=%s "
            "selected=%s high_priority=%s package_match=%s possible_match=%s "
            "skip=%s blocked=%s delivered=%s",
            source,
            raw_by_source.get(source, 0),
            discovery.stats.unique_by_source.get(source, 0),
            discovery.stats.rejected_by_source.get(source, 0),
            discovery.stats.eligible_by_source.get(source, 0),
            selected_by_source.get(source, 0),
            decisions.get("high_priority", 0),
            decisions.get("package_match", 0),
            decisions.get("possible_match", 0),
            decisions.get("skip", 0),
            decisions.get("blocked", 0),
            delivered_by_source.get(source, 0),
        )


def _log_ats_registry_metrics(store: PostgresJobStore, discovery, learned_stats: LearnedAtsStats) -> None:
    """Log one final ats_registry line summarizing registry health this run."""
    rejected_boards = store.list_rejected_ats_boards()
    logger.info(
        "ats_registry total=%s discovered=%s scanned=%s successful=%s failed=%s "
        "jobs_raw=%s rejected=%s rejected_total=%s",
        store.count_ats_boards(),
        discovery.stats.ats_boards_discovered,
        learned_stats.boards_scanned,
        learned_stats.boards_successful,
        learned_stats.boards_failed,
        learned_stats.jobs_raw,
        learned_stats.boards_rejected,
        len(rejected_boards),
    )
    for entry in rejected_boards:
        logger.info(
            "ats_registry rejected board: %s:%s (%s)",
            entry.provider,
            entry.board_identifier,
            entry.rejected_reason,
        )


def _due_watch_state(
    store: PostgresJobStore,
) -> dict[str, tuple[str, str | None, int, str | None]]:
    """Snapshot due watch health so logs count persisted check outcomes only."""
    return {
        watch["id"]: (
            watch["company_name"],
            watch["last_verified_at"],
            watch["consecutive_failures"],
            watch["paused_until"],
        )
        for watch in store.list_due_company_watches(datetime.now(timezone.utc))
    }


def _watch_check_outcomes(
    store: PostgresJobStore,
    before: dict[str, tuple[str, str | None, int, str | None]],
) -> tuple[int, int]:
    """Return persisted successful/failed checks and newly applied pauses."""
    checks = 0
    paused = 0
    for _watch_id, (
        company_name,
        verified_at,
        failures,
        paused_until,
    ) in before.items():
        watch = store.get_company_watch(company_name)
        if watch is None:
            continue
        check_recorded = (
            watch["last_verified_at"] != verified_at
            or watch["consecutive_failures"] != failures
        )
        if check_recorded:
            checks += 1
        if (
            watch["paused_until"] is not None
            and watch["paused_until"] != paused_until
        ):
            paused += 1
    return checks, paused


def _watch_promotion_state(watch) -> tuple[object, ...] | None:
    """Return the persisted fields that constitute a meaningful promotion."""
    if watch is None:
        return None
    return (
        watch["promotion_source"],
        watch["careers_url"],
        watch["ats_provider"],
        watch["ats_identifier"],
        watch["confidence"],
    )


def cover_letter_output_dir(settings: Settings) -> Path:
    return Path(settings.output_dir) / "cover_letters"


def generate_cover_letter_on_demand(
    settings: Settings,
    job_id: str,
    *,
    store: PostgresJobStore,
    ai: AIProvider,
    telegram: TelegramClient,
) -> bool:
    """Generate (or resend) one job's cover letter on demand and deliver it.

    A repeat call for a job that already has a saved cover letter resends the
    existing PDF for free instead of calling the model again. If the requested
    job was merged away, all reads and writes follow its redirect to the
    surviving job. A missing job with no redirect returns False after telling
    the user that the job is no longer available.
    """
    job = store.get_job(job_id)
    resolved_job_id = job_id
    if job is None:
        survivor_id = store.resolve_merged_job_id(job_id)
        if survivor_id is None:
            logger.warning("no job or merge redirect found for job_id=%s", job_id)
            telegram.send_message(
                "This job is no longer available, so I can't generate a cover letter for it."
            )
            return False
        resolved_job_id = survivor_id
        job = store.get_job(resolved_job_id)

    evaluation = store.get_evaluation(resolved_job_id)
    if job is None or evaluation is None:
        logger.warning(
            "no job/evaluation found for resolved_job_id=%s; cannot generate cover letter",
            resolved_job_id,
        )
        return False

    material = store.get_material(resolved_job_id)
    if material is not None:
        text = material.cover_letter_text
    else:
        try:
            candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, ai, store)
            text = generate_cover_letter(
                job, evaluation, candidate_context, settings.cover_letter_template, ai, date.today()
            )
        except (AIBudgetExceeded, AIQuotaPaused):
            logger.warning("cover letter generation deferred by AI quota for job_id=%s", job_id)
            telegram.send_message(
                f"Couldn't generate a cover letter for {job.company} - {job.title} right now "
                "(AI quota limit) - try again later."
            )
            return False
        except Exception:
            logger.exception("cover letter generation failed for job_id=%s", job_id)
            telegram.send_message(
                f"Couldn't generate a cover letter for {job.company} - {job.title} - something went wrong."
            )
            return False
        store.save_material(
            resolved_job_id,
            Material(job_id=resolved_job_id, cover_letter_text=text),
        )

    out_dir = cover_letter_output_dir(settings)
    pdf_path = render_cover_letter_pdf(text, job.company, job.title, out_dir)
    caption = f"{job.company} - {job.title} - {evaluation.total_score} - {job.url}"
    document_id = telegram.send_document(pdf_path, caption)
    if document_id is not None:
        store.mark_delivered(resolved_job_id, "telegram_document", document_id)
    return document_id is not None


def should_run_scheduled(now: datetime, timezone: str, scheduled_hour: int) -> bool:
    local_hour = now.astimezone(ZoneInfo(timezone)).hour
    return local_hour == scheduled_hour


def _closed_job_ids(store, job_ids: list[str]) -> set[str]:
    """Which of `job_ids` sit on a posting found gone (#186); empty on failure.

    Failing to read this must not stop a run delivering. The worst case is one
    run treating a closed posting as open, which is what every run did before
    freshness existed; the delivery retry path filters closed postings in SQL
    regardless.
    """
    if not job_ids:
        return set()
    try:
        return store.closed_job_ids(job_ids)
    except Exception:
        logger.exception(
            "could not read which candidates' postings are closed; treating all as open"
        )
        return set()


def _extract_and_store_facets(
    job_id: str,
    job: Job,
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
) -> JobFacets | None:
    """Read one job's objective facets and store them, returning them.

    Returns None when the response could not be read as facets. Nothing is
    written in that case -- the job is left unenriched with no marker on it,
    so the next run that reaches it tries again. That is deliberate: an
    unreadable response says nothing about the posting, and recording it as a
    permanent property of the job would be a lie that never expires.

    A store write that does not land is counted but not fatal: the facets are
    still returned, so the run can score with them, and the job is extracted
    again next time because nothing was persisted.

    Rolling capacity and an exhausted platform allowance both propagate, and
    neither is counted here. What to do about capacity differs between the two
    callers -- a job waiting to be scored must wait for it, while the backfill
    pass, which nobody is waiting on, gives up its turn -- so that answer does
    not belong here. An exhausted allowance is not a failure at all: the
    posting was never read, nothing was spent, and no user was charged (#128),
    so counting it as an extraction failure would make a run that behaved
    correctly look unhealthy.
    """
    try:
        facets = extract_facets(PostingFacts.from_job(job), ai)
    except (AITemporaryCapacity, PlatformAllowanceExhausted):
        # Nothing was spent and nothing was charged (#128), so this is not a
        # read attempt and must not be recorded as one: the queue is welcome
        # to try this posting later in the same run if the allowance frees up.
        raise
    except FacetExtractionError:
        logger.exception("facet extraction response could not be parsed for job_id=%s", job_id)
        _note_facet_read_attempt(store, job_id)
        summary.facet_extraction_attempted += 1
        summary.facet_extraction_failed += 1
        summary.extraction_parse_failures += 1
        return None
    except Exception:
        logger.exception("facet extraction failed for job_id=%s", job_id)
        _note_facet_read_attempt(store, job_id)
        summary.facet_extraction_attempted += 1
        summary.facet_extraction_failed += 1
        return None

    _note_facet_read_attempt(store, job_id)
    summary.facet_extraction_attempted += 1
    try:
        store.save_job_facets(job_id, facets)
    except Exception:
        logger.exception("storing facets failed for job_id=%s", job_id)
        summary.facet_extraction_failed += 1
    return facets


def _note_facet_read_attempt(store: PostgresJobStore, job_id: str) -> None:
    """Tell the store this run has spent a read on `job_id`'s posting.

    Called on every path that actually made a provider call, and on none that
    did not: an exhausted allowance or a full rolling window spends nothing
    and is not an attempt. A read that *failed* is -- it leaves the posting
    uncurrent, and the extract_facets queue holds a message for it that would
    otherwise be drained moments later in this same run and buy the same
    non-answer again, which is what that stage's dead-letter-immediately rule
    exists to prevent (#185; only reachable once #179 made every run hold the
    ingestion connection).

    Never fatal. Failing to record it costs at most one duplicate read.
    """
    try:
        store.note_facet_read_attempt(job_id)
    except Exception:
        logger.exception("could not record the facet read attempt for job_id=%s", job_id)


def _extract_facets_for_run(
    run_candidates: list[tuple[str, Job | None]],
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
    *,
    limit: int,
) -> set[str]:
    """Read this run's shortlist before matching runs at all, then drain the queue.

    `run_candidates` are this run's shortlist (#188): `matching.match_jobs`
    cannot itself read a posting, only skip a row with no current facets, so
    a job discovered today has to be read here, before that call, to be
    scorable today.

    Only jobs with no current facets are extracted, so the ordinary steady
    state -- everything already read, nothing rewritten -- costs one store
    read and no provider call at all.

    `limit` is `max_jobs_per_run` -- the shortlist size the user's search
    profile already sets as "how much AI work one run may do". What the
    shortlist leaves unspent drains the `extract_facets` queue (#185) -- the
    durable backfill over postings the run's own crawl re-saw and found
    lacking current facets, enqueued as a side effect of every job persist
    (`PostgresJobStore` write methods call `_enqueue_needing_facets_for_job_ids`).
    There is no separate backfill step: the queue is it, and a failure
    draining it retries or dead-letters visibly instead of vanishing with a
    cancelled run.

    Nothing in here may end the run or change what it delivers.

    Returns the subset of `run_candidates` left needing facets specifically
    because the platform key's allowance was exhausted (#188) -- distinct
    from one that failed to parse, or was never reached at all for lack of
    `limit`. A caller reporting *why* a candidate still has no facets
    (`RunSummary.scoring_deferred_by_read_budget` vs
    `.scoring_skipped_without_facets`) reads this rather than guessing from
    the aggregate counters, which mix this call's work with the queue
    drain's.
    """
    ordered_candidates = list(dict.fromkeys(job_id for job_id, _job in run_candidates))
    known_jobs = {job_id: job for job_id, job in run_candidates if job is not None}

    try:
        needed = store.jobs_needing_facets(ordered_candidates)
    except Exception:
        # Facet work is optional; failing to work out what needs it must
        # never be a reason a run stops delivering.
        needed = set()
        logger.exception("could not determine which jobs need facet extraction")

    # Reuse is what the shortlist scored against *without paying*, mirroring
    # `_enrich_companies_for_run`'s identical measurement for companies: a
    # candidate this pre-pass did not have to read because an earlier run
    # (by this user or, since #175, any other) already read its posting.
    summary.facets_reused += len(set(ordered_candidates) - needed)

    remaining = limit
    quota_blocked = False
    budget_deferred_ids: set[str] = set()

    for job_id in ordered_candidates:
        if job_id not in needed:
            continue
        if quota_blocked or remaining <= 0:
            # Not reached this call. Once the platform allowance is known to
            # be exhausted, everything still needed behind it in rank order
            # is exhausted for the identical reason -- there is no point
            # attempting each to learn that again.
            if quota_blocked:
                budget_deferred_ids.add(job_id)
            continue
        job = known_jobs.get(job_id)
        if job is None:
            # A candidate can arrive as `(job_id, None)` -- its job is not
            # already in hand the way a freshly ranked shortlist entry is --
            # so this is the one path that pays a read for it.
            try:
                job = store.get_job(job_id)
            except Exception:
                logger.exception("could not load job_id=%s for facet extraction", job_id)
                continue
        if job is None:
            continue
        try:
            wait_out_capacity(
                lambda: _extract_and_store_facets(job_id, job, store, ai, summary),
                doing="reading the posting",
                job_id=job_id,
                max_waits=_READ_CAPACITY_WAITS,
            )
        except AITemporaryCapacity:
            # The platform key's rolling window stayed full even after
            # waiting it out a bounded number of times -- for this run that
            # is indistinguishable from having no allowance at all, so it is
            # treated the same way an exhausted allowance is: the posting
            # is not read today, and neither is anything ranked behind it.
            logger.info(
                "facet extraction gave up waiting on rolling capacity for job_id=%s",
                job_id,
            )
            quota_blocked = True
            budget_deferred_ids.add(job_id)
        except PlatformAllowanceExhausted as exc:
            logger.info("facet extraction deferred for job_id=%s: %s", job_id, exc)
            quota_blocked = True
            budget_deferred_ids.add(job_id)
        else:
            remaining -= 1

    drained: list = []
    if not quota_blocked and remaining > 0:
        try:
            drained = store.drain_extract_facets_queue(ai, limit=remaining)
        except Exception:
            logger.exception("draining the extract_facets queue failed")
        else:
            for outcome in drained:
                if outcome.skipped:
                    # A message for a posting something else had already read.
                    # Draining it cost nothing, so counting it would report a
                    # run reading one advertisement three times.
                    continue
                summary.facet_extraction_attempted += 1
                if outcome.failed:
                    summary.facet_extraction_failed += 1
                if outcome.parse_failure:
                    summary.extraction_parse_failures += 1

    logger.info(
        "facet_extraction candidates=%s needed=%s attempted=%s failed=%s "
        "limit=%s quota_blocked=%s parse_failures=%s drained=%s",
        len(ordered_candidates),
        len(needed),
        summary.facet_extraction_attempted,
        summary.facet_extraction_failed,
        limit,
        quota_blocked,
        summary.extraction_parse_failures,
        len(drained),
    )
    return budget_deferred_ids


#: How much of what a run has left company enrichment may take. A run's
#: `max_jobs_per_run` is already the answer to "how much AI work may one run
#: do", and companies are far fewer than postings -- a quarter of the
#: remainder is enough to drain a corpus of employers over consecutive runs.
#: Reusing that figure rather than adding a knob keeps this bounded without
#: asking an operator to size it.
_COMPANY_EXTRACTION_SHARE = 4


def _company_extraction_limit(remaining: int) -> int:
    """How many companies one run may read, from what its posting reads left.

    `remaining` is what is left of `max_jobs_per_run` once the run's posting
    extractions are subtracted, so a day that spends its whole allowance
    reading postings leaves this at zero -- which is the intended answer.
    Company facts must never be bought with a posting read the user's scoring
    needed.
    """
    return max(0, remaining // _COMPANY_EXTRACTION_SHARE)


def _companies_in_rank_order(
    ranked: list[tuple[str, Job, int]],
) -> dict[str, list[Job]]:
    """Group the run's candidates by employer, best-ranked employer first.

    The key is `normalize_company_name`, which is the engine's one notion of
    "the same employer" -- what `job_hunter_company_watch` keys on and what
    `canonical.py` compares -- and deliberately not `normalize_text`, under
    which "Acme Ltd" and "Acme" would be read once each.

    Grouping is what makes the cost claim true: ten postings from one employer
    are one entry here, so they cost one extraction between them rather than
    ten. Rank order decides which employers a bounded run gets to first, so
    the budget goes on the companies whose postings this user is most likely
    to be shown.
    """
    grouped: dict[str, list[Job]] = {}
    for _job_id, job, _score in ranked:
        identity = normalize_company_name(job.company or "")
        if not identity:
            continue
        grouped.setdefault(identity, []).append(job)
    return grouped


def _extract_one_company(
    identity: str,
    jobs: list[Job],
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
) -> CompanyFacets | None:
    """Read one employer's facts and store them, returning them.

    Returns None when the response could not be read. Nothing is written in
    that case, so the next run tries again -- an unreadable response says
    nothing about the company, and recording it as a permanent property would
    be a lie that only expires with the refresh interval.

    Rolling capacity and an exhausted platform allowance propagate untouched
    and are not counted: the company was never read, nothing was spent, and no
    user was charged. Counting either as a failure would make a run that
    behaved correctly look unhealthy.
    """
    try:
        evidence = CompanyEvidence.from_postings(jobs[0].company or identity, jobs)
        facets = extract_company_facets(evidence, ai)
    except (AITemporaryCapacity, PlatformAllowanceExhausted):
        raise
    except CompanyFacetExtractionError:
        logger.exception(
            "company extraction response could not be parsed for company=%r", identity
        )
        summary.company_extraction_attempted += 1
        summary.company_extraction_failed += 1
        return None
    except Exception:
        logger.exception("company extraction failed for company=%r", identity)
        summary.company_extraction_attempted += 1
        summary.company_extraction_failed += 1
        return None

    summary.company_extraction_attempted += 1
    try:
        store.save_company_facets(facets)
    except Exception:
        # Counted but not fatal, exactly as a failed facet write is: the facts
        # are still returned, so this run scores with them, and the company is
        # read again next run because nothing was persisted.
        logger.exception("storing company facets failed for company=%r", identity)
        summary.company_extraction_failed += 1
    return facets


def _enrich_companies_for_run(
    ranked: list[tuple[str, Job, int]],
    known: dict[str, CompanyFacets],
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
    *,
    limit: int,
) -> dict[str, CompanyFacets]:
    """Read the employers behind this run's candidates, once each.

    Runs after every posting read the run makes, and after scoring, so what it
    reads reaches the *next* run's ordering and prompts rather than this
    one's. That is deliberate -- see the call site -- and it is why this
    returns the map for tests to inspect while the pipeline ignores it: the
    value of this pass is the rows it leaves in the corpus, not anything it
    hands back today.

    An employer missing from the returned map is one nothing is established
    about, which every caller must treat as neutral rather than negative.

    Nothing in here may end the run or change what it delivers. Company facts
    are extra evidence; a run that reads none of them still scores and still
    delivers, with the company dimensions absent.

    The steady state costs two store reads for the whole run and no provider
    call at all -- the eligible set's stored facts, read by the caller before
    ranking, and one staleness check:
    companies are refreshed on a long interval (`COMPANY_FACET_REFRESH`), and
    an employer already read stays read across every posting it publishes in
    between. That ratio is the entire argument for this table, which is why
    the numbers behind it are logged rather than assumed (#120).
    """
    grouped = _companies_in_rank_order(ranked)
    summary.companies_seen = len(grouped)
    if not grouped:
        return {}

    # `known` was read for the eligible set before ranking, so it is not read
    # again here; it is copied because what this pass adds must not leak back
    # into the caller's view of what the corpus held before the run.
    known = {
        identity: facets for identity, facets in known.items() if identity in grouped
    }

    try:
        needed = store.companies_needing_facets(list(grouped))
    except Exception:
        # Optional work: failing to work out what needs reading must never be
        # a reason a run stops delivering.
        logger.exception("could not determine which companies need extraction")
        needed = set()

    # Reuse is what the run scored against *without paying*, so a stored row
    # this run is about to re-read is not reuse. Counting `known` whole would
    # report a run that refreshed all eight of its eight employers as a 100%
    # saving while it paid for every one of them -- and this counter exists
    # precisely so the amortisation claim is measured rather than believed.
    summary.company_facets_reused = len(set(known) - needed)

    attempted = 0
    quota_blocked = False
    capacity_blocked = False
    for identity, jobs in grouped.items():
        if attempted >= limit or identity not in needed:
            continue
        try:
            facets = _extract_one_company(identity, jobs, store, ai, summary)
        except AITemporaryCapacity:
            # The platform key's rolling window is full and nobody is waiting
            # on this pass, so it gives up its turn rather than holding the run
            # open. It never reached the provider, so it is not an attempt.
            #
            # The whole pass ends rather than trying the next employer.
            # Capacity does not clear inside this loop, so continuing would
            # walk every remaining company into the adapter's preflight -- a
            # pause read and two ledger reads each, hundreds of round trips on
            # a corpus of any size -- to be refused every time, and `attempted`
            # never rises so `limit` would not stop it. The employers not
            # reached keep their turn for the next run, which is what they
            # would have had anyway.
            logger.info(
                "company extraction stopped on rolling capacity at company=%r",
                identity,
            )
            capacity_blocked = True
            break
        except PlatformAllowanceExhausted as exc:
            # Not a failure, and logged so it cannot be read as one: the
            # platform key is spent or absent, no company was touched, and the
            # next run reads them.
            logger.info("company extraction deferred: %s", exc)
            quota_blocked = True
            break
        attempted += 1
        if facets is not None:
            known[identity] = facets

    logger.info(
        "company_extraction companies=%s postings=%s reused=%s needed=%s "
        "attempted=%s failed=%s limit=%s quota_blocked=%s capacity_blocked=%s",
        len(grouped),
        len(ranked),
        summary.company_facets_reused,
        len(needed),
        summary.company_extraction_attempted,
        summary.company_extraction_failed,
        limit,
        quota_blocked,
        capacity_blocked,
    )
    return known


def _format_ai_usage_log(summary: AIUsageSummary, account: str) -> str:
    """One structured log line per ledger at run completion.

    `account` names which key the numbers are about -- `user` for the one the
    person running this owns, `platform` for the shared key that funds
    objective extraction (#128). The two are reported side by side and never
    summed: they have separate ceilings, and an average of the two would hide
    either of them approaching its own.
    """
    purposes = ",".join(
        f"{purpose}:{summary.purpose_counts[purpose]}"
        for purpose in AI_PURPOSES
        if purpose in summary.purpose_counts
    )
    return (
        f"ai_usage account={account} "
        f"run_calls={summary.requests_today} "
        f"rpd_pct={summary.rpd_percent:.1f} "
        f"rpm_peak_pct={summary.rpm_peak_percent:.1f} "
        f"tpm_peak_pct={summary.tpm_peak_percent:.1f} "
        f"input={summary.input_tokens_today} "
        f"output={summary.output_tokens_today} "
        f"thinking={summary.thinking_tokens_today} "
        f"purposes={purposes}"
    )


def _build_navigation_session(items: list[DigestItem], now: datetime) -> NavigationSession:
    ordered = sorted(items, key=navigation_sort_key)
    return NavigationSession(
        session_id=secrets.token_urlsafe(12),
        cards=[
            NavigationCard(
                job_id=item.job_id,
                title=item.title,
                company=item.company,
                location=item.location,
                score=item.score,
                url=item.url,
                market_id=item.market_id,
                market_note=item.market_note,
                availability_note=item.availability_note,
                display_credit_text=item.display_credit_text,
                display_credit_url=item.display_credit_url,
            )
            for item in ordered
        ],
        telegram_message_id=None,
        created_at=now.isoformat(),
        expires_at=(now + _NAVIGATION_SESSION_TTL).isoformat(),
    )


def run_pipeline(
    settings: Settings,
    *,
    sources=None,
    store: PostgresJobStore,
    ai: AIProvider,
    usage: AIUsageTracker | None = None,
    platform_usage: AIUsageTracker | None = None,
    telegram: TelegramClient | None = None,
    http: HttpClient | None = None,
) -> RunSummary:
    """Run one discovery-to-delivery pass.

    `usage` is the ledger governing the user-funded half of `ai`'s calls,
    passed in rather than read off the provider: what a run spent is the
    ledger's question, not the text generation port's, and a run given no
    ledger simply reports no usage. `platform_usage` is the same for the
    platform key that funds shared extraction (#128); a deployment with no
    platform key has none, and does no extraction.
    """
    http = http or HttpClient()

    # Whether this run can add to the corpus at all (#179). Postings, their
    # facets, company facts and ATS board health have no user dimension and
    # are writable only by the privileged ingestion role, so a deployment
    # with no direct Postgres connection can score and deliver but cannot
    # discover or enrich. That mode is supported deliberately -- a run that
    # still delivers from existing postings is worth having during a
    # transient outage, and refusing to start would turn a degraded day into
    # an outage -- but it is a different thing from the pre-#179 fallback,
    # which was merely slower and converged on the same state. This one is
    # scoring-only over a corpus that can never update, so it is skipped
    # rather than attempted, and the run summary reports the zeroes.
    can_ingest = store.can_write_shared_rows
    if not can_ingest:
        logger.warning(
            "no direct Postgres connection: this run cannot write shared rows, so "
            "discovery and enrichment are skipped entirely. It will score and "
            "deliver from the postings that already exist, and the corpus will not "
            "change until SUPABASE_DB_URL is configured again."
        )

    if can_ingest:
        try:
            backfilled = store.backfill_ats_identity()
            if backfilled:
                logger.info("backfilled ATS identity on %s stored jobs", backfilled)
        except Exception:
            logger.exception("ATS identity backfill failed")

    try:
        sync_manual_watch_seeds(store, settings.policy.manual_company_watch)
    except Exception:
        logger.exception("manual company watch sync failed")

    search_breaker = CircuitBreaker(_SEARCH_FAILURE_THRESHOLD)
    # Brave source-discovery needs a `SupabaseClient` to build its persisted
    # budget (see `build_brave_budget`'s docstring). Rather than adding a
    # separate `supabase_client` parameter callers would have to remember to
    # pass, the client is derived from the `PostgresJobStore` this function
    # is already given.
    supabase_client = store.client
    brave_budget = build_brave_budget(settings, supabase_client)
    query_date = datetime.now(ZoneInfo(settings.timezone)).date()
    base_sources = (
        sources
        if sources is not None
        else build_sources(
            settings,
            http,
            store=store,
            search_breaker=search_breaker,
            query_date=query_date,
            brave_budget=brave_budget,
            supabase_client=supabase_client,
        )
    )
    sources = [
        *base_sources,
        GmailStagedSource(store),
        CompanyWatchSource(store, http),
    ]
    due_watches = _due_watch_state(store)
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: _targeted_canonical_candidates(
            http,
            job,
            search_breaker,
            settings.brave_search_api_key,
            brave_budget,
        ),
        watch_target=lambda company: _persisted_watch_target(store, company),
    )
    if telegram is None and not settings.dry_run:
        telegram = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id, http)

    summary = RunSummary()
    digest_items: list[DigestItem] = []
    try:
        candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, ai, store)
    except (AIBudgetExceeded, AIQuotaPaused):
        candidate_context = None
        logger.warning(
            "candidate context load deferred by AI quota; evaluation and cover letters "
            "will be deferred this run"
        )
    else:
        logger.info(
            "profile extraction: source=%s error=%s",
            candidate_context.source,
            candidate_context.load_error or "none",
        )
    preferences = candidate_context.preferences if candidate_context is not None else None
    if can_ingest:
        # Conditional requests and the cursors that drive them need the
        # privileged connection, and a run without one has no cursor table to
        # read. Passing None then is what keeps such a deployment on exactly
        # the behaviour it had before (issue #184).
        ingestion = getattr(store, "platform_ingestion", None)
        cursors = SourceCursorStore(ingestion) if ingestion is not None else None
        discovery = collect_candidates(
            sources,
            store,
            http,
            settings.policy,
            resolver=resolver,
            preferences=preferences,
            cursors=cursors,
        )
    else:
        # An empty crawl rather than a skipped one, so everything downstream
        # -- ranking, the shortlist, the digest, the counters -- runs its
        # normal path over nothing new. The pending-evaluation queue is what
        # this run still has to work with, and it is read below.
        discovery = DiscoveryResult(
            eligible=[], rediscovered_job_ids=[], stats=DiscoveryStats()
        )
    summary.postings_written = discovery.stats.postings_discovered
    search_planned, search_attempted, search_succeeded, search_results = (
        _aggregate_targeted_search_stats(base_sources)
    )
    watch_checks, watch_paused = _watch_check_outcomes(store, due_watches)
    summary.skipped += discovery.stats.prefilter_rejected + discovery.stats.profession_rejected

    # A posting a freshness re-check found gone (#186) is neither scored nor
    # delivered. `job_hunter_match_jobs` excludes a closed posting outright
    # (#188) -- the caller can no longer be trusted to filter it out, now
    # that matching reads the whole corpus rather than a pre-filtered
    # `eligible` list -- but discovery's own candidate set still reads this
    # once, so a dead posting is never selected, ranked, or sent to the
    # facet pre-pass below either.
    closed_job_ids = _closed_job_ids(
        store,
        [job_id for job_id, _job in discovery.eligible]
        + list(discovery.rediscovered_job_ids),
    )
    eligible = [
        (job_id, job) for job_id, job in discovery.eligible if job_id not in closed_job_ids
    ]
    # `Job.availability` (and any other field the canonical resolver sets
    # in-memory this run) is not persisted -- it exists only on the object
    # discovery just built (see models.py's `availability` docstring). A
    # matched job that came out of this run's own discovery must keep using
    # that object rather than a plain `store.get_job` re-fetch, or the
    # availability warning below silently goes dark for every fresh
    # candidate. A job `matching.match_jobs` surfaces from outside this
    # run's discovery (a retry, a reused row) was never resolved this run
    # either way, so `store.get_job` is the correct, unchanged source for it.
    eligible_jobs_by_id = {job_id: job for job_id, job in eligible}
    if closed_job_ids:
        logger.info(
            "closed postings skipped this run: %s (found gone by a freshness re-check)",
            len(closed_job_ids),
        )

    # What the corpus already knows about the employers behind this run's
    # candidates, read once for the whole run (#198). Ordering consults it
    # before anything is read, so a run pays for nothing to have the company
    # dimensions count; the enrichment pass below then reads the employers
    # nobody has got to yet, in the order the ranking put them, and those
    # answers reach this run's scoring and the next run's ordering.
    try:
        company_facets = store.get_company_facets_bulk(
            [job.company for _job_id, job in eligible]
        )
    except Exception:
        # Company facts are extra evidence. Failing to read them must never
        # be a reason a run stops ranking or delivering.
        logger.exception("could not read stored company facets for the eligible set")
        company_facets = {}

    # This SQL call is a second one from what `matching.match_jobs` makes
    # below for the actual scoring decision (#188) -- this one exists only to
    # order and diversity-cap *today's newly discovered* candidates for the
    # `eligible sources:`/`selected sources:` log lines, which describe what
    # this run's crawl contributed. Neither `ranked` nor `selected` bounds
    # what gets scored or delivered any more; that is `matching.match_jobs`'s
    # decision alone, over the whole corpus. `sql_match_by_job_id` is empty
    # whenever there is no profile to rank against or the call failed, and
    # `ranked` falls back to the pre-#187 Python ranking for the log lines
    # only.
    sql_match_by_job_id: dict[str, dict[str, Any]] = {}
    if preferences is not None:
        try:
            sql_match_by_job_id = {
                row["job_id"]: row
                for row in store.match_jobs(
                    preferred_roles=preferences.preferred_roles,
                    preferred_seniority=preferences.preferred_seniority,
                    must_have_signals=preferences.must_have_signals,
                    nice_to_have_signals=preferences.nice_to_have_signals,
                    preferred_locations=preferences.preferred_locations,
                    avoid_signals=preferences.avoid_signals,
                )
            }
        except Exception:
            logger.exception("SQL ranking failed; falling back to the Python ranking")
            sql_match_by_job_id = {}

    if sql_match_by_job_id:
        job_by_id = {job_id: job for job_id, job in eligible}
        # `sql_match_by_job_id` iterates in the SQL ranking's own order
        # (score desc, company, title, job_id); filtering a totally ordered
        # sequence down to this run's eligible ids keeps it ordered, so no
        # re-sort is needed here.
        ranked = [
            (row["job_id"], job_by_id[row["job_id"]], row["score"])
            for row in sql_match_by_job_id.values()
            if row["job_id"] in job_by_id
        ]
    else:
        ranked = rank_jobs(eligible, settings.policy, preferences, company_facets)
    selected = _select_candidates(ranked, settings.policy, preferences)
    eligible_source_counts = _source_counts(ranked)
    selected_source_counts = _source_counts(selected)
    selected_by_market = _market_counts(selected)
    decision_counts: dict[str, dict[str, int]] = {}
    decision_counts_by_source: dict[str, dict[str, int]] = {}
    deferred_by_budget = max(0, len(ranked) - len(selected))
    # The user's daily offer limit is the run's delivery budget. Walking
    # `matching.match_jobs`'s ordered results and stopping once it is met
    # needs no assumed ratio between candidates evaluated and offers
    # delivered, and keeps adapting when that ratio moves.
    offer_limit = settings.policy.daily_offer_limit
    delivered_offers = 0
    cap_deferred_count = 0
    logger.info(
        "discovery: raw=%s unique=%s newly_discovered=%s prefilter_rejected=%s profession_rejected=%s eligible=%s selected=%s deferred_by_budget=%s canonical_network_attempts=%s sources=%s",
        discovery.stats.raw,
        discovery.stats.unique,
        discovery.stats.newly_discovered,
        discovery.stats.prefilter_rejected,
        discovery.stats.profession_rejected,
        discovery.stats.eligible,
        len(selected),
        deferred_by_budget,
        discovery.stats.canonical_network_attempts,
        len(eligible_source_counts),
    )
    logger.info("eligible sources: %s", _format_source_counts(eligible_source_counts))
    logger.info("selected sources: %s", _format_source_counts(selected_source_counts))

    companies_promoted = 0
    deferred_by_read_budget_ids: set[str] = set()

    # The facet pre-pass, before matching runs at all (#188). `matching
    # .match_jobs` cannot itself read a posting -- it can only skip a row
    # with no current facets -- so a job discovered today has to be read
    # here to be scorable today; a job the durable `extract_facets` queue
    # would otherwise reach eventually gets no priority boost from being
    # unread, exactly as before. Skipped entirely without the privileged
    # connection (#179): facets are a shared row, so the call would be paid
    # for and then refused.
    if store.can_write_shared_rows:
        shortlist_job_ids = [job_id for job_id, _job, _score in selected]
        budget_deferred_ids = _extract_facets_for_run(
            [(job_id, job) for job_id, job, _score in selected],
            store,
            ai,
            summary,
            limit=settings.policy.max_jobs_per_run,
        )
        # Of this run's shortlist, whatever the pre-pass still could not read
        # splits into "the platform key ran out" and "everything else" (a
        # parse failure, or never reached at all) -- the same two health
        # signals `_evaluate_and_deliver_one_job` used to report per job,
        # now read back from the pre-pass's own accounting instead of
        # guessed from the aggregate counters it shares with the queue
        # drain that runs after it.
        try:
            still_needing_facets = store.jobs_needing_facets(shortlist_job_ids)
        except Exception:
            logger.exception("could not determine which shortlist jobs still need reading")
            still_needing_facets = set()
        deferred_by_read_budget_ids = still_needing_facets & budget_deferred_ids
        summary.scoring_deferred_by_read_budget += len(deferred_by_read_budget_ids)

    # The one matching operation decides which jobs, in what order -- scored,
    # facet-blocked, or reused from an earlier run -- up to this run's AI
    # budget (#188). Skipped when the candidate profile itself could not
    # load: nothing can be scored without it, and it is what the operation
    # ranks against. A failure in the call itself must not cost the run its
    # digest (#145) -- discovery and enrichment already happened -- so it is
    # logged and treated as "nothing new to match this run" rather than
    # allowed to propagate.
    match_result = MatchResult(
        matched=[], failed_job_ids=[], parse_failure_job_ids=[], skipped_without_facets_job_ids=[]
    )
    if candidate_context is not None:
        try:
            match_result = match_jobs(
                store,
                ai,
                settings.policy,
                candidate_context,
                limit=settings.policy.max_jobs_per_run,
            )
        except Exception:
            logger.exception("matching failed; delivering nothing new this run")
    else:
        logger.warning(
            "candidate context unavailable this run; scoring skipped entirely"
        )
    summary.errors += len(match_result.failed_job_ids)
    summary.evaluation_attempted += len(match_result.failed_job_ids)
    summary.scoring_parse_failures += len(match_result.parse_failure_job_ids)
    # `deferred_by_read_budget_ids` is excluded: a job the platform key
    # could not read this run already has its own, more specific counter
    # above, and every one of them also lacks facets in `match_jobs`'s own
    # eyes -- counting it here too would report the same job under both
    # health signals instead of the one that actually explains it.
    summary.scoring_skipped_without_facets += len(
        set(match_result.skipped_without_facets_job_ids) - deferred_by_read_budget_ids
    )

    # Company enrichment runs after matching, on what the facet pre-pass left
    # of the shared budget -- see the module-level rationale on
    # `_enrich_companies_for_run` for why company reads never take a posting
    # read a user's scoring needed. It also has to run after `match_jobs`
    # itself: what this pass reads must reach the *next* run's ordering and
    # prompts, not this one's -- an employer's facts appearing mid-run would
    # make `matching.match_jobs`'s own company lookup, made moments earlier
    # for the very same run, silently inconsistent with what enrichment just
    # wrote.
    if store.can_write_shared_rows:
        _enrich_companies_for_run(
            ranked,
            company_facets,
            store,
            ai,
            summary,
            limit=_company_extraction_limit(
                settings.policy.max_jobs_per_run - summary.facet_extraction_attempted
            ),
        )

    for matched_job in match_result.matched:
        job_id = matched_job.job_id
        evaluation = matched_job.evaluation
        already_delivered = False

        try:
            if matched_job.fresh:
                # A job selected can have been merged away before its evaluation
                # is written; `save_evaluation` follows the redirect and returns
                # the id it actually wrote against (#145).
                written_job_id = store.save_evaluation(job_id, evaluation)
                if written_job_id != job_id:
                    job_id = written_job_id
                    # A duplicate can merge into a job already delivered: the
                    # merge moves the deliveries onto the survivor, and
                    # `matching.match_jobs`'s own already-delivered check, made
                    # against the duplicate's id before the merge happened,
                    # never saw it.
                    already_delivered = store.has_delivery(job_id, "telegram_message")

                if matched_job.scored:
                    summary.evaluation_attempted += 1
                    summary.evaluated += 1
                else:
                    summary.blocked_by_facets += 1

                job = eligible_jobs_by_id.get(job_id) or store.get_job(job_id)
                if job is None:
                    continue

                try:
                    promotion_before = _watch_promotion_state(store.get_company_watch(job.company))
                    promoted_watch_id = promote_company(
                        store,
                        job_id=job_id,
                        job=job,
                        evaluation=evaluation,
                        package_threshold=settings.policy.thresholds.get("package", 75),
                    )
                    promotion_after = _watch_promotion_state(store.get_company_watch(job.company))
                    if promoted_watch_id is not None and promotion_after != promotion_before:
                        companies_promoted += 1
                except Exception:
                    logger.exception("company watch promotion failed for job_id=%s", job_id)
                    summary.errors += 1

                if evaluation.total_score != evaluation.raw_model_score:
                    logger.info(
                        "capped match score job_id=%s raw=%s effective=%s decision=%s",
                        job_id, evaluation.raw_model_score, evaluation.total_score, evaluation.decision,
                    )

                _record_decision(decision_counts, job.market_id, evaluation.decision)
                _record_decision(
                    decision_counts_by_source, metric_source_label(job.source), evaluation.decision
                )

                if already_delivered:
                    logger.info(
                        "job_id=%s was merged into a job already delivered; not offering it twice",
                        job_id,
                    )
                    continue
            else:
                # Reused: already evaluated on an earlier run, not yet
                # delivered. Nothing to persist or promote -- that already
                # happened the run that first decided it.
                job = eligible_jobs_by_id.get(job_id) or store.get_job(job_id)
                if job is None:
                    continue

            if evaluation.total_score < settings.policy.match_score_floor:
                # The floor withholds every tier, warnings included -- but only
                # an offer the floor took away signals that the floor is set too
                # high. A reused row was already counted the run it was first
                # decided, so only a fresh decision touches these counters.
                if matched_job.fresh:
                    if evaluation.decision in _OFFER_DECISIONS:
                        summary.withheld_by_score_floor += 1
                    else:
                        summary.skipped += 1
                continue

            if evaluation.decision in _OFFER_DECISIONS:
                # The daily offer limit is the run's delivery *pacing*, not just
                # this call's scoring budget -- it must bound a reused row too.
                # Under the pre-#188 design a cap-deferred candidate was simply
                # never evaluated, so retries were always a small, incidental
                # backlog (a failed send, a rediscovery). Since #188 scoring no
                # longer stops at the cap (`matching.match_jobs` scores the whole
                # ranked pool), the "not yet delivered" backlog routinely holds
                # everything the cap withheld today -- and without this check it
                # would all flood out uncapped the next call, defeating the
                # cap's entire purpose. `delivered_offers` counts fresh and
                # reused alike so the pacing is real either way.
                if delivered_offers >= offer_limit:
                    cap_deferred_count += 1
                    continue

            try:
                credit = store.posting_display_credit(matched_job.posting_id)
            except Exception:
                # A source's attribution obligation is metadata about how the
                # digest item is *displayed*, not whether it belongs there --
                # a transient RPC error here must not cost an already-scored,
                # already-persisted job its place in today's digest the way
                # letting it propagate to the loop's own `except Exception`
                # would (and would misreport a scoring success as a "job
                # handling failed").
                logger.exception(
                    "could not read display credit for posting_id=%s", matched_job.posting_id
                )
                credit = None
            item = DigestItem(
                job_id=job_id,
                company=job.company,
                title=job.title,
                score=evaluation.total_score,
                decision=evaluation.decision,
                url=job.url,
                hard_blockers=evaluation.hard_blockers,
                location=job.location,
                market_id=evaluation.market_id or job.market_id or "",
                market_note=evaluation.location_note or "",
                availability_note=_AVAILABILITY_WARNING if job.availability == UNVERIFIED else "",
                display_credit_text=(credit or {}).get("text", ""),
                display_credit_url=(credit or {}).get("link_url", ""),
            )
            digest_items.append(item)

            if evaluation.decision in _OFFER_DECISIONS:
                delivered_offers += 1
            if matched_job.fresh:
                if evaluation.decision in _READY_DECISIONS:
                    summary.ready_to_apply += 1
                elif evaluation.decision == "possible_match":
                    summary.possible_matches += 1
                else:
                    summary.skipped += 1
        except Exception:
            # No single job may end a run (#145): everything above --
            # persisting the evaluation, promoting the company, building the
            # digest item -- must be contained the same way the model call
            # inside `matching.match_jobs` already is, or one bad row loses
            # every remaining candidate and the digest with them.
            logger.exception("job handling failed for job_id=%s", matched_job.job_id)
            summary.errors += 1
            continue

    logger.info(
        "evaluation_capacity selected=%s evaluated=%s blocked_by_facets=%s "
        "deferred_by_budget=%s "
        "daily_offer_limit=%s delivered_offers=%s "
        "deferred_by_offer_cap=%s match_score_floor=%s "
        "withheld_by_score_floor=%s skipped_without_facets=%s "
        "deferred_by_read_budget=%s parse_failures=%s",
        len(selected),
        summary.evaluated,
        summary.blocked_by_facets,
        deferred_by_budget,
        offer_limit,
        delivered_offers,
        cap_deferred_count,
        settings.policy.match_score_floor,
        summary.withheld_by_score_floor,
        summary.scoring_skipped_without_facets,
        summary.scoring_deferred_by_read_budget,
        summary.scoring_parse_failures,
    )

    now_for_usage = datetime.now(timezone.utc)
    usage_summary = usage.snapshot(now_for_usage) if usage is not None else None
    if usage_summary is not None:
        logger.info(_format_ai_usage_log(usage_summary, "user"))
    if platform_usage is not None:
        # Reported, never warned about over Telegram: an exhausted platform
        # key is the operator's problem and there is nothing the person
        # reading the digest could do about it. The user-funded warning below
        # stays about the key that user actually holds.
        #
        # Contained, because this is a *report* and the digest has not been
        # sent yet. It reads two tables that a deployment which has not run
        # the #128 migration does not have, and losing a whole run's delivered
        # work to a failed log line would be the worst possible trade.
        try:
            logger.info(
                _format_ai_usage_log(platform_usage.snapshot(now_for_usage), "platform")
            )
        except Exception:
            logger.exception("could not read platform AI usage for this run")

    delivered_by_market: dict[str, int] = {}
    delivered_by_source: dict[str, int] = {}
    if not settings.dry_run:
        deliverable_items = select_deliverable_items(digest_items)
        interactive_sender = getattr(telegram, "send_job_card", None)
        supports_navigation = callable(interactive_sender)

        if deliverable_items and not supports_navigation:
            message_id = telegram.send_message(build_digest(deliverable_items))
            if message_id is not None:
                for item in deliverable_items:
                    # The digest is already sent. Failing to record one item's
                    # delivery must not lose the record of the others (#145).
                    try:
                        delivered_id = store.mark_delivered(
                            item.job_id, "telegram_message", message_id
                        )
                        _bump_market_count(delivered_by_market, item.market_id)
                        delivered_job = store.get_job(delivered_id)
                        if delivered_job is not None:
                            _bump_source_count(
                                delivered_by_source, metric_source_label(delivered_job.source)
                            )
                    except Exception:
                        logger.exception(
                            "recording delivery failed for job_id=%s company=%s",
                            item.job_id,
                            item.company,
                        )
                        summary.errors += 1

        pending_reviews = store.pending_review_events()
        if pending_reviews:
            review_items = [
                ReviewItem(
                    event_id=row["id"],
                    company=row["company"],
                    role_title=row["role_title"],
                    occurred_at=row["occurred_at"],
                    subject=row["subject"],
                    rationale=row["rationale"],
                    event_type=row["event_type"],
                    source_message_id=row["source_message_id"],
                    source_thread_id=row["source_thread_id"],
                )
                for row in pending_reviews
            ]
            for review_text, event_ids in build_gmail_review_digest_chunks(review_items):
                review_message_id = telegram.send_message(review_text)
                if review_message_id is None:
                    break
                store.mark_review_delivered(event_ids, review_message_id)

        if usage_summary is not None:
            warning = build_ai_pause_warning(usage_summary)
            if warning is not None:
                try:
                    telegram.send_message(warning)
                except Exception:
                    logger.exception("failed to send AI pause warning to Telegram")

        if deliverable_items and supports_navigation:
            now = datetime.now(timezone.utc)
            store.prune_navigation_sessions(now.isoformat())
            session = _build_navigation_session(deliverable_items, now)
            store.create_navigation_session(session)
            text, keyboard = build_navigation_card(
                session.cards[0],
                session.session_id,
                0,
                len(session.cards),
            )
            message_id = interactive_sender(text, keyboard)
            if message_id is not None:
                store.attach_navigation_message_id(session.session_id, str(message_id))
                for card in session.cards:
                    # Same containment as the plain-digest branch above.
                    try:
                        delivered_id = store.mark_delivered(
                            card.job_id, "telegram_message", str(message_id)
                        )
                        _bump_market_count(delivered_by_market, card.market_id)
                        delivered_job = store.get_job(delivered_id)
                        if delivered_job is not None:
                            _bump_source_count(
                                delivered_by_source, metric_source_label(delivered_job.source)
                            )
                    except Exception:
                        logger.exception(
                            "recording delivery failed for job_id=%s company=%s",
                            card.job_id,
                            card.company,
                        )
                        summary.errors += 1

    _log_market_metrics(
        settings,
        discovery,
        search_planned,
        search_attempted,
        search_succeeded,
        search_results,
        selected_by_market,
        decision_counts,
        delivered_by_market,
    )
    _log_source_metrics(
        discovery,
        _raw_counts_by_source(discovery.stats.per_source),
        selected_source_counts,
        decision_counts_by_source,
        delivered_by_source,
    )
    # The persisted counterpart to the log line above. Without it nothing
    # writes job_hunter_source_crawls -- the crawl_source queue has no
    # consumer yet -- and the scheduler would band every source on an empty
    # history (issue #184).
    ingestion = getattr(store, "platform_ingestion", None)
    if ingestion is not None and discovery.stats.source_outcomes:
        record_run_crawls(
            ingestion,
            discovery.stats,
            discovery.stats.keys_by_label,
            novelty_measured=discovery.stats.novelty_measured,
        )
    _log_ats_registry_metrics(store, discovery, _learned_ats_stats(base_sources))

    logger.info(
        "company watch outcomes: companies_promoted=%s watch_checks=%s watch_paused=%s",
        companies_promoted,
        watch_checks,
        watch_paused,
    )

    return summary

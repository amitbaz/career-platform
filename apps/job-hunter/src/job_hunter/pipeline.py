from __future__ import annotations

import logging
import secrets
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from job_hunter import content_confidence
from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.availability import UNVERIFIED
from job_hunter.candidate_context import get_candidate_context
from job_hunter.canonical import CanonicalResolver, parse_supported_ats_url
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.cover_letter import generate_cover_letter
from job_hunter.discovery import collect_candidates, metric_source_label
from job_hunter.evaluation import evaluate_job
from job_hunter.facets import PostingFacts, extract_facets
from job_hunter.gemini import GeminiClient
from job_hunter.gemini_usage import (
    GEMINI_PURPOSES,
    GeminiBudgetExceeded,
    GeminiQuotaPaused,
    GeminiTemporaryCapacity,
)
from job_hunter.hard_blockers import (
    BlockingThresholds,
    blocked_evaluation,
    hard_blockers_from_facets,
)
from job_hunter.http import HttpClient
from job_hunter.job_identity import normalize_company_name
from job_hunter.market_policy import market_by_id
from job_hunter.models import (
    AtsReference,
    CandidateContext,
    DigestItem,
    GeminiUsageSummary,
    Job,
    Material,
    NavigationCard,
    NavigationSession,
    ReviewItem,
    RunSummary,
    Settings,
)
from job_hunter.pdf import render_cover_letter_pdf
from job_hunter.ranking import rank_jobs, select_diverse_candidates
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
    build_gemini_pause_warning,
    build_gmail_review_digest_chunks,
    select_deliverable_items,
)
from job_hunter.telegram_navigation import build_navigation_card, navigation_sort_key
from job_hunter.watchlist import promote_company, sync_manual_watch_seeds

logger = logging.getLogger(__name__)

_AVAILABILITY_WARNING = "⚠️ Availability not verified - check the posting before applying"
_READY_DECISIONS = {"high_priority", "package_match"}
#: The outcomes the daily offer limit counts: the offers themselves, ready to
#: apply or possible. A `skip` costs a Gemini call but no delivery budget, and
#: so does a `blocked` job -- it reaches Telegram only under "Needs review /
#: blockers", which is a warning about a job, not an offer to act on.
_OFFER_DECISIONS = _READY_DECISIONS | {"possible_match"}
_NAVIGATION_SESSION_TTL = timedelta(days=30)
_SUPPORTED_WATCH_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})
_SEARCH_FAILURE_THRESHOLD = 5
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
    gemini: GeminiClient,
    telegram: TelegramClient,
) -> bool:
    """Generate (or resend) one job's cover letter on demand and deliver it.

    A repeat call for a job that already has a saved cover letter resends the
    existing PDF for free instead of calling Gemini again. If the requested
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
            candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, gemini, store)
            text = generate_cover_letter(
                job, evaluation, candidate_context, settings.cover_letter_template, gemini, date.today()
            )
        except (GeminiBudgetExceeded, GeminiQuotaPaused):
            logger.warning("cover letter generation deferred by Gemini quota for job_id=%s", job_id)
            telegram.send_message(
                f"Couldn't generate a cover letter for {job.company} - {job.title} right now "
                "(Gemini quota limit) - try again later."
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


def _requeue_pending_delivery(
    job_id: str,
    store: PostgresJobStore,
    digest_items: list[DigestItem],
    match_score_floor: int,
) -> None:
    """Re-add a rediscovered job's digest entry if it was never delivered.

    `match_score_floor` is the profile's inclusive delivery floor. A retry
    is held to the same floor as a first delivery, so lowering the floor
    releases previously withheld jobs and raising it withdraws them,
    rather than letting the retry path deliver what a fresh run would not.
    """
    evaluation = store.get_evaluation(job_id)
    if evaluation is None or evaluation.total_score < match_score_floor:
        return

    job = store.get_job(job_id)
    if job is None:
        return

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
    )

    if not store.has_delivery(job_id, "telegram_message"):
        digest_items.append(item)


#: What one facet-extraction attempt did. `skipped` covers an attempt that
#: never reached the provider, so it costs no budget and consumes no slot;
#: `quota_blocked` ends facet work for the run.
_FACET_EXTRACTED = "extracted"
_FACET_SKIPPED = "skipped"
_FACET_QUOTA_BLOCKED = "quota_blocked"


def _extract_job_facets(
    job_id: str,
    job: Job,
    store: PostgresJobStore,
    gemini: GeminiClient,
    summary: RunSummary,
) -> str:
    """Read and store one job's objective facets. One step of the run's pass.

    Nothing here may end the run, and nothing here may change what the run
    delivers: the facets are written and never read back into scoring. A
    failure of any kind -- an unusable response, a store write that did not
    land -- leaves the job unenriched with no marker on it, so the next run
    that reaches the job tries again. That is deliberate: an unreadable
    response says nothing about the posting, and recording it as a permanent
    property of the job would be a lie that never expires.
    """
    try:
        facets = extract_facets(PostingFacts.from_job(job), gemini)
    except GeminiTemporaryCapacity:
        # `GeminiClient._preflight_with_pacing` already slept out one rolling
        # window and re-checked, so reaching this means capacity is still
        # full. Evaluation keeps waiting because a user is waiting on the
        # answer; facets are shared work nobody is waiting on, so this job
        # keeps its turn for the next run. It never reached the provider, so
        # it is not an attempt and must not consume a slot in the run's
        # bounded budget -- otherwise a run under sustained rolling pressure
        # would burn its whole allowance extracting nothing.
        logger.info("facet extraction skipped on rolling capacity for job_id=%s", job_id)
        return _FACET_SKIPPED
    except (GeminiBudgetExceeded, GeminiQuotaPaused):
        logger.info("facet extraction deferred by provider quota for job_id=%s", job_id)
        return _FACET_QUOTA_BLOCKED
    except Exception:
        logger.exception("facet extraction failed for job_id=%s", job_id)
        summary.facet_extraction_attempted += 1
        summary.facet_extraction_failed += 1
        return _FACET_EXTRACTED

    summary.facet_extraction_attempted += 1
    try:
        store.save_job_facets(job_id, facets)
    except Exception:
        logger.exception("storing facets failed for job_id=%s", job_id)
        summary.facet_extraction_failed += 1
    return _FACET_EXTRACTED


def _extract_facets_for_run(
    run_candidates: list[tuple[str, Job | None]],
    backfill_ids: list[str],
    store: PostgresJobStore,
    gemini: GeminiClient,
    summary: RunSummary,
    *,
    limit: int,
) -> None:
    """Give this run's jobs their objective facets, and backfill the rest.

    `run_candidates` are the jobs this run put through evaluation; `backfill_ids`
    are jobs it rediscovered, which were evaluated on an earlier run and so
    never re-enter the shortlist. Both survived the non-AI filters. Ids may
    repeat within or across the two; the first occurrence wins.

    Only jobs with no current facets are extracted, so the ordinary steady
    state -- everything already read, nothing rewritten -- costs one pair of
    store reads and no provider call at all.

    `limit` is the shortlist size the user's search profile already sets as
    "how much AI work one run may do". Reusing it rather than adding a knob
    keeps the work bounded without asking an operator to size it. Half of it
    is **reserved for the backfill**: spending the budget in priority order
    alone would mean a day that discovers a full shortlist leaves nothing for
    the corpus, and the backfill would only ever progress on quiet days --
    which is not a backfill. The reserve is what makes the existing corpus
    drain over consecutive runs whether or not discovery is productive.

    Nothing in here may end the run or change what it delivers.
    """
    ordered_candidates = list(dict.fromkeys(job_id for job_id, _job in run_candidates))
    seen = set(ordered_candidates)
    ordered_backfill = [
        job_id for job_id in dict.fromkeys(backfill_ids) if job_id not in seen
    ]
    known_jobs = {job_id: job for job_id, job in run_candidates if job is not None}

    try:
        needed = store.jobs_needing_facets(ordered_candidates + ordered_backfill)
    except Exception:
        # Facet work is optional; failing to work out what needs it must
        # never be a reason a run stops delivering.
        logger.exception("could not determine which jobs need facet extraction")
        return

    backfill_reserve = limit // 2
    remaining = limit
    skipped = 0
    quota_blocked = False

    def extract_up_to(ids: list[str], allowance: int) -> None:
        nonlocal remaining, skipped, quota_blocked
        spent = 0
        for job_id in ids:
            if quota_blocked or spent >= allowance or remaining <= 0:
                return
            if job_id not in needed:
                continue
            job = known_jobs.get(job_id)
            if job is None:
                # Only the backfill pays this read: the shortlist arrives with
                # its jobs already in hand.
                try:
                    job = store.get_job(job_id)
                except Exception:
                    logger.exception(
                        "could not load job_id=%s for facet extraction", job_id
                    )
                    continue
            if job is None:
                continue
            outcome = _extract_job_facets(job_id, job, store, gemini, summary)
            if outcome == _FACET_QUOTA_BLOCKED:
                quota_blocked = True
            elif outcome == _FACET_SKIPPED:
                skipped += 1
            else:
                spent += 1
                remaining -= 1

    extract_up_to(ordered_candidates, limit - backfill_reserve)
    # Whatever the shortlist left unspent flows to the backfill, so a quiet
    # day drains the corpus faster rather than wasting the allowance.
    extract_up_to(ordered_backfill, remaining)

    logger.info(
        "facet_extraction candidates=%s backfill=%s needed=%s attempted=%s "
        "failed=%s skipped_by_capacity=%s limit=%s backfill_reserve=%s "
        "quota_blocked=%s",
        len(ordered_candidates),
        len(ordered_backfill),
        len(needed),
        summary.facet_extraction_attempted,
        summary.facet_extraction_failed,
        skipped,
        limit,
        backfill_reserve,
        quota_blocked,
    )


def _evaluate_and_deliver_job(
    job_id: str,
    job: Job,
    candidate_context: CandidateContext,
    settings: Settings,
    store: PostgresJobStore,
    gemini: GeminiClient,
    digest_items: list[DigestItem],
    summary: RunSummary,
    queued_job_ids: set[str],
    facet_ready_ids: set[str],
) -> tuple[bool, bool, str | None, bool, bool]:
    """Evaluate one job and add it to the digest, containing its failures.

    No single job may end a run. The inner function already catches a failed
    Gemini call, but everything after it -- persisting the evaluation,
    promoting the company, building the digest item -- could still raise out
    of the evaluation loop and kill the process, discarding every remaining
    candidate and the digest with them (#145). This wrapper is the guarantee
    that the invariant holds for the whole per-job unit of work and not just
    the model call: one job's failure is counted in `summary.errors`, logged
    with enough identity to trace it, and the run continues.
    """
    try:
        return _evaluate_and_deliver_one_job(
            job_id,
            job,
            candidate_context,
            settings,
            store,
            gemini,
            digest_items,
            summary,
            queued_job_ids,
            facet_ready_ids,
        )
    except Exception:
        logger.exception(
            "job handling failed for job_id=%s source=%s company=%s",
            job_id,
            metric_source_label(job.source),
            job.company,
        )
        summary.errors += 1
        return False, False, None, False, False


def _facet_decided_blockers(
    job_id: str,
    job: Job,
    settings: Settings,
    store: PostgresJobStore,
    facet_ready_ids: set[str],
) -> list[str]:
    """Return the hard blockers this job's stored facets establish, if any.

    Empty means "score it": the facets are missing, stale, or simply do not
    settle the question. Every step here fails open on purpose (#127) --
    including a store read that raises, because failing to read a fact is not
    evidence of a disqualifying one, and dropping a job over it would be a far
    worse failure than spending the call.

    `facet_ready_ids` is the run's answer to "whose facets describe the job as
    it stands now", taken once from `store.jobs_needing_facets` so the
    description-hash invalidation that gates re-extraction is the same
    mechanism that gates blocking, rather than a second notion of a changed
    posting.
    """
    if job_id not in facet_ready_ids:
        return []
    if not content_confidence.is_sufficient(job.content_confidence):
        # Thin or unverified content is the one case where a facet may be
        # reading a search-result snippet rather than the posting.
        # `evaluate_job` already refuses a confident decision on such a job,
        # and a block is a confident decision -- so it goes to the model,
        # which sees the same thin text and can weigh it in context.
        return []
    try:
        facets = store.get_job_facets(job_id)
    except Exception:
        logger.exception("could not read facets for job_id=%s", job_id)
        return []
    if facets is None:
        return []

    market = (
        market_by_id(settings.policy, job.market_id)
        if job.market_id and settings.policy.markets
        else None
    )
    return hard_blockers_from_facets(
        facets, BlockingThresholds.for_job(job, settings.policy, market)
    )


def _evaluate_and_deliver_one_job(
    job_id: str,
    job: Job,
    candidate_context: CandidateContext,
    settings: Settings,
    store: PostgresJobStore,
    gemini: GeminiClient,
    digest_items: list[DigestItem],
    summary: RunSummary,
    queued_job_ids: set[str],
    facet_ready_ids: set[str],
) -> tuple[bool, bool, str | None, bool, bool]:
    """Evaluate one job and add it to the digest.

    Returns (promoted, blocked, decision, offered, scored). `summary.evaluation_attempted`
    is incremented here rather than reported back, so it counts the fresh
    Gemini evaluations actually made (not the already-evaluated shortcut
    below) even when a later step for the same job fails and the caller never
    sees a return value. `offered` is True when this job will reach the user
    as an offer, which is what the daily offer limit counts. `scored` is False
    when the decision came from the job's facets rather than from the model,
    so the caller can keep `summary.evaluated` a count of model evaluations.
    """
    if store.get_evaluation(job_id) is not None and store.has_delivery(job_id, "telegram_message"):
        store.complete_ai_work("job_evaluation", job_id)
        return False, False, None, False, False

    # A job the facts already disqualify for this user costs nothing to
    # establish (#127): the comparison is between the posting's shared facets
    # and this profile's own numbers, and everything below handles the
    # resulting evaluation exactly as it handles the model's.
    facet_blockers = _facet_decided_blockers(job_id, job, settings, store, facet_ready_ids)
    scored = not facet_blockers
    if facet_blockers:
        evaluation = blocked_evaluation(job, facet_blockers)
        logger.info(
            "blocked job_id=%s from facets without a scoring call: %s",
            job_id,
            "; ".join(facet_blockers),
        )
    else:
        while True:
            try:
                evaluation = evaluate_job(
                    job,
                    candidate_context,
                    settings.policy,
                    gemini,
                )
                break
            except GeminiTemporaryCapacity as exc:
                logger.info(
                    "Gemini temporary capacity reached; waiting %.2fs before retrying job_id=%s",
                    exc.retry_after_seconds,
                    job_id,
                )
                time.sleep(exc.retry_after_seconds)
            except (GeminiBudgetExceeded, GeminiQuotaPaused):
                logger.warning(
                    "job evaluation deferred by Gemini quota for job_id=%s",
                    job_id,
                )
                store.enqueue_ai_work("job_evaluation", job_id)
                return False, True, None, False, False
            except Exception:
                logger.exception("evaluation failed for job_id=%s", job_id)
                summary.evaluation_attempted += 1
                summary.errors += 1
                return False, False, None, False, False

        summary.evaluation_attempted += 1

    # A job selected earlier in the run can have been merged away since --
    # discovery merges duplicates while it is still building the shortlist --
    # so the id that row lives under now is whatever the store wrote against,
    # not necessarily the one selected. Everything below has to use that one:
    # the id it replaced names a row that no longer exists (#145).
    written_job_id = store.save_evaluation(job_id, evaluation)
    if not scored:
        # Counted here rather than where the block was decided: the counter
        # reports what the run did, and a write that did not land leaves the
        # job unevaluated and eligible again tomorrow, to be counted then.
        summary.blocked_by_facets += 1
    already_delivered = False
    if written_job_id != job_id:
        job_id = written_job_id
        # The surviving row is the one the merge kept the better fields on --
        # the canonical URL a card sends the user to, above all -- so the
        # digest describes it rather than the row that was discarded.
        surviving_job = store.get_job(job_id)
        if surviving_job is not None:
            job = surviving_job
        # Keep the run's working set honest about where this job ended up:
        # the pending-delivery sweep at the end of the run subtracts these
        # ids, and without the survivor in it the same job is queued into the
        # digest a second time.
        queued_job_ids.add(job_id)
        # A duplicate can merge into a job that was already sent: the merge
        # moves the deliveries onto the survivor, so the already-delivered
        # check at the top of this function, made against the duplicate's id,
        # saw none.
        already_delivered = store.has_delivery(job_id, "telegram_message")
    store.complete_ai_work("job_evaluation", job_id)

    if evaluation.total_score != evaluation.raw_model_score:
        logger.info(
            "capped match score job_id=%s raw=%s effective=%s decision=%s",
            job_id, evaluation.raw_model_score, evaluation.total_score, evaluation.decision,
        )

    promoted = False
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
        promoted = promoted_watch_id is not None and promotion_after != promotion_before
    except Exception:
        logger.exception("company watch promotion failed for job_id=%s", job_id)
        summary.errors += 1

    if evaluation.total_score < settings.policy.match_score_floor:
        # The floor withholds every tier, warnings included -- but only an
        # offer that the floor took away is a signal that the floor is set
        # too high. A `skip` or a `blocked` was never going to be an offer,
        # so it stays on the decision-ladder counter it has always used;
        # counting it here would drown the number this exists to expose.
        if evaluation.decision in _OFFER_DECISIONS:
            summary.withheld_by_score_floor += 1
        else:
            summary.skipped += 1
        return promoted, False, evaluation.decision, False, scored

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
    )
    if already_delivered:
        logger.info(
            "job_id=%s was merged into a job already delivered; not offering it twice",
            job_id,
        )
    else:
        digest_items.append(item)

    if evaluation.decision in _READY_DECISIONS:
        summary.ready_to_apply += 1
    elif evaluation.decision == "possible_match":
        summary.possible_matches += 1
    else:
        summary.skipped += 1

    offered = not already_delivered and evaluation.decision in _OFFER_DECISIONS
    return promoted, False, evaluation.decision, offered, scored


def _format_gemini_usage_log(summary: GeminiUsageSummary) -> str:
    """One structured log line at run completion: totals plus per-purpose counts."""
    purposes = ",".join(
        f"{purpose}:{summary.purpose_counts[purpose]}"
        for purpose in GEMINI_PURPOSES
        if purpose in summary.purpose_counts
    )
    return (
        f"gemini_usage run_calls={summary.requests_today} "
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
    gemini: GeminiClient,
    telegram: TelegramClient | None = None,
    http: HttpClient | None = None,
) -> RunSummary:
    http = http or HttpClient()
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
        candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, gemini, store)
    except (GeminiBudgetExceeded, GeminiQuotaPaused):
        candidate_context = None
        logger.warning(
            "candidate context load deferred by Gemini quota; evaluation and cover letters "
            "will be deferred this run"
        )
    else:
        logger.info(
            "profile extraction: source=%s error=%s",
            candidate_context.source,
            candidate_context.load_error or "none",
        )
    preferences = candidate_context.preferences if candidate_context is not None else None
    discovery = collect_candidates(
        sources,
        store,
        http,
        settings.policy,
        resolver=resolver,
        preferences=preferences,
    )
    search_planned, search_attempted, search_succeeded, search_results = (
        _aggregate_targeted_search_stats(base_sources)
    )
    watch_checks, watch_paused = _watch_check_outcomes(store, due_watches)
    summary.skipped += discovery.stats.prefilter_rejected + discovery.stats.profession_rejected
    ranked = rank_jobs(discovery.eligible, settings.policy, preferences)
    selected = _select_candidates(ranked, settings.policy, preferences)
    eligible_source_counts = _source_counts(ranked)
    selected_source_counts = _source_counts(selected)
    selected_by_market = _market_counts(selected)
    decision_counts: dict[str, dict[str, int]] = {}
    decision_counts_by_source: dict[str, dict[str, int]] = {}
    deferred_by_budget = max(0, len(ranked) - len(selected))
    quota_deferred_count = 0
    # The user's daily offer limit is the run's delivery budget. Walking the
    # selected candidates in rank order and stopping once it is met needs no
    # assumed ratio between candidates evaluated and offers delivered, and
    # keeps adapting when that ratio moves.
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

    pending_evaluation_ids = [row["job_id"] for row in store.list_pending_ai_work("job_evaluation")]
    pending_evaluation_id_set = set(pending_evaluation_ids)

    queued_job_ids = (
        {job_id for job_id, _job, _score in selected}
        | pending_evaluation_id_set
    )
    companies_promoted = 0
    for job_id in discovery.rediscovered_job_ids:
        _requeue_pending_delivery(
            job_id,
            store,
            digest_items,
            settings.policy.match_score_floor,
        )

    # Whose stored facets describe the job as it stands, asked once for the
    # whole run rather than per job (#127). `jobs_needing_facets` answers the
    # negative of it, and reusing it keeps one notion of a changed posting:
    # a job whose description moved since extraction is not blocked on the
    # facts read from the old text, it is scored.
    evaluation_candidate_ids = pending_evaluation_ids + [
        job_id for job_id, _job, _score in selected
    ]
    try:
        facet_ready_ids = set(evaluation_candidate_ids) - store.jobs_needing_facets(
            evaluation_candidate_ids
        )
    except Exception:
        # Failing to work out which jobs have current facets must never cost a
        # job its scoring call: every candidate falls through to the model.
        logger.exception("could not determine which jobs have current facets")
        facet_ready_ids = set()

    quota_blocked = candidate_context is None
    if quota_blocked and pending_evaluation_ids:
        logger.warning(
            "candidate context unavailable this run; leaving %s pending job_evaluation "
            "retries queued",
            len(pending_evaluation_ids),
        )

    for job_id in pending_evaluation_ids:
        if quota_blocked:
            continue
        # Left in the queue rather than completed, so it is retried tomorrow.
        if delivered_offers >= offer_limit:
            cap_deferred_count += 1
            continue
        job = store.get_job(job_id)
        if job is None:
            store.complete_ai_work("job_evaluation", job_id)
            continue
        promoted, blocked, decision, offered, scored = _evaluate_and_deliver_job(
            job_id,
            job,
            candidate_context,
            settings,
            store,
            gemini,
            digest_items,
            summary,
            queued_job_ids,
            facet_ready_ids,
        )
        if decision is not None and scored:
            summary.evaluated += 1
        if offered:
            delivered_offers += 1
        if blocked:
            quota_deferred_count += 1
        _record_decision(decision_counts, job.market_id, decision)
        _record_decision(decision_counts_by_source, metric_source_label(job.source), decision)
        if promoted:
            companies_promoted += 1
        quota_blocked = quota_blocked or blocked

    for job_id, job, _score in selected:
        if job_id in pending_evaluation_id_set:
            continue
        # The user has the offers they asked for. The rest of the shortlist is
        # left unevaluated -- not queued, not discarded: it ranks again on the
        # next run, so a low limit trades breadth for pace rather than jobs.
        if delivered_offers >= offer_limit:
            cap_deferred_count += 1
            continue
        if quota_blocked:
            # Outside the per-job wrapper, so it needs its own guard: this
            # write carries the same foreign key as the evaluation, and
            # letting it raise here would end the run on the very failure
            # #145 is about.
            try:
                store.enqueue_ai_work("job_evaluation", job_id)
            except Exception:
                logger.exception(
                    "deferring evaluation failed for job_id=%s source=%s",
                    job_id,
                    metric_source_label(job.source),
                )
                summary.errors += 1
            quota_deferred_count += 1
            continue
        promoted, blocked, decision, offered, scored = _evaluate_and_deliver_job(
            job_id,
            job,
            candidate_context,
            settings,
            store,
            gemini,
            digest_items,
            summary,
            queued_job_ids,
            facet_ready_ids,
        )
        if decision is not None and scored:
            summary.evaluated += 1
        if offered:
            delivered_offers += 1
        if blocked:
            quota_deferred_count += 1
        _record_decision(decision_counts, job.market_id, decision)
        _record_decision(decision_counts_by_source, metric_source_label(job.source), decision)
        if promoted:
            companies_promoted += 1
        quota_blocked = quota_blocked or blocked

    # Objective facets (#125), deliberately *after* every evaluation.
    #
    # Every job below survived the non-AI filters -- this run's shortlist and
    # retry queue did so this run, a rediscovered job did so on the run that
    # first evaluated it -- so this pass never spends a provider call on a
    # posting the prefilter or the profession gate rejected. Rediscovered jobs
    # are how the existing corpus acquires facets at all: an already-evaluated
    # job never re-enters the shortlist, so leaving them out would mean only
    # jobs first seen today ever gained facets, and a failed extraction would
    # never be retried.
    #
    # The ordering is what keeps this work unable to cost the user a digest.
    # A provider 429 persists a pause against the *model*, not the purpose
    # (`GeminiUsageTracker.record_429`), and the evaluation loops treat
    # `GeminiQuotaPaused` as blocking for the rest of the run -- so a 429
    # tripped by a facet call made first would defer every evaluation behind
    # it and deliver nothing. Running last, facet work can only ever spend
    # what the run's own offers did not need. The internal core reserve
    # protects evaluation from the *budget*; this ordering protects it from
    # the provider.
    _extract_facets_for_run(
        [(job_id, None) for job_id in pending_evaluation_ids]
        + [(job_id, job) for job_id, job, _score in selected],
        discovery.rediscovered_job_ids,
        store,
        gemini,
        summary,
        limit=settings.policy.max_jobs_per_run,
    )

    logger.info(
        "evaluation_capacity selected=%s evaluated=%s blocked_by_facets=%s "
        "deferred_by_budget=%s "
        "quota_deferred=%s daily_offer_limit=%s delivered_offers=%s "
        "deferred_by_offer_cap=%s match_score_floor=%s "
        "withheld_by_score_floor=%s",
        len(selected),
        summary.evaluated,
        summary.blocked_by_facets,
        deferred_by_budget,
        quota_deferred_count,
        offer_limit,
        delivered_offers,
        cap_deferred_count,
        settings.policy.match_score_floor,
        summary.withheld_by_score_floor,
    )

    for job_id in (
        set(store.pending_delivery_job_ids(settings.policy.match_score_floor))
        - queued_job_ids
        - set(discovery.rediscovered_job_ids)
    ):
        _requeue_pending_delivery(
            job_id,
            store,
            digest_items,
            settings.policy.match_score_floor,
        )

    tracker = getattr(gemini, "_tracker", None)
    usage_summary = tracker.snapshot(datetime.now(timezone.utc)) if tracker is not None else None
    if usage_summary is not None:
        logger.info(_format_gemini_usage_log(usage_summary))

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
            warning = build_gemini_pause_warning(usage_summary)
            if warning is not None:
                try:
                    telegram.send_message(warning)
                except Exception:
                    logger.exception("failed to send Gemini pause warning to Telegram")

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
    _log_ats_registry_metrics(store, discovery, _learned_ats_stats(base_sources))

    logger.info(
        "company watch outcomes: companies_promoted=%s watch_checks=%s watch_paused=%s",
        companies_promoted,
        watch_checks,
        watch_paused,
    )

    return summary

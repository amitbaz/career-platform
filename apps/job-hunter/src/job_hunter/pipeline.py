from __future__ import annotations

import logging
import os
import secrets
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from job_hunter.candidate_context import get_candidate_context
from job_hunter.canonical import CanonicalResolver, parse_supported_ats_url
from job_hunter.circuit_breaker import CircuitBreaker
from job_hunter.cover_letter import generate_cover_letter
from job_hunter.discovery import collect_candidates, metric_source_label
from job_hunter.evaluation import evaluate_job
from job_hunter.gemini import GeminiClient
from job_hunter.gemini_usage import (
    GEMINI_PURPOSES,
    GeminiBudgetExceeded,
    GeminiQuotaPaused,
    GeminiTemporaryCapacity,
)
from job_hunter.http import HttpClient
from job_hunter.job_identity import normalize_company_name
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
from job_hunter.navigation_store import (
    attach_navigation_message_id,
    create_navigation_session,
    prune_navigation_sessions,
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
from job_hunter.sources.learned_ats import LearnedAtsStats
from job_hunter.store import JobStore
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

_READY_DECISIONS = {"high_priority", "package_match"}
_MIN_DELIVERABLE_SCORE = 61
_NAVIGATION_SESSION_TTL = timedelta(days=30)
_SUPPORTED_WATCH_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})
_SEARCH_FAILURE_THRESHOLD = 5
_CANONICAL_SEARCH_SITES = (
    "site:jobs.ashbyhq.com OR site:jobs.lever.co OR "
    "site:boards.greenhouse.io OR careers"
)


def _targeted_canonical_candidates(
    http: HttpClient,
    job: Job,
    breaker: CircuitBreaker,
    brave_budget: BraveRequestBudget | None = None,
) -> list[Job]:
    """Run one bounded public search for the employer's original posting."""
    company = " ".join(job.company.replace('"', " ").split())
    title = " ".join(job.title.replace('"', " ").split())
    if not company or not title:
        return []

    query = f'"{company}" "{title}" ({_CANONICAL_SEARCH_SITES})'
    brave_api_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    backend = build_search_backend(
        http,
        brave_api_key,
        enable_brave=brave_budget is not None,
        on_brave_attempt=brave_budget.reserve if brave_budget is not None else None,
    )
    candidates = TargetedSearchSource(backend, [query], breaker=breaker).discover()
    for candidate in candidates:
        ats = parse_supported_ats_url(candidate.url)
        if ats is not None and (
            normalize_company_name(ats.board) == normalize_company_name(job.company)
        ):
            candidate.company = ats.board
    return candidates


def _persisted_watch_target(
    store: JobStore, company_name: str
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


def _log_ats_registry_metrics(store: JobStore, discovery, learned_stats: LearnedAtsStats) -> None:
    """Log one final ats_registry line summarizing registry health this run."""
    logger.info(
        "ats_registry total=%s discovered=%s scanned=%s successful=%s failed=%s jobs_raw=%s",
        store.count_ats_boards(),
        discovery.stats.ats_boards_discovered,
        learned_stats.boards_scanned,
        learned_stats.boards_successful,
        learned_stats.boards_failed,
        learned_stats.jobs_raw,
    )


def _due_watch_state(
    store: JobStore,
) -> dict[int, tuple[str, str | None, int, str | None]]:
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
    store: JobStore,
    before: dict[int, tuple[str, str | None, int, str | None]],
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
    return Path(settings.db_path).parent / "cover_letters"


def generate_cover_letter_on_demand(
    settings: Settings,
    job_id: int,
    *,
    store: JobStore,
    gemini: GeminiClient,
    telegram: TelegramClient,
) -> bool:
    """Generate (or resend) one job's cover letter on demand and deliver it.

    A repeat call for a job that already has a saved cover letter resends the
    existing PDF for free instead of calling Gemini again.
    """
    job = store.get_job(job_id)
    evaluation = store.get_evaluation(job_id)
    if job is None or evaluation is None:
        logger.warning("no job/evaluation found for job_id=%s; cannot generate cover letter", job_id)
        return False

    material = store.get_material(job_id)
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
        store.save_material(job_id, Material(job_id=job_id, cover_letter_text=text))

    out_dir = cover_letter_output_dir(settings)
    pdf_path = render_cover_letter_pdf(text, job.company, job.title, out_dir)
    caption = f"{job.company} - {job.title} - {evaluation.total_score} - {job.url}"
    document_id = telegram.send_document(pdf_path, caption)
    if document_id is not None:
        store.mark_delivered(job_id, "telegram_document", document_id)
    return document_id is not None


def should_run_scheduled(now: datetime, timezone: str, scheduled_hour: int) -> bool:
    local_hour = now.astimezone(ZoneInfo(timezone)).hour
    return local_hour == scheduled_hour


def _requeue_pending_delivery(
    job_id: int,
    store: JobStore,
    digest_items: list[DigestItem],
) -> None:
    """Re-add a rediscovered job's digest entry if it was never delivered."""
    evaluation = store.get_evaluation(job_id)
    if evaluation is None or evaluation.total_score < _MIN_DELIVERABLE_SCORE:
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


def _evaluate_and_deliver_job(
    job_id: int,
    job: Job,
    candidate_context: CandidateContext,
    settings: Settings,
    store: JobStore,
    gemini: GeminiClient,
    digest_items: list[DigestItem],
    summary: RunSummary,
) -> tuple[bool, bool, str | None]:
    """Evaluate one job and add it to the digest."""
    if store.get_evaluation(job_id) is not None and store.has_delivery(job_id, "telegram_message"):
        store.complete_ai_work("job_evaluation", job_id)
        return False, False, None

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
            return False, True, None
        except Exception:
            logger.exception("evaluation failed for job_id=%s", job_id)
            summary.errors += 1
            return False, False, None

    store.save_evaluation(job_id, evaluation)
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
            package_threshold=settings.policy.thresholds["package"],
        )
        promotion_after = _watch_promotion_state(store.get_company_watch(job.company))
        promoted = promoted_watch_id is not None and promotion_after != promotion_before
    except Exception:
        logger.exception("company watch promotion failed for job_id=%s", job_id)
        summary.errors += 1

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
    digest_items.append(item)

    if evaluation.decision in _READY_DECISIONS:
        summary.ready_to_apply += 1
    elif evaluation.decision == "possible_match":
        summary.possible_matches += 1
    else:
        summary.skipped += 1

    return promoted, False, evaluation.decision

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
    store: JobStore | None = None,
    gemini: GeminiClient,
    telegram: TelegramClient | None = None,
    http: HttpClient | None = None,
) -> RunSummary:
    http = http or HttpClient()
    store = store or JobStore(settings.db_path)
    try:
        sync_manual_watch_seeds(store, settings.policy.manual_company_watch)
    except Exception:
        logger.exception("manual company watch sync failed")

    search_breaker = CircuitBreaker(_SEARCH_FAILURE_THRESHOLD)
    brave_budget = build_brave_budget(settings)
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
            http, job, search_breaker, brave_budget
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
    evaluated_count = 0
    quota_deferred_count = 0
    logger.info(
        "discovery: raw=%s unique=%s prefilter_rejected=%s profession_rejected=%s eligible=%s selected=%s deferred_by_budget=%s canonical_network_attempts=%s sources=%s",
        discovery.stats.raw,
        discovery.stats.unique,
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
        _requeue_pending_delivery(job_id, store, digest_items)

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
        job = store.get_job(job_id)
        if job is None:
            store.complete_ai_work("job_evaluation", job_id)
            continue
        promoted, blocked, decision = _evaluate_and_deliver_job(
            job_id, job, candidate_context, settings, store, gemini, digest_items, summary
        )
        if decision is not None:
            evaluated_count += 1
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
        if quota_blocked:
            store.enqueue_ai_work("job_evaluation", job_id)
            quota_deferred_count += 1
            continue
        promoted, blocked, decision = _evaluate_and_deliver_job(
            job_id, job, candidate_context, settings, store, gemini, digest_items, summary
        )
        if decision is not None:
            evaluated_count += 1
        if blocked:
            quota_deferred_count += 1
        _record_decision(decision_counts, job.market_id, decision)
        _record_decision(decision_counts_by_source, metric_source_label(job.source), decision)
        if promoted:
            companies_promoted += 1
        quota_blocked = quota_blocked or blocked

    logger.info(
        "evaluation_capacity selected=%s evaluated=%s deferred_by_budget=%s quota_deferred=%s",
        len(selected),
        evaluated_count,
        deferred_by_budget,
        quota_deferred_count,
    )

    for job_id in set(store.pending_delivery_job_ids()) - queued_job_ids - set(discovery.rediscovered_job_ids):
        _requeue_pending_delivery(job_id, store, digest_items)

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
                    store.mark_delivered(item.job_id, "telegram_message", message_id)
                    _bump_market_count(delivered_by_market, item.market_id)
                    delivered_job = store.get_job(item.job_id)
                    if delivered_job is not None:
                        _bump_source_count(
                            delivered_by_source, metric_source_label(delivered_job.source)
                        )

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
            prune_navigation_sessions(store, now.isoformat())
            session = _build_navigation_session(deliverable_items, now)
            create_navigation_session(store, session)
            text, keyboard = build_navigation_card(
                session.cards[0],
                session.session_id,
                0,
                len(session.cards),
            )
            message_id = interactive_sender(text, keyboard)
            if message_id is not None:
                attach_navigation_message_id(store, session.session_id, str(message_id))
                for card in session.cards:
                    store.mark_delivered(card.job_id, "telegram_message", str(message_id))
                    _bump_market_count(delivered_by_market, card.market_id)
                    delivered_job = store.get_job(card.job_id)
                    if delivered_job is not None:
                        _bump_source_count(
                            delivered_by_source, metric_source_label(delivered_job.source)
                        )

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

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
from job_hunter.company_facets import (
    CompanyEvidence,
    CompanyFacetExtractionError,
    extract_company_facets,
)
from job_hunter.evaluation import EvaluationError, evaluate_job
from job_hunter.facets import FacetExtractionError, PostingFacts, extract_facets
from job_hunter.ai import (
    AI_PURPOSES,
    AIBudgetExceeded,
    AIProvider,
    AIQuotaPaused,
    AITemporaryCapacity,
    PlatformAllowanceExhausted,
)
from job_hunter.ai.usage import AIUsageTracker
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


#: How many rolling windows the inline read of a posting will wait out before
#: giving up its turn. Scoring waits without a bound because it paces against
#: the user's own key, where the only other claimant is this run. Reading a
#: posting paces against the *platform* key (#128), which every user's run
#: shares: two overlapping runs can hold each other over the rolling ceiling
#: indefinitely, and a run that waits for that to clear delivers nothing at
#: all. Three windows is long enough to ride out a burst and short enough that
#: the run still finishes; a posting not read by then is deferred like any
#: other the platform could not pay for.
_READ_CAPACITY_WAITS = 3


def _waiting_out_capacity(call, *, doing: str, job_id: str, max_waits: int | None = None):
    """Run `call`, waiting out the provider's rolling window.

    Both provider calls a user is waiting on -- reading the posting and
    scoring it -- wait rather than give up their turn: the run is producing
    this person's digest and there is no later chance today.
    The adapter's preflight pacing has already slept out one window and
    re-checked before raising, so each pass here is a second, deliberate wait.

    `max_waits` bounds that patience where the capacity being waited on is not
    this run's alone to clear; `None` waits as long as it takes, and re-raises
    nothing. The last `AITemporaryCapacity` propagates when the bound is spent,
    for the caller to turn into whatever giving up means to it.

    The backfill pass deliberately does not use this. Nobody is waiting on it,
    so it gives up its turn instead of holding the run open.
    """
    waits = 0
    while True:
        try:
            return call()
        except AITemporaryCapacity as exc:
            if max_waits is not None and waits >= max_waits:
                logger.info(
                    "AI temporary capacity still full after %s waits; giving up on "
                    "%s for job_id=%s",
                    waits,
                    doing,
                    job_id,
                )
                raise
            waits += 1
            logger.info(
                "AI temporary capacity reached; waiting %.2fs before %s for job_id=%s",
                exc.retry_after_seconds,
                doing,
                job_id,
            )
            time.sleep(exc.retry_after_seconds)


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
        raise
    except FacetExtractionError:
        logger.exception("facet extraction response could not be parsed for job_id=%s", job_id)
        summary.facet_extraction_attempted += 1
        summary.facet_extraction_failed += 1
        summary.extraction_parse_failures += 1
        return None
    except Exception:
        logger.exception("facet extraction failed for job_id=%s", job_id)
        summary.facet_extraction_attempted += 1
        summary.facet_extraction_failed += 1
        return None

    summary.facet_extraction_attempted += 1
    try:
        store.save_job_facets(job_id, facets)
    except Exception:
        logger.exception("storing facets failed for job_id=%s", job_id)
        summary.facet_extraction_failed += 1
    return facets


def _facets_for_scoring(
    job_id: str,
    job: Job,
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
    needs_facets: set[str],
) -> JobFacets | None:
    """The facets a scoring call must be given, reading the posting if needed.

    Scoring no longer receives the job description (#126), so a job cannot be
    scored until its posting has been read once. `needs_facets` is the run's
    single `jobs_needing_facets` answer, so the ordinary case -- a posting
    already read on an earlier run, by this user or by any other (#175) --
    costs one store read and no provider call, and the same posting is never
    read twice in a run. Those reuses are counted, so the run log can say how
    much of its scoring rode on work it did not pay for.

    Returns None when the posting could not be read this run, or when the job
    has no posting to read facets from or store them against. The caller must
    leave the job unscored rather than score it against nothing.

    `needs_facets` is narrowed as the run reads, so it ends the scoring loops
    holding exactly the postings the run has *not* spent a read on. The
    backfill pass is given those and no others: a posting whose read failed
    here must not be read a second time in the same run, which would spend two
    calls to learn the same nothing.
    """
    if job_id not in needs_facets:
        try:
            facets = store.get_job_facets(job_id)
        except Exception:
            logger.exception("could not read stored facets for job_id=%s", job_id)
            return None
        if facets is not None:
            summary.facets_reused += 1
            return facets
        # The bulk check said this job had current facets and there are none.
        # Ask again for this one job before spending a call, because the two
        # ways that happens want opposite answers: a row replaced or removed
        # mid-run has to be read again, while a job with no posting has
        # nowhere to store an extraction at all -- reading it would cost a
        # call, discard the result, and cost the same call on every later run
        # forever. A failed re-ask is treated as the first case, which costs
        # one call rather than silently dropping a job from the run.
        try:
            still_needed = store.jobs_needing_facets([job_id])
        except Exception:
            logger.exception("could not re-check whether job_id=%s needs reading", job_id)
            still_needed = {job_id}
        if job_id not in still_needed:
            logger.info(
                "job_id=%s has no facets and no posting to store any against; "
                "not scored this run",
                job_id,
            )
            return None
        logger.info("facets for job_id=%s vanished after the run's bulk check", job_id)

    try:
        facets = _waiting_out_capacity(
            lambda: _extract_and_store_facets(job_id, job, store, ai, summary),
            doing="reading the posting",
            job_id=job_id,
            max_waits=_READ_CAPACITY_WAITS,
        )
    except AITemporaryCapacity as exc:
        # The platform key's rolling window stayed full, which for this run is
        # indistinguishable from having no allowance: the posting is not read
        # today. Raised as the same exhaustion every other platform refusal
        # raises, so the caller has one thing to handle and the job keeps its
        # turn -- `needs_facets.discard` below is not reached.
        raise PlatformAllowanceExhausted(
            "the platform key's rolling capacity stayed full while reading a posting"
        ) from exc
    # A turn is spent once the provider has actually answered -- including an
    # answer that could not be read, which cost a call. A refusal that never
    # reached the provider raises out of here instead, leaving the job in the
    # set, because it has not had its turn.
    needs_facets.discard(job_id)
    return facets


def _backfill_one_job_facets(
    job_id: str,
    job: Job,
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
) -> str:
    """One step of the run's backfill pass over jobs nothing is waiting on.

    Nothing here may end the run, and nothing here may change what the run
    delivers: this pass runs after every scoring call the run makes, over the
    jobs those calls did not need.
    """
    try:
        _extract_and_store_facets(job_id, job, store, ai, summary)
    except AITemporaryCapacity:
        # The adapter's preflight pacing already slept out one rolling
        # window and re-checked, so reaching this means capacity is still
        # full. Scoring keeps waiting because a user is waiting on the
        # answer; this job keeps its turn for the next run. It never reached
        # the provider, so it is not an attempt and must not consume a slot in
        # the run's bounded budget -- otherwise a run under sustained rolling
        # pressure would burn its whole allowance extracting nothing.
        logger.info("facet extraction skipped on rolling capacity for job_id=%s", job_id)
        return _FACET_SKIPPED
    except PlatformAllowanceExhausted as exc:
        # Not an extraction failure, and logged so that it cannot be read as
        # one: the platform key is spent (or absent), the posting is untouched
        # in the corpus, and the next run reads it. Nothing about this run is
        # wrong, so nothing here raises or counts.
        logger.info(
            "facet extraction deferred for job_id=%s: %s", job_id, exc
        )
        return _FACET_QUOTA_BLOCKED
    return _FACET_EXTRACTED


def _extract_facets_for_run(
    run_candidates: list[tuple[str, Job | None]],
    backfill_ids: list[str],
    store: PostgresJobStore,
    ai: AIProvider,
    summary: RunSummary,
    *,
    limit: int,
) -> None:
    """Read the postings this run's scoring did not need, and backfill the rest.

    `run_candidates` are the jobs this run selected but did not read -- the
    shortlist tail the offer cap never reached, and the retry queue it never
    got to. A job it *did* score was read inline first, so it is not here.
    `backfill_ids` are jobs it rediscovered, which were scored on an earlier
    run and so never re-enter the shortlist. Both survived the non-AI filters.
    Ids may repeat within or across the two; the first occurrence wins.

    Only jobs with no current facets are extracted, so the ordinary steady
    state -- everything already read, nothing rewritten -- costs one pair of
    store reads and no provider call at all.

    `limit` is what is left of `max_jobs_per_run` -- the shortlist size the
    user's search profile already sets as "how much AI work one run may do" --
    once the run's inline reads have been subtracted. It bounds this pass, not
    the run: an inline read is the unavoidable cost of scoring a job and is
    never refused for want of budget, so a run that scores a full shortlist of
    unread postings simply leaves this pass nothing. Reusing that figure rather
    than adding a knob keeps the backfill bounded without asking an operator to
    size it. Half of what remains is **reserved for the backfill**: spending
    the budget in priority order alone would mean a day that discovers a full
    shortlist leaves nothing for the corpus, and the backfill would only ever
    progress on quiet days --
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
            outcome = _backfill_one_job_facets(job_id, job, store, ai, summary)
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
        "quota_blocked=%s parse_failures=%s",
        len(ordered_candidates),
        len(ordered_backfill),
        len(needed),
        summary.facet_extraction_attempted,
        summary.facet_extraction_failed,
        skipped,
        limit,
        backfill_reserve,
        quota_blocked,
        summary.extraction_parse_failures,
    )


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


def _company_for_job(
    job: Job,
    store: PostgresJobStore,
    company_facets: dict[str, CompanyFacets | None],
) -> CompanyFacets | None:
    """This job's employer, from the run's map or from one store read.

    The run's bulk read covers the employers behind the *eligible* set. A job
    replayed from the pending-evaluation queue was selected on an earlier run
    and its employer may not appear in this one's discovery at all, so without
    this it would be scored against "nothing has been established" while a
    `job_hunter_companies` row sat there unread -- the same posting and the
    same profile getting a different prompt depending on which run reached it.

    The result is memoized either way, a miss included, so an employer nothing
    is known about costs one read per run rather than one per posting.
    """
    identity = normalize_company_name(job.company or "")
    if not identity:
        return None
    if identity in company_facets:
        return company_facets[identity]
    try:
        facets = store.get_company_facets(job.company or "")
    except Exception:
        # Company facts are extra evidence. Failing to read them scores the
        # job without them rather than not scoring it.
        logger.exception("could not read company facets for company=%r", job.company)
        facets = None
    company_facets[identity] = facets
    return facets


def _evaluate_and_deliver_job(
    job_id: str,
    job: Job,
    candidate_context: CandidateContext,
    settings: Settings,
    store: PostgresJobStore,
    ai: AIProvider,
    digest_items: list[DigestItem],
    summary: RunSummary,
    queued_job_ids: set[str],
    needs_facets: set[str],
    company_facets: dict[str, CompanyFacets | None],
) -> tuple[bool, bool, str | None, bool, bool]:
    """Evaluate one job and add it to the digest, containing its failures.

    No single job may end a run. The inner function already catches a failed
    model call, but everything after it -- persisting the evaluation,
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
            ai,
            digest_items,
            summary,
            queued_job_ids,
            needs_facets,
            company_facets,
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
    job: Job, facets: JobFacets, settings: Settings
) -> list[str]:
    """Return the hard blockers this job's facets establish, if any (#127).

    `facets` are the ones scoring is about to be given, so blocking reads
    exactly what the model would have read -- there is no second, staler view
    of the posting to disagree with it, and no extra store read.

    Empty means "score it". Every step fails open on purpose: an absent fact
    is not evidence of a disqualifying one, and dropping a job over one would
    be a far worse failure than spending the call.
    """
    if not content_confidence.is_sufficient(job.content_confidence):
        # Thin or unverified content is the one case where a facet may have
        # been read from a search-result snippet rather than the posting.
        # `evaluate_job` already refuses a confident decision on such a job,
        # and a block is a confident decision -- so it goes to the model,
        # which sees the same thin material and can weigh it in context.
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
    ai: AIProvider,
    digest_items: list[DigestItem],
    summary: RunSummary,
    queued_job_ids: set[str],
    needs_facets: set[str],
    company_facets: dict[str, CompanyFacets | None],
) -> tuple[bool, bool, str | None, bool, bool]:
    """Evaluate one job and add it to the digest.

    Returns (promoted, blocked, decision, offered, scored). `summary.evaluation_attempted`
    is incremented here rather than reported back, so it counts the fresh
    model evaluations actually made (not the already-evaluated shortcut
    below) even when a later step for the same job fails and the caller never
    sees a return value. `offered` is True when this job will reach the user
    as an offer, which is what the daily offer limit counts. `scored` is False
    when the decision came from the job's facets rather than from the model,
    so the caller can keep `summary.evaluated` a count of model evaluations.
    """
    if store.get_evaluation(job_id) is not None and store.has_delivery(job_id, "telegram_message"):
        store.complete_ai_work("job_evaluation", job_id)
        return False, False, None, False, False

    # Scoring is handed the posting's facets, not its description (#126), so
    # a posting nobody has read yet is read here, once, before it is scored.
    try:
        facets = _facets_for_scoring(job_id, job, store, ai, summary, needs_facets)
    except PlatformAllowanceExhausted as exc:
        # The platform key is out, not the user's (#128). That distinction is
        # the whole of this branch: this must not block the run the way a
        # paused user model does, because the user's own key is untouched and
        # every job whose posting has already been read still scores. Only a
        # posting nobody has read yet waits for tomorrow, and it waits in the
        # queue, unenriched and undamaged.
        logger.warning(
            "the posting for job_id=%s was not read; not scored this run: %s",
            job_id,
            exc,
        )
        summary.scoring_deferred_by_read_budget += 1
        store.enqueue_ai_work("job_evaluation", job_id)
        return False, False, None, False, False

    if facets is None:
        # Scoring against an empty requirements list would read "this posting
        # demands nothing" instead of "nobody has read this posting", which
        # inflates the score of exactly the jobs least is known about. The job
        # keeps its place in the ranking and is scored on a later run.
        logger.warning(
            "job_id=%s has no readable facets; not scored this run", job_id
        )
        summary.scoring_skipped_without_facets += 1
        return False, False, None, False, False

    # A job those same facts already disqualify for this user costs nothing
    # more to establish (#127): the comparison is between the posting's shared
    # facets and this profile's own numbers, and everything below handles the
    # resulting evaluation exactly as it handles the model's.
    facet_blockers = _facet_decided_blockers(job, facets, settings)
    scored = not facet_blockers
    if facet_blockers:
        evaluation = blocked_evaluation(job, facet_blockers)
        logger.info(
            "blocked job_id=%s from facets without a scoring call: %s",
            job_id,
            "; ".join(facet_blockers),
        )
    else:
        try:
            evaluation = _waiting_out_capacity(
                lambda: evaluate_job(
                    job,
                    facets,
                    candidate_context,
                    settings.policy,
                    ai,
                    _company_for_job(job, store, company_facets),
                ),
                doing="scoring",
                job_id=job_id,
            )
        except (AIBudgetExceeded, AIQuotaPaused):
            logger.warning(
                "job evaluation deferred by AI quota for job_id=%s",
                job_id,
            )
            store.enqueue_ai_work("job_evaluation", job_id)
            return False, True, None, False, False
        except EvaluationError:
            logger.exception("evaluation response could not be parsed for job_id=%s", job_id)
            summary.evaluation_attempted += 1
            summary.scoring_parse_failures += 1
            summary.errors += 1
            return False, False, None, False, False
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
    # What the corpus already knows about the employers behind this run's
    # candidates, read once for the whole run (#198). Ordering consults it
    # before anything is read, so a run pays for nothing to have the company
    # dimensions count; the enrichment pass below then reads the employers
    # nobody has got to yet, in the order the ranking put them, and those
    # answers reach this run's scoring and the next run's ordering.
    try:
        company_facets = store.get_company_facets_bulk(
            [job.company for _job_id, job in discovery.eligible]
        )
    except Exception:
        # Company facts are extra evidence. Failing to read them must never
        # be a reason a run stops ranking or delivering.
        logger.exception("could not read stored company facets for the eligible set")
        company_facets = {}
    ranked = rank_jobs(discovery.eligible, settings.policy, preferences, company_facets)
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

    # Which of the jobs this run may score have not been read yet, asked once
    # for the whole run rather than per job. Scoring needs a posting's facets
    # (#126), and this is the one mechanism that decides whether a stored set
    # is still current -- the same description hash that gates re-evaluation.
    scoring_candidate_ids = pending_evaluation_ids + [job_id for job_id, _job, _score in selected]
    try:
        needs_facets = store.jobs_needing_facets(scoring_candidate_ids)
    except Exception:
        # Reading the posting again costs a provider call; not scoring at all
        # costs the user their digest. Assume nothing has been read.
        logger.exception("could not determine which postings still need reading")
        needs_facets = set(scoring_candidate_ids)

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
            ai,
            digest_items,
            summary,
            queued_job_ids,
            needs_facets,
            company_facets,
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
            ai,
            digest_items,
            summary,
            queued_job_ids,
            needs_facets,
            company_facets,
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

    # The backfill half of objective extraction, over the postings this run's
    # scoring did not need. A job that was scored has already been read, so
    # `jobs_needing_facets` inside this pass skips it; what is left is the
    # shortlist tail the offer cap never reached, and the jobs discovery
    # rediscovered.
    #
    # Rediscovered jobs are how the existing corpus acquires facets at all: an
    # already-evaluated job never re-enters the shortlist, so leaving them out
    # would mean only jobs first seen today ever gained facets, and a failed
    # extraction would never be retried. Every job here survived the non-AI
    # filters -- this run's shortlist did so this run, a rediscovered job did
    # so on the run that first evaluated it -- so this pass never spends a
    # provider call on a posting the prefilter or the profession gate rejected.
    #
    # It runs *after* every scoring call. Since #128 the two halves cannot
    # take each other's budget at all -- extraction spends the platform key
    # and its own ledger, scoring spends the user's -- so a 429 tripped here
    # pauses the platform model row and leaves every score untouched. The
    # ordering survives for the remaining reason: this pass is given what the
    # run's inline reads left of the budget, which is not known until they are
    # done.
    #
    # The run's whole facet budget is `max_jobs_per_run`, and the inline reads
    # have already spent part of it, so the backfill gets what is left.
    _extract_facets_for_run(
        [(job_id, None) for job_id in pending_evaluation_ids if job_id in needs_facets]
        + [(job_id, job) for job_id, job, _score in selected if job_id in needs_facets],
        discovery.rediscovered_job_ids,
        store,
        ai,
        summary,
        limit=max(0, settings.policy.max_jobs_per_run - summary.facet_extraction_attempted),
    )

    # Company enrichment runs last, after every posting read this run makes.
    #
    # Both spend the *same* platform key against the same shared-extraction
    # ledger, so they are not independent budgets: a company call taken early
    # is a posting read the run may not be able to afford later. The two are
    # not equally important. A posting's facets are a precondition for scoring
    # it at all (#126) -- a job whose posting goes unread is not scored and the
    # user does not see it today -- while a company's facts are extra evidence
    # that changes how a job scores, never whether it does. Running this pass
    # first let an optional workload deny the user offers, so it runs on what
    # the required work leaves, and its own `limit` bounds it further.
    #
    # The cost is that a company read here reaches the *next* run's scoring
    # and ordering rather than this one's. For a fact cached for 180 days and
    # amortised over every role that employer publishes, a one-run delay is
    # not worth a single lost offer.
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

    logger.info(
        "evaluation_capacity selected=%s evaluated=%s blocked_by_facets=%s "
        "deferred_by_budget=%s "
        "quota_deferred=%s daily_offer_limit=%s delivered_offers=%s "
        "deferred_by_offer_cap=%s match_score_floor=%s "
        "withheld_by_score_floor=%s skipped_without_facets=%s "
        "deferred_by_read_budget=%s parse_failures=%s",
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
        summary.scoring_skipped_without_facets,
        summary.scoring_deferred_by_read_budget,
        summary.scoring_parse_failures,
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
    _log_ats_registry_metrics(store, discovery, _learned_ats_stats(base_sources))

    logger.info(
        "company watch outcomes: companies_promoted=%s watch_checks=%s watch_paused=%s",
        companies_promoted,
        watch_checks,
        watch_paused,
    )

    return summary

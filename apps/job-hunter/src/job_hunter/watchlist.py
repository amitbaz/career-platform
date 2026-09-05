"""Promotion policy and persistence coordination for watched companies."""

from __future__ import annotations

from .job_identity import normalize_company_name
from .models import CompanyWatchSeed, Evaluation, Job
from .store import JobStore


_AUTO_PROMOTION_DECISIONS = frozenset({"high_priority", "package_match"})
_SUPPORTED_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})


def should_auto_promote(evaluation: Evaluation) -> bool:
    """Return whether an evaluation's final outcome permits watch promotion.

    Score-threshold enforcement belongs to the threshold-aware promotion path;
    this helper deliberately accepts only the final evaluation.
    """
    return (
        evaluation.status == "ok"
        and evaluation.decision in _AUTO_PROMOTION_DECISIONS
        and not evaluation.hard_blockers
    )


def sync_manual_watch_seeds(
    store: JobStore, seeds: list[CompanyWatchSeed]
) -> None:
    """Idempotently synchronize configured company watch seeds into storage."""
    for seed in seeds:
        store.upsert_company_watch(
            company_name=seed.company_name,
            careers_url=seed.careers_url,
            ats_provider=seed.ats_provider,
            ats_identifier=seed.ats_identifier,
            discovered_from_job_id=None,
            promotion_source="manual",
            confidence=1.0,
        )


def promote_company(
    store: JobStore,
    *,
    job_id: int,
    job: Job,
    evaluation: Evaluation,
    confidence: float = 1.0,
    package_threshold: int | None = None,
) -> int | None:
    """Promote a strongly evaluated job's company using final job metadata.

    When supplied, ``package_threshold`` rejects inconsistent evaluations whose
    score is below the configured package floor. No independent score is used.
    """
    if not should_auto_promote(evaluation):
        return None
    if package_threshold is not None and evaluation.total_score < package_threshold:
        return None
    if not normalize_company_name(job.company):
        return None

    provider = (job.ats_provider or "").strip().lower()
    board = (job.ats_board or "").strip()
    if provider in _SUPPORTED_ATS_PROVIDERS and board:
        ats_provider: str | None = provider
        ats_identifier: str | None = board
        careers_url = ""
    else:
        ats_provider = None
        ats_identifier = None
        careers_url = job.canonical_url or ""

    return store.upsert_company_watch(
        company_name=job.company,
        careers_url=careers_url,
        ats_provider=ats_provider,
        ats_identifier=ats_identifier,
        discovered_from_job_id=job_id,
        promotion_source="automatic",
        confidence=confidence,
    )

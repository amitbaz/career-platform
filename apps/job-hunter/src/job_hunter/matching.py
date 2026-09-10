"""Answer matching as one on-demand operation over stored facets (#187).

`match_jobs` is the operation a dashboard, an on-demand search and the daily
digest all call: one ranking implementation, not one per caller. It ranks
and flags the requesting user's whole corpus in a single SQL round trip
(`PostgresJobStore.match_jobs`, a term-for-term port of
`ranking.profile_priority_score` and `hard_blockers.hard_blockers_from_facets`
-- see `supabase/migrations/20260910140000_job_hunter_match_jobs.sql` and
`docs/superpowers/specs/2026-09-10-answer-matching-as-one-operation-design.md`),
so filtering and hard blocking cost nothing before a provider call. Only the
first `limit` rows that survive both -- unblocked and already read -- are
handed to the model, on the caller's own credentials and ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from job_hunter.evaluation import evaluate_job
from job_hunter.hard_blockers import blocked_evaluation
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import CandidateContext, Evaluation, SearchPolicy

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider
    from job_hunter.postgres_store import PostgresJobStore


@dataclass(frozen=True, slots=True)
class MatchedJob:
    """One ranked row, plus what the operation decided about it.

    `scored` is False for a facet-decided block (`evaluation` came from
    `hard_blockers.blocked_evaluation`, at no provider cost) and for a row
    this call chose not to reach at all; it is True only when `evaluation`
    is the model's own answer.
    """

    job_id: str
    posting_id: str
    score: int
    evaluation: Evaluation
    scored: bool


def match_jobs(
    store: "PostgresJobStore",
    ai: "AIProvider",
    policy: SearchPolicy,
    candidate_context: CandidateContext,
    limit: int,
) -> list[MatchedJob]:
    """Rank this user's whole corpus in SQL, then score the top `limit` of it.

    Iterates the SQL ranking in order. A hard-blocked row becomes a
    `blocked_evaluation` and never reaches `evaluate_job`; a row nobody has
    read yet (`has_facets` is False) is skipped, since neither this function
    nor `evaluate_job` can score a posting with no facets. Everything else is
    scored by the model until `limit` model-scored jobs have been produced or
    the ranking is exhausted.
    """
    preferences = candidate_context.preferences
    rows = store.match_jobs(
        preferred_roles=preferences.preferred_roles,
        preferred_seniority=preferences.preferred_seniority,
        must_have_signals=preferences.must_have_signals,
        nice_to_have_signals=preferences.nice_to_have_signals,
        preferred_locations=preferences.preferred_locations,
        avoid_signals=preferences.avoid_signals,
    )

    results: list[MatchedJob] = []
    scored_count = 0
    company_facets_cache: dict[str, Any] = {}

    for row in rows:
        if scored_count >= limit:
            break

        job_id = row["job_id"]
        blockers = row["hard_blockers"] or []

        if blockers:
            job = store.get_job(job_id)
            if job is None:
                continue
            evaluation = blocked_evaluation(job, blockers)
            results.append(
                MatchedJob(job_id, row["posting_id"], row["score"], evaluation, scored=False)
            )
            continue

        if not row["has_facets"]:
            continue

        job = store.get_job(job_id)
        if job is None:
            continue
        facets = store.get_job_facets(job_id)
        if facets is None:
            continue

        company_identity = normalize_company_name(job.company or "")
        if company_identity and company_identity not in company_facets_cache:
            company_facets_cache.update(store.get_company_facets_bulk([job.company or ""]))
        company = company_facets_cache.get(company_identity)

        evaluation = evaluate_job(job, facets, candidate_context, policy, ai, company)
        results.append(
            MatchedJob(job_id, row["posting_id"], row["score"], evaluation, scored=True)
        )
        scored_count += 1

    return results

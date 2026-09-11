"""Engine Lab: card selection and the measurement ledger (#257).

Full design: docs/superpowers/specs/2026-09-11-engine-lab-review-ledger-design.md.

This module is the whole of the "engine-owned interface" Engine Lab needs: it
decides which posting becomes which cohort's card, and is the only place that
writes an impression or a judgement. It has no identity or login logic of its
own -- the owner tried a bespoke Flask review page first and rejected it as
not worth building; whatever tool eventually calls into this module (Retool,
per issue #283, or otherwise) brings its own identity and its own database
credentials, and simply passes `reviewer_id` as a free-form string for
whoever is judging.

No function here ever calls an AI provider. Card selection reads only what
`matching.match_jobs` (#187) already computes and persists on a normal pipeline
run: `store.match_jobs`'s SQL ranking (score, hard blockers, facet presence) and
`store.get_evaluations_bulk`'s already-scored rationale text. A profile with no
extracted `CandidateContext` yet, or no CV on file, is reported as
`EngineLabUnavailable` with the reason -- never as a trigger to extract one.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from job_hunter.candidate_context import (
    CANDIDATE_CONTEXT_SCHEMA_VERSION,
    _cache_key,
    _context_from_dict,
    _hash,
)
from job_hunter.config import SupabaseSettings, _ai_model, _profile_row_to_legacy_dict
from job_hunter.http import HttpClient
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient

if TYPE_CHECKING:
    from job_hunter.postgres_store import PostgresJobStore

COHORTS = ("intended", "audit_hard_excluded", "audit_unresolved", "audit_below_threshold")

# The weight a bucket gets when it has a candidate this call. Renormalised
# over whichever buckets are actually non-empty, so an empty audit bucket
# never blocks a card from being produced. See the design doc for why this
# is a per-call random pick rather than a fixed daily slot order: a fixed
# order would let a reviewer learn the concealment by counting.
_COHORT_WEIGHTS = {
    "intended": 0.7,
    "audit_hard_excluded": 0.1,
    "audit_unresolved": 0.1,
    "audit_below_threshold": 0.1,
}

_MATCHING_VERSION_SCORED = "match_jobs-evaluations-v1"
_MATCHING_VERSION_FACET_ONLY = "hard-blockers-v1"
_EXPLANATION_VERSION_STUB = "why-line-stub-v1"

_CONFIGURATION_FIELDS = (
    "salary_floor_eur",
    "thresholds",
    "match_score_floor",
    "blocked_title_keywords",
)


class EngineLabUnavailable(RuntimeError):
    """No card can be produced right now, with the reason (AGENTS.md rule 5).

    Never raised for "nothing left in this bucket today" -- an exhausted
    corpus is a `select_next_card` call that simply finds no non-empty
    bucket, which is also this exception; the reason string is what tells
    the two apart for whoever reads it.
    """


@dataclass(frozen=True, slots=True)
class ReviewCard:
    impression_id: str
    posting_title: str
    posting_company: str
    posting_location: str
    posting_url: str
    why_line: str
    profile_version: str
    posting_version: str
    matching_version: str
    explanation_version: str
    configuration_version: str


@dataclass(frozen=True, slots=True)
class CohortSummary:
    cohort: str
    impressions: int
    judged: int
    worth_applying_rate: float | None
    helpful_rate: float | None


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def subject_store_client(http: HttpClient, settings: SupabaseSettings) -> SupabaseClient:
    """A client acting as the platform's own trusted process, scoped to `JOB_HUNTER_USER_ID`.

    The same identity every other Job Hunter process (the pipeline, the
    webhook) already acts as. Used to build the `PostgresJobStore` that
    `select_next_card` reads, and as the client every ledger write/read in
    this module goes through -- there is no separate per-reviewer identity
    or session here; whatever calls this module supplies its own `reviewer_id`
    as a plain string.
    """
    return SupabaseClient(http, settings, AccessTokenMinter(settings.user_id, settings.signing_key_jwk))


# Versions ----------------------------------------------------------------------


def _configuration_version(profile_data: dict[str, Any]) -> str:
    payload = {field: profile_data.get(field) for field in _CONFIGURATION_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# Card selection ------------------------------------------------------------------


def _why_line_for_scored(evaluation, score: int) -> str:
    if evaluation is not None and evaluation.rationale:
        return evaluation.rationale
    return f"Score {score}/100 -- no grounded reasoning recorded for this posting yet."


def _bucket_candidates(rows: list[dict[str, Any]], score_floor: int) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {cohort: [] for cohort in COHORTS}
    for row in rows:
        if row["hard_blockers"]:
            buckets["audit_hard_excluded"].append(row)
        elif not row["has_facets"]:
            buckets["audit_unresolved"].append(row)
        elif row["score"] >= score_floor:
            buckets["intended"].append(row)
        else:
            buckets["audit_below_threshold"].append(row)
    return buckets


def _choose_cohort(buckets: dict[str, list[dict[str, Any]]]) -> str:
    available = [cohort for cohort in COHORTS if buckets[cohort]]
    if not available:
        raise EngineLabUnavailable("no eligible postings in any cohort right now")
    weights = [_COHORT_WEIGHTS[cohort] for cohort in available]
    return random.choices(available, weights=weights, k=1)[0]


def _build_card(
    store: "PostgresJobStore",
    client: SupabaseClient,
    row: dict[str, Any],
    cohort: str,
    *,
    reviewer_id: str,
    profile_version: str,
    configuration_version: str,
) -> ReviewCard:
    job = store.get_job(row["job_id"])
    if job is None:
        raise EngineLabUnavailable(f"job {row['job_id']} vanished between selection and render")

    if cohort == "audit_hard_excluded":
        why_line = "Excluded: " + "; ".join(row["hard_blockers"])
        matching_version = _MATCHING_VERSION_FACET_ONLY
    elif cohort == "audit_unresolved":
        why_line = "Not enough information yet to evaluate this posting."
        matching_version = _MATCHING_VERSION_FACET_ONLY
    else:
        evaluation = store.get_evaluations_bulk([row["job_id"]]).get(row["job_id"])
        why_line = _why_line_for_scored(evaluation, row["score"])
        matching_version = _MATCHING_VERSION_SCORED

    posting_version = store.get_posting_description_hash(row["posting_id"]) or ""

    impression_rows = client.insert(
        "job_hunter_engine_lab_impressions",
        [
            {
                "reviewer_id": reviewer_id,
                "posting_id": row["posting_id"],
                "cohort": cohort,
                "profile_version": profile_version,
                "posting_version": posting_version,
                "matching_version": matching_version,
                "explanation_version": _EXPLANATION_VERSION_STUB,
                "configuration_version": configuration_version,
            }
        ],
    )
    impression_id = impression_rows[0]["id"]

    return ReviewCard(
        impression_id=impression_id,
        posting_title=job.title,
        posting_company=job.company,
        posting_location=job.location,
        posting_url=job.canonical_url or job.url,
        why_line=why_line,
        profile_version=profile_version,
        posting_version=posting_version,
        matching_version=matching_version,
        explanation_version=_EXPLANATION_VERSION_STUB,
        configuration_version=configuration_version,
    )


def shown_posting_ids_today(client: SupabaseClient, reviewer_id: str) -> set[str]:
    """Postings already shown to this reviewer since UTC midnight."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = client.select(
        "job_hunter_engine_lab_impressions",
        params={
            "reviewer_id": f"eq.{reviewer_id}",
            "shown_at": f"gte.{start.isoformat()}",
            "select": "posting_id",
        },
    )
    return {row["posting_id"] for row in rows}


def select_next_card(
    store: "PostgresJobStore",
    client: SupabaseClient,
    *,
    reviewer_id: str,
    already_shown_posting_ids: set[str],
) -> ReviewCard:
    """Pick one candidate, write its impression, then return the rendered card.

    The impression row exists in the database before this function returns:
    "durable before render" is a property of the return value, not a promise
    about it -- there is no code path that builds a `ReviewCard` without the
    insert above having already succeeded.
    """
    documents = store.get_source_documents()
    cv_text = documents.get("cv", "")
    if not cv_text.strip():
        raise EngineLabUnavailable("no CV is on file yet; nothing to match against")

    profile_result = store.get_search_profile()
    if profile_result is None:
        raise EngineLabUnavailable("no search profile exists yet for this account")
    profile_row, market_rows = profile_result
    profile_data = _profile_row_to_legacy_dict(profile_row, market_rows)
    score_floor = int(profile_data.get("match_score_floor", 80))
    configuration_version = _configuration_version(profile_data)

    profile_version = _cache_key(_hash(cv_text), _ai_model(), CANDIDATE_CONTEXT_SCHEMA_VERSION)
    cached = store.get_candidate_context(profile_version)
    if cached is None:
        raise EngineLabUnavailable(
            "no candidate context has been extracted yet for this profile; run the "
            "pipeline once first -- Engine Lab never makes its own AI call"
        )
    preferences = _context_from_dict(cached.context).preferences

    rows = store.match_jobs(
        preferred_roles=preferences.preferred_roles,
        preferred_seniority=preferences.preferred_seniority,
        must_have_signals=preferences.must_have_signals,
        nice_to_have_signals=preferences.nice_to_have_signals,
        preferred_locations=preferences.preferred_locations,
        avoid_signals=preferences.avoid_signals,
    )
    rows = [row for row in rows if row["posting_id"] not in already_shown_posting_ids]

    buckets = _bucket_candidates(rows, score_floor)
    cohort = _choose_cohort(buckets)
    row = buckets[cohort][0]

    return _build_card(
        store,
        client,
        row,
        cohort,
        reviewer_id=reviewer_id,
        profile_version=profile_version,
        configuration_version=configuration_version,
    )


# Judgements ------------------------------------------------------------------------


def record_judgement(
    client: SupabaseClient,
    *,
    reviewer_id: str,
    impression_id: str,
    worth_applying: bool,
    why_line_judgement: str,
    problem_reason: str | None,
) -> None:
    if why_line_judgement not in ("helpful", "flawed"):
        raise ValueError(f"why_line_judgement must be 'helpful' or 'flawed', got {why_line_judgement!r}")
    client.insert(
        "job_hunter_engine_lab_judgements",
        [
            {
                "impression_id": impression_id,
                "reviewer_id": reviewer_id,
                "worth_applying": worth_applying,
                "why_line_judgement": why_line_judgement,
                "problem_reason": problem_reason,
            }
        ],
    )


def reveal_cohort(client: SupabaseClient, impression_id: str) -> str:
    """The cohort a judged card actually belonged to -- read only after judging."""
    rows = client.select(
        "job_hunter_engine_lab_impressions",
        params={"id": f"eq.{impression_id}", "select": "cohort"},
    )
    if not rows:
        raise ValueError(f"no impression {impression_id}")
    return rows[0]["cohort"]


# Daily summary -----------------------------------------------------------------------


def daily_summary(client: SupabaseClient, day: date) -> list[CohortSummary]:
    """One row per known cohort, even a cohort with zero impressions that day.

    A plain `group by` over the day's rows would simply omit an empty
    cohort; starting from the four known cohort names and folding counts
    onto them is what makes a missing cohort visible instead of absent.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    impressions = client.select(
        "job_hunter_engine_lab_impressions",
        params={
            "and": f"(shown_at.gte.{start.isoformat()},shown_at.lt.{end.isoformat()})",
            "select": "id,cohort",
        },
    )

    ids_by_cohort: dict[str, list[str]] = {cohort: [] for cohort in COHORTS}
    for row in impressions:
        ids_by_cohort.setdefault(row["cohort"], []).append(row["id"])

    all_ids = [row["id"] for row in impressions]
    judgements: list[dict[str, Any]] = []
    for chunk in _chunked(all_ids, 200):
        judgements.extend(
            client.select(
                "job_hunter_engine_lab_judgements",
                params={"impression_id": f"in.({','.join(chunk)})"},
            )
        )
    judgement_by_impression = {row["impression_id"]: row for row in judgements}

    summaries = []
    for cohort in COHORTS:
        ids = ids_by_cohort.get(cohort, [])
        judged_rows = [judgement_by_impression[i] for i in ids if i in judgement_by_impression]
        judged = len(judged_rows)
        summaries.append(
            CohortSummary(
                cohort=cohort,
                impressions=len(ids),
                judged=judged,
                worth_applying_rate=(
                    sum(1 for j in judged_rows if j["worth_applying"]) / judged if judged else None
                ),
                helpful_rate=(
                    sum(1 for j in judged_rows if j["why_line_judgement"] == "helpful") / judged
                    if judged
                    else None
                ),
            )
        )
    return summaries

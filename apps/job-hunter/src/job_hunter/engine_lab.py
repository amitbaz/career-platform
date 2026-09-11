"""Engine Lab: the private review page's identity, card selection and ledger (#257).

Full design: docs/superpowers/specs/2026-09-11-engine-lab-review-ledger-design.md.

This module is the whole of the "engine-owned interface" Engine Lab needs: it is
the only place that decides who is signed in, which posting becomes which
cohort's card, and the only place that writes an impression or a judgement.
`engine_lab_web.py` turns what this module returns into HTML and turns a
request into a call here -- it contains no identity, selection, ranking or
explanation logic of its own.

Identity is real Supabase Auth (GoTrue's own emailed one-time code), not a
platform-minted token: `send_login_code`/`verify_login_code` call GoTrue's
`/auth/v1/otp` and `/auth/v1/verify` directly. A verified session's own
`access_token` is used as-is for that person's PostgREST calls -- there is no
custom JWT claim anywhere in this design. "The owner" is decided once, by
this module comparing the verified email against `ENGINE_LAB_OWNER_EMAIL`;
everything after that is enforced by the three `job_hunter_engine_lab_*`
security-definer functions the migration defines.

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
import time
from dataclasses import dataclass, replace
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

# GoTrue expires an access token after roughly an hour; refresh once fewer
# than this many seconds remain, mirroring AccessTokenMinter's own margin.
_SESSION_REFRESH_MARGIN_SECONDS = 60


class EngineLabUnavailable(RuntimeError):
    """No card can be produced right now, with the reason (AGENTS.md rule 5).

    Never raised for "nothing left in this bucket today" -- an exhausted
    corpus is a `select_next_card` call that simply finds no non-empty
    bucket, which is also this exception; the reason string is what tells
    the two apart for whoever reads it.
    """


class LoginError(RuntimeError):
    """A GoTrue email/code exchange failed, with its own reported reason."""


@dataclass(frozen=True, slots=True)
class AuthSession:
    """One verified Supabase Auth session -- a real `auth.users` identity.

    `access_token` is GoTrue's own JWT, used as-is for this person's
    PostgREST calls; this module never mints one itself.
    """

    user_id: str
    email: str
    access_token: str
    refresh_token: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class Collaborator:
    user_id: str
    email: str
    is_owner: bool
    revoked_at: str | None


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


# Login: real Supabase Auth, emailed one-time code -----------------------------


def _auth_headers(settings: SupabaseSettings) -> dict[str, str]:
    return {"apikey": settings.publishable_key, "Content-Type": "application/json"}


def _auth_error_message(response) -> str:
    try:
        payload = response.json()
    except Exception:
        return f"Supabase Auth request failed with HTTP {response.status_code}"
    return (
        payload.get("msg")
        or payload.get("error_description")
        or payload.get("error")
        or f"Supabase Auth request failed with HTTP {response.status_code}"
    )


def _session_from_payload(payload: dict[str, Any]) -> AuthSession:
    user = payload["user"]
    return AuthSession(
        user_id=user["id"],
        email=user.get("email") or "",
        access_token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        expires_at=time.time() + float(payload.get("expires_in", 3600)),
    )


def send_login_code(http: HttpClient, settings: SupabaseSettings, email: str) -> None:
    """Ask GoTrue to email `email` a one-time login code.

    `create_user=True` so a first-time invitee (who has no `auth.users` row
    yet) can still receive one -- their Engine Lab access is decided
    afterwards, by whether they can claim an invite, not by whether GoTrue
    happens to already know them.
    """
    response = http.post(
        f"{settings.url}/auth/v1/otp",
        headers=_auth_headers(settings),
        json={"email": email, "create_user": True},
    )
    if response.status_code >= 400:
        raise LoginError(_auth_error_message(response))


def verify_login_code(http: HttpClient, settings: SupabaseSettings, email: str, code: str) -> AuthSession:
    """Exchange an emailed code for a real Supabase Auth session."""
    response = http.post(
        f"{settings.url}/auth/v1/verify",
        headers=_auth_headers(settings),
        json={"type": "email", "email": email, "token": code},
    )
    if response.status_code >= 400:
        raise LoginError(_auth_error_message(response))
    return _session_from_payload(response.json())


def refresh_session(http: HttpClient, settings: SupabaseSettings, refresh_token: str) -> AuthSession | None:
    """Exchange a refresh token for a fresh session, or `None` if it no longer works."""
    response = http.post(
        f"{settings.url}/auth/v1/token",
        params={"grant_type": "refresh_token"},
        headers=_auth_headers(settings),
        json={"refresh_token": refresh_token},
    )
    if response.status_code >= 400:
        return None
    return _session_from_payload(response.json())


def session_needs_refresh(session: AuthSession) -> bool:
    return session.expires_at - time.time() <= _SESSION_REFRESH_MARGIN_SECONDS


# Clients -----------------------------------------------------------------------


class _StaticToken:
    """Wraps an already-issued access token to satisfy `SupabaseClient`'s
    minter interface (`.user_id`, `.token()`).

    This is a *real* Supabase Auth session token, never one this platform
    minted itself -- see the module docstring.
    """

    def __init__(self, user_id: str, access_token: str) -> None:
        self.user_id = user_id
        self._access_token = access_token

    def token(self) -> str:
        return self._access_token


def session_client(http: HttpClient, settings: SupabaseSettings, session: AuthSession) -> SupabaseClient:
    """A `SupabaseClient` acting as one verified reviewer, using their own real session."""
    scoped_settings = replace(settings, user_id=session.user_id)
    return SupabaseClient(http, scoped_settings, _StaticToken(session.user_id, session.access_token))


def subject_store_client(http: HttpClient, settings: SupabaseSettings) -> SupabaseClient:
    """A client acting as the platform's own trusted process, scoped to `JOB_HUNTER_USER_ID`.

    This has nothing to do with who is logged into Engine Lab -- it is
    "whose corpus and profile are being matched", the same identity every
    other Job Hunter process (the pipeline, the webhook) already acts as.
    Used to build the `PostgresJobStore` that `select_next_card` reads.
    """
    return SupabaseClient(http, settings, AccessTokenMinter(settings.user_id, settings.signing_key_jwk))


# Collaborators -----------------------------------------------------------------


def _collaborator_from_row(row: dict[str, Any]) -> Collaborator:
    return Collaborator(
        user_id=row.get("user_id") or "",
        email=row["email"],
        is_owner=bool(row.get("is_owner")),
        revoked_at=row.get("revoked_at"),
    )


def bootstrap_owner_if_matching(client: SupabaseClient, *, verified_email: str, owner_email: str) -> None:
    """Claim the owner role for the current session, iff its email is the configured owner's.

    `owner_email` (`ENGINE_LAB_OWNER_EMAIL`) is the one fact this whole
    scheme rests on, and only this module -- never the database -- reads
    it. The RPC re-checks `verified_email` against the session's own JWT,
    so a mismatched call here would fail there too; the check here is what
    stops it being attempted at all for anyone else.
    """
    if verified_email.strip().lower() != owner_email.strip().lower():
        return
    client.rpc("job_hunter_engine_lab_bootstrap_owner", {"p_email": verified_email})


def claim_invite(client: SupabaseClient) -> bool:
    """Backfill the caller's own pending invite with their real user_id, if one exists."""
    result = client.rpc("job_hunter_engine_lab_claim_invite")
    return bool(result and result[0])


def invite_collaborator(client: SupabaseClient, email: str) -> None:
    """Owner-only (enforced by the RPC): add or un-revoke a collaborator by email."""
    client.rpc("job_hunter_engine_lab_invite", {"p_email": email})


def get_own_collaborator(client: SupabaseClient, user_id: str) -> Collaborator | None:
    """The caller's own collaborator row, or `None` if they are not (or no longer) one."""
    rows = client.select(
        "job_hunter_engine_lab_collaborators",
        params={"user_id": f"eq.{user_id}", "select": "user_id,email,is_owner,revoked_at"},
    )
    if not rows:
        return None
    collaborator = _collaborator_from_row(rows[0])
    return None if collaborator.revoked_at else collaborator


def list_collaborators(client: SupabaseClient) -> list[Collaborator]:
    """Every invited collaborator, active or not -- readable only by the owner (RLS)."""
    rows = client.select(
        "job_hunter_engine_lab_collaborators",
        params={"select": "user_id,email,is_owner,revoked_at", "order": "invited_at.asc"},
    )
    return [_collaborator_from_row(row) for row in rows]


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
    reviewer_client: SupabaseClient,
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

    impression_rows = reviewer_client.insert(
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


def shown_posting_ids_today(reviewer_client: SupabaseClient, reviewer_id: str) -> set[str]:
    """Postings already shown to this reviewer since UTC midnight."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = reviewer_client.select(
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
    reviewer_client: SupabaseClient,
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
        reviewer_client,
        row,
        cohort,
        reviewer_id=reviewer_id,
        profile_version=profile_version,
        configuration_version=configuration_version,
    )


# Judgements ------------------------------------------------------------------------


def record_judgement(
    reviewer_client: SupabaseClient,
    *,
    reviewer_id: str,
    impression_id: str,
    worth_applying: bool,
    why_line_judgement: str,
    problem_reason: str | None,
) -> None:
    if why_line_judgement not in ("helpful", "flawed"):
        raise ValueError(f"why_line_judgement must be 'helpful' or 'flawed', got {why_line_judgement!r}")
    reviewer_client.insert(
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

    Owner-only in practice: RLS only lets a plain collaborator see their own
    rows, so this only reflects the whole ledger when called with the
    owner's own session.

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

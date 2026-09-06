"""Row <-> model mapping helpers for the Postgres-backed job store.

Every later `PostgresJobStore` method reads and writes through these
functions, so the two type conversions PostgREST forces on the SQLite
original live in exactly one place:

- Timestamps are ``timestamptz`` in Postgres, not SQLite's naive TEXT.
  ``to_iso``/``from_iso`` always carry an explicit UTC offset so a
  timestamp is never compared as a bare string.
- ``remote`` is a nullable boolean. SQLite gave ``1``/``0``/``None``;
  Postgres (via PostgREST) gives ``True``/``False``/``None``. Both must
  map to the same Python value.

``touch()`` exists because ticket #66 deliberately added no database
trigger to maintain ``updated_at``: Python must set it on every write to
the tables that carry the column. Verified directly against
``supabase/migrations/202609060002_job_hunter_discovery_state.sql``, that
list is ``job_hunter_company_watch``, ``job_hunter_ai_quota_state``,
``job_hunter_pending_ai_work``, and ``job_hunter_gmail_sync_state`` --
*not* ``job_hunter_ats_registry``, which has no ``updated_at`` column at
all. (An earlier draft of the task brief named ``job_hunter_ats_registry``
instead of ``job_hunter_ai_quota_state``; the migration is the source of
truth here.)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from job_hunter.models import (
    AtsRegistryEntry,
    Evaluation,
    Job,
    Material,
    NavigationCard,
    NavigationSession,
)


def to_iso(value: datetime | None) -> str | None:
    """Render a datetime as UTC-normalised ISO-8601 with an explicit offset.

    A naive datetime is treated as already being UTC (matching the SQLite
    original's convention) rather than raising or guessing the local zone.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 string (as PostgREST returns for timestamptz) to UTC."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def touch(values: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``values`` with ``updated_at`` set to now (UTC, ISO-8601).

    Callers writing to ``job_hunter_company_watch``, ``job_hunter_ai_quota_state``,
    ``job_hunter_pending_ai_work``, or ``job_hunter_gmail_sync_state`` must
    route every insert/update through this -- there is no trigger doing it
    for them.
    """
    result = dict(values)
    result["updated_at"] = to_iso(datetime.now(timezone.utc))
    return result


def _to_optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def job_from_row(row: dict[str, Any]) -> Job:
    """Map a ``job_hunter_jobs`` PostgREST row to a `Job`.

    ``original_url``, ``market_hint``, ``source_page_html``, and
    ``availability`` are not persisted columns -- they stay at the `Job`
    dataclass defaults.
    """
    return Job(
        source=row.get("source") or "",
        title=row.get("title") or "",
        company=row.get("company") or "",
        location=row.get("location") or "",
        url=row.get("url") or "",
        description=row.get("description") or "",
        source_job_id=row.get("source_job_id"),
        remote=_to_optional_bool(row.get("remote")),
        canonical_url=row.get("canonical_url") or "",
        ats_provider=row.get("ats_provider"),
        ats_board=row.get("ats_board"),
        ats_job_id=row.get("ats_job_id"),
        market_id=row.get("market_id") or None,
        content_confidence=row.get("content_confidence") or "",
    )


def evaluation_from_row(row: dict[str, Any]) -> Evaluation:
    """Map a ``job_hunter_evaluations`` PostgREST row to an `Evaluation`.

    ``job_id`` is the row's uuid string, not a SQLite integer.
    """
    return Evaluation(
        job_id=row["job_id"],
        total_score=row.get("total_score", 0),
        scores=row.get("scores_json") or {},
        decision=row.get("decision") or "",
        hard_blockers=row.get("hard_blockers_json") or [],
        strengths=row.get("strengths_json") or [],
        gaps=row.get("gaps_json") or [],
        salary_note=row.get("salary_note") or "",
        location_note=row.get("location_note") or "",
        rationale=row.get("rationale") or "",
        model=row.get("model") or "",
        status=row.get("status") or "ok",
        market_id=row.get("market_id") or "",
        content_confidence=row.get("content_confidence_at_eval") or "",
        requirements=row.get("requirements_json") or {},
        raw_model_score=row.get("raw_model_score", 0),
    )


def material_from_row(row: dict[str, Any]) -> Material:
    """Map a ``job_hunter_materials`` PostgREST row to a `Material`."""
    return Material(
        job_id=row["job_id"],
        cover_letter_text=row.get("cover_letter_text") or "",
    )


def ats_entry_from_row(row: dict[str, Any]) -> AtsRegistryEntry:
    """Map a ``job_hunter_ats_registry`` PostgREST row to an `AtsRegistryEntry`.

    `AtsRegistryEntry`'s timestamp fields are typed ``str`` (matching the
    SQLite original's TEXT columns), so PostgREST's ISO-8601 strings pass
    through unchanged -- no `from_iso` conversion needed here.
    """
    return AtsRegistryEntry(
        provider=row["provider"],
        board_identifier=row["board_identifier"],
        company_name=row.get("company_name") or "",
        market_hint=row.get("market_hint") or "",
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_checked_at=row.get("last_checked_at"),
        last_success_at=row.get("last_success_at"),
        last_eligible_at=row.get("last_eligible_at"),
        last_job_count=row.get("last_job_count", 0),
        eligible_jobs_seen=row.get("eligible_jobs_seen", 0),
        consecutive_failures=row.get("consecutive_failures", 0),
        active=bool(row.get("active", True)),
        paused_until=row.get("paused_until"),
        rejected_reason=row.get("rejected_reason"),
    )


def navigation_session_from_row(row: dict[str, Any]) -> NavigationSession:
    """Map a ``job_hunter_telegram_navigation_sessions`` PostgREST row.

    ``cards_json`` is a jsonb column; PostgREST hands it back already
    decoded as a list of dicts.
    """
    cards = [
        NavigationCard(
            job_id=card["job_id"],
            title=card.get("title") or "",
            company=card.get("company") or "",
            location=card.get("location") or "",
            score=card.get("score", 0),
            url=card.get("url") or "",
            market_id=card.get("market_id") or "",
            market_note=card.get("market_note") or "",
            availability_note=card.get("availability_note") or "",
        )
        for card in row.get("cards_json") or []
    ]
    return NavigationSession(
        session_id=row["session_id"],
        cards=cards,
        telegram_message_id=row.get("telegram_message_id"),
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )

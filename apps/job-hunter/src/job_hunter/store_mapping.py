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
    CompanyFacets,
    Compensation,
    Evaluation,
    Job,
    JobFacets,
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


def posting_facts(row: dict[str, Any]) -> dict[str, Any]:
    """Return whichever half of ``row`` states the advertisement's own facts.

    A row selected with ``posting:job_hunter_postings(...)`` embedded says
    every posting-level fact twice: once in the columns ``job_hunter_jobs``
    still duplicates and once under ``posting``. The posting is the record
    of the advertisement (issue #177), so it answers all of them.

    Every key the posting carries wins, including an empty or false one:
    ``company = ''`` and ``remote = false`` are answers, not absences, and a
    per-field truthiness fallback would let a stale duplicate override them.
    A key the posting was not asked for falls through to the row, so a
    column added to the select on one side only reads as itself rather than
    silently as ``""`` -- but a posting-level column belongs in
    ``_POSTING_FACT_EMBED``'s list, which is what makes the posting answer it.

    ``posting_id`` is nullable, so a job row can still arrive without a
    posting: a direct insert (pgTAP fixtures,
    ``scripts/migrate_sqlite_to_postgres.py``) bypasses the RPC that writes
    one. Such a row falls back to its own columns, which still carry the
    same values, so this ticket is reversible on its own and CI is green
    whichever order the migrate batches land in.
    """
    posting = row.get("posting")
    return {**row, **posting} if isinstance(posting, dict) else row


def job_from_row(row: dict[str, Any]) -> Job:
    """Map a ``job_hunter_jobs`` PostgREST row to a `Job`.

    Composed from the posting plus the caller's membership row: everything
    the advertisement itself says comes from `posting_facts`, and what the
    job row alone knows comes from the job row. The `Job` is identical in
    content to the one the same select produced before the posting existed.

    Two fields come from the job row, for the same reason:

    - ``market_id`` is which markets *this user* matched the posting to.
    - ``url`` is the usable URL for the *merged* row. A job row can stand
      for several postings -- the fingerprint is source-scoped, so the same
      advertisement on an aggregator and on the employer's ATS is two
      postings (20260909100000), and `job_hunter_merge_jobs` collapses
      their job rows into one whose ``url`` is the resolved canonical URL.
      ``posting_id`` then names only one of those postings, and its ``url``
      is whatever *that* source was seen under -- the aggregator link, not
      the employer's. Reading it here would put the worse link in the
      digest. ``description`` has no such problem: the merge keeps the
      posting whose description it kept, so the pointer already names the
      row the surviving text came from.

      This is the one posting-level fact #177 leaves on the job row. #176
      has since made merging a posting-level decision -- merging two job
      rows across postings merges the postings behind them, and the
      redirect is recorded once for everyone -- so the evidence a job row
      accumulates now has somewhere else it could live. Whether `url`
      should move there is #178's question, not an oversight here: a job
      row still absorbs several postings, and until it stops doing so its
      `url` is the only one resolved across all of them.

    ``original_url``, ``market_hint``, ``source_page_html``, and
    ``availability`` are not persisted columns -- they stay at the `Job`
    dataclass defaults.
    """
    facts = posting_facts(row)
    return Job(
        source=facts.get("source") or "",
        title=facts.get("title") or "",
        company=facts.get("company") or "",
        location=facts.get("location") or "",
        url=row.get("url") or "",
        description=facts.get("description") or "",
        source_job_id=facts.get("source_job_id"),
        remote=_to_optional_bool(facts.get("remote")),
        canonical_url=facts.get("canonical_url") or "",
        ats_provider=facts.get("ats_provider"),
        ats_board=facts.get("ats_board"),
        ats_job_id=facts.get("ats_job_id"),
        market_id=row.get("market_id") or None,
        content_confidence=facts.get("content_confidence") or "",
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


def job_facets_from_row(row: dict[str, Any]) -> JobFacets:
    """Map a ``job_hunter_job_facets`` PostgREST row to a `JobFacets`.

    ``hiring_regions``, ``stack`` and ``source_supplied`` are ``text[]``
    columns, which PostgREST hands back as JSON arrays; ``requirements_json``
    is jsonb and comes back already decoded. Compensation is split across
    five columns so it stays filterable, and is reassembled here.
    """
    return JobFacets(
        seniority=row.get("seniority") or "unknown",
        remote_policy=row.get("remote_policy") or "unknown",
        relocation_policy=row.get("relocation_policy") or "unknown",
        hiring_regions=list(row.get("hiring_regions") or []),
        stack=list(row.get("stack") or []),
        compensation=Compensation(
            disclosed=bool(row.get("compensation_disclosed")),
            currency=row.get("compensation_currency") or "",
            minimum=row.get("compensation_min"),
            maximum=row.get("compensation_max"),
            period=row.get("compensation_period") or "",
        ),
        requirements=list(row.get("requirements_json") or []),
        source_supplied=list(row.get("source_supplied") or []),
        description_hash_at_extraction=row.get("description_hash_at_extraction") or "",
        model=row.get("model") or "",
    )


def company_facets_from_row(row: dict[str, Any]) -> CompanyFacets:
    """Map a ``job_hunter_companies`` PostgREST row to a `CompanyFacets`.

    Every dimension falls back to ``"unknown"`` rather than to an empty
    string: an absent value here means "not established", which is a real
    first-class value the ranking and the prompt both read, and an empty
    string is not one of them.
    """
    return CompanyFacets(
        identity=row.get("identity") or "",
        display_name=row.get("display_name") or "",
        industry=row.get("industry") or "unknown",
        business_model=row.get("business_model") or "unknown",
        stage=row.get("stage") or "unknown",
        size_band=row.get("size_band") or "unknown",
        headquarters_region=row.get("headquarters_region") or "unknown",
        source_supplied=list(row.get("source_supplied") or []),
        model=row.get("model") or "",
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

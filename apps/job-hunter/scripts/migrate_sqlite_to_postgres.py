"""One-shot migration of a legacy Job Hunter SQLite database into Postgres.

Run once per owner, by hand, against the SQLite file store.py used to write
before issue #70 deleted it. Safe to run more than once: every write is an
upsert against the destination table's user-scoped unique key (the same
constraints migration 202609060003 added), so a second run converges on the
same rows instead of duplicating them.

Import order matters. Tables are migrated in foreign-key order -- ``jobs``
first, everything that references a job next -- building an
``old_int_id -> new_uuid`` map as each table is inserted, because every
legacy integer primary key becomes a fresh Postgres uuid. Only ``jobs.id``
needs such a map, since it is the only destination id another migrated table
refers to.

Gmail intake and Telegram navigation are not migrated at all: issue #287
deleted them from the engine and dropped their tables
(``gmail_sync_state``, ``gmail_messages``, ``inbound_job_candidates``,
``review_deliveries`` and ``telegram_navigation_sessions``, migration
20260912140000), so their legacy rows have nowhere to land.
``application_events`` is the one table of that group that survives -- it is
a general application-lifecycle signal, not a Gmail one -- and is still
migrated below.

Three legacy tables are skipped outright rather than migrated, because nothing
depends on their historical content and the next real run rebuilds them from
scratch: ``pending_ai_work`` (a retry queue -- stale entries would just be
retried again), ``gemini_quota_state`` (a pause timer that should start fresh
rather than resume a stale pause from a different runtime), and
``candidate_context_cache`` (a cache keyed by a profile hash that would need
revalidating anyway).

Dangling foreign keys: a legacy row whose foreign key points at a job that
did not migrate (should not happen in an internally-consistent SQLite file,
but the legacy schema had no fingerprint-collision guard against exactly the
race this port fixes) is handled per table:

- ``job_sources``, ``evaluations``, ``materials``, ``deliveries``: the
  Postgres column is ``NOT NULL``, so the row is dropped and logged. There is
  no lossless way to keep provenance for a job that isn't there.
- ``company_watch.discovered_from_job_id``: since #204 this only matters for
  an automatic row, which now migrates onto the shared
  ``job_hunter_company_watch_health`` (see ``_migrate_company_watch``); that
  column there is unenforced provenance with no foreign key at all, but a
  dangling value is nulled anyway rather than kept wrong. A manual row is
  migrated without the column, because that table dropped it entirely --
  a manual watch never carried one.
- ``application_events.job_id``: nullable in Postgres for the same reason as
  ``company_watch`` -- an application event is itself a first-class signal
  (an email thread, a confidence score) independent of whether the job
  survived deduplication.
- Tables with no foreign key to another job_hunter table (``ats_registry``,
  ``search_api_usage``) migrate unconditionally. ``search_api_usage`` lands
  on ``job_hunter_platform_search_usage`` (issue #184): the legacy database
  was single-user, so its rows collapse onto the provider key with no
  ``user_id`` and no loss.

Timestamps: legacy SQLite stored naive ISO-8601 TEXT with no UTC offset.
Postgres columns are ``timestamptz``. The port's standing rule elsewhere
(``store_mapping.to_iso``) is that a *caller-supplied* naive datetime is
assumed to already be UTC rather than guessed at another zone -- it is never
refused, because the legacy writer (`datetime.now(timezone.utc).isoformat()`
minus the offset in older rows) always meant UTC in practice, and there is no
live caller here to ask for a correction. This migration applies the exact
same rule to historical strings for consistency, and — because silently
reinterpreting the clock of years-old data deserves to be auditable — logs
one INFO line per naive value encountered, naming the table, column and raw
string, so a run's log is a complete record of every assumption it made.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from job_hunter.config import load_ingestion_dsn, load_supabase_settings
from job_hunter.http import HttpClient
from job_hunter.pg import IngestionDatabase
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient

logger = logging.getLogger(__name__)

#: Rebuilt by the next real run; migrating stale rows would be actively wrong.
_SKIPPED_TABLES = ("pending_ai_work", "gemini_quota_state", "candidate_context_cache")


#: Mirrors `postgres_store._SUPPORTED_ATS_PROVIDERS`.
_SUPPORTED_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})


def _watch_endpoint_strength(
    careers_url: str, ats_provider: str | None, ats_identifier: str | None
) -> int:
    """Rank a watch endpoint: supported ATS > generic URL > company only.

    A duplicate of `postgres_store._watch_endpoint_strength`, kept local
    rather than imported: that one is a private module helper, and this
    script's own migration functions all decide things in the open rather
    than reaching into `PostgresJobStore`'s internals.
    """
    if ats_provider in _SUPPORTED_ATS_PROVIDERS and ats_identifier:
        return 3
    if careers_url:
        return 2
    return 1


def _iso(table: str, column: str, value: str | None) -> str | None:
    """Normalize a legacy TEXT timestamp to an offset-aware ISO-8601 string.

    A naive value is assumed UTC (see the module docstring) and the
    assumption is logged so it is auditable after the fact.
    """
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        logger.info(
            "migrate_sqlite_to_postgres: naive timestamp %s.%s=%r assumed UTC",
            table,
            column,
            value,
        )
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat()


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if not _table_exists(conn, table):
        return []
    conn.row_factory = sqlite3.Row
    cur = conn.execute(f"SELECT * FROM {table}")
    return [dict(row) for row in cur.fetchall()]


def _json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    return json.loads(value)


def _json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    return json.loads(value)


def _upsert_one(
    client: SupabaseClient, table: str, payload: dict[str, Any], *, on_conflict: str
) -> dict[str, Any]:
    rows = client.upsert(table, [payload], on_conflict=on_conflict)
    return rows[0]


def _upsert_posting(ingestion: Any, posting: dict[str, Any]) -> str:
    """Write one advertisement over the privileged connection, and return its id.

    Since #179 `job_hunter_postings` is writable only by the ingestion role,
    so this one write in the migration cannot go through the user's client
    the way every other write here does. Everything else this script writes
    is per-user and stays exactly where it was.

    Idempotent like the rest of the script: a second run of the same file
    conflicts on the fingerprint and updates rather than duplicating. It
    overwrites rather than merging because a legacy row is this owner's whole
    record of the advertisement and there is nothing else yet to preserve.
    """
    columns = list(posting)
    placeholders = ", ".join(["%s"] * len(columns))
    assignments = ", ".join(
        f"{column} = excluded.{column}" for column in columns if column != "fingerprint"
    )
    with ingestion.connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"insert into public.job_hunter_postings ({', '.join(columns)}) "
                f"values ({placeholders}) "
                f"on conflict (fingerprint) do update set {assignments} "
                "returning id",
                tuple(posting.values()),
            )
            return str(cursor.fetchone()[0])


def migrate(
    sqlite_path: Path, client: SupabaseClient, ingestion: Any
) -> dict[str, int]:
    """Migrate one legacy SQLite database into Postgres for ``client``'s user.

    Returns the number of rows migrated per destination table (Postgres
    table names, without the ``job_hunter_`` prefix stripped -- i.e. keys are
    the legacy table names this function reads, since that is the caller's
    frame of reference). Re-running with the same file and client is safe:
    every write is an upsert against a user-scoped unique key.

    ``ingestion`` is the privileged Postgres connection. It is required rather
    than optional because a legacy job row becomes an advertisement plus a
    membership of it (#178), and since #179 nobody but the ingestion role may
    write the advertisement -- so a migration without it could not write a
    single job.
    """
    counts: dict[str, int] = {}
    conn = sqlite3.connect(str(sqlite_path))
    try:
        job_id_map = _migrate_jobs(conn, client, ingestion, counts)
        _migrate_job_sources(conn, client, job_id_map, counts)
        _migrate_evaluations(conn, client, job_id_map, counts)
        _migrate_materials(conn, client, job_id_map, counts)
        _migrate_deliveries(conn, client, job_id_map, counts)
        _migrate_company_watch(conn, client, ingestion, job_id_map, counts)
        _migrate_ats_registry(conn, client, counts)
        _migrate_application_events(conn, client, job_id_map, counts)
        _migrate_search_api_usage(conn, client, counts)
        _migrate_gemini_usage(conn, client, counts)
    finally:
        conn.close()
    for table in _SKIPPED_TABLES:
        counts.setdefault(table, 0)
    return counts


def _migrate_jobs(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    ingestion: Any,
    counts: dict[str, int],
) -> dict[int, str]:
    job_id_map: dict[int, str] = {}
    migrated = 0
    for row in _rows(conn, "jobs"):
        # One legacy row becomes two: the advertisement, keyed by the
        # fingerprint it always carried, and this user's membership of it
        # (#178). The fingerprint's uniqueness moved to the posting with the
        # columns, so that is what the first upsert conflicts on and
        # `(user_id, posting_id)` is what the second one does.
        first_seen_at = _iso("jobs", "first_seen_at", row["first_seen_at"])
        last_seen_at = _iso("jobs", "last_seen_at", row["last_seen_at"])
        posting = {
            "fingerprint": row["fingerprint"],
            "source": row.get("source") or "",
            "source_job_id": row.get("source_job_id"),
            "url": row.get("url") or "",
            "canonical_url": row.get("canonical_url") or "",
            "company": row.get("company") or "",
            "title": row.get("title") or "",
            "location": row.get("location") or "",
            "remote": _bool(row.get("remote")),
            "description": row.get("description") or "",
            "description_hash": row.get("description_hash") or "",
            "content_confidence": row.get("content_confidence") or "",
            "ats_provider": row.get("ats_provider"),
            "ats_board": row.get("ats_board"),
            "ats_job_id": row.get("ats_job_id"),
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
        }
        posting_id = _upsert_posting(ingestion, posting)
        payload = {
            "user_id": client.user_id,
            "posting_id": posting_id,
            "market_id": row.get("market_id") or "",
            "status": row.get("status") or "new",
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
        }
        new_row = _upsert_one(
            client, "job_hunter_jobs", payload, on_conflict="user_id,posting_id"
        )
        job_id_map[row["id"]] = new_row["id"]
        migrated += 1
    counts["jobs"] = migrated
    return job_id_map


def _migrate_job_sources(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    job_id_map: dict[int, str],
    counts: dict[str, int],
) -> None:
    migrated = 0
    for row in _rows(conn, "job_sources"):
        new_job_id = job_id_map.get(row["job_id"])
        if new_job_id is None:
            logger.warning(
                "migrate_sqlite_to_postgres: dropping job_sources row %s -- job %s did not migrate",
                row["id"],
                row["job_id"],
            )
            continue
        payload = {
            "user_id": client.user_id,
            "job_id": new_job_id,
            "source": row["source"],
            "source_job_id": row.get("source_job_id"),
            "source_url": row.get("source_url") or "",
            "identity_key": row["identity_key"],
            "first_seen_at": _iso("job_sources", "first_seen_at", row["first_seen_at"]),
            "last_seen_at": _iso("job_sources", "last_seen_at", row["last_seen_at"]),
        }
        _upsert_one(client, "job_hunter_job_sources", payload, on_conflict="job_id,identity_key")
        migrated += 1
    counts["job_sources"] = migrated


def _migrate_evaluations(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    job_id_map: dict[int, str],
    counts: dict[str, int],
) -> None:
    migrated = 0
    for row in _rows(conn, "evaluations"):
        new_job_id = job_id_map.get(row["job_id"])
        if new_job_id is None:
            logger.warning(
                "migrate_sqlite_to_postgres: dropping evaluations row %s -- job %s did not migrate",
                row["id"],
                row["job_id"],
            )
            continue
        payload = {
            "user_id": client.user_id,
            "job_id": new_job_id,
            "total_score": row.get("total_score", 0),
            "raw_model_score": row.get("raw_model_score", 0),
            "scores_json": _json_dict(row.get("scores_json")),
            "decision": row.get("decision") or "",
            "hard_blockers_json": _json_list(row.get("hard_blockers_json")),
            "strengths_json": _json_list(row.get("strengths_json")),
            "gaps_json": _json_list(row.get("gaps_json")),
            "requirements_json": _json_dict(row.get("requirements_json")),
            "salary_note": row.get("salary_note") or "",
            "location_note": row.get("location_note") or "",
            "rationale": row.get("rationale") or "",
            "model": row.get("model") or "",
            "status": row.get("status") or "ok",
            "market_id": row.get("market_id") or "",
            "description_hash_at_eval": row.get("description_hash_at_eval") or "",
            "content_confidence_at_eval": row.get("content_confidence_at_eval") or "",
            "evaluated_at": _iso("evaluations", "evaluated_at", row["evaluated_at"]),
        }
        _upsert_one(
            client, "job_hunter_evaluations", payload, on_conflict="user_id,job_id,evaluated_at"
        )
        migrated += 1
    counts["evaluations"] = migrated


def _migrate_materials(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    job_id_map: dict[int, str],
    counts: dict[str, int],
) -> None:
    migrated = 0
    for row in _rows(conn, "materials"):
        new_job_id = job_id_map.get(row["job_id"])
        if new_job_id is None:
            logger.warning(
                "migrate_sqlite_to_postgres: dropping materials row %s -- job %s did not migrate",
                row["id"],
                row["job_id"],
            )
            continue
        payload = {
            "user_id": client.user_id,
            "job_id": new_job_id,
            "cover_letter_text": row.get("cover_letter_text") or "",
            "generated_at": _iso("materials", "generated_at", row["generated_at"]),
        }
        _upsert_one(
            client, "job_hunter_materials", payload, on_conflict="user_id,job_id,generated_at"
        )
        migrated += 1
    counts["materials"] = migrated


def _migrate_deliveries(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    job_id_map: dict[int, str],
    counts: dict[str, int],
) -> None:
    migrated = 0
    for row in _rows(conn, "deliveries"):
        new_job_id = job_id_map.get(row["job_id"])
        if new_job_id is None:
            logger.warning(
                "migrate_sqlite_to_postgres: dropping deliveries row %s -- job %s did not migrate",
                row["id"],
                row["job_id"],
            )
            continue
        payload = {
            "user_id": client.user_id,
            "job_id": new_job_id,
            "delivery_type": row.get("delivery_type") or "",
            "status": row.get("status") or "sent",
            "delivered_at": _iso("deliveries", "delivered_at", row["delivered_at"]),
            "telegram_message_id": row.get("telegram_message_id"),
        }
        _upsert_one(
            client,
            "job_hunter_deliveries",
            payload,
            on_conflict="user_id,job_id,delivery_type,delivered_at",
        )
        migrated += 1
    counts["deliveries"] = migrated


def _migrate_company_watch(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    ingestion: Any,
    job_id_map: dict[int, str],
    counts: dict[str, int],
) -> None:
    """Migrate one legacy watch row to wherever #204 now keeps it.

    A manual row (``promotion_source == "manual"``) still lands on the
    per-user ``job_hunter_company_watch``, minus the two columns #204
    dropped from it: ``promotion_source`` (every row left is one) and
    ``discovered_from_job_id`` (a manual watch never carried one).

    An automatic row instead promotes onto the shared
    ``job_hunter_company_watch_health``, over the privileged connection --
    #179's pattern, adopted by that table from creation. It is keyed on
    #198's company entity rather than the legacy row's own identity, so
    the entity is ensured first exactly as
    ``PostgresJobStore._ensure_company_id`` does: an insert that never
    overwrites an existing company's real facets, with ``extracted_at``
    pinned to the epoch so a bare stub still reads as never-extracted.
    ``discovered_from_job_id`` is nulled when dangling for the same
    audit-log reason the original did, even though the shared table's
    version of that column is unenforced provenance rather than a foreign
    key -- a dangling value there could not violate anything, but it would
    still be a wrong answer to "which job suggested this company" and the
    same warning applies either way.
    """
    migrated = 0
    for row in _rows(conn, "company_watch"):
        discovered_from = row.get("discovered_from_job_id")
        new_discovered_from = job_id_map.get(discovered_from) if discovered_from is not None else None
        if discovered_from is not None and new_discovered_from is None:
            logger.warning(
                "migrate_sqlite_to_postgres: company_watch row %s -- "
                "discovered_from_job_id %s did not migrate, nulling it",
                row["id"],
                discovered_from,
            )

        if row["promotion_source"] == "manual":
            payload = {
                "user_id": client.user_id,
                "company_name": row["company_name"],
                "normalized_company_name": row["normalized_company_name"],
                "careers_url": row.get("careers_url") or "",
                "ats_provider": row.get("ats_provider"),
                "ats_identifier": row.get("ats_identifier"),
                "confidence": row.get("confidence", 0),
                "active": bool(row.get("active", 1)),
                "paused_until": _iso("company_watch", "paused_until", row.get("paused_until")),
                "first_seen_at": _iso("company_watch", "first_seen_at", row["first_seen_at"]),
                "last_verified_at": _iso(
                    "company_watch", "last_verified_at", row.get("last_verified_at")
                ),
                "last_successful_check_at": _iso(
                    "company_watch", "last_successful_check_at", row.get("last_successful_check_at")
                ),
                "consecutive_failures": row.get("consecutive_failures", 0),
                "created_at": _iso("company_watch", "created_at", row["created_at"]),
                "updated_at": _iso("company_watch", "updated_at", row["updated_at"]),
            }
            _upsert_one(
                client,
                "job_hunter_company_watch",
                payload,
                on_conflict="user_id,normalized_company_name",
            )
        else:
            careers_url = row.get("careers_url") or ""
            ats_provider = row.get("ats_provider")
            ats_identifier = row.get("ats_identifier")
            confidence = row.get("confidence", 0)
            with ingestion.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "insert into public.job_hunter_companies "
                        "  (identity, display_name, extracted_at) "
                        "values (%s, %s, to_timestamp(0)) "
                        "on conflict (identity) do update set identity = excluded.identity "
                        "returning id",
                        (row["normalized_company_name"], row["company_name"]),
                    )
                    company_id = cursor.fetchone()[0]

                    # Ranked the same way a live promotion ranks repeated
                    # writes to one company (supported ATS beats a generic
                    # URL beats company-only; equal strength needs greater
                    # confidence to replace), so migrating more than one
                    # automatic row for the same employer -- more than one
                    # legacy database, or duplicate rows within one -- cannot
                    # have a weaker endpoint silently overwrite a stronger
                    # one already migrated.
                    cursor.execute(
                        "select careers_url, ats_provider, ats_identifier, confidence "
                        "  from public.job_hunter_company_watch_health "
                        " where company_id = %s",
                        (company_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        write_url, write_provider, write_identifier, write_confidence = (
                            careers_url, ats_provider, ats_identifier, confidence,
                        )
                    else:
                        existing_url, existing_provider, existing_identifier, existing_confidence = existing
                        candidate_strength = _watch_endpoint_strength(
                            careers_url, ats_provider, ats_identifier
                        )
                        existing_strength = _watch_endpoint_strength(
                            existing_url, existing_provider, existing_identifier
                        )
                        replace = candidate_strength > existing_strength or (
                            candidate_strength == existing_strength
                            and confidence > existing_confidence
                        )
                        if replace:
                            write_url, write_provider, write_identifier, write_confidence = (
                                careers_url, ats_provider, ats_identifier, confidence,
                            )
                        else:
                            write_url, write_provider, write_identifier, write_confidence = (
                                existing_url, existing_provider, existing_identifier, existing_confidence,
                            )

                    cursor.execute(
                        "insert into public.job_hunter_company_watch_health "
                        "  (company_id, careers_url, ats_provider, ats_identifier, "
                        "   confidence, discovered_from_job_id, first_seen_at, "
                        "   last_verified_at, last_successful_check_at, "
                        "   consecutive_failures, active, paused_until, "
                        "   created_at, updated_at) "
                        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                        "on conflict (company_id) do update set "
                        "  careers_url = excluded.careers_url, "
                        "  ats_provider = excluded.ats_provider, "
                        "  ats_identifier = excluded.ats_identifier, "
                        "  confidence = excluded.confidence, "
                        "  discovered_from_job_id = coalesce(excluded.discovered_from_job_id, "
                        "    public.job_hunter_company_watch_health.discovered_from_job_id), "
                        "  active = excluded.active, "
                        "  paused_until = excluded.paused_until, "
                        "  updated_at = excluded.updated_at",
                        (
                            company_id,
                            write_url,
                            write_provider,
                            write_identifier,
                            write_confidence,
                            new_discovered_from,
                            _iso("company_watch", "first_seen_at", row["first_seen_at"]),
                            _iso("company_watch", "last_verified_at", row.get("last_verified_at")),
                            _iso(
                                "company_watch",
                                "last_successful_check_at",
                                row.get("last_successful_check_at"),
                            ),
                            row.get("consecutive_failures", 0),
                            bool(row.get("active", 1)),
                            _iso("company_watch", "paused_until", row.get("paused_until")),
                            _iso("company_watch", "created_at", row["created_at"]),
                            _iso("company_watch", "updated_at", row["updated_at"]),
                        ),
                    )
        migrated += 1
    counts["company_watch"] = migrated


def _migrate_ats_registry(
    conn: sqlite3.Connection, client: SupabaseClient, counts: dict[str, int]
) -> None:
    migrated = 0
    for row in _rows(conn, "ats_registry"):
        payload = {
            "user_id": client.user_id,
            "provider": row["provider"],
            "board_identifier": row["board_identifier"],
            "company_name": row.get("company_name") or "",
            "market_hint": row.get("market_hint") or "",
            "first_seen_at": _iso("ats_registry", "first_seen_at", row["first_seen_at"]),
            "last_seen_at": _iso("ats_registry", "last_seen_at", row["last_seen_at"]),
            "last_checked_at": _iso("ats_registry", "last_checked_at", row.get("last_checked_at")),
            "last_success_at": _iso("ats_registry", "last_success_at", row.get("last_success_at")),
            "last_eligible_at": _iso(
                "ats_registry", "last_eligible_at", row.get("last_eligible_at")
            ),
            "last_job_count": row.get("last_job_count", 0),
            "eligible_jobs_seen": row.get("eligible_jobs_seen", 0),
            "consecutive_failures": row.get("consecutive_failures", 0),
            "active": bool(row.get("active", 1)),
            "paused_until": _iso("ats_registry", "paused_until", row.get("paused_until")),
            "rejected_reason": row.get("rejected_reason"),
        }
        _upsert_one(
            client,
            "job_hunter_ats_registry",
            payload,
            on_conflict="user_id,provider,board_identifier",
        )
        migrated += 1
    counts["ats_registry"] = migrated


def _migrate_application_events(
    conn: sqlite3.Connection,
    client: SupabaseClient,
    job_id_map: dict[int, str],
    counts: dict[str, int],
) -> None:
    migrated = 0
    for row in _rows(conn, "application_events"):
        legacy_job_id = row.get("job_id")
        new_job_id = job_id_map.get(legacy_job_id) if legacy_job_id is not None else None
        if legacy_job_id is not None and new_job_id is None:
            logger.warning(
                "migrate_sqlite_to_postgres: application_events row %s -- "
                "job %s did not migrate, nulling job_id",
                row["id"],
                legacy_job_id,
            )
        payload = {
            "user_id": client.user_id,
            "job_id": new_job_id,
            "event_type": row["event_type"],
            "occurred_at": _iso("application_events", "occurred_at", row["occurred_at"]),
            "source": row.get("source") or "gmail",
            "source_message_id": row["source_message_id"],
            "source_thread_id": row.get("source_thread_id"),
            "confidence": row.get("confidence", 0.0),
            "company": row.get("company") or "",
            "role_title": row.get("role_title") or "",
            "rationale": row.get("rationale") or "",
            "created_at": _iso("application_events", "created_at", row["created_at"]),
        }
        _upsert_one(
            client,
            "job_hunter_application_events",
            payload,
            on_conflict="user_id,source_message_id",
        )
        migrated += 1
    counts["application_events"] = migrated


def _migrate_search_api_usage(
    conn: sqlite3.Connection, client: SupabaseClient, counts: dict[str, int]
) -> None:
    """Carry the legacy per-user ledger onto the platform ledger (issue #184).

    The legacy SQLite database was single-user, so dropping ``user_id`` and
    keying on ``(provider, occurred_at)`` loses nothing: a row that used to
    be "this user's call" and a row that is now "a call against the key"
    name the same event.
    """
    migrated = 0
    for row in _rows(conn, "search_api_usage"):
        payload = {
            "provider": row["provider"],
            "occurred_at": _iso("search_api_usage", "occurred_at", row["occurred_at"]),
        }
        _upsert_one(
            client,
            "job_hunter_platform_search_usage",
            payload,
            on_conflict="provider,occurred_at",
        )
        migrated += 1
    counts["search_api_usage"] = migrated


def _migrate_gemini_usage(
    conn: sqlite3.Connection, client: SupabaseClient, counts: dict[str, int]
) -> None:
    """Carry the AI accounting ledger across (`gemini_usage` -> `job_hunter_ai_usage`).

    The destination is the renamed table from issue #70: one row per model
    call, read back by `ai_usage_rows` to pace against Gemini's rolling
    per-minute, per-day and token limits. Dropping it would leave those
    windows empty, so a migration part-way through a day would let the run
    exceed the free-tier daily cap it had already partly spent -- the same
    shape as an empty search budget ledger.

    `run_id` is nullable in the legacy schema but NOT NULL in Postgres, so a
    missing one becomes `'unknown'`, matching both the backfill in migration
    202609060003 and `PostgresJobStore.record_ai_usage`'s own fallback.
    `provider` does not exist in the legacy table, which predates any second
    provider; every row it holds is Gemini.
    """
    migrated = 0
    for row in _rows(conn, "gemini_usage"):
        payload = {
            "user_id": client.user_id,
            "provider": "gemini",
            "occurred_at": _iso("gemini_usage", "occurred_at", row["occurred_at"]),
            "run_id": row["run_id"] or "unknown",
            "model": row["model"],
            "purpose": row["purpose"],
            "status": row["status"],
            "estimated_input_tokens": row["estimated_input_tokens"],
            "prompt_tokens": row["prompt_tokens"],
            "output_tokens": row["output_tokens"],
            "thinking_tokens": row["thinking_tokens"],
            "cached_tokens": row["cached_tokens"],
            "total_tokens": row["total_tokens"],
            "http_status": row["http_status"],
            "error_code": row["error_code"],
        }
        _upsert_one(
            client,
            "job_hunter_ai_usage",
            payload,
            on_conflict="user_id,run_id,model,purpose,occurred_at",
        )
        migrated += 1
    counts["gemini_usage"] = migrated


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point: `python -m scripts.migrate_sqlite_to_postgres`.

    The module docstring says this migration is run once, by hand. That needs
    a way to actually run it, so this builds a `SupabaseClient` from the
    environment exactly as `cli._build_client` does -- meaning it targets
    whatever `SUPABASE_URL` points at. Point it at the local stack first and
    rehearse against a copy of the real file: the destination is decided by
    environment variables alone, and there is no confirmation prompt beyond
    the one below.

    Prints the per-table counts `migrate` returns, which is what the operator
    compares against the source database's own counts.
    """
    parser = argparse.ArgumentParser(
        prog="migrate_sqlite_to_postgres",
        description="Migrate one legacy Job Hunter SQLite file into Postgres.",
    )
    parser.add_argument(
        "--sqlite", required=True, type=Path, help="Path to the legacy SQLite file"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (required when stdin is not a terminal)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.sqlite.is_file():
        parser.error(f"no such SQLite file: {args.sqlite}")

    settings = load_supabase_settings()
    if not args.yes:
        print(f"About to migrate {args.sqlite} into {settings.url}")
        print(f"  as user {settings.user_id}")
        if input("Type 'migrate' to proceed: ").strip() != "migrate":
            print("Aborted.")
            return 1

    client = SupabaseClient(
        HttpClient(),
        settings,
        AccessTokenMinter(settings.user_id, settings.signing_key_jwk),
    )
    dsn = load_ingestion_dsn()
    if dsn is None:
        parser.error(
            "SUPABASE_DB_URL is not set. Since #179 a job's advertisement is "
            "written only by the privileged ingestion role, so this migration "
            "needs the direct Postgres connection as well as the user's."
        )
    ingestion = IngestionDatabase(dsn)
    try:
        counts = migrate(args.sqlite, client, ingestion)
    finally:
        ingestion.close()

    width = max(len(name) for name in counts)
    print(f"\nMigrated into {settings.url}:")
    for name in sorted(counts):
        print(f"  {name:<{width}}  {counts[name]:>7}")
    print(f"  {'TOTAL':<{width}}  {sum(counts.values()):>7}")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by hand
    raise SystemExit(main())

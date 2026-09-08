"""Tests for the one-shot SQLite -> Postgres data migration.

`build_legacy_db` recreates the legacy schema captured in
`.superpowers/sdd/2026-09-06-job-hunter-postgres-store-port/legacy-sqlite-ddl.txt`
(jobs, job_sources, evaluations, materials, deliveries) plus the
navigation_store.py / search_budget.py / store.py tables that lived
alongside it (company_watch, ats_registry, gmail_sync_state, gmail_messages,
inbound_job_candidates, application_events, review_deliveries,
search_api_usage, telegram_navigation_sessions) -- store.py itself is gone
(Task 14), so this is the only place that schema is reconstructed.
"""

from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from job_hunter.store_mapping import from_iso
from job_hunter.supabase_client import SupabaseClient
from scripts.migrate_sqlite_to_postgres import main as migration_main
from scripts.migrate_sqlite_to_postgres import migrate

_SCHEMA = """
CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint      TEXT    NOT NULL UNIQUE,
    source           TEXT    NOT NULL DEFAULT '',
    source_job_id    TEXT,
    url              TEXT    NOT NULL DEFAULT '',
    canonical_url    TEXT    NOT NULL DEFAULT '',
    company          TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL DEFAULT '',
    location         TEXT    NOT NULL DEFAULT '',
    remote           INTEGER,
    description      TEXT    NOT NULL DEFAULT '',
    description_hash TEXT    NOT NULL DEFAULT '',
    ats_provider     TEXT,
    ats_board        TEXT,
    ats_job_id       TEXT,
    market_id        TEXT    NOT NULL DEFAULT '',
    content_confidence TEXT NOT NULL DEFAULT '',
    first_seen_at    TEXT    NOT NULL,
    last_seen_at     TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'new'
);

CREATE TABLE job_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_job_id TEXT,
    source_url TEXT NOT NULL DEFAULT '',
    identity_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(job_id, identity_key)
);

CREATE TABLE company_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    normalized_company_name TEXT NOT NULL UNIQUE,
    careers_url TEXT NOT NULL DEFAULT '',
    ats_provider TEXT,
    ats_identifier TEXT,
    discovered_from_job_id INTEGER REFERENCES jobs(id),
    promotion_source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    first_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_successful_check_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE evaluations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                 INTEGER NOT NULL REFERENCES jobs(id),
    total_score            INTEGER NOT NULL DEFAULT 0,
    scores_json            TEXT    NOT NULL DEFAULT '{}',
    decision               TEXT    NOT NULL DEFAULT '',
    hard_blockers_json     TEXT    NOT NULL DEFAULT '[]',
    strengths_json         TEXT    NOT NULL DEFAULT '[]',
    gaps_json              TEXT    NOT NULL DEFAULT '[]',
    salary_note            TEXT    NOT NULL DEFAULT '',
    location_note          TEXT    NOT NULL DEFAULT '',
    rationale              TEXT    NOT NULL DEFAULT '',
    model                  TEXT    NOT NULL DEFAULT '',
    status                 TEXT    NOT NULL DEFAULT 'ok',
    description_hash_at_eval TEXT NOT NULL DEFAULT '',
    market_id TEXT NOT NULL DEFAULT '',
    content_confidence_at_eval TEXT NOT NULL DEFAULT '',
    requirements_json TEXT NOT NULL DEFAULT '{}',
    raw_model_score INTEGER NOT NULL DEFAULT 0,
    evaluated_at           TEXT    NOT NULL
);

CREATE TABLE materials (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL REFERENCES jobs(id),
    cover_letter_text TEXT    NOT NULL DEFAULT '',
    generated_at      TEXT    NOT NULL
);

CREATE TABLE deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL REFERENCES jobs(id),
    delivery_type       TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'sent',
    delivered_at        TEXT    NOT NULL,
    telegram_message_id TEXT
);

CREATE TABLE gmail_sync_state (
    account_id TEXT PRIMARY KEY,
    history_id TEXT,
    last_successful_sync_at TEXT,
    last_processed_message_at TEXT,
    backfill_completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE gmail_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
);

CREATE TABLE inbound_job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL DEFAULT 'gmail',
    source_message_id TEXT NOT NULL,
    source_candidate_key TEXT NOT NULL,
    source_platform TEXT NOT NULL DEFAULT '',
    source_job_id TEXT,
    url TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(origin, source_message_id, source_candidate_key)
);

CREATE TABLE application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'gmail',
    source_message_id TEXT NOT NULL UNIQUE,
    source_thread_id TEXT,
    confidence REAL NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role_title TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE review_deliveries (
    event_id INTEGER PRIMARY KEY REFERENCES application_events(id),
    delivered_at TEXT NOT NULL,
    telegram_message_id TEXT
);

CREATE TABLE ats_registry (
    provider TEXT NOT NULL,
    board_identifier TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    market_hint TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_eligible_at TEXT,
    last_job_count INTEGER NOT NULL DEFAULT 0,
    eligible_jobs_seen INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    rejected_reason TEXT,
    PRIMARY KEY(provider, board_identifier)
);

CREATE TABLE search_api_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE gemini_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    run_id TEXT,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    thinking_tokens INTEGER,
    cached_tokens INTEGER,
    total_tokens INTEGER,
    http_status INTEGER,
    error_code TEXT
);

CREATE TABLE telegram_navigation_sessions (
    session_id          TEXT PRIMARY KEY,
    cards_json          TEXT NOT NULL,
    telegram_message_id TEXT,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL
);
"""

_JOB_DEFAULTS = {
    "source": "",
    "source_job_id": None,
    "url": "",
    "canonical_url": "",
    "company": "",
    "title": "",
    "location": "",
    "remote": None,
    "description": "",
    "description_hash": "",
    "ats_provider": None,
    "ats_board": None,
    "ats_job_id": None,
    "market_id": "",
    "content_confidence": "",
    "first_seen_at": "2026-09-01T00:00:00+00:00",
    "last_seen_at": "2026-09-01T00:00:00+00:00",
    "status": "new",
}


def _insert(conn: sqlite3.Connection, table: str, defaults: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    for row in rows:
        merged = {**defaults, **row}
        columns = ", ".join(merged.keys())
        placeholders = ", ".join("?" for _ in merged)
        conn.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
            list(merged.values()),
        )


def build_legacy_db(
    tmp_path: Path,
    *,
    jobs: list[dict[str, Any]] | None = None,
    job_sources: list[dict[str, Any]] | None = None,
    evaluations: list[dict[str, Any]] | None = None,
    materials: list[dict[str, Any]] | None = None,
    deliveries: list[dict[str, Any]] | None = None,
    company_watch: list[dict[str, Any]] | None = None,
    ats_registry: list[dict[str, Any]] | None = None,
    gmail_sync_state: list[dict[str, Any]] | None = None,
    gmail_messages: list[dict[str, Any]] | None = None,
    inbound_job_candidates: list[dict[str, Any]] | None = None,
    application_events: list[dict[str, Any]] | None = None,
    review_deliveries: list[dict[str, Any]] | None = None,
    search_api_usage: list[dict[str, Any]] | None = None,
    gemini_usage: list[dict[str, Any]] | None = None,
    sessions: list[dict[str, Any]] | None = None,
) -> Path:
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA)
        _insert(conn, "jobs", _JOB_DEFAULTS, jobs or [])
        _insert(
            conn,
            "job_sources",
            {"source_job_id": None, "source_url": "", "first_seen_at": "2026-09-01T00:00:00+00:00", "last_seen_at": "2026-09-01T00:00:00+00:00"},
            job_sources or [],
        )
        _insert(
            conn,
            "evaluations",
            {
                "total_score": 0,
                "scores_json": "{}",
                "decision": "",
                "hard_blockers_json": "[]",
                "strengths_json": "[]",
                "gaps_json": "[]",
                "salary_note": "",
                "location_note": "",
                "rationale": "",
                "model": "",
                "status": "ok",
                "description_hash_at_eval": "",
                "market_id": "",
                "content_confidence_at_eval": "",
                "requirements_json": "{}",
                "raw_model_score": 0,
                "evaluated_at": "2026-09-01T00:00:00+00:00",
            },
            evaluations or [],
        )
        _insert(
            conn,
            "materials",
            {"cover_letter_text": "", "generated_at": "2026-09-01T00:00:00+00:00"},
            materials or [],
        )
        _insert(
            conn,
            "deliveries",
            {
                "delivery_type": "telegram_message",
                "status": "sent",
                "delivered_at": "2026-09-01T00:00:00+00:00",
                "telegram_message_id": None,
            },
            deliveries or [],
        )
        _insert(
            conn,
            "company_watch",
            {
                "careers_url": "",
                "ats_provider": None,
                "ats_identifier": None,
                "discovered_from_job_id": None,
                "confidence": 0,
                "active": 1,
                "paused_until": None,
                "last_verified_at": None,
                "last_successful_check_at": None,
                "consecutive_failures": 0,
                "first_seen_at": "2026-09-01T00:00:00+00:00",
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
                "promotion_source": "manual",
            },
            company_watch or [],
        )
        _insert(
            conn,
            "ats_registry",
            {
                "company_name": "",
                "market_hint": "",
                "first_seen_at": "2026-09-01T00:00:00+00:00",
                "last_seen_at": "2026-09-01T00:00:00+00:00",
                "last_checked_at": None,
                "last_success_at": None,
                "last_eligible_at": None,
                "last_job_count": 0,
                "eligible_jobs_seen": 0,
                "consecutive_failures": 0,
                "active": 1,
                "paused_until": None,
                "rejected_reason": None,
            },
            ats_registry or [],
        )
        _insert(
            conn,
            "gmail_sync_state",
            {
                "history_id": None,
                "last_successful_sync_at": None,
                "last_processed_message_at": None,
                "backfill_completed_at": None,
                "created_at": "2026-09-01T00:00:00+00:00",
                "updated_at": "2026-09-01T00:00:00+00:00",
            },
            gmail_sync_state or [],
        )
        _insert(
            conn,
            "gmail_messages",
            {
                "thread_id": None,
                "sender": "",
                "subject": "",
                "rationale": "",
                "occurred_at": "2026-09-01T00:00:00+00:00",
                "processed_at": "2026-09-01T00:00:00+00:00",
            },
            gmail_messages or [],
        )
        _insert(
            conn,
            "inbound_job_candidates",
            {
                "origin": "gmail",
                "source_platform": "",
                "source_job_id": None,
                "url": "",
                "company": "",
                "title": "",
                "location": "",
                "remote": None,
                "description": "",
                "created_at": "2026-09-01T00:00:00+00:00",
                "last_seen_at": "2026-09-01T00:00:00+00:00",
            },
            inbound_job_candidates or [],
        )
        _insert(
            conn,
            "application_events",
            {
                "job_id": None,
                "source": "gmail",
                "source_thread_id": None,
                "company": "",
                "role_title": "",
                "rationale": "",
                "occurred_at": "2026-09-01T00:00:00+00:00",
                "created_at": "2026-09-01T00:00:00+00:00",
                "confidence": 0.0,
            },
            application_events or [],
        )
        _insert(
            conn,
            "review_deliveries",
            {"delivered_at": "2026-09-01T00:00:00+00:00", "telegram_message_id": None},
            review_deliveries or [],
        )
        _insert(conn, "search_api_usage", {}, search_api_usage or [])
        _insert(
            conn,
            "gemini_usage",
            {
                "run_id": None,
                "status": "success",
                "estimated_input_tokens": 0,
                "prompt_tokens": None,
                "output_tokens": None,
                "thinking_tokens": None,
                "cached_tokens": None,
                "total_tokens": None,
                "http_status": None,
                "error_code": None,
            },
            gemini_usage or [],
        )
        _insert(conn, "telegram_navigation_sessions", {"telegram_message_id": None}, sessions or [])
        conn.commit()
    finally:
        conn.close()
    return db_path


def test_navigation_cards_get_their_job_ids_remapped(tmp_path, supabase_client: SupabaseClient):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[{"id": 41, "fingerprint": "fp-a", "title": "Dev"}],
        sessions=[
            {
                "session_id": "s1",
                "cards_json": json.dumps(
                    [
                        {
                            "job_id": 41,
                            "title": "Dev",
                            "company": "Acme",
                            "location": "",
                            "score": 88,
                            "url": "https://x",
                        }
                    ]
                ),
                "telegram_message_id": "77",
                "created_at": "2026-09-01T00:00:00+00:00",
                "expires_at": "2026-10-01T00:00:00+00:00",
            }
        ],
    )

    migrate(sqlite_path, supabase_client)

    stored = supabase_client.select(
        "job_hunter_telegram_navigation_sessions", params={"session_id": "eq.s1"}
    )[0]
    migrated_job = supabase_client.select("job_hunter_jobs", params={"fingerprint": "eq.fp-a"})[0]
    assert stored["cards_json"][0]["job_id"] == migrated_job["id"]
    assert stored["cards_json"][0]["job_id"] != 41


def test_cards_whose_job_did_not_migrate_are_dropped(tmp_path, supabase_client: SupabaseClient):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[],
        sessions=[
            {
                "session_id": "s2",
                "cards_json": json.dumps(
                    [{"job_id": 999, "title": "Gone", "company": "", "location": "", "score": 10, "url": ""}]
                ),
                "telegram_message_id": None,
                "created_at": "2026-09-01T00:00:00+00:00",
                "expires_at": "2026-10-01T00:00:00+00:00",
            }
        ],
    )

    migrate(sqlite_path, supabase_client)

    stored = supabase_client.select(
        "job_hunter_telegram_navigation_sessions", params={"session_id": "eq.s2"}
    )
    assert stored == [] or stored[0]["cards_json"] == []


def test_migration_is_rerunnable(tmp_path, supabase_client: SupabaseClient):
    sqlite_path = build_legacy_db(tmp_path, jobs=[{"id": 1, "fingerprint": "fp-x", "title": "Dev"}])
    first = migrate(sqlite_path, supabase_client)
    second = migrate(sqlite_path, supabase_client)
    assert first == second
    assert len(supabase_client.select("job_hunter_jobs", params={"fingerprint": "eq.fp-x"})) == 1


def test_migrate_returns_per_table_row_counts(tmp_path, supabase_client: SupabaseClient):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[{"id": 1, "fingerprint": "fp-count", "title": "Dev"}],
    )
    counts = migrate(sqlite_path, supabase_client)
    assert counts["jobs"] == 1
    assert counts["job_sources"] == 0
    # Rebuilt on the next real run -- never migrated.
    assert counts["pending_ai_work"] == 0
    assert counts["ai_quota_state"] == 0
    assert counts["candidate_context_cache"] == 0


def test_evaluations_and_materials_and_deliveries_are_remapped_and_preserved(
    tmp_path, supabase_client: SupabaseClient
):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[{"id": 5, "fingerprint": "fp-eval", "title": "Dev"}],
        evaluations=[{"job_id": 5, "total_score": 77, "decision": "advance", "evaluated_at": "2026-08-01T00:00:00+00:00"}],
        materials=[{"job_id": 5, "cover_letter_text": "Dear hiring manager", "generated_at": "2026-08-01T01:00:00+00:00"}],
        deliveries=[{"job_id": 5, "delivery_type": "telegram_message", "delivered_at": "2026-08-01T02:00:00+00:00"}],
    )

    migrate(sqlite_path, supabase_client)

    job = supabase_client.select("job_hunter_jobs", params={"fingerprint": "eq.fp-eval"})[0]
    evaluation = supabase_client.select(
        "job_hunter_evaluations", params={"job_id": f"eq.{job['id']}"}
    )[0]
    material = supabase_client.select(
        "job_hunter_materials", params={"job_id": f"eq.{job['id']}"}
    )[0]
    delivery = supabase_client.select(
        "job_hunter_deliveries", params={"job_id": f"eq.{job['id']}"}
    )[0]
    assert evaluation["total_score"] == 77
    assert evaluation["decision"] == "advance"
    assert material["cover_letter_text"] == "Dear hiring manager"
    assert delivery["delivery_type"] == "telegram_message"
    # Historical timestamps survive the port rather than being overwritten with "now".
    assert evaluation["evaluated_at"].startswith("2026-08-01T00:00:00")


def test_evaluation_row_is_dropped_when_its_job_did_not_migrate(
    tmp_path, supabase_client: SupabaseClient
):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[],
        evaluations=[{"job_id": 999, "total_score": 50, "evaluated_at": "2026-08-01T00:00:00+00:00"}],
    )

    counts = migrate(sqlite_path, supabase_client)

    assert counts["evaluations"] == 0


def test_company_watch_nulls_dangling_discovered_from_job_id(
    tmp_path, supabase_client: SupabaseClient
):
    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[],
        company_watch=[
            {
                "company_name": "Acme",
                "normalized_company_name": "acme",
                "discovered_from_job_id": 999,
                "promotion_source": "automatic",
                "confidence": 0.9,
            }
        ],
    )

    counts = migrate(sqlite_path, supabase_client)

    assert counts["company_watch"] == 1
    row = supabase_client.select(
        "job_hunter_company_watch", params={"normalized_company_name": "eq.acme"}
    )[0]
    assert row["discovered_from_job_id"] is None


def test_review_delivery_event_id_is_remapped(tmp_path, supabase_client: SupabaseClient):
    sqlite_path = build_legacy_db(
        tmp_path,
        application_events=[
            {
                "id": 7,
                "event_type": "applied",
                "source_message_id": "msg-7",
                "occurred_at": "2026-08-01T00:00:00+00:00",
                "created_at": "2026-08-01T00:00:00+00:00",
                "confidence": 0.9,
            }
        ],
        review_deliveries=[{"event_id": 7, "delivered_at": "2026-08-02T00:00:00+00:00"}],
    )

    migrate(sqlite_path, supabase_client)

    event = supabase_client.select(
        "job_hunter_application_events", params={"source_message_id": "eq.msg-7"}
    )[0]
    review = supabase_client.select(
        "job_hunter_review_deliveries", params={"event_id": f"eq.{event['id']}"}
    )
    assert len(review) == 1
    assert review[0]["event_id"] != 7


def test_review_delivery_is_dropped_when_its_event_did_not_migrate(
    tmp_path, supabase_client: SupabaseClient
):
    sqlite_path = build_legacy_db(
        tmp_path,
        application_events=[],
        review_deliveries=[{"event_id": 999, "delivered_at": "2026-08-02T00:00:00+00:00"}],
    )

    counts = migrate(sqlite_path, supabase_client)

    assert counts["review_deliveries"] == 0


def test_naive_legacy_timestamp_is_assumed_utc(tmp_path, supabase_client: SupabaseClient, caplog):
    import logging

    sqlite_path = build_legacy_db(
        tmp_path,
        jobs=[
            {
                "id": 1,
                "fingerprint": "fp-naive",
                "title": "Dev",
                "first_seen_at": "2026-08-01T12:00:00",
                "last_seen_at": "2026-08-01T12:00:00",
            }
        ],
    )

    with caplog.at_level(logging.INFO):
        migrate(sqlite_path, supabase_client)

    job = supabase_client.select("job_hunter_jobs", params={"fingerprint": "eq.fp-naive"})[0]
    assert job["first_seen_at"].startswith("2026-08-01T12:00:00")
    assert any("assumed UTC" in message for message in caplog.messages)


def test_tables_absent_from_the_sqlite_file_migrate_as_zero(tmp_path, supabase_client: SupabaseClient):
    db_path = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()

    counts = migrate(db_path, supabase_client)

    assert all(value == 0 for value in counts.values())


def test_ai_usage_rows_are_carried_with_their_token_accounting(
    tmp_path, supabase_client: SupabaseClient
):
    """The AI ledger must survive the move, values intact.

    `ai_usage_rows` reads this table back to pace against Gemini's
    rolling per-minute, per-day and token limits, so dropping it would leave
    those windows empty and let a run exceed a daily cap it had already
    partly spent.
    """
    sqlite_path = build_legacy_db(
        tmp_path,
        gemini_usage=[
            {
                "occurred_at": "2026-09-02T10:05:37.841539+00:00",
                "run_id": "33617613605",
                "model": "gemini-3.6-flash",
                "purpose": "gmail_semantic",
                "status": "success",
                "estimated_input_tokens": 5172,
                "prompt_tokens": 8388,
                "output_tokens": 657,
                "total_tokens": 9045,
            }
        ],
    )

    counts = migrate(sqlite_path, supabase_client)

    assert counts["gemini_usage"] == 1
    stored = supabase_client.select(
        "job_hunter_ai_usage", params={"run_id": "eq.33617613605"}
    )
    assert len(stored) == 1
    row = stored[0]
    assert row["provider"] == "gemini"
    assert row["model"] == "gemini-3.6-flash"
    assert row["purpose"] == "gmail_semantic"
    assert row["status"] == "success"
    assert row["estimated_input_tokens"] == 5172
    assert row["prompt_tokens"] == 8388
    assert row["output_tokens"] == 657
    assert row["total_tokens"] == 9045
    assert from_iso(row["occurred_at"]) == datetime(
        2026, 9, 2, 10, 5, 37, 841539, tzinfo=timezone.utc
    )


def test_gemini_usage_row_without_a_run_id_becomes_unknown(
    tmp_path, supabase_client: SupabaseClient
):
    """`run_id` is nullable in SQLite and NOT NULL in Postgres.

    Migration 202609060003 backfilled existing nulls to 'unknown' and
    `record_ai_usage` uses the same sentinel, so a legacy row with no
    run id must land on it rather than failing the insert.
    """
    sqlite_path = build_legacy_db(
        tmp_path,
        gemini_usage=[
            {
                "occurred_at": "2026-09-02T11:00:00+00:00",
                "run_id": None,
                "model": "gemini-3.6-flash",
                "purpose": "evaluation",
                "status": "success",
            }
        ],
    )

    migrate(sqlite_path, supabase_client)

    stored = supabase_client.select(
        "job_hunter_ai_usage", params={"purpose": "eq.evaluation"}
    )
    assert [row["run_id"] for row in stored] == ["unknown"]


def test_cli_rejects_a_missing_sqlite_file(tmp_path, capsys):
    """The entry point fails on a bad path before it builds any client.

    `migrate` is destructive to the destination in the sense that it writes;
    a typo in `--sqlite` should stop at argument parsing rather than after a
    connection is opened.
    """
    with pytest.raises(SystemExit) as excinfo:
        migration_main(["--sqlite", str(tmp_path / "nope.sqlite3"), "--yes"])

    assert excinfo.value.code == 2
    assert "no such SQLite file" in capsys.readouterr().err


def test_cli_aborts_without_confirmation(tmp_path, monkeypatch, capsys):
    """Without `--yes` the operator must type the confirmation word.

    The destination is decided entirely by environment variables, so the
    prompt naming the URL and user is the only thing between a rehearsal
    against a local stack and a write to production.
    """
    sqlite_path = build_legacy_db(tmp_path)
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "aaaaaaaa-0000-0000-0000-000000000001")
    monkeypatch.setenv("SUPABASE_URL", "https://example.test")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "publishable")
    monkeypatch.setenv(
        "SUPABASE_SIGNING_KEY_B64",
        base64.b64encode(json.dumps({"kid": "k", "kty": "EC"}).encode()).decode(),
    )
    monkeypatch.setattr("builtins.input", lambda *_: "no")

    assert migration_main(["--sqlite", str(sqlite_path)]) == 1
    out = capsys.readouterr().out
    assert "https://example.test" in out
    assert "Aborted." in out

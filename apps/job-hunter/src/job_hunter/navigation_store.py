from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from typing import Protocol

from job_hunter.models import NavigationCard, NavigationSession

_CREATE_TELEGRAM_NAVIGATION_SESSIONS = """
CREATE TABLE IF NOT EXISTS telegram_navigation_sessions (
    session_id          TEXT PRIMARY KEY,
    cards_json          TEXT NOT NULL,
    telegram_message_id TEXT,
    created_at          TEXT NOT NULL,
    expires_at          TEXT NOT NULL
)
"""


class _StoreLike(Protocol):
    _conn: sqlite3.Connection


def ensure_navigation_schema(store: _StoreLike) -> None:
    with store._conn:
        store._conn.execute(_CREATE_TELEGRAM_NAVIGATION_SESSIONS)


def create_navigation_session(store: _StoreLike, session: NavigationSession) -> None:
    ensure_navigation_schema(store)
    cards_json = json.dumps([asdict(card) for card in session.cards])
    with store._conn:
        store._conn.execute(
            """
            INSERT OR REPLACE INTO telegram_navigation_sessions
                (session_id, cards_json, telegram_message_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session.session_id,
                cards_json,
                session.telegram_message_id,
                session.created_at,
                session.expires_at,
            ),
        )


def attach_navigation_message_id(store: _StoreLike, session_id: str, message_id: str) -> bool:
    with store._conn:
        cursor = store._conn.execute(
            "UPDATE telegram_navigation_sessions SET telegram_message_id=? WHERE session_id=?",
            (message_id, session_id),
        )
    return cursor.rowcount > 0


def get_navigation_session(store: _StoreLike, session_id: str) -> NavigationSession | None:
    try:
        row = store._conn.execute(
            """
            SELECT session_id, cards_json, telegram_message_id, created_at, expires_at
            FROM telegram_navigation_sessions WHERE session_id=?
            """,
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise

    if row is None:
        return None
    cards = [NavigationCard(**card) for card in json.loads(row["cards_json"])]
    return NavigationSession(
        session_id=row["session_id"],
        cards=cards,
        telegram_message_id=row["telegram_message_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
    )


def prune_navigation_sessions(store: _StoreLike, now_iso: str) -> int:
    ensure_navigation_schema(store)
    with store._conn:
        cursor = store._conn.execute(
            "DELETE FROM telegram_navigation_sessions WHERE expires_at < ?",
            (now_iso,),
        )
    return cursor.rowcount

import sqlite3

import pytest

from job_hunter.models import NavigationSession
from job_hunter.navigation_store import create_navigation_session, get_navigation_session
from job_hunter.store import JobStore


def test_read_only_store_reads_navigation_state_without_mutating_schema(tmp_path):
    db = tmp_path / "state.sqlite3"
    with JobStore(db) as writable:
        create_navigation_session(
            writable,
            NavigationSession(
                session_id="s1",
                cards=[],
                telegram_message_id="42",
                created_at="2026-09-01T00:00:00+00:00",
                expires_at="2099-01-01T00:00:00+00:00",
            ),
        )

    with JobStore(db, read_only=True) as readonly:
        session = get_navigation_session(readonly, "s1")
        assert session is not None
        assert session.telegram_message_id == "42"
        with pytest.raises(sqlite3.OperationalError):
            create_navigation_session(readonly, session)

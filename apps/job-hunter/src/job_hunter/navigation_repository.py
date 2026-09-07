from __future__ import annotations

from typing import Protocol

from job_hunter.models import NavigationSession
from job_hunter.postgres_store import PostgresJobStore


class NavigationSessionRepository(Protocol):
    def get_session(self, session_id: str) -> NavigationSession | None: ...


class PostgresNavigationRepository:
    """Reads Telegram navigation sessions straight from Postgres.

    Replaces the old `GitHubArtifactNavigationRepository`, which loaded a
    SQLite snapshot artifact from GitHub Actions and opened it read-only.
    There is no snapshot step anymore: the webhook process mints its own
    per-user token (via `store`'s underlying `SupabaseClient`) and reads
    the live `job_hunter_telegram_navigation_sessions` table directly.
    """

    def __init__(self, store: PostgresJobStore) -> None:
        self._store = store

    def get_session(self, session_id: str) -> NavigationSession | None:
        return self._store.get_navigation_session(session_id)

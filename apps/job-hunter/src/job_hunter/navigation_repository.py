from __future__ import annotations

from typing import Callable, Protocol

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

    `store_factory` is called at most once -- lazily, on the first
    `get_session` call, not at construction. Building a real store means
    building a `SupabaseClient`, which needs Supabase env vars; the
    webhook app depends on that construction being deferred so importing
    it (and serving `/health`) never requires those vars to be set, while
    a real callback still fails immediately the first time one is needed.
    """

    def __init__(self, store_factory: Callable[[], PostgresJobStore]) -> None:
        self._store_factory = store_factory
        self._store: PostgresJobStore | None = None

    def get_session(self, session_id: str) -> NavigationSession | None:
        if self._store is None:
            self._store = self._store_factory()
        return self._store.get_navigation_session(session_id)

from __future__ import annotations

from typing import Protocol

from job_hunter.models import NavigationSession
from job_hunter.navigation_store import get_navigation_session
from job_hunter.store import JobStore


class NavigationSessionRepository(Protocol):
    def get_session(self, session_id: str) -> NavigationSession | None: ...


class GitHubArtifactNavigationRepository:
    def __init__(self, state_loader) -> None:
        self._state_loader = state_loader

    def get_session(self, session_id: str) -> NavigationSession | None:
        snapshot = self._state_loader.load_latest()
        if snapshot is None:
            return None
        with JobStore(snapshot.path, read_only=True) as store:
            return get_navigation_session(store, session_id)

from pathlib import Path

import pytest

from job_hunter.github_state import ArtifactStateSnapshot
from job_hunter.models import NavigationCard, NavigationSession
from job_hunter.navigation_store import create_navigation_session
from job_hunter.store import JobStore


class FakeStateLoader:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    def load_latest(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.snapshot


def _snapshot_with_session(tmp_path: Path):
    db = tmp_path / "job_hunter.sqlite3"
    with JobStore(db) as store:
        create_navigation_session(
            store,
            NavigationSession(
                session_id="session-1",
                cards=[
                    NavigationCard(
                        1,
                        "Senior Frontend Developer",
                        "Example GmbH",
                        "Berlin",
                        87,
                        "https://example.test/job/1",
                    )
                ],
                telegram_message_id="42",
                created_at="2026-09-01T10:00:00+00:00",
                expires_at="2099-01-01T00:00:00+00:00",
            ),
        )
    return ArtifactStateSnapshot(7, db, "2026-09-01T10:00:00Z")


def test_repository_reads_session_from_latest_artifact(tmp_path):
    from job_hunter.navigation_repository import GitHubArtifactNavigationRepository

    loader = FakeStateLoader(_snapshot_with_session(tmp_path))
    repository = GitHubArtifactNavigationRepository(loader)

    session = repository.get_session("session-1")

    assert session is not None
    assert session.telegram_message_id == "42"
    assert session.cards[0].company == "Example GmbH"
    assert loader.calls == 1


def test_repository_returns_none_when_snapshot_is_missing():
    from job_hunter.navigation_repository import GitHubArtifactNavigationRepository

    repository = GitHubArtifactNavigationRepository(FakeStateLoader())
    assert repository.get_session("session-1") is None


def test_repository_returns_none_when_session_is_missing(tmp_path):
    from job_hunter.navigation_repository import GitHubArtifactNavigationRepository

    db = tmp_path / "job_hunter.sqlite3"
    with JobStore(db):
        pass
    snapshot = ArtifactStateSnapshot(8, db, "2026-09-01T10:00:00Z")
    repository = GitHubArtifactNavigationRepository(FakeStateLoader(snapshot))

    assert repository.get_session("missing") is None


def test_repository_opens_snapshot_read_only(monkeypatch, tmp_path):
    import job_hunter.navigation_repository as module

    calls = []

    class FakeStore:
        def __init__(self, path, *, read_only=False):
            calls.append((Path(path), read_only))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    monkeypatch.setattr(module, "JobStore", FakeStore)
    monkeypatch.setattr(module, "get_navigation_session", lambda store, session_id: None)
    snapshot = ArtifactStateSnapshot(9, tmp_path / "state.sqlite3", "2026-09-01T10:00:00Z")
    repository = module.GitHubArtifactNavigationRepository(FakeStateLoader(snapshot))

    repository.get_session("session-1")

    assert calls == [(snapshot.path, True)]


def test_repository_propagates_artifact_loader_failure():
    from job_hunter.navigation_repository import GitHubArtifactNavigationRepository

    repository = GitHubArtifactNavigationRepository(
        FakeStateLoader(error=RuntimeError("github unavailable"))
    )

    with pytest.raises(RuntimeError, match="github unavailable"):
        repository.get_session("session-1")

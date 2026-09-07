from job_hunter.navigation_repository import PostgresNavigationRepository


class FakeStore:
    def __init__(self, session=None, error=None):
        self.session = session
        self.error = error
        self.calls = []

    def get_navigation_session(self, session_id):
        self.calls.append(session_id)
        if self.error is not None:
            raise self.error
        return self.session


def test_repository_delegates_to_the_store():
    fake = FakeStore(session="a-session")
    repository = PostgresNavigationRepository(lambda: fake)

    result = repository.get_session("session-1")

    assert result == "a-session"
    assert fake.calls == ["session-1"]


def test_repository_returns_none_when_session_is_missing():
    fake = FakeStore(session=None)
    repository = PostgresNavigationRepository(lambda: fake)

    assert repository.get_session("missing") is None


def test_repository_propagates_store_failure():
    import pytest

    fake = FakeStore(error=RuntimeError("supabase unavailable"))
    repository = PostgresNavigationRepository(lambda: fake)

    with pytest.raises(RuntimeError, match="supabase unavailable"):
        repository.get_session("session-1")


def test_repository_calls_the_factory_at_most_once():
    """The factory builds a real store lazily on first use, then is cached.

    Building a `PostgresJobStore` means minting a fresh `SupabaseClient`;
    calling the factory again on every `get_session` would rebuild that
    unnecessarily on every webhook callback.
    """
    fake = FakeStore(session="a-session")
    calls = []

    def factory():
        calls.append(1)
        return fake

    repository = PostgresNavigationRepository(factory)

    repository.get_session("session-1")
    repository.get_session("session-2")

    assert len(calls) == 1
    assert fake.calls == ["session-1", "session-2"]

"""Tests for `PostgresJobStore`'s navigation-session methods.

These replace `test_navigation_store.py` (deleted along with
`navigation_store.py` in issue #70 task 12): the five free functions there
became `PostgresJobStore` methods, so these tests exercise them the same way
every other ported group is tested -- against the `store`/`supabase_client`
fixtures backed by the local Supabase stack.
"""

from __future__ import annotations

from job_hunter.models import NavigationCard, NavigationSession


def _session(**overrides) -> NavigationSession:
    defaults = dict(
        session_id="session-1",
        cards=[
            NavigationCard(1, "Senior FE", "Acme", "Berlin", 91, "https://example.test/1")
        ],
        telegram_message_id=None,
        created_at="2026-08-31T12:00:00+00:00",
        expires_at="2026-09-30T12:00:00+00:00",
    )
    defaults.update(overrides)
    return NavigationSession(**defaults)


def test_navigation_session_round_trip(store):
    store.create_navigation_session(_session())

    assert store.attach_navigation_message_id("session-1", "42") is True

    loaded = store.get_navigation_session("session-1")
    assert loaded is not None
    assert loaded.telegram_message_id == "42"
    assert loaded.cards[0].location == "Berlin"


def test_attach_navigation_message_id_is_false_for_unknown_session(store):
    """Proves the return value is pinned, not just always-True.

    A wrong implementation that returns `True` unconditionally (e.g. from an
    `update` call whose `_parse` swallowed the empty-list distinction) would
    pass the round-trip test above but pass this one too if it always
    returned `True` -- this asserts the negative case explicitly so an
    always-`True` stub fails here.
    """
    assert store.attach_navigation_message_id("does-not-exist", "99") is False


def test_get_navigation_session_is_none_for_unknown_session(store):
    assert store.get_navigation_session("does-not-exist") is None


def test_prune_navigation_sessions_deletes_expired_only_and_counts_them(store):
    expired_a = _session(
        session_id="expired-a",
        created_at="2026-08-01T00:00:00+00:00",
        expires_at="2026-08-31T00:00:00+00:00",
    )
    expired_b = _session(
        session_id="expired-b",
        created_at="2026-08-02T00:00:00+00:00",
        expires_at="2026-08-30T00:00:00+00:00",
    )
    active = _session(
        session_id="active",
        created_at="2026-08-31T00:00:00+00:00",
        expires_at="2026-09-30T00:00:00+00:00",
    )
    store.create_navigation_session(expired_a)
    store.create_navigation_session(expired_b)
    store.create_navigation_session(active)

    # Pins the count itself, not just "something got deleted": a wrong
    # implementation that deletes everything, or only ever reports 0 or 1,
    # both fail this exact assertion (two expired, one still active).
    assert store.prune_navigation_sessions("2026-09-01T00:00:00+00:00") == 2
    assert store.get_navigation_session("expired-a") is None
    assert store.get_navigation_session("expired-b") is None
    assert store.get_navigation_session("active") is not None


def test_create_navigation_session_upsert_converges_on_repeat_write(store):
    """A retried POST (HttpClient retries 5xx) must converge, not duplicate."""
    store.create_navigation_session(_session(telegram_message_id=None))
    store.create_navigation_session(_session(telegram_message_id="7"))

    loaded = store.get_navigation_session("session-1")
    assert loaded is not None
    assert loaded.telegram_message_id == "7"


def test_navigation_session_is_isolated_by_user(store, other_supabase_client):
    from job_hunter.postgres_store import PostgresJobStore

    store.create_navigation_session(_session())

    other_store = PostgresJobStore(other_supabase_client)
    assert other_store.get_navigation_session("session-1") is None

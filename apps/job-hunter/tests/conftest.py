"""Shared fixtures for the Job Hunter suite.

The store is Postgres now, so store-backed tests run against the local
Supabase stack rather than an in-process engine. Start it with
`supabase start` from the repository root (`pnpm db:key` once first).

Only tests that ask for `supabase_client`, `other_supabase_client`, or
`store` touch the stack at all: requesting one of them skips the test when
the stack isn't configured, and cleans both seed users' rows from every
job_hunter_* table (in foreign-key-safe order) before and after the test
runs. Every other test in the suite is unaffected. Deletes go through each user's own
client and token, never a service_role key, so a truncation bug cannot
reach another user's data.

The two seed users are not fixed. The run claims a pair from the pool in
`tests/seed_pool.py` for its whole session, so a suite in another worktree
holds a different pair and the two cannot see -- or delete -- each other's
rows. That is what lets them run at the same time instead of queueing
behind `scripts/stack_lock.py`.
"""

from __future__ import annotations

import base64
import json
import os
from contextlib import ExitStack, contextmanager

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.http import HttpClient
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient, SupabaseError
from tests.seed_pool import SeedSlot, claim_slot

_REQUIRED = (
    "SUPABASE_TEST_URL",
    "SUPABASE_TEST_PUBLISHABLE_KEY",
    "SUPABASE_TEST_SIGNING_KEY_B64",
)

# The seed users belong to whichever pool slot this run claimed; ask the
# `seed_users` fixture rather than naming a UUID. RLS only lets users
# supabase/seed.sql created see anything at all, so an invented UUID reads
# and writes nothing.

# Deletion order for every job_hunter_* table, children before parents, so a
# delete never trips a foreign-key violation. Derived from the actual
# constraints in supabase/migrations/202609060002_job_hunter_discovery_state.sql:
#
#   job_hunter_review_deliveries  -> job_hunter_application_events (event_id, user_id)
#   job_hunter_application_events -> job_hunter_jobs               (job_id, user_id), nullable
#   job_hunter_job_sources        -> job_hunter_jobs               (job_id, user_id)
#   job_hunter_company_watch      -> job_hunter_jobs               (discovered_from_job_id, user_id), nullable
#   job_hunter_evaluations        -> job_hunter_jobs               (job_id, user_id)
#   job_hunter_materials          -> job_hunter_jobs               (job_id, user_id)
#   job_hunter_deliveries         -> job_hunter_jobs               (job_id, user_id)
#   job_hunter_pending_ai_work    -> job_hunter_jobs               (job_id, user_id) on delete cascade
#   job_hunter_job_merges         -> job_hunter_jobs               (survivor_id, user_id) on delete cascade
#
# job_hunter_job_merges also holds a duplicate_id, deliberately without a
# foreign key: it names the row the merge deleted, which is the whole point of
# the record.
#
# The remaining tables (ats_registry, ai_usage, ai_quota_state,
# candidate_context_cache, search_api_usage, gmail_sync_state,
# gmail_messages, inbound_job_candidates, telegram_navigation_sessions)
# carry no foreign key to another job_hunter_* table, so their position
# relative to each other is unconstrained; they are listed after the
# tables whose position matters. job_hunter_jobs is the root every other
# table (transitively) hangs off of, so it must be last.
#
#   job_hunter_search_profile_markets -> job_hunter_search_profiles (profile_id, user_id)
#
# job_hunter_search_profiles carries no foreign key to job_hunter_jobs, so
# its own position relative to the other parentless tables is unconstrained;
# only its market child must come before it.
_TABLES_CHILD_FIRST = (
    "job_hunter_review_deliveries",
    "job_hunter_application_events",
    "job_hunter_job_sources",
    "job_hunter_company_watch",
    "job_hunter_evaluations",
    "job_hunter_materials",
    "job_hunter_deliveries",
    "job_hunter_pending_ai_work",
    "job_hunter_job_merges",
    "job_hunter_ats_registry",
    "job_hunter_ai_usage",
    "job_hunter_ai_quota_state",
    "job_hunter_candidate_context_cache",
    "job_hunter_search_api_usage",
    "job_hunter_gmail_sync_state",
    "job_hunter_gmail_messages",
    "job_hunter_inbound_job_candidates",
    "job_hunter_telegram_navigation_sessions",
    "job_hunter_search_profile_markets",
    "job_hunter_search_profiles",
    "job_hunter_jobs",
)


def _settings_for(user_id: str) -> SupabaseSettings:
    return SupabaseSettings(
        url=os.environ["SUPABASE_TEST_URL"].rstrip("/"),
        publishable_key=os.environ["SUPABASE_TEST_PUBLISHABLE_KEY"],
        user_id=user_id,
        signing_key_jwk=json.loads(
            base64.b64decode(os.environ["SUPABASE_TEST_SIGNING_KEY_B64"])
        ),
    )


def _client_for(user_id: str) -> SupabaseClient:
    settings = _settings_for(user_id)
    return SupabaseClient(
        HttpClient(), settings, AccessTokenMinter(user_id, settings.signing_key_jwk)
    )


# Postgres SQLSTATE for foreign_key_violation, which PostgREST returns in
# the body of its 409. See _truncate for why this one is worth naming.
_FOREIGN_KEY_VIOLATION = "23503"

_CONCURRENT_STACK_HELP = """\
Cleaning {table} hit a foreign-key violation. The rows this deletes had no
children a moment earlier, so something inserted one while this run was
cleaning up -- meaning another writer is using this run's seed user
{user_id} right now.

That should be impossible. This run claimed that user, with its partner,
from the pool in tests/seed_pool.py, and holds an flock on the slot for its
whole session -- so no other run using the pool can have been given it. Something bypassed
it: a worktree on a revision from before the pool existed (those use the
slot-0 pair unconditionally), a hand-run script, or a psql session.

Find the other writer and let it finish:

    git worktree list                  # other active workspaces
    ps -eo args | grep '[-]m pytest'   # other pytest runs
    ls ~/.cache/career-platform/seed-slots  # slots and the PIDs holding them

Then run again. See "Working alongside other sessions" in the repository
root AGENTS.md.\
"""


def _truncate(client: SupabaseClient) -> None:
    """Delete every row the client's user owns, in foreign-key-safe order.

    Deliberately does not swallow failures: a failed delete here means the
    next test starts on a dirty table, which would poison results silently
    across the whole suite. Let it fail loudly instead.

    The one failure worth translating is a foreign-key violation. The
    delete is a sequence of separate PostgREST calls rather than one
    transaction, so a row another session inserts between a child's delete
    and its parent's makes the parent's delete fail here -- and the
    resulting 409 says nothing about the actual cause, which is a second
    writer on a stack that is shared per machine.
    """
    for table in _TABLES_CHILD_FIRST:
        try:
            client.delete(table, params={"created_at": "gte.2000-01-01"})
        except SupabaseError as exc:
            if _FOREIGN_KEY_VIOLATION not in str(exc):
                raise
            raise RuntimeError(
                _CONCURRENT_STACK_HELP.format(
                    table=table, user_id=client.user_id
                )
            ) from exc


def _clean_seed_users(slot: SeedSlot) -> None:
    for user_id in (slot.user_a, slot.user_b):
        _truncate(_client_for(user_id))


@contextmanager
def _capture_suspended(config: pytest.Config):
    """Let writes reach the real terminal for the duration of the block.

    `capturemanager` is pytest's own plugin rather than published API, so
    a version that no longer offers it falls back to staying captured:
    losing the progress message is a worse experience, never a broken run.
    """
    manager = config.pluginmanager.getplugin("capturemanager")
    disabled = getattr(manager, "global_and_fixture_disabled", None)
    if disabled is None:
        yield
        return
    with disabled():
        yield


@pytest.fixture(scope="session")
def _stack_env() -> None:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "local Supabase stack not configured; missing " + ", ".join(missing)
        )


@pytest.fixture(scope="session")
def seed_users(_stack_env: None, pytestconfig: pytest.Config) -> SeedSlot:
    """Claim one pool slot for the whole session; release it at the end.

    Session-scoped because the unit of isolation is the run, not the test:
    a slot claimed per test would churn lockfiles and, worse, let a second
    run slip into the gap between two of this run's tests and clean rows
    out from under it.

    Blocks while every slot is taken. That is the behaviour every run had
    before the pool existed, so a busy machine is slower here and never
    wrong -- but it has to look like waiting rather than like a hang, and
    fixture setup runs inside pytest's global capture, which replays what
    it swallowed only when the item errors. A wait that ends well would
    therefore print nothing at all. Capture is suspended for the claim so
    the pool's progress reaches the terminal as it happens.
    """
    with ExitStack() as claim:
        with _capture_suspended(pytestconfig):
            slot = claim.enter_context(claim_slot())
        yield slot


@pytest.fixture
def _cleanup_seed_users(seed_users: SeedSlot):
    """Truncate both seed users' rows before and after a store-backed test.

    Depended on by `supabase_client`, `other_supabase_client`, and (via
    `supabase_client`) `store`, and cached per test by pytest, so it runs
    exactly once per test no matter how many of those three a test asks
    for. Tests that ask for none of them never evaluate this fixture, so
    they never touch the stack and never depend on it being up.
    """
    _clean_seed_users(seed_users)
    yield
    _clean_seed_users(seed_users)


@pytest.fixture
def supabase_client(
    _cleanup_seed_users: None, seed_users: SeedSlot
) -> SupabaseClient:
    return _client_for(seed_users.user_a)


@pytest.fixture
def other_supabase_client(
    _cleanup_seed_users: None, seed_users: SeedSlot
) -> SupabaseClient:
    return _client_for(seed_users.user_b)


@pytest.fixture
def store(supabase_client: SupabaseClient):
    from job_hunter.postgres_store import PostgresJobStore

    return PostgresJobStore(supabase_client)

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
"""

from __future__ import annotations

import base64
import json
import os

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.http import HttpClient
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient, SupabaseError

_REQUIRED = (
    "SUPABASE_TEST_URL",
    "SUPABASE_TEST_PUBLISHABLE_KEY",
    "SUPABASE_TEST_SIGNING_KEY_B64",
)

# The two fixed users supabase/seed.sql creates (also used by the pgTAP
# isolation suite, supabase/tests/pgtap/job_hunter_isolation.sql). Do not
# invent different UUIDs: RLS only lets these two see anything at all.
SEED_USER_A = "aaaaaaaa-0000-0000-0000-000000000001"
SEED_USER_B = "bbbbbbbb-0000-0000-0000-000000000002"

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
cleaning up -- meaning another session is using the shared local Supabase
stack right now.

The stack is one instance per machine, not one per worktree, and every
session's fixtures clean the same two seed users. Two runs at once therefore
delete each other's rows mid-test, which surfaces as a scatter of unrelated
failures ("assert [] == ['acme']") rather than as anything pointing here.

Find the other run and let it finish:

    git worktree list                  # other active workspaces
    ps -eo args | grep '[-]m pytest'   # other pytest runs
    docker ps                          # whether the stack is up

Then run again. See "Working alongside other sessions" in the repository
root AGENTS.md -- database-touching suites are meant to be serialised.\
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
                _CONCURRENT_STACK_HELP.format(table=table)
            ) from exc


def _clean_seed_users() -> None:
    for user_id in (SEED_USER_A, SEED_USER_B):
        _truncate(_client_for(user_id))


@pytest.fixture(scope="session")
def _stack_env() -> None:
    missing = [name for name in _REQUIRED if not os.environ.get(name)]
    if missing:
        pytest.skip(
            "local Supabase stack not configured; missing " + ", ".join(missing)
        )


@pytest.fixture
def _cleanup_seed_users(_stack_env: None):
    """Truncate both seed users' rows before and after a store-backed test.

    Depended on by `supabase_client`, `other_supabase_client`, and (via
    `supabase_client`) `store`, and cached per test by pytest, so it runs
    exactly once per test no matter how many of those three a test asks
    for. Tests that ask for none of them never evaluate this fixture, so
    they never touch the stack and never depend on it being up.
    """
    _clean_seed_users()
    yield
    _clean_seed_users()


@pytest.fixture
def supabase_client(_cleanup_seed_users: None) -> SupabaseClient:
    return _client_for(SEED_USER_A)


@pytest.fixture
def other_supabase_client(_cleanup_seed_users: None) -> SupabaseClient:
    return _client_for(SEED_USER_B)


@pytest.fixture
def store(supabase_client: SupabaseClient):
    from job_hunter.postgres_store import PostgresJobStore

    return PostgresJobStore(supabase_client)

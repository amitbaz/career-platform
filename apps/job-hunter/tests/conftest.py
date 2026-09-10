"""Shared fixtures for the Job Hunter suite.

The store is Postgres now, so store-backed tests run against the local
Supabase stack rather than an in-process engine. Start it with
`supabase start` from the repository root (`pnpm db:key` once first).

Only tests that ask for `supabase_client`, `other_supabase_client`, or
`store` touch the stack at all: requesting one of them skips the test when
the stack isn't configured, and cleans both seed users' rows from every
per-user job_hunter_* table (in foreign-key-safe order) before and after the
test runs. The shared tables -- the posting and its facets -- are not
cleaned; since #179 no user may delete a row in one, and even the privileged
role should not, because they are shared with every concurrent suite.
`_postings_unique_to_this_test` keeps each test on postings of its own
instead. Every other test in the suite is unaffected. Deletes go through each user's own
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
import hashlib
import json
import logging
import os
import uuid
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
    # Ingestion's direct, privileged connection. Required since #179, where it
    # stopped being an optimisation: the shared tables are writable only by the
    # privileged role, so a stack without this URL cannot persist a posting, a
    # facet, a company or a board at all. A suite run against such a stack
    # would not be a slower version of the real one -- it would be one where
    # every write path under test is unreachable, which is exactly the "green
    # because it never ran" failure #208 exists to stop.
    "SUPABASE_TEST_DB_URL",
)
_ALLOW_MISSING_STACK_ENV = "JOB_HUNTER_ALLOW_MISSING_STACK"
_ALLOW_MISSING_STACK_OPTION = "--allow-missing-stack"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        _ALLOW_MISSING_STACK_OPTION,
        action="store_true",
        default=False,
        help=(
            "run without the local Supabase stack and explicitly skip store-backed tests; "
            "valid only when none of the required SUPABASE_TEST_* variables is set"
        ),
    )


def _missing_stack_environment() -> tuple[str, ...]:
    return tuple(name for name in _REQUIRED if not os.environ.get(name))


def _missing_stack_is_allowed(config: pytest.Config) -> bool:
    return config.getoption(_ALLOW_MISSING_STACK_OPTION) or (
        os.environ.get(_ALLOW_MISSING_STACK_ENV) == "1"
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Reject accidental loss of all store-backed coverage before collection.

    This is a hard failure rather than a warning because a green run is evidence only when
    missing stack configuration cannot silently remove most of the suite. The opt-out remains
    available for deliberate non-database runs, but only a fully absent environment can use it:
    a partial environment is a configuration error, not an intention.
    """
    missing = _missing_stack_environment()
    if not missing:
        return

    stack_is_absent = len(missing) == len(_REQUIRED)
    if stack_is_absent and _missing_stack_is_allowed(session.config):
        return

    state = "not configured" if stack_is_absent else "partially configured"
    raise pytest.UsageError(
        f"local Supabase stack is {state}; missing {', '.join(missing)}. "
        "Configure all four required variables for the full suite. For a deliberate "
        "non-database run, unset all four and set JOB_HUNTER_ALLOW_MISSING_STACK=1 or pass "
        "--allow-missing-stack; partial configuration always fails."
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
# job_hunter_job_facets is deliberately absent, and so is job_hunter_postings.
# Since #175 neither carries a user_id -- both hang off the shared
# advertisement rather than off one person's copy of it -- so there is nothing
# for a per-user delete to match, and neither table has a delete policy for a
# user to delete through. `_postings_unique_to_this_test` is what keeps them
# from leaking between tests instead.
#
# job_hunter_job_merges also holds a duplicate_id, deliberately without a
# foreign key: it names the row the merge deleted, which is the whole point of
# the record.
#
# The remaining tables (ats_registry, ai_usage, ai_quota_state,
# candidate_context_cache, gmail_sync_state,
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


@pytest.fixture(autouse=True)
def _postings_unique_to_this_test():
    """Give each test postings no other test or run can collide with.

    A posting is shared: `job_hunter_postings` has no user_id, and since
    #175 neither do the facets hanging off it, so neither table is cleaned
    between tests the way per-user rows are. Two tests that wrote the same
    fingerprint would therefore read each other's facets -- the next test in
    this file, a later suite run on this machine, or a worktree running at
    the same time. Salting the fingerprint with one value per test gives
    each test postings of its own.

    Relationships inside a test are untouched: the same `Job` still hashes
    to the same fingerprint, so deduplication, merging and re-discovery
    behave exactly as they do in production, including across several
    pipeline runs in one test. Autouse because the salt has to be in place
    before the first store write, whether or not the test asked for a store.

    The cost is that each store-backed test leaves a posting and its facets
    behind: nothing can delete them, since neither table has a delete policy
    for a user to delete through. They accumulate on the local stack until
    the next `pnpm db:reset`, which is cheap next to a suite that reads
    another run's answers.
    """
    from job_hunter import postgres_store

    salt = uuid.uuid4().hex
    real = postgres_store.job_fingerprint

    def salted(job) -> str:
        return hashlib.sha256(f"{salt}:{real(job)}".encode("utf-8")).hexdigest()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(postgres_store, "job_fingerprint", salted)
        yield


@pytest.fixture(scope="session")
def _stack_env(pytestconfig: pytest.Config) -> None:
    missing = _missing_stack_environment()
    if missing:
        opt_out = (
            _ALLOW_MISSING_STACK_OPTION
            if pytestconfig.getoption(_ALLOW_MISSING_STACK_OPTION)
            else f"{_ALLOW_MISSING_STACK_ENV}=1"
        )
        pytest.skip(
            f"store-backed test skipped by explicit {opt_out} opt-out; "
            "the local Supabase stack is not configured"
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
def ingestion_database():
    """Ingestion's privileged connection, as a run holds one (#179).

    Every store fixture gets one, because since #179 a store without one
    cannot write a posting, a facet, a company or a board -- so a store
    built without one is not a store under test, it is the degraded mode.
    Tests that want *that* build their own store with `ingestion=None`.

    Closed at the end of each test so a suite does not accumulate one pool
    per test against a pooler with a bounded client budget.
    """
    from job_hunter.pg import IngestionDatabase

    database = IngestionDatabase(os.environ["SUPABASE_TEST_DB_URL"])
    try:
        # Purged on the way in rather than on the way out. A test is entitled
        # to close the store it was given -- some assert exactly that -- and a
        # psycopg pool cannot be reopened, so a teardown purge would fail for
        # a reason that has nothing to do with the test. Cleaning before each
        # test protects every test that runs after this one, which is the
        # whole point; the last test of a session leaves its messages for the
        # first test of the next.
        _purge_stage_queues(database)
        yield database
    finally:
        database.close()


#: The one stage queue a leftover message can make a test lie about, named as
#: `postgres_stage_queue._QUEUE_NAMES` names it. Duplicated rather than
#: imported so a rename there fails this cleanup loudly instead of silently
#: cleaning nothing.
_EXTRACT_FACETS_QUEUE_TABLE = "pgmq.q_job_hunter_extract_facets"


def _purge_stage_queues(database) -> None:
    """Drop orphaned extract_facets messages before every store-backed test.

    The queues are shared engine machinery with no user dimension, so the
    seed-user partitioning that keeps two concurrent runs apart does not reach
    them: a message another test left behind is drained by the next test that
    runs a pipeline, and it is drained *at the platform key's expense*. That
    turns "this run read one posting" into "this run read one posting and four
    of somebody else's", which is both a false assertion and a real cost.

    Orphaned, not all, and that distinction is what makes this safe to do on a
    stack another suite is using. A message names a posting; this run's own
    leftovers name postings whose membership rows `_clean_seed_users` has
    already deleted, so nothing holds them any more. A concurrently running
    suite's in-flight messages name postings it still holds a row on, and are
    left alone. The rule reads the same way outside the tests: a queued read
    for an advertisement nobody is a member of is work nobody asked for.

    Bounded wait and best-effort. Losing the race costs a test that may see
    another suite's messages; raising would fail a suite that did nothing
    wrong.
    """
    try:
        with database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local lock_timeout = '5s'")
                cursor.execute(
                    f"delete from {_EXTRACT_FACETS_QUEUE_TABLE} q "
                    " where q.message ? 'posting_id' "
                    "   and not exists ("
                    "         select 1 from public.job_hunter_jobs j "
                    "          where j.posting_id = (q.message->>'posting_id')::uuid)"
                )
    except Exception:
        logging.getLogger(__name__).warning(
            "could not clear orphaned messages from %s before this test; a "
            "concurrent suite is using it, so a facet-read count here may "
            "include its messages",
            _EXTRACT_FACETS_QUEUE_TABLE,
        )


@pytest.fixture
def seed_postings(ingestion_database):
    """Insert postings the way ingestion does, and return their ids.

    Since #179 a posting is writable only by the privileged role, so a test
    that needs one to hang a membership row off cannot insert it through
    PostgREST as a user any more. This is the seam that used to be
    `client.insert("job_hunter_postings", ...)`, and it is a fixture rather
    than a helper so the connection is the same one the store under test
    holds.

    Takes whole rows so a caller can pin whichever columns its assertion is
    about and leave the rest to their defaults, exactly as the PostgREST
    insert did.
    """

    def _seed(rows: list[dict]) -> list[str]:
        ids: list[str] = []
        with ingestion_database.connection() as connection:
            with connection.cursor() as cursor:
                for row in rows:
                    columns = ", ".join(row)
                    placeholders = ", ".join(["%s"] * len(row))
                    cursor.execute(
                        f"insert into public.job_hunter_postings ({columns}) "
                        f"values ({placeholders}) returning id",
                        tuple(row.values()),
                    )
                    ids.append(str(cursor.fetchone()[0]))
        return ids

    return _seed


@pytest.fixture
def store(supabase_client: SupabaseClient, ingestion_database):
    from job_hunter.postgres_store import PostgresJobStore

    return PostgresJobStore(supabase_client, ingestion_database)


@pytest.fixture
def other_store(other_supabase_client: SupabaseClient, ingestion_database):
    """A second user's store, on the same stack and the same postings.

    What the two users share is exactly what #175 makes shared: the posting
    row and its facets. Everything either one writes about their own
    relationship to a job stays invisible to the other.

    It shares the ingestion connection with `store`, which is faithful: the
    privileged role has no user identity, so there is not a second one to
    hold. What keeps the two users apart is the user id each store passes
    into `job_hunter_upsert_job`, and a test that proves isolation is
    proving that argument is honoured.
    """
    from job_hunter.postgres_store import PostgresJobStore

    return PostgresJobStore(other_supabase_client, ingestion_database)

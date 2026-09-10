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
from datetime import datetime, timezone

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

    Every module that computes a fingerprint has to be patched here, not
    just the one a given test happens to exercise: each does
    `from ...normalize import job_fingerprint`, which binds the name into
    its own module at import time, long before this fixture runs, so a
    patch aimed at `normalize` itself would reach neither. `postgres_store`
    (writes a posting) and `crawl_source` (looks one up by fingerprint,
    issue #184) are the two today. A test that seeds through one and reads
    through the other -- exactly what `test_crawl_source.py`'s hash
    short-circuit test does -- silently compares a salted fingerprint
    against an unsalted one unless both bindings carry the same salt. A
    third module computing a fingerprint needs a third line added here, or
    it will disagree with these two the same way.
    """
    from job_hunter import crawl_source, postgres_store

    salt = uuid.uuid4().hex
    real = postgres_store.job_fingerprint

    def salted(job) -> str:
        return hashlib.sha256(f"{salt}:{real(job)}".encode("utf-8")).hexdigest()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(postgres_store, "job_fingerprint", salted)
        patch.setattr(crawl_source, "job_fingerprint", salted)
        yield


@pytest.fixture(autouse=True)
def _company_watches_owned_by_this_test(monkeypatch):
    """Keep company-watch discovery on the watches this test created.

    Automatic watches live in the shared `job_hunter_company_watch_health`
    (#204), which has no user_id and no delete policy, so every test that
    promotes one leaves it behind -- due, and pointing at a made-up careers
    host. Unfiltered, the next `run_pipeline` anywhere in the suite (this
    run, a later one, or another xdist worker right now) scans that backlog
    for real: DNS failures, HTTP retry backoff through the same
    `time.sleep` a test may be counting, and failure counters another test
    asserts on.

    Filtering by id rather than by name covers every way a test creates a
    watch -- directly, through `watchlist.promote_company`, or inside a
    pipeline run -- since they all go through `upsert_company_watch`. A
    manual watch is per-user and already cleaned between tests; tracking it
    too costs nothing and keeps the rule to one line.
    """
    from job_hunter.postgres_store import PostgresJobStore

    watch_ids: set[str] = set()
    real_upsert = PostgresJobStore.upsert_company_watch
    real_list_due = PostgresJobStore.list_due_company_watches

    def upsert_for_this_test(store, **kwargs):
        watch_id = real_upsert(store, **kwargs)
        if watch_id is not None:
            watch_ids.add(watch_id)
        return watch_id

    def list_due_for_this_test(store, *args, **kwargs):
        return [
            row
            for row in real_list_due(store, *args, **kwargs)
            if row["id"] in watch_ids
        ]

    monkeypatch.setattr(PostgresJobStore, "upsert_company_watch", upsert_for_this_test)
    monkeypatch.setattr(
        PostgresJobStore, "list_due_company_watches", list_due_for_this_test
    )


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
        yield database
    finally:
        database.close()


#: Each worker's queue sequences start inside a distinct block this wide.
#: Retry/dead-letter rows key on ``(stage, message_id)`` rather than queue
#: name, so disjoint message ids are as load-bearing as disjoint pgmq tables.
_STAGE_QUEUE_MESSAGE_ID_BLOCK = 1_000_000


def _test_stage_queue_names(testrun_uid: str, worker_id: str):
    """Return pgmq names owned by one test run's worker process."""
    from job_hunter.postgres_stage_queue import _QUEUE_NAMES

    namespace = hashlib.sha256(
        f"{testrun_uid}:{worker_id}".encode()
    ).hexdigest()[:12]
    return {
        stage: f"jh_test_{namespace}_{stage.value}"
        for stage in _QUEUE_NAMES
    }


def _test_stage_queue_sequence_start(testrun_uid: str, worker_id: str) -> int:
    """Reserve a disjoint message-id block for stage bookkeeping rows."""
    digest = hashlib.sha256(f"{testrun_uid}:{worker_id}".encode()).digest()
    block = int.from_bytes(digest[:7], "big") % 8_999_999_999_999 + 1
    return block * _STAGE_QUEUE_MESSAGE_ID_BLOCK


@pytest.fixture(scope="session")
def _isolated_stage_queue_names(testrun_uid, worker_id):
    """Create real pgmq queues private to one xdist worker for this run."""
    from job_hunter.pg import IngestionDatabase

    names = _test_stage_queue_names(testrun_uid, worker_id)
    sequence_start = _test_stage_queue_sequence_start(testrun_uid, worker_id)
    database = IngestionDatabase(os.environ["SUPABASE_TEST_DB_URL"])
    try:
        with database.connection() as connection:
            with connection.cursor() as cursor:
                for name in names.values():
                    cursor.execute("select pgmq.create(%s)", (name,))
                    cursor.execute(
                        "select setval(%s::regclass, %s, false)",
                        (f"pgmq.q_{name}_msg_id_seq", sequence_start),
                    )
        yield names
    finally:
        try:
            with database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "delete from public.job_hunter_stage_attempts "
                        "where message_id >= %s and message_id < %s",
                        (
                            sequence_start,
                            sequence_start + _STAGE_QUEUE_MESSAGE_ID_BLOCK,
                        ),
                    )
                    cursor.execute(
                        "delete from public.job_hunter_stage_dead_letters "
                        "where message_id >= %s and message_id < %s",
                        (
                            sequence_start,
                            sequence_start + _STAGE_QUEUE_MESSAGE_ID_BLOCK,
                        ),
                    )
                    for name in names.values():
                        cursor.execute("select pgmq.drop_queue(%s)", (name,))
        finally:
            database.close()


def _purge_stage_queues(database, queue_names) -> None:
    """Empty this worker's private queues before every store-backed test.

    The queues are shared engine machinery with no user dimension, so the
    seed-user partitioning that keeps two concurrent runs apart does not reach
    them: a message another test left behind is drained by the next test that
    runs a pipeline, and it is drained *at the platform key's expense*. That
    turns "this run read one posting" into "this run read one posting and four
    of somebody else's", which is both a false assertion and a real cost.

    The queues are namespaced by xdist's run and worker identities. Tests on
    one worker execute serially, so clearing that worker's queues cannot touch
    another running test, another worker, or another worktree's suite.
    """
    try:
        with database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local lock_timeout = '5s'")
                for name in queue_names.values():
                    cursor.execute("select pgmq.purge_queue(%s)", (name,))
    except Exception:
        logging.getLogger(__name__).warning(
            "could not clear this worker's private stage queues before the test"
        )


@pytest.fixture
def _clean_isolated_stage_queues(ingestion_database, _isolated_stage_queue_names):
    _purge_stage_queues(ingestion_database, _isolated_stage_queue_names)
    return _isolated_stage_queue_names


#: Calendar months with exactly 30 days, centuries past any real clock.
#: `brave_ledger_window` hands out one of these per test rather than any
#: month, so the day-of-month arithmetic `tests/test_brave_budget.py`
#: asserts on (days remaining in the month, "the last day", "the next day")
#: stays valid whichever one a test lands on. The range is wide so that two
#: concurrent runs, each starting at their own offset, almost never overlap.
_THIRTY_DAY_MONTHS = tuple(
    (year, month) for year in range(2200, 9999) for month in (4, 6, 9, 11)
)


@pytest.fixture(scope="session")
def _brave_ledger_windows(request, testrun_uid) -> dict[str, tuple[int, int]]:
    """Every Brave-ledger test in this run, mapped to a window of its own.

    Issue #236: replaces the blanket platform-table wipe that used to run
    before/after every `tests/test_brave_budget.py` test. That wipe cleared
    the whole `job_hunter_platform_search_usage` table, which is exactly the
    "own the whole table" pattern that breaks under xdist. Distinct months
    mean distinct rows, so nothing is wiped from under a running test.

    Counted, not hashed. The first version hashed each node id into a window,
    and eight tests in 400 windows already had two sharing one: whenever xdist
    put that pair on different workers, one test's cleanup deleted the
    other's rows mid-assertion. Every xdist worker collects the full item list
    and shares one `testrun_uid`, so each computes this same map with no
    coordination and no two tests in the run can collide.

    Offset by `testrun_uid` rather than fixed, because a fixed mapping gives
    a second suite running at the same moment -- another worktree on this
    shared stack -- the identical window for every test, and the same
    cross-run deletion. Nothing needs the window to repeat across runs: each
    test cleans its own window before it starts.
    """
    nodeids = sorted(
        item.nodeid
        for item in request.session.items
        if "brave_ledger_window" in getattr(item, "fixturenames", ())
    )
    digest = hashlib.sha256(testrun_uid.encode()).digest()
    base = int.from_bytes(digest[:8], "big")
    return {
        nodeid: _THIRTY_DAY_MONTHS[(base + index) % len(_THIRTY_DAY_MONTHS)]
        for index, nodeid in enumerate(nodeids)
    }


@pytest.fixture
def brave_ledger_window(request, ingestion_database, _brave_ledger_windows):
    """A (year, month) window this test owns exclusively in the Brave ledger.

    Cleaned before and after, but only the rows inside this test's own
    window -- never the whole `job_hunter_platform_search_usage` table --
    so a concurrently running test in another worker, necessarily in a
    different window (see `_brave_ledger_windows`), is never touched.
    """
    year, month = _brave_ledger_windows[request.node.nodeid]
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    def _clean() -> None:
        with ingestion_database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("set local lock_timeout = '5s'")
                cursor.execute(
                    "delete from public.job_hunter_platform_search_usage "
                    "where provider = 'brave' "
                    "and occurred_at >= %s and occurred_at < %s",
                    (start, end),
                )

    _clean()
    try:
        yield year, month, start, end
    finally:
        _clean()


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
def store(
    supabase_client: SupabaseClient,
    ingestion_database,
    _clean_isolated_stage_queues,
):
    from job_hunter.postgres_store import PostgresJobStore

    return PostgresJobStore(
        supabase_client,
        ingestion_database,
        stage_queue_names=_clean_isolated_stage_queues,
    )


@pytest.fixture
def other_store(
    other_supabase_client: SupabaseClient,
    ingestion_database,
    _clean_isolated_stage_queues,
):
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

    return PostgresJobStore(
        other_supabase_client,
        ingestion_database,
        stage_queue_names=_clean_isolated_stage_queues,
    )


def insert_search_profile(supabase_client, user_id, **overrides):
    """Sync a `job_hunter_search_profiles` row for this user (#187, #188).

    `job_hunter_match_jobs` -- the only ranking/blocking path since #188,
    there is no Python fallback left -- inner-joins the caller's profile, so
    with no row it returns nothing at all rather than an unblocked/unranked
    default. Upserted (not inserted) so a test that wants a different value
    for a second `run_pipeline` call -- same user, same row -- can call this
    again rather than colliding on the unique `user_id`.
    """
    row = dict(
        user_id=user_id,
        timezone="Europe/Berlin",
        scheduled_hour=9,
        max_jobs_per_run=35,
        source_minimum_per_run=0,
        source_max_share=0.5,
        salary_floor_eur=90000,
        max_search_queries_per_run=30,
        max_canonical_resolutions_per_run=80,
        max_learned_ats_boards_per_run=75,
    )
    row.update(overrides)
    supabase_client.upsert("job_hunter_search_profiles", [row], on_conflict="user_id")


@pytest.fixture
def default_search_profile(request):
    """Give the requesting test a `job_hunter_search_profiles` row for
    `job_hunter_match_jobs` to rank against (#187, #188) -- with none, it
    returns nothing at all, and there is no Python fallback left to catch
    that (#188 removed it).

    Not autouse: some tests (`test_store.py`'s own profile tests,
    `test_matching.py`'s hand-rolled `.insert()`-based helper) need a fresh
    user with *no* row, or manage the row themselves and would collide with
    a default inserted ahead of them. A test file that runs `run_pipeline`
    end-to-end and expects real matches back (`test_pipeline.py`,
    `test_pipeline_navigator.py`) opts in with its own local autouse fixture
    that requests this one -- see either file for the pattern. A test that
    needs a non-default `salary_floor_eur` calls `insert_search_profile`
    again itself; it upserts, so this default never collides with that.
    """
    if "store" in request.fixturenames:
        supabase_client = request.getfixturevalue("supabase_client")
        insert_search_profile(supabase_client, supabase_client.user_id)
    if "other_store" in request.fixturenames:
        other_supabase_client = request.getfixturevalue("other_supabase_client")
        insert_search_profile(other_supabase_client, other_supabase_client.user_id)

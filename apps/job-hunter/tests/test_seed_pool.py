"""The pool of seed user pairs that lets store-backed runs go in parallel.

Pure unit tests: they exercise the pool's arithmetic and its file locking
against a temporary directory, and never touch the Supabase stack.
"""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.seed_pool import POOL_SIZE, claim_slot, describe_slot, user_pair


#: The `apps/job-hunter` directory, so a subprocess can import `tests.*`
#: the same way pytest does.
_TESTS_PARENT = Path(__file__).resolve().parent.parent

#: The repository root, three levels up from apps/job-hunter/tests.
_REPO_ROOT = _TESTS_PARENT.parent.parent


def test_slot_zero_is_the_pair_that_already_existed():
    """seed.sql, the pgTAP suite and every doc name these two UUIDs.

    Changing them would silently invalidate all three, so slot 0 has to
    reproduce them exactly rather than merely being "a" pair.
    """
    assert user_pair(0) == (
        "aaaaaaaa-0000-0000-0000-000000000001",
        "bbbbbbbb-0000-0000-0000-000000000002",
    )


def test_each_slot_gets_its_own_pair():
    a_users = [user_pair(slot)[0] for slot in range(POOL_SIZE)]
    b_users = [user_pair(slot)[1] for slot in range(POOL_SIZE)]

    assert len(set(a_users + b_users)) == 2 * POOL_SIZE, (
        "two runs sharing a UUID would delete each other's rows, which is "
        "the whole failure this pool exists to remove"
    )


def test_a_run_claims_a_slot_and_gets_that_slot_s_users(tmp_path):
    with claim_slot(directory=tmp_path) as slot:
        assert (slot.user_a, slot.user_b) == user_pair(slot.index)


def test_a_second_run_gets_a_different_slot(tmp_path):
    """The point of the pool: two runs at once, on disjoint users.

    `flock` is held by the open file description, not by the process, so
    two claims from one process contend exactly as two processes would.
    """
    with claim_slot(directory=tmp_path) as first:
        with claim_slot(directory=tmp_path) as second:
            assert first.index != second.index
            assert {first.user_a, first.user_b}.isdisjoint(
                {second.user_a, second.user_b}
            )


def test_a_released_slot_is_reused(tmp_path):
    with claim_slot(directory=tmp_path) as first:
        taken = first.index

    with claim_slot(directory=tmp_path) as second:
        assert second.index == taken, (
            "a slot nobody holds must be claimable again, or the pool "
            "drains over a working day"
        )


def test_a_killed_run_does_not_keep_its_slot(tmp_path):
    """Crash safety, and the reason ownership is an `flock` and not a record.

    A run that is killed never reaches its release. The kernel drops the
    lock when the fd closes with the process, so the slot comes back
    without anyone deciding that a claim has aged out.
    """
    script = tmp_path / "holder.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(_TESTS_PARENT)!r})\n"
        "from tests.seed_pool import claim_slot\n"
        f"with claim_slot(directory={str(tmp_path)!r}) as slot:\n"
        "    print(slot.index, flush=True)\n"
        "    time.sleep(300)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    try:
        held = int(holder.stdout.readline())
        with claim_slot(directory=tmp_path) as ours:
            assert ours.index != held, "a live holder's slot must not be handed out"
    finally:
        holder.kill()
        holder.wait()

    with claim_slot(directory=tmp_path) as after:
        assert after.index == held, "the dead run's slot must be reusable"


def test_seed_sql_creates_exactly_the_pool():
    """The pool cannot create its own users, so seed.sql must already have.

    Test writes go through PostgREST with a minted JWT, which has no reach
    into `auth.users`. A slot whose users were never seeded fails with a
    foreign-key error deep inside an unrelated test, so the two lists are
    compared here instead.
    """
    seed_sql = (_REPO_ROOT / "supabase" / "seed.sql").read_text()
    seeded = set(re.findall(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", seed_sql))
    expected = {user for slot in range(POOL_SIZE) for user in user_pair(slot)}

    assert expected <= seeded, (
        "seed.sql is missing pool users "
        f"{sorted(expected - seeded)}; run `pnpm db:reset` after adding them"
    )


def test_a_released_slot_names_nobody(tmp_path):
    """A free slot must not still name whoever used it last.

    The exhaustion message points the reader at these records, so a record
    that outlives its run sends them after a process that is not there.
    """
    with claim_slot(directory=tmp_path) as slot:
        held = slot.index
        assert str(os.getpid()) in describe_slot(tmp_path, held)

    assert describe_slot(tmp_path, held) == "free"


def test_a_killed_run_s_record_is_not_believed(tmp_path):
    """A crash cannot clear the record, so the reader is told by liveness."""
    script = tmp_path / "holder.py"
    script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(_TESTS_PARENT)!r})\n"
        "from tests.seed_pool import claim_slot\n"
        f"with claim_slot(directory={str(tmp_path)!r}) as slot:\n"
        "    print(slot.index, flush=True)\n"
        "    time.sleep(300)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
    )
    held = int(holder.stdout.readline())
    assert str(holder.pid) in describe_slot(tmp_path, held)

    holder.kill()
    holder.wait()

    described = describe_slot(tmp_path, held)
    assert str(holder.pid) not in described
    assert described == "free"


@pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in (
            "SUPABASE_TEST_URL",
            "SUPABASE_TEST_PUBLISHABLE_KEY",
            "SUPABASE_TEST_SIGNING_KEY_B64",
        )
    ),
    reason="needs the stack configured; the seed_users fixture skips otherwise",
)
def test_a_full_pool_says_so_while_it_waits(tmp_path):
    """The wait has to reach the terminal, or it is indistinguishable from
    a frozen suite.

    The claim happens in a session-scoped fixture, inside pytest's global
    capture. Captured setup output is replayed only when the item errors --
    so a wait that *succeeds*, which is the ordinary case, would print
    nothing at all and leave `pytest -q` looking hung for as long as the
    pool stays full.

    The pool here frees up part way through, so the run goes green: the
    message must survive that.
    """
    hog = tmp_path / "hog.py"
    hog.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(_TESTS_PARENT)!r})\n"
        "from contextlib import ExitStack\n"
        "from tests.seed_pool import POOL_SIZE, claim_slot\n"
        "with ExitStack() as stack:\n"
        "    for _ in range(POOL_SIZE):\n"
        f"        stack.enter_context(claim_slot(directory={str(tmp_path)!r}))\n"
        "    print('all held', flush=True)\n"
        "    time.sleep(300)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, str(hog)], stdout=subprocess.PIPE, text=True
    )
    waiter = None
    output_parts: list[str] = []
    marker = "seed user slots are in use"
    try:
        assert holder.stdout.readline().strip() == "all held"
        waiter = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "tests/integration/test_seed_slot_fixtures.py",
            ],
            cwd=_TESTS_PARENT,
            env={**os.environ, "CAREER_PLATFORM_SEED_SLOT_DIR": str(tmp_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with selectors.DefaultSelector() as selector:
            selector.register(waiter.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 30
            while marker not in "".join(output_parts) and time.monotonic() < deadline:
                if not selector.select(timeout=1):
                    if waiter.poll() is not None:
                        break
                    continue
                line = waiter.stdout.readline()
                if not line:
                    break
                output_parts.append(line)
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.wait()

    assert waiter is not None
    remainder, _ = waiter.communicate(timeout=180)
    output = "".join(output_parts) + remainder
    assert waiter.returncode == 0, output
    assert marker in output, (
        "a run waiting on a full pool must say so while it waits, not only "
        "when the wait ends badly"
    )

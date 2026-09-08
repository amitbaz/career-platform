"""`scripts/stack_lock.py` distinguishes reading the stack from rebuilding it.

Store-backed test runs no longer need to exclude each other: they claim
disjoint seed users from the pool (`tests/seed_pool.py`) and are isolated
by RLS. What they still cannot survive is `supabase db reset`, which drops
the database out from under them no matter whose users they hold.

So a test run takes the lock shared and a reset takes it exclusive: many
runs at once, but never one alongside a reset.

These tests drive the real script in subprocesses against a temporary
lockfile; they never touch the Supabase stack.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "stack_lock.py"

#: Long enough that a second invocation cannot finish "by accident" while
#: the holder still runs, short enough to keep the suite quick.
HOLD_SECONDS = 3.0


def _run(args: list[str], lock: Path, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        env={"CAREER_PLATFORM_STACK_LOCK": str(lock), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _holder(args: list[str], lock: Path) -> subprocess.Popen:
    """Start a run that holds the lock for HOLD_SECONDS, once it prints."""
    process = subprocess.Popen(
        [
            sys.executable,
            str(_SCRIPT),
            *args,
            sys.executable,
            "-c",
            f"import sys, time; print('holding', flush=True); time.sleep({HOLD_SECONDS})",
        ],
        env={"CAREER_PLATFORM_STACK_LOCK": str(lock), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout.readline().strip() == "holding"
    return process


def test_two_shared_runs_do_not_wait_for_each_other(tmp_path):
    """The whole point: several test suites on the stack at once."""
    lock = tmp_path / "stack.lock"
    holder = _holder(["--shared"], lock)
    try:
        started = time.monotonic()
        second = _run(
            ["--shared", sys.executable, "-c", "print('ran')"],
            lock,
            timeout=HOLD_SECONDS,
        )
        elapsed = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()

    assert second.returncode == 0
    assert "ran" in second.stdout
    assert elapsed < HOLD_SECONDS, "a shared run must not queue behind another"


def test_an_exclusive_run_waits_for_a_shared_one(tmp_path):
    """`pnpm db:reset` during a suite is what the lock still has to stop."""
    lock = tmp_path / "stack.lock"
    holder = _holder(["--shared"], lock)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _run(
                [sys.executable, "-c", "print('ran')"],
                lock,
                timeout=HOLD_SECONDS / 2,
            )
    finally:
        holder.kill()
        holder.wait()


def test_a_shared_run_waits_for_an_exclusive_one(tmp_path):
    """And the other way round: no suite starts mid-reset."""
    lock = tmp_path / "stack.lock"
    holder = _holder([], lock)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            _run(
                ["--shared", sys.executable, "-c", "print('ran')"],
                lock,
                timeout=HOLD_SECONDS / 2,
            )
    finally:
        holder.kill()
        holder.wait()


def test_the_command_still_runs_and_its_exit_code_is_passed_through(tmp_path):
    lock = tmp_path / "stack.lock"

    result = _run(["--shared", sys.executable, "-c", "raise SystemExit(3)"], lock, 10)

    assert result.returncode == 3


def test_waiting_for_shared_holders_does_not_name_a_run_that_has_finished(tmp_path):
    """Shared holders overlap, so no one of them owns the holder record.

    The record is left by the last exclusive run, which by then has exited.
    Naming its PID would send a reader after a process that is not there;
    the wait is real, so the message has to be vague rather than wrong.
    """
    lock = tmp_path / "stack.lock"
    finished = _run([sys.executable, "-c", "pass"], lock, 10)
    assert finished.returncode == 0

    holder = _holder(["--shared"], lock)
    try:
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            _run([sys.executable, "-c", "pass"], lock, timeout=HOLD_SECONDS / 2)
    finally:
        holder.kill()
        holder.wait()

    waiting = (caught.value.stderr or b"").decode()
    assert "waiting for the local Supabase stack" in waiting
    # `_record_holder` writes the command as one space-joined string, so
    # that is the form a stale record would leak in.
    assert " ".join(finished.args) not in waiting
    assert "PID" not in waiting, (
        "the recorded PID belongs to a run that has already exited"
    )


def test_a_waiting_exclusive_run_is_not_starved_by_new_shared_ones(tmp_path):
    """A reset must eventually get in, however busy the machine is.

    Shared holders overlap, so an exclusive waiter that only retried would
    need a moment when *no* suite is running -- and with several sessions
    starting suites back to back, that moment may never come. Once a reset
    is waiting, new suites queue behind it.
    """
    lock = tmp_path / "stack.lock"
    holder = _holder(["--shared"], lock)
    try:
        reset = subprocess.Popen(
            [sys.executable, str(_SCRIPT), sys.executable, "-c", "print('reset')"],
            env={"CAREER_PLATFORM_STACK_LOCK": str(lock), "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE,
            text=True,
        )
        # Long enough for it to register that it is waiting, short enough
        # that the shared holder is still holding.
        time.sleep(1.0)

        started = time.monotonic()
        suite = _run(
            ["--shared", sys.executable, "-c", "print('suite')"],
            lock,
            timeout=HOLD_SECONDS * 5,
        )
        waited = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait()

    assert reset.wait(timeout=HOLD_SECONDS * 5) == 0
    assert suite.returncode == 0
    assert waited >= 1.0, (
        "a suite started while a reset was already waiting must queue "
        "behind it, not slip in alongside the shared holders"
    )

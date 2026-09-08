#!/usr/bin/env python3
"""Serialise access to the local Supabase stack.

The stack is one instance per machine, but this repository is worked on
from several git worktrees at once, often by several agent sessions at
once. `supabase db reset` run against a stack someone else is using drops
the database out from under them, and there is no partitioning that makes
that safe.

This wrapper takes a machine-wide advisory lock before running its
command, so those operations queue instead of corrupting each other.

    python3 scripts/stack_lock.py <command> [args...]        # exclusive
    python3 scripts/stack_lock.py --shared <command> [args...]

The lock has two modes, because two kinds of work share the stack:

`--shared` is for work that only reads and writes its own rows -- a store-
backed test run, which claims its own pair of seed users from the pool in
`apps/job-hunter/tests/seed_pool.py` and is isolated from other runs by
RLS. Several of those may hold the lock at once, which is the point: they
no longer have to take turns.

Exclusive (the default) is for work that is destructive machine-wide no
matter whose rows it touches -- `supabase db reset` above all -- and for
any suite that has not been converted to the pool and so still uses the
fixed pair. An exclusive holder excludes everyone, in both directions.

Shared holders overlap, so an exclusive waiter that merely retried could
be starved: with several sessions starting suites back to back there need
never be an instant when none of them holds the lock. A second "intent"
lockfile beside the first fixes that. A shared run takes it, takes the
main lock, and immediately drops the intent; an exclusive run takes it and
keeps it. So a waiting reset holds the intent, new suites queue on it, the
suites already running drain, and the reset gets in.

The lock lives outside the repository, at `~/.cache/career-platform/
stack.lock` by default, because every worktree must contend for the same
one -- a lockfile inside the tree would give each worktree its own and
defeat the point.

`fcntl.flock` is held by an open file descriptor, so the kernel releases
it when this process exits for any reason, including a crash or a kill.
There is no stale lock to clear by hand.

Environment:
  CAREER_PLATFORM_STACK_LOCK          override the lockfile path
  CAREER_PLATFORM_STACK_LOCK_TIMEOUT  seconds to wait before giving up
                                      (default 1800; 0 waits forever)
  CAREER_PLATFORM_STACK_LOCK_DISABLE  set to 1 to skip locking entirely
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 2.0
#: How often to remind the user we are still queued, in seconds. Long
#: enough not to spam a log, short enough that a wait never looks like a
#: hang.
HEARTBEAT_SECONDS = 30.0


def _lock_path() -> Path:
    """The lockfile, resolved on demand.

    `Path.home()` raises when HOME is unset and the uid has no passwd
    entry, so it must not run for an invocation that overrides the path.
    """
    override = os.environ.get("CAREER_PLATFORM_STACK_LOCK")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "career-platform" / "stack.lock"


def _timeout_seconds() -> float:
    raw = os.environ.get("CAREER_PLATFORM_STACK_LOCK_TIMEOUT", "1800").strip()
    try:
        value = float(raw)
    except ValueError:
        return 1800.0
    return value


def _describe_holder(path: Path) -> str:
    """Best-effort description of whoever currently holds the lock.

    Reading is unsynchronised on purpose: flock is advisory, so a reader
    needs no lock, and a torn or empty read here must never be worse than
    a vaguer message.

    Only exclusive holders write the record, because shared ones overlap
    and would each clobber the others'. So the record can name a run that
    has already exited while shared holders keep the lock -- if its PID is
    gone, describe the wait vaguely rather than send a reader after a
    process that is not there.
    """
    try:
        info = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return "another run"
    pid = info.get("pid")
    if pid and not _is_running(pid):
        return "one or more test runs"
    command = info.get("command")
    started = info.get("started_at")
    parts = []
    if pid:
        parts.append(f"PID {pid}")
    if command:
        parts.append(command)
    if started:
        parts.append(f"since {started}")
    return ", ".join(parts) if parts else "another run"


def _is_running(pid: int) -> bool:
    """Whether `pid` still exists. Signal 0 checks without delivering."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Alive, owned by another user. Not a case this repository's local
        # stack produces, but it is not "gone".
        return True
    return True


def _record_holder(handle, command: list[str]) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(
        {
            "pid": os.getpid(),
            "command": " ".join(command),
            "cwd": os.getcwd(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        handle,
    )
    handle.flush()


def _intent_path(path: Path) -> Path:
    """The queueing lock beside the main one. See the module docstring."""
    return path.with_name(path.name + ".intent")


def _acquire(handle, path: Path, timeout: float, *, shared: bool = False) -> None:
    """Block until the lock is ours, reporting progress on stderr."""
    mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    try:
        fcntl.flock(handle.fileno(), mode)
        return
    except BlockingIOError:
        pass

    print(
        f"stack_lock: waiting for the local Supabase stack ({_describe_holder(path)}).",
        file=sys.stderr,
    )
    waited = 0.0
    last_heartbeat = 0.0
    while True:
        try:
            fcntl.flock(handle.fileno(), mode)
            print(
                f"stack_lock: acquired after {waited:.0f}s.",
                file=sys.stderr,
            )
            return
        except BlockingIOError:
            if timeout and waited >= timeout:
                raise SystemExit(
                    f"stack_lock: gave up after {waited:.0f}s waiting for "
                    f"{path} ({_describe_holder(path)}). If that run is stuck, "
                    f"kill it; to bypass the lock deliberately, set "
                    f"CAREER_PLATFORM_STACK_LOCK_DISABLE=1."
                )
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            if waited - last_heartbeat >= HEARTBEAT_SECONDS:
                last_heartbeat = waited
                print(
                    f"stack_lock: still waiting ({waited:.0f}s, "
                    f"{_describe_holder(path)}).",
                    file=sys.stderr,
                )


def main(argv: list[str]) -> int:
    command = argv[1:]
    shared = bool(command) and command[0] == "--shared"
    if shared:
        command = command[1:]
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print(__doc__, file=sys.stderr)
        return 2

    if os.environ.get("CAREER_PLATFORM_STACK_LOCK_DISABLE") == "1":
        return subprocess.run(command).returncode

    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # "a+" creates the file without truncating one another process may be
    # holding, and still allows the read in _describe_holder.
    timeout = _timeout_seconds()
    with open(path, "a+") as handle, open(_intent_path(path), "a+") as intent:
        # The intent lock is taken first in both modes, so an exclusive run
        # that is already waiting stops new shared runs from arriving.
        _acquire(intent, path, timeout, shared=shared)
        _acquire(handle, path, timeout, shared=shared)
        if shared:
            # Nothing is queueing behind a suite, so let the next one in
            # rather than making them run one at a time after all.
            fcntl.flock(intent.fileno(), fcntl.LOCK_UN)
        else:
            # Only an exclusive holder can safely rewrite this: shared
            # holders overlap, so each would clobber the others' record
            # and leave a waiter reading a name that has already finished.
            _record_holder(handle, command)
        return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

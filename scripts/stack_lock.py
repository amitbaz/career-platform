#!/usr/bin/env python3
"""Serialise access to the local Supabase stack.

The stack is one instance per machine, but this repository is worked on
from several git worktrees at once, often by several agent sessions at
once. Every store-backed test truncates the same two seed users
(`tests/conftest.py`), so two concurrent runs delete each other's rows
mid-test. That surfaces as a scatter of unrelated assertion failures
rather than as anything naming the real cause, and `supabase db reset`
run against a stack someone else is using is worse still.

This wrapper takes a machine-wide advisory lock before running its
command, so those operations queue instead of corrupting each other.

    python3 scripts/stack_lock.py <command> [args...]

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

DEFAULT_LOCK = Path.home() / ".cache" / "career-platform" / "stack.lock"
POLL_SECONDS = 2.0
#: How often to remind the user we are still queued, in seconds. Long
#: enough not to spam a log, short enough that a wait never looks like a
#: hang.
HEARTBEAT_SECONDS = 30.0


def _lock_path() -> Path:
    override = os.environ.get("CAREER_PLATFORM_STACK_LOCK")
    return Path(override) if override else DEFAULT_LOCK


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
    """
    try:
        info = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        return "another run"
    pid = info.get("pid")
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


def _acquire(handle, path: Path, timeout: float) -> None:
    """Block until the lock is ours, reporting progress on stderr."""
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    with open(path, "a+") as handle:
        _acquire(handle, path, _timeout_seconds())
        _record_holder(handle, command)
        return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

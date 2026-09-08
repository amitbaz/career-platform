"""A pool of seed user pairs, one claimed per test run.

Store-backed tests are isolated from each other by `user_id`: RLS scopes
every query, so two runs that use different users cannot see, or delete,
each other's rows. Until this module existed every run used the same two
users, so the only way to stay correct was to take turns behind
`scripts/stack_lock.py` -- with several worktrees active that meant a
five-minute suite waiting ten.

A run claims one slot from a fixed pool, uses that slot's two UUIDs as its
seed users, and releases the slot when it exits. Runs on different slots
have nothing in common and can proceed at the same time.

The pool is fixed rather than grown on demand because test writes go
through PostgREST with a minted JWT, which cannot insert into `auth.users`;
the users must already exist. `supabase/seed.sql` creates exactly the pairs
this module names -- `tests/test_seed_pool.py` asserts the two agree.

Environment:
  CAREER_PLATFORM_SEED_SLOT_DIR      override where slot lockfiles live
  CAREER_PLATFORM_SEED_SLOT_TIMEOUT  seconds to wait for a free slot before
                                     giving up (default 1800; 0 waits forever)
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

#: Number of user pairs `supabase/seed.sql` creates. This is the cap on
#: real parallelism: a ninth concurrent run waits for a slot, which is what
#: every run used to do. Raising it means editing seed.sql too, and then
#: `supabase db reset` to apply it.
POOL_SIZE = 8


def user_pair(slot: int) -> tuple[str, str]:
    """The two seed user UUIDs belonging to `slot`.

    Slot 0 is the pair that predates the pool, reproduced exactly: the
    pgTAP suite (`supabase/tests/pgtap/`) and the repository docs name
    those literals, and they stay correct only if slot 0 keeps them.
    """
    if not 0 <= slot < POOL_SIZE:
        raise ValueError(f"slot {slot} is outside the pool of {POOL_SIZE}")
    # The slot goes in the third group, zero-padded to its full four hex
    # digits, so slot 0 reproduces the pre-pool pair exactly and the shape
    # survives a pool larger than nine.
    return (
        f"aaaaaaaa-0000-0000-{slot:04x}-000000000001",
        f"bbbbbbbb-0000-0000-{slot:04x}-000000000002",
    )


def _default_slot_dir() -> Path:
    """Where slot lockfiles live when nothing overrides it.

    Outside every worktree, like `scripts/stack_lock.py`'s lockfile and for
    the same reason: a directory inside the tree would give each worktree
    its own pool, so two worktrees would both claim "slot 0" and be back to
    sharing users.

    Resolved on demand rather than at import. `conftest.py` imports this
    module for every pytest run, including the pure-unit ones that never
    reach the stack, and `Path.home()` raises when HOME is unset and the
    uid has no passwd entry -- a container run as `--user 1001`. That must
    not fail collection.
    """
    return Path.home() / ".cache" / "career-platform" / "seed-slots"

POLL_SECONDS = 1.0
#: How often to say we are still queued. Long enough not to spam a log,
#: short enough that a full pool never looks like a hang.
HEARTBEAT_SECONDS = 30.0


@dataclass(frozen=True)
class SeedSlot:
    """One claimed pool slot and the two users it owns."""

    index: int
    user_a: str
    user_b: str


def _slot_dir(directory: Path | None) -> Path:
    if directory is not None:
        return Path(directory)
    override = os.environ.get("CAREER_PLATFORM_SEED_SLOT_DIR")
    return Path(override) if override else _default_slot_dir()


def _timeout_seconds() -> float:
    raw = os.environ.get("CAREER_PLATFORM_SEED_SLOT_TIMEOUT", "1800").strip()
    try:
        return float(raw)
    except ValueError:
        return 1800.0


def _is_running(pid: int) -> bool:
    """Whether `pid` still exists. Signal 0 checks without delivering.

    Duplicated from `scripts/stack_lock.py` rather than shared: that script
    is deliberately importable with nothing but the standard library, and
    this module lives under `apps/job-hunter/tests`.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def describe_slot(directory: Path, slot: int) -> str:
    """Who holds `slot`, for a message that has to send someone looking.

    Returns "free" unless a live process recorded itself. A clean release
    clears the record, but a killed run cannot, so a record whose PID is
    gone is treated as no record at all -- the kernel has already handed
    the slot back.
    """
    try:
        info = json.loads((Path(directory) / f"slot-{slot}.lock").read_text() or "{}")
    except (OSError, ValueError):
        return "free"
    pid = info.get("pid")
    if not pid or not _is_running(pid):
        return "free"
    parts = [f"PID {pid}"]
    if info.get("cwd"):
        parts.append(info["cwd"])
    if info.get("started_at"):
        parts.append(f"since {info['started_at']}")
    return ", ".join(parts)


def _record_holder(handle, slot: int) -> None:
    handle.seek(0)
    handle.truncate()
    json.dump(
        {
            "pid": os.getpid(),
            "slot": slot,
            "cwd": os.getcwd(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        handle,
    )
    handle.flush()


def _try_claim(directory: Path, slot: int):
    """Take `slot` if it is free, returning the fd that owns it, else None.

    The lock is held by the open file description, so the kernel drops it
    when this handle closes -- including when the process is killed or
    crashes. A slot therefore cannot be leaked, and there is no timestamp
    heuristic deciding when someone else's claim has gone stale.
    """
    # "a+" creates the file without truncating one another process may be
    # holding, and still allows reading the holder record back.
    handle = open(directory / f"slot-{slot}.lock", "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    _record_holder(handle, slot)
    return handle


@contextmanager
def claim_slot(directory: Path | None = None):
    """Claim a free pool slot for the duration of the block.

    Blocks while every slot is taken, which is the behaviour every run had
    before the pool existed, so a full pool is a slowdown and never a
    wrong result.
    """
    path = _slot_dir(directory)
    path.mkdir(parents=True, exist_ok=True)
    timeout = _timeout_seconds()

    waited = 0.0
    last_heartbeat = 0.0
    announced = False
    while True:
        for index in range(POOL_SIZE):
            handle = _try_claim(path, index)
            if handle is None:
                continue
            if waited:
                print(
                    f"seed_pool: claimed slot {index} after {waited:.0f}s.",
                    file=sys.stderr,
                )
            try:
                yield SeedSlot(index, *user_pair(index))
            finally:
                # Clear the record before dropping the lock: while we still
                # hold it nobody else can be mid-read, and a free slot must
                # not name the run that just left it.
                handle.seek(0)
                handle.truncate()
                handle.flush()
                handle.close()
            return

        if not announced:
            announced = True
            print(
                f"seed_pool: all {POOL_SIZE} seed user slots are in use; "
                "waiting for one to free up.",
                file=sys.stderr,
            )
        if timeout and waited >= timeout:
            holders = "\n".join(
                f"  slot {index}: {describe_slot(path, index)}"
                for index in range(POOL_SIZE)
            )
            raise SystemExit(
                f"seed_pool: gave up after {waited:.0f}s waiting for one of "
                f"{POOL_SIZE} seed user slots in {path}. That many runs are "
                f"active at once, or a stuck one is holding a slot:\n"
                f"{holders}"
            )
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS
        if waited - last_heartbeat >= HEARTBEAT_SECONDS:
            last_heartbeat = waited
            print(
                f"seed_pool: still waiting for a seed user slot ({waited:.0f}s).",
                file=sys.stderr,
            )

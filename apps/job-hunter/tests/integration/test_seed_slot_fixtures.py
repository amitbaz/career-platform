"""The store fixtures serve the run's claimed slot, not two fixed users.

This is what makes two worktrees' suites able to run at once: RLS scopes
every query by `user_id`, so runs holding different slots are invisible to
each other. A regression here is silent -- the suite still passes alone --
so it is asserted rather than assumed.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from job_hunter.supabase_client import SupabaseClient
from tests.seed_pool import POOL_SIZE, SeedSlot, user_pair

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (
            os.environ.get("SUPABASE_TEST_URL")
            and os.environ.get("SUPABASE_TEST_PUBLISHABLE_KEY")
            and os.environ.get("SUPABASE_TEST_SIGNING_KEY_B64")
        ),
        reason="no Supabase stack configured; see tests/conftest.py",
    ),
]


def test_the_run_holds_one_slot_from_the_pool(seed_users: SeedSlot) -> None:
    assert 0 <= seed_users.index < POOL_SIZE
    assert (seed_users.user_a, seed_users.user_b) == user_pair(seed_users.index)


def test_both_clients_are_the_claimed_slot_s_users(
    supabase_client: SupabaseClient,
    other_supabase_client: SupabaseClient,
    seed_users: SeedSlot,
) -> None:
    assert supabase_client.user_id == seed_users.user_a
    assert other_supabase_client.user_id == seed_users.user_b


def test_the_claimed_users_exist_in_the_database(
    supabase_client: SupabaseClient,
) -> None:
    """A slot whose users seed.sql never created must fail here, not later.

    This is the case where a stack predates the pool: `seed.sql`'s extra
    users land only on `pnpm db:reset`, so a run that claims slot 3 against
    an un-reset stack has a user that does not exist. Selecting would not
    catch it -- the RLS policy is `auth.uid() = user_id` and never joins
    `auth.users`, so a token minted for a UUID nobody created reads back a
    clean, empty result. Only a write resolves the foreign key, and its
    23503 is the same code `_truncate` reads as "another writer is on your
    seed users", which would name the wrong cause.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = supabase_client.insert(
        "job_hunter_jobs",
        [
            {
                "user_id": supabase_client.user_id,
                "fingerprint": f"seed-slot-check-{uuid.uuid4()}",
                "first_seen_at": now,
                "last_seen_at": now,
            }
        ],
    )

    assert len(rows) == 1
    supabase_client.delete("job_hunter_jobs", params={"id": f"eq.{rows[0]['id']}"})

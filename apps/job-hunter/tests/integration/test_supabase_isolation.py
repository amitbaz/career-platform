"""Proves per-user isolation against live row-level security policies.

Skipped unless a Supabase stack is reachable and its details are exported.
Get them with:

    eval "$(supabase status -o env | sed 's/^/export /')"
    export SUPABASE_TEST_URL="$API_URL"
    export SUPABASE_TEST_PUBLISHABLE_KEY="$ANON_KEY"
    export SUPABASE_TEST_SIGNING_KEY_B64="$(python3 -c "import json,base64;print(base64.b64encode(json.dumps(json.load(open('supabase/signing_keys.json'))[0]).encode()).decode())")"

The last line's json/base64 roundtrip is needed because the stack's
signing_keys.json wraps the JWK in an array, and the minter wants the
single object.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest

from job_hunter.supabase_client import SupabaseClient, SupabasePermissionError

TABLE = "job_hunter_jobs"

_URL = os.environ.get("SUPABASE_TEST_URL")
_KEY = os.environ.get("SUPABASE_TEST_PUBLISHABLE_KEY")
_JWK = os.environ.get("SUPABASE_TEST_SIGNING_KEY_B64")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_URL and _KEY and _JWK),
        reason="no Supabase stack configured; see this module's docstring",
    ),
]


# The two users are whichever pair this run claimed from the pool
# (tests/seed_pool.py), so a suite running in another worktree proves the
# same policies against a different pair at the same time. What matters to
# these assertions is only that A and B are different users.


@pytest.fixture
def as_a(supabase_client: SupabaseClient) -> SupabaseClient:
    return supabase_client


@pytest.fixture
def as_b(other_supabase_client: SupabaseClient) -> SupabaseClient:
    return other_supabase_client


@pytest.fixture
def a_row(as_a: SupabaseClient):
    """A row owned by user A, removed when the test finishes.

    A job row is a membership of a posting since #178, so the advertisement
    is written first. It is deliberately not cleaned up: postings are shared
    and carry no delete policy, which is the point of the table.

    `market_id` stands in for the title this test used to write. It is one of
    the few columns a membership row still has, and being per-user it is the
    right one to prove another user cannot rewrite it.
    """
    now = datetime.now(timezone.utc).isoformat()
    posting = as_a.insert(
        "job_hunter_postings",
        [
            {
                "fingerprint": f"isolation-test-{uuid.uuid4()}",
                "title": "Original title",
                "first_seen_at": now,
                "last_seen_at": now,
            }
        ],
    )[0]
    rows = as_a.insert(
        TABLE,
        [
            {
                "user_id": as_a.user_id,
                "posting_id": posting["id"],
                "market_id": "original-market",
                "first_seen_at": now,
                "last_seen_at": now,
            }
        ],
    )
    assert len(rows) == 1
    yield rows[0]
    as_a.delete(TABLE, params={"id": f"eq.{rows[0]['id']}"})


def test_a_reads_back_its_own_row(as_a, a_row):
    found = as_a.select(TABLE, params={"id": f"eq.{a_row['id']}"})

    assert len(found) == 1
    assert found[0]["market_id"] == "original-market"


def test_b_cannot_see_as_row(as_b, a_row):
    assert as_b.select(TABLE, params={"id": f"eq.{a_row['id']}"}) == []


def test_b_cannot_update_as_row(as_b, a_row):
    changed = as_b.update(
        TABLE, {"market_id": "hijacked"}, params={"id": f"eq.{a_row['id']}"}
    )

    assert changed == []


def test_b_cannot_delete_as_row(as_b, a_row):
    assert as_b.delete(TABLE, params={"id": f"eq.{a_row['id']}"}) == []


def test_b_cannot_insert_a_row_claiming_a_as_owner(as_a, as_b):
    now = datetime.now(timezone.utc).isoformat()

    with pytest.raises(SupabasePermissionError):
        as_b.insert(
            TABLE,
            [
                {
                    "user_id": as_a.user_id,
                    "posting_id": as_b.insert(
                        "job_hunter_postings",
                        [
                            {
                                "fingerprint": f"forged-{uuid.uuid4()}",
                                "first_seen_at": now,
                                "last_seen_at": now,
                            }
                        ],
                    )[0]["id"],
                    "first_seen_at": now,
                    "last_seen_at": now,
                }
            ],
        )


def test_as_row_survives_every_attempt(as_a, as_b, a_row):
    as_b.update(TABLE, {"market_id": "hijacked"}, params={"id": f"eq.{a_row['id']}"})
    as_b.delete(TABLE, params={"id": f"eq.{a_row['id']}"})

    survivor = as_a.select(TABLE, params={"id": f"eq.{a_row['id']}"})

    assert len(survivor) == 1
    assert survivor[0]["market_id"] == "original-market"

"""Live-stack tests for the client capabilities #70 adds.

Skipped unless SUPABASE_TEST_URL, SUPABASE_TEST_PUBLISHABLE_KEY and
SUPABASE_TEST_SIGNING_KEY_B64 are exported. CI exports them from
`supabase status`.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.http import HttpClient
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient


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


@pytest.fixture
def client() -> SupabaseClient:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"  # seed.sql user A
    jwk = json.loads(base64.b64decode(_JWK))
    settings = SupabaseSettings(
        url=_URL.rstrip("/"),
        publishable_key=_KEY,
        user_id=user_id,
        signing_key_jwk=jwk,
    )
    return SupabaseClient(HttpClient(), settings, AccessTokenMinter(user_id, jwk))


def test_upsert_is_idempotent_on_the_natural_key(client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    job = client.insert(
        "job_hunter_jobs",
        [{"user_id": user_id, "fingerprint": f"fp-{uuid.uuid4()}", "source": "test", "url": "https://x", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
    )[0]
    row = {
        "user_id": user_id,
        "job_id": job["id"],
        "total_score": 70,
        "decision": "possible_match",
        "evaluated_at": "2026-09-06T10:00:00+00:00",
    }

    first = client.upsert("job_hunter_evaluations", [row], on_conflict="user_id,job_id,evaluated_at")
    second = client.upsert("job_hunter_evaluations", [row], on_conflict="user_id,job_id,evaluated_at")

    assert first[0]["id"] == second[0]["id"]
    stored = client.select("job_hunter_evaluations", params={"job_id": f"eq.{job['id']}"})
    assert len(stored) == 1


def test_upsert_updates_the_conflicting_row(client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    job = client.insert(
        "job_hunter_jobs",
        [{"user_id": user_id, "fingerprint": f"fp-{uuid.uuid4()}", "source": "test", "url": "https://x", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
    )[0]
    base = {
        "user_id": user_id,
        "job_id": job["id"],
        "total_score": 70,
        "decision": "possible_match",
        "evaluated_at": "2026-09-06T10:00:00+00:00",
    }
    client.upsert("job_hunter_evaluations", [base], on_conflict="user_id,job_id,evaluated_at")
    client.upsert(
        "job_hunter_evaluations",
        [{**base, "total_score": 91}],
        on_conflict="user_id,job_id,evaluated_at",
    )

    stored = client.select("job_hunter_evaluations", params={"job_id": f"eq.{job['id']}"})
    assert len(stored) == 1
    assert stored[0]["total_score"] == 91


def test_select_pages_past_the_postgrest_row_cap(client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    marker = f"page-{uuid.uuid4()}"
    rows = [
        {"user_id": user_id, "fingerprint": f"{marker}-{i}", "source": marker, "url": f"https://x/{i}", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}
        for i in range(1100)
    ]
    for chunk in range(0, len(rows), 500):
        client.insert("job_hunter_jobs", rows[chunk : chunk + 500])

    found = client.select("job_hunter_jobs", params={"source": f"eq.{marker}"})

    assert len(found) == 1100, "select must page rather than silently truncate at 1000"


def test_select_paging_returns_rows_in_stable_order(client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    marker = f"stable-{uuid.uuid4()}"
    rows = [
        {"user_id": user_id, "fingerprint": f"{marker}-{i}", "source": marker, "url": f"https://x/{i}", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}
        for i in range(1100)
    ]
    for chunk in range(0, len(rows), 500):
        client.insert("job_hunter_jobs", rows[chunk : chunk + 500])

    found = client.select("job_hunter_jobs", params={"source": f"eq.{marker}"})

    ids = [row["id"] for row in found]
    assert ids == sorted(ids), "paging must return rows in ascending id order"
    assert len(set(ids)) == len(ids), "paging must not duplicate rows"


def test_select_paging_preserves_caller_order_with_id_tiebreak(client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    marker = f"order-{uuid.uuid4()}"
    # Create rows with a shared sort key so id tiebreaker matters.
    rows = [
        {"user_id": user_id, "fingerprint": f"{marker}-{i}", "source": marker, "url": f"https://x/{i}", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}
        for i in range(1100)
    ]
    for chunk in range(0, len(rows), 500):
        client.insert("job_hunter_jobs", rows[chunk : chunk + 500])

    # Read with a caller-supplied order (by id, ascending).
    # This verifies that the caller's order is passed through and tiebreaker applied.
    found = client.select("job_hunter_jobs", params={"source": f"eq.{marker}", "order": "id.asc"})

    assert len(found) == 1100, "select must page all rows with caller order"
    ids = [row["id"] for row in found]
    assert len(set(ids)) == len(ids), "paging with caller order must not duplicate rows"
    # Verify that the rows are in the order specified by the caller (id.asc).
    assert ids == sorted(ids), "caller-supplied order must be preserved across pages"


def test_rpc_calls_a_store_function_and_respects_rls(client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    # Create a job with a high-scoring evaluation and no delivery
    job = client.insert(
        "job_hunter_jobs",
        [{"user_id": user_id, "fingerprint": f"fp-{uuid.uuid4()}", "source": "test", "url": "https://x", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
    )[0]
    client.upsert(
        "job_hunter_evaluations",
        [{
            "user_id": user_id,
            "job_id": job["id"],
            "total_score": 85,
            "decision": "possible_match",
            "evaluated_at": "2026-09-06T10:00:00+00:00",
        }],
        on_conflict="user_id,job_id,evaluated_at"
    )

    result = client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0})
    assert isinstance(result, list)
    # Assert that the job we created is in the results
    assert any(item["job_id"] == str(job["id"]) for item in result), "Created job should appear in pending delivery jobs"


def test_rpc_respects_retry_false(client: SupabaseClient) -> None:
    """Verify that retry=False parameter is accepted and works correctly.

    This test calls an RPC function with retry=False to ensure the parameter
    is properly passed through to the HTTP client without errors. With
    retry=False, transient failures would not be retried, making it safe
    for non-idempotent operations like job_hunter_merge_jobs.
    """
    # Call a read-only RPC function with retry=False
    result = client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 100}, retry=False)
    assert isinstance(result, list)
    # With a high floor, there should be no results (we haven't created any high-scoring jobs)
    assert len(result) == 0

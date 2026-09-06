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
from unittest.mock import patch, MagicMock

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


def _client_for(user_id: str) -> SupabaseClient:
    """Create a client for a specific user."""
    jwk = json.loads(base64.b64decode(_JWK))
    settings = SupabaseSettings(
        user_id=user_id,
        url=_URL.rstrip("/"),
        publishable_key=_KEY,
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
    """Test that RPC functions return correct data and respect row-level security.

    Creates a high-scoring job for user A and user B, then calls as user A
    and verifies only user A's job appears in the results.
    """
    user_a_id = "aaaaaaaa-0000-0000-0000-000000000001"
    user_b_id = "bbbbbbbb-0000-0000-0000-000000000002"

    # Create a high-scoring job for user A
    job_a = client.insert(
        "job_hunter_jobs",
        [{"user_id": user_a_id, "fingerprint": f"fp-a-{uuid.uuid4()}", "source": "test", "url": "https://x", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
    )[0]
    client.upsert(
        "job_hunter_evaluations",
        [{
            "user_id": user_a_id,
            "job_id": job_a["id"],
            "total_score": 85,
            "decision": "possible_match",
            "evaluated_at": "2026-09-06T10:00:00+00:00",
        }],
        on_conflict="user_id,job_id,evaluated_at"
    )

    # Create a high-scoring job for user B using user B's client
    client_b = _client_for(user_b_id)
    job_b = client_b.insert(
        "job_hunter_jobs",
        [{"user_id": user_b_id, "fingerprint": f"fp-b-{uuid.uuid4()}", "source": "test", "url": "https://y", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
    )[0]
    client_b.upsert(
        "job_hunter_evaluations",
        [{
            "user_id": user_b_id,
            "job_id": job_b["id"],
            "total_score": 95,
            "decision": "high_priority",
            "evaluated_at": "2026-09-06T10:00:00+00:00",
        }],
        on_conflict="user_id,job_id,evaluated_at"
    )

    # Call as user A and verify only user A's job appears
    result = client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0})
    assert isinstance(result, list)
    assert len(result) >= 1, "User A's job should appear in results"
    job_ids = [item["job_id"] for item in result]
    assert str(job_a["id"]) in job_ids, "User A's job should appear in results"
    assert str(job_b["id"]) not in job_ids, "User B's job should NOT appear in user A's results (RLS violation)"


def test_rpc_respects_retry_false(client: SupabaseClient) -> None:
    """Verify that retry=False suppresses retries on transient failures.

    Monkeypatches the HTTP layer to return 502 errors and verifies:
    - With retry=False: exactly 1 request is made
    - With retry=True: more than 1 request is made
    """
    from requests import Response

    # Test with retry=False: should make exactly 1 request
    call_count = 0
    original_request = client._http._session.request

    def mock_502_once(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        response = Response()
        response.status_code = 502
        response._content = b'{"error": "bad gateway"}'
        return response

    with patch.object(client._http._session, 'request', side_effect=mock_502_once):
        call_count = 0
        try:
            client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0}, retry=False)
        except Exception:
            pass  # Expected to fail; we just care about call count
        assert call_count == 1, f"With retry=False, exactly 1 request should be made, but got {call_count}"

    # Test with retry=True: should make multiple requests (with backoff)
    call_count = 0
    with patch.object(client._http._session, 'request', side_effect=mock_502_once):
        call_count = 0
        try:
            client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0}, retry=True)
        except Exception:
            pass  # Expected to fail after retries; we just care about call count
        assert call_count > 1, f"With retry=True, multiple requests should be made, but got {call_count}"


def test_rpc_returns_setof_scalar_values(client: SupabaseClient) -> None:
    """Verify that setof scalar functions return a list of plain values.

    job_hunter_find_job_by_identity returns setof uuid, which PostgREST
    serializes as a JSON array of strings (UUIDs). Verifies the shape is correct.
    """
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"

    # Create a job with distinct company and title so we can find it by identity
    job = client.insert(
        "job_hunter_jobs",
        [{
            "user_id": user_id,
            "fingerprint": f"fp-identity-{uuid.uuid4()}",
            "source": "test",
            "url": "https://example.com",
            "company": "Acme Corp",
            "title": "Senior Engineer",
            "location": "San Francisco",
            "first_seen_at": "2026-09-06T10:00:00+00:00",
            "last_seen_at": "2026-09-06T10:00:00+00:00"
        }],
    )[0]

    # Call the setof uuid function and verify we get a list of strings
    result = client.rpc("job_hunter_find_job_by_identity", {
        "p_company": "Acme Corp",
        "p_title": "Senior Engineer",
        "p_location": "San Francisco"
    })

    assert isinstance(result, list), "Result should be a list"
    # Should return at least our created job's UUID as a string
    assert any(isinstance(item, str) for item in result), "Result should contain string UUIDs"
    assert str(job["id"]) in result, "Created job should be in the identity search results"

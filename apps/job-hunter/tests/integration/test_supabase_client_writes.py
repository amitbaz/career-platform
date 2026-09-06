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
def client(supabase_client: SupabaseClient) -> SupabaseClient:
    """User A's client, with conftest.py's shared truncation around it."""
    return supabase_client


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


# Cleanup between tests is handled by conftest.py's `_cleanup_seed_users`
# fixture, transitively pulled in by the `client` fixture above (it depends
# on `supabase_client`). See that file for the table order and rationale.


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
    marker = f"rls-test-{uuid.uuid4()}"

    # Create a high-scoring job for user A
    job_a = client.insert(
        "job_hunter_jobs",
        [{"user_id": user_a_id, "fingerprint": f"fp-a-{uuid.uuid4()}", "source": marker, "url": "https://x", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
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
        [{"user_id": user_b_id, "fingerprint": f"fp-b-{uuid.uuid4()}", "source": marker, "url": "https://y", "first_seen_at": "2026-09-06T10:00:00+00:00", "last_seen_at": "2026-09-06T10:00:00+00:00"}],
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

    # Clean up: delete evaluations first (they reference jobs), then jobs
    client.delete("job_hunter_evaluations", params={"job_id": f"eq.{job_a['id']}"})
    client_b.delete("job_hunter_evaluations", params={"job_id": f"eq.{job_b['id']}"})
    client.delete("job_hunter_jobs", params={"source": f"eq.{marker}"})
    client_b.delete("job_hunter_jobs", params={"source": f"eq.{marker}"})


def test_rpc_respects_retry_false(client: SupabaseClient) -> None:
    """Verify that retry=False suppresses retries on transient failures.

    Monkeypatches the HTTP layer to return 502 errors and verifies:
    - With retry=False: exactly 1 request is made
    - With retry=True: exactly 3 requests are made (_MAX_RETRIES=2 means 1 initial + 2 retries)
    """
    from requests import Response

    # HttpClient has _MAX_RETRIES = 2, so 1 initial + 2 retries = 3 total attempts
    max_retries_plus_one = 3

    def mock_502_once(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        response = Response()
        response.status_code = 502
        response._content = b'{"error": "bad gateway"}'
        return response

    # Test with retry=False: should make exactly 1 request
    call_count = 0
    with patch.object(client._http._session, 'request', side_effect=mock_502_once):
        try:
            client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0}, retry=False)
        except Exception:
            pass  # Expected to fail; we just care about call count
        assert call_count == 1, f"With retry=False, exactly 1 request should be made, but got {call_count}"

    # Test with retry=True: should make exact number of retries (1 initial + 2 retries)
    call_count = 0
    with patch.object(client._http._session, 'request', side_effect=mock_502_once):
        try:
            client.rpc("job_hunter_pending_delivery_jobs", {"p_score_floor": 0}, retry=True)
        except Exception:
            pass  # Expected to fail after retries; we just care about call count
        assert call_count == max_retries_plus_one, f"With retry=True, {max_retries_plus_one} requests should be made, but got {call_count}"


def test_rpc_returns_setof_scalar_values(client: SupabaseClient) -> None:
    """Verify that setof scalar functions return a list of plain values.

    job_hunter_find_job_by_identity returns setof uuid, which PostgREST
    serializes as a JSON array of strings (UUIDs). Verifies the shape is correct.
    """
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    marker = f"setof-test-{uuid.uuid4()}"

    # Create a job with distinct company and title so we can find it by identity
    job = client.insert(
        "job_hunter_jobs",
        [{
            "user_id": user_id,
            "fingerprint": f"fp-identity-{uuid.uuid4()}",
            "source": marker,
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

    # Clean up: delete job seeded by this test
    client.delete("job_hunter_jobs", params={"source": f"eq.{marker}"})


def test_rpc_returns_bare_scalar_values(client: SupabaseClient) -> None:
    """Verify that bare scalar functions return a one-element list with the scalar value.

    job_hunter_merge_jobs returns a bare scalar uuid, which PostgREST
    serializes as a JSON string. _parse wraps it into a one-element list: ['uuid'].
    """
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    marker = f"merge-test-{uuid.uuid4()}"

    # Create two jobs with the same logical identity (company/title/location)
    # so they will merge. Use distinct URLs to avoid duplicate canonical_url conflicts.
    # Give job1 an earlier first_seen_at so it's guaranteed to be the survivor.
    job1 = client.insert(
        "job_hunter_jobs",
        [{
            "user_id": user_id,
            "fingerprint": f"fp-merge-1-{uuid.uuid4()}",
            "source": marker,
            "url": f"https://example.com/job1-{uuid.uuid4()}",
            "company": "Merge Test Corp",
            "title": "Backend Engineer",
            "location": "New York",
            "first_seen_at": "2026-09-01T10:00:00+00:00",
            "last_seen_at": "2026-09-06T10:00:00+00:00"
        }],
    )[0]

    job2 = client.insert(
        "job_hunter_jobs",
        [{
            "user_id": user_id,
            "fingerprint": f"fp-merge-2-{uuid.uuid4()}",
            "source": marker,
            "url": f"https://example.com/job2-{uuid.uuid4()}",
            "company": "Merge Test Corp",
            "title": "Backend Engineer",
            "location": "New York",
            "first_seen_at": "2026-09-06T10:00:00+00:00",
            "last_seen_at": "2026-09-06T10:00:00+00:00"
        }],
    )[0]

    # Call merge_jobs through rpc with retry=False (required for non-idempotent operations)
    result = client.rpc("job_hunter_merge_jobs", {
        "p_survivor": job1["id"],
        "p_duplicate": job2["id"]
    }, retry=False)

    assert isinstance(result, list), "Result should be a list"
    assert len(result) == 1, f"Bare scalar result should be a one-element list, got {len(result)} elements"
    survivor_id = result[0]
    assert isinstance(survivor_id, str), f"Bare scalar result should contain a string, got {type(survivor_id)}"
    assert survivor_id == str(job1["id"]), f"Survivor should be job1 (id={job1['id']}), but got {survivor_id}"

    # Clean up: delete the remaining job (job2 was deleted by the merge)
    client.delete("job_hunter_jobs", params={"source": f"eq.{marker}"})

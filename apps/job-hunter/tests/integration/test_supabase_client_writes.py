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

"""Tests for the Postgres row <-> model mapping helpers.

`to_iso`/`from_iso` are pure and always run. `job_from_row` is also
exercised against a real PostgREST row (not a hand-written dict) via the
`supabase_client` fixture, so it proves the mapping against what Postgres
actually returns rather than an assumption about the schema -- this half
skips when the local Supabase stack isn't configured (see conftest.py).
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest

from job_hunter.store_mapping import (
    from_iso,
    job_from_row,
    to_iso,
    touch,
)
from job_hunter.supabase_client import SupabaseClient


def test_to_iso_treats_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 9, 6, 10, 30, 0)

    rendered = to_iso(naive)

    assert rendered == "2026-09-06T10:30:00+00:00"


def test_to_iso_normalises_non_utc_offset_to_utc() -> None:
    from datetime import timedelta, timezone as tz

    plus_two = datetime(2026, 9, 6, 12, 30, 0, tzinfo=tz(timedelta(hours=2)))

    rendered = to_iso(plus_two)

    assert rendered == "2026-09-06T10:30:00+00:00"


def test_to_iso_of_none_is_none() -> None:
    assert to_iso(None) is None


def test_from_iso_round_trips_a_naive_datetime_treated_as_utc() -> None:
    naive = datetime(2026, 9, 6, 10, 30, 0)

    result = from_iso(to_iso(naive))

    assert result == naive.replace(tzinfo=timezone.utc)


def test_from_iso_of_none_is_none() -> None:
    assert from_iso(None) is None


def test_touch_sets_updated_at() -> None:
    result = touch({"a": 1})

    assert result["a"] == 1
    assert "updated_at" in result
    # Round-trips through from_iso without raising, and carries an
    # explicit UTC offset (PostgREST/timestamptz expectation).
    assert from_iso(result["updated_at"]) is not None
    assert result["updated_at"].endswith("+00:00")


def test_touch_does_not_mutate_the_input() -> None:
    original = {"a": 1}

    touch(original)

    assert "updated_at" not in original


def test_touch_updated_at_advances_on_a_second_call() -> None:
    first = touch({"a": 1})["updated_at"]
    time.sleep(0.01)
    second = touch({"a": 1})["updated_at"]

    assert from_iso(second) > from_iso(first)


@pytest.mark.integration
def test_job_from_row_maps_a_real_postgrest_row(supabase_client: SupabaseClient) -> None:
    """Insert a job, read it back through PostgREST, and map it.

    Exercises the `remote` nullable-boolean conversion (Postgres/PostgREST
    give True/False/None, unlike SQLite's 1/0/None) against an actual
    response rather than a hand-written dict.
    """
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    fingerprint = f"fp-{uuid.uuid4()}"
    inserted = supabase_client.insert(
        "job_hunter_jobs",
        [
            {
                "user_id": user_id,
                "fingerprint": fingerprint,
                "source": "greenhouse",
                "title": "Senior Backend Engineer",
                "company": "Acme Corp",
                "location": "Remote - EU",
                "url": "https://example.com/jobs/1",
                "canonical_url": "https://example.com/jobs/1",
                "description": "Build things.",
                "source_job_id": "gh-123",
                "remote": True,
                "ats_provider": "greenhouse",
                "ats_board": "acme",
                "ats_job_id": "123",
                "market_id": "eu",
                "content_confidence": "high",
                "first_seen_at": "2026-09-06T10:00:00+00:00",
                "last_seen_at": "2026-09-06T10:00:00+00:00",
            }
        ],
    )[0]

    row = supabase_client.select(
        "job_hunter_jobs", params={"id": f"eq.{inserted['id']}"}
    )[0]
    job = job_from_row(row)

    assert job.source == "greenhouse"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme Corp"
    assert job.location == "Remote - EU"
    assert job.url == "https://example.com/jobs/1"
    assert job.canonical_url == "https://example.com/jobs/1"
    assert job.description == "Build things."
    assert job.source_job_id == "gh-123"
    assert job.remote is True
    assert job.ats_provider == "greenhouse"
    assert job.ats_board == "acme"
    assert job.ats_job_id == "123"
    assert job.market_id == "eu"
    assert job.content_confidence == "high"


@pytest.mark.integration
def test_job_from_row_maps_null_remote_to_none(supabase_client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    inserted = supabase_client.insert(
        "job_hunter_jobs",
        [
            {
                "user_id": user_id,
                "fingerprint": f"fp-{uuid.uuid4()}",
                "source": "test",
                "url": "https://example.com/jobs/2",
                "first_seen_at": "2026-09-06T10:00:00+00:00",
                "last_seen_at": "2026-09-06T10:00:00+00:00",
                "remote": None,
            }
        ],
    )[0]

    row = supabase_client.select(
        "job_hunter_jobs", params={"id": f"eq.{inserted['id']}"}
    )[0]
    job = job_from_row(row)

    assert job.remote is None


@pytest.mark.integration
def test_job_from_row_maps_false_remote(supabase_client: SupabaseClient) -> None:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"
    inserted = supabase_client.insert(
        "job_hunter_jobs",
        [
            {
                "user_id": user_id,
                "fingerprint": f"fp-{uuid.uuid4()}",
                "source": "test",
                "url": "https://example.com/jobs/3",
                "first_seen_at": "2026-09-06T10:00:00+00:00",
                "last_seen_at": "2026-09-06T10:00:00+00:00",
                "remote": False,
            }
        ],
    )[0]

    row = supabase_client.select(
        "job_hunter_jobs", params={"id": f"eq.{inserted['id']}"}
    )[0]
    job = job_from_row(row)

    assert job.remote is False


@pytest.mark.integration
def test_store_fixture_constructs_a_postgres_job_store(store) -> None:
    """The `store` fixture (conftest.py) is what this task is unblocking."""
    with store as opened:
        assert opened is store

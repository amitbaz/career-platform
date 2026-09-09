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


def _insert_membership(client: SupabaseClient, seed_postings, **posting_fields) -> dict:
    """Write an advertisement and the caller's membership of it, and read it back.

    Two rows since #178: everything the advertisement says goes on the
    posting, and the job row carries only the caller's own facts. The read
    asks for the posting as an embed, which is the shape `job_from_row`
    composes a `Job` from.

    The posting is seeded over the privileged connection (#179); the
    membership row and the read back stay on PostgREST, because the shape
    PostgREST returns is what this file is about.
    """
    posting_id = seed_postings(
        [
            {
                "fingerprint": f"fp-{uuid.uuid4()}",
                "first_seen_at": "2026-09-06T10:00:00+00:00",
                "last_seen_at": "2026-09-06T10:00:00+00:00",
                **posting_fields,
            }
        ]
    )[0]
    inserted = client.insert(
        "job_hunter_jobs",
        [
            {
                "user_id": client.user_id,
                "posting_id": posting_id,
                "market_id": "eu",
                "first_seen_at": "2026-09-06T10:00:00+00:00",
                "last_seen_at": "2026-09-06T10:00:00+00:00",
            }
        ],
    )[0]
    return client.select(
        "job_hunter_jobs",
        params={
            "id": f"eq.{inserted['id']}",
            "select": (
                "market_id,posting:job_hunter_postings"
                "(source,title,company,location,description,source_job_id,remote,"
                "content_confidence,url,canonical_url,ats_provider,ats_board,ats_job_id)"
            ),
        },
    )[0]


@pytest.mark.integration
def test_job_from_row_maps_a_real_postgrest_row(supabase_client: SupabaseClient, seed_postings) -> None:
    """Insert a job, read it back through PostgREST, and map it.

    Exercises the `remote` nullable-boolean conversion (Postgres/PostgREST
    give True/False/None, unlike SQLite's 1/0/None) against an actual
    response rather than a hand-written dict.
    """
    row = _insert_membership(supabase_client, seed_postings,
        source="greenhouse",
        title="Senior Backend Engineer",
        company="Acme Corp",
        location="Remote - EU",
        url="https://example.com/jobs/1",
        canonical_url="https://example.com/jobs/1",
        description="Build things.",
        source_job_id="gh-123",
        remote=True,
        ats_provider="greenhouse",
        ats_board="acme",
        ats_job_id="123",
        content_confidence="high",
    )
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
def test_job_from_row_maps_null_remote_to_none(supabase_client: SupabaseClient, seed_postings) -> None:
    row = _insert_membership(supabase_client, seed_postings, source="test", url="https://example.com/jobs/2", remote=None
    )
    job = job_from_row(row)

    assert job.remote is None


@pytest.mark.integration
def test_job_from_row_maps_false_remote(supabase_client: SupabaseClient, seed_postings) -> None:
    row = _insert_membership(supabase_client, seed_postings, source="test", url="https://example.com/jobs/3", remote=False
    )
    job = job_from_row(row)

    assert job.remote is False


@pytest.mark.integration
def test_store_fixture_constructs_a_postgres_job_store(store) -> None:
    """The `store` fixture (conftest.py) is what this task is unblocking."""
    with store as opened:
        assert opened is store


# `job_from_row` and the posting (issues #177, #178) --------------------------
#
# The posting is the record of the advertisement, and it decides every
# posting-level fact as a set rather than field by field -- otherwise a
# posting that genuinely says `remote is false` or `company is ''` would fall
# back to whatever the mapping was handed alongside it. The job row has no
# copy of its own to fall back to since #178; what these dicts stand in for is
# a select that did not ask for the embed.


def _job_row_with(**overrides) -> dict:
    row = {
        "source": "aggregator",
        "title": "Stale Title",
        "company": "Stale Co",
        "location": "Stale City",
        "url": "https://stale.example/1",
        "description": "stale description",
        "source_job_id": "stale-1",
        "remote": True,
        "content_confidence": "aggregator_text",
        "market_id": "eu",
    }
    row.update(overrides)
    return row


def test_job_from_row_reads_posting_level_facts_from_the_posting() -> None:
    job = job_from_row(
        _job_row_with(
            posting={
                "source": "greenhouse",
                "title": "Senior Backend Engineer",
                "company": "Acme GmbH",
                "location": "Berlin",
                "description": "the shared description",
                "source_job_id": "gh-9",
                "remote": False,
                "content_confidence": "official_ats",
            }
        )
    )

    assert job.source == "greenhouse"
    assert job.title == "Senior Backend Engineer"
    assert job.company == "Acme GmbH"
    assert job.location == "Berlin"
    assert job.description == "the shared description"
    assert job.source_job_id == "gh-9"
    assert job.remote is False
    assert job.content_confidence == "official_ats"


def test_job_from_row_takes_the_market_from_the_membership_row() -> None:
    """`market_id` is which markets *this user* matched the posting to."""
    job = job_from_row(_job_row_with(posting={"title": "Shared Title", "remote": None}))

    assert job.market_id == "eu"


def test_job_from_row_lets_the_posting_answer_a_fact_with_an_empty_value() -> None:
    job = job_from_row(_job_row_with(posting={"company": "", "remote": None}))

    assert job.company == ""
    assert job.remote is None


def test_job_from_row_falls_back_to_the_row_when_the_embed_was_not_selected() -> None:
    """A mapping with no `posting` key reads as itself rather than as blanks.

    `posting_id` is `not null` since #178, so this is no longer a job row
    without an advertisement -- it is a select that did not ask for the embed.
    """
    job = job_from_row(_job_row_with(posting=None))

    assert job.title == "Stale Title"
    assert job.company == "Stale Co"
    assert job.remote is True


def test_job_from_row_takes_the_url_from_the_posting() -> None:
    """The resolved link is the posting's since #178.

    #177 kept `url` on the job row because a merged job row was the only row
    that had seen every posting behind it. #176 moved merging onto the
    posting -- the survivor carries the folded link -- so the posting is now
    that row, and reading it here is the same answer for every user rather
    than one each. See `job_from_row`.
    """
    job = job_from_row(
        _job_row_with(
            url="https://stale-membership-copy.example/9",
            posting={"url": "https://acme.example/careers/9", "remote": None},
        )
    )

    assert job.url == "https://acme.example/careers/9"

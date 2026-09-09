"""The `extract_facets` queue consumer (issue #185).

Unit-level: what the stage does with a message given a fake privileged
connection and a stubbed `extract_facets`. The SQL itself -- that the insert
lands, that `on conflict (posting_id)` replaces a prior row -- is a pgTAP
concern; this file is about the stage's own contract with `stage_queue.py`.
"""

from __future__ import annotations

import uuid

import pytest

from job_hunter import extract_facets_stage as stage_module
from job_hunter.ai import (
    AIQuotaPaused,
    AITemporaryCapacity,
    CredentialUnavailable,
    PlatformAllowanceExhausted,
)
from job_hunter.extract_facets_stage import ExtractedFacets, ExtractFacetsStage
from job_hunter.facets import FacetExtractionError
from job_hunter.models import Compensation, JobFacets
from job_hunter.stage_queue import (
    PermanentStageFailure,
    QueueMessage,
    QuotaExhausted,
    Stage,
    TransientStageFailure,
)

POSTING_ID = str(uuid.uuid4())

_POSTING_ROW = (
    "Backend Engineer",  # title
    "Acme",  # company
    "Berlin",  # location
    True,  # remote
    "Hire anywhere in the EU.",  # description
    "official_ats",  # content_confidence
    "ashby",  # source
)

_FACETS = JobFacets(
    seniority="senior",
    remote_policy="remote",
    relocation_policy="unknown",
    hiring_regions=["europe"],
    stack=["python"],
    compensation=Compensation(),
    requirements=[],
    source_supplied=[],
    model="test-model",
)


class FakeCursor:
    def __init__(self, plan):
        self._plan = plan
        self.executed: list[tuple[str, object]] = []
        self._row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "select title" in sql:
            if self._plan.get("read_error"):
                raise self._plan["read_error"]
            self._row = self._plan.get("posting_row")
        elif "insert into public.job_hunter_job_facets" in sql:
            if self._plan.get("write_error"):
                raise self._plan["write_error"]
            self._row = None
        else:
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._row


class FakeConnection:
    def __init__(self, plan):
        self._cursor = FakeCursor(plan)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self):
        return self._cursor


class FakeDatabase:
    def __init__(self, plan):
        self._connection = FakeConnection(plan)

    def connection(self):
        return self._connection


def _message(payload) -> QueueMessage:
    return QueueMessage(stage=Stage.EXTRACT_FACETS, message_id=1, payload=payload)


def _stage(plan, ai=None):
    return ExtractFacetsStage(FakeDatabase(plan), ai)


# Payload validation ----------------------------------------------------------


def test_rejects_a_message_for_the_wrong_stage():
    stage = _stage({})
    message = QueueMessage(
        stage=Stage.RESOLVE_PERSIST,
        message_id=1,
        payload={"posting_id": POSTING_ID},
    )
    with pytest.raises(PermanentStageFailure):
        stage(message)


def test_rejects_a_payload_with_extra_keys():
    stage = _stage({})
    with pytest.raises(PermanentStageFailure):
        stage(_message({"posting_id": POSTING_ID, "extra": 1}))


def test_rejects_a_non_uuid_posting_id():
    stage = _stage({})
    with pytest.raises(PermanentStageFailure):
        stage(_message({"posting_id": "not-a-uuid"}))


# Vanished posting --------------------------------------------------------------


def test_a_vanished_posting_dead_letters_immediately():
    stage = _stage({"posting_row": None})
    with pytest.raises(PermanentStageFailure):
        stage(_message({"posting_id": POSTING_ID}))


def test_reading_the_posting_failing_is_transient():
    stage = _stage({"read_error": RuntimeError("connection reset")})
    with pytest.raises(TransientStageFailure):
        stage(_message({"posting_id": POSTING_ID}))


# Successful extraction ---------------------------------------------------------


def test_extracts_and_stores_facets(monkeypatch):
    monkeypatch.setattr(stage_module, "extract_facets", lambda posting, ai: _FACETS)
    stage = _stage({"posting_row": _POSTING_ROW})

    result = stage(_message({"posting_id": POSTING_ID}))

    assert result == ExtractedFacets(posting_id=POSTING_ID, facets=_FACETS)


def test_writing_facets_failing_is_transient(monkeypatch):
    monkeypatch.setattr(stage_module, "extract_facets", lambda posting, ai: _FACETS)
    stage = _stage(
        {"posting_row": _POSTING_ROW, "write_error": RuntimeError("write failed")}
    )
    with pytest.raises(TransientStageFailure):
        stage(_message({"posting_id": POSTING_ID}))


# Unparseable output --------------------------------------------------------------


def test_unparseable_output_dead_letters_immediately(monkeypatch):
    error = FacetExtractionError("could not parse response")

    def explode(posting, ai):
        raise error

    monkeypatch.setattr(stage_module, "extract_facets", explode)
    stage = _stage({"posting_row": _POSTING_ROW})

    with pytest.raises(PermanentStageFailure) as excinfo:
        stage(_message({"posting_id": POSTING_ID}))
    assert excinfo.value.__cause__ is error


# Platform allowance / quota refusals are paced, not failed ----------------------


@pytest.mark.parametrize(
    "raised",
    [
        PlatformAllowanceExhausted("no allowance left"),
        AIQuotaPaused("paused", paused_until="2026-01-01T00:00:00Z", reason="daily_quota"),
        CredentialUnavailable("no platform credential"),
    ],
)
def test_platform_refusals_return_to_the_queue_without_a_failure(monkeypatch, raised):
    def explode(posting, ai):
        raise raised

    monkeypatch.setattr(stage_module, "extract_facets", explode)
    stage = _stage({"posting_row": _POSTING_ROW})

    with pytest.raises(QuotaExhausted) as excinfo:
        stage(_message({"posting_id": POSTING_ID}))
    assert excinfo.value.retry_after_seconds == stage_module._ALLOWANCE_RETRY_DELAY_SECONDS


def test_rolling_capacity_is_paced_by_its_own_retry_after(monkeypatch):
    def explode(posting, ai):
        raise AITemporaryCapacity("rolling window full", retry_after_seconds=12.5)

    monkeypatch.setattr(stage_module, "extract_facets", explode)
    stage = _stage({"posting_row": _POSTING_ROW})

    with pytest.raises(QuotaExhausted) as excinfo:
        stage(_message({"posting_id": POSTING_ID}))
    assert excinfo.value.retry_after_seconds == 12


# Unclassified failures stay unclassified (the runner treats them as transient) --


def test_an_unrecognised_provider_error_is_not_caught_here(monkeypatch):
    def explode(posting, ai):
        raise TimeoutError("provider timed out")

    monkeypatch.setattr(stage_module, "extract_facets", explode)
    stage = _stage({"posting_row": _POSTING_ROW})

    with pytest.raises(TimeoutError):
        stage(_message({"posting_id": POSTING_ID}))

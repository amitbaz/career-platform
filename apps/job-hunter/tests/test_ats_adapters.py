import logging

import requests

from job_hunter.sources import ashby, greenhouse, lever
from job_hunter.sources.ashby import AshbySource
from job_hunter.sources.base import is_stale_board_error
from job_hunter.sources.greenhouse import GreenhouseSource
from job_hunter.sources.lever import LeverSource


class _Response:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _http_error(status_code: int) -> requests.HTTPError:
    return requests.HTTPError(f"status {status_code}", response=_Response(status_code))


def test_is_stale_board_error_true_for_404():
    assert is_stale_board_error(_http_error(404)) is True


def test_is_stale_board_error_false_for_other_status_codes():
    assert is_stale_board_error(_http_error(500)) is False
    assert is_stale_board_error(_http_error(429)) is False


def test_is_stale_board_error_false_for_non_http_errors():
    assert is_stale_board_error(RuntimeError("network down")) is False


class _RaisingHttp:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get_json(self, url, **kwargs):
        raise self._exc


def test_greenhouse_404_logs_compact_without_traceback(caplog):
    with caplog.at_level(logging.INFO):
        jobs = list(GreenhouseSource("dead-token", _RaisingHttp(_http_error(404))).discover())
    assert jobs == []
    records = [r for r in caplog.records if "dead-token" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None


def test_greenhouse_unexpected_error_logs_full_traceback(caplog):
    with caplog.at_level(logging.WARNING):
        jobs = list(GreenhouseSource("acme", _RaisingHttp(RuntimeError("boom"))).discover())
    assert jobs == []
    records = [r for r in caplog.records if "acme" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None


def test_lever_404_logs_compact_without_traceback(caplog):
    with caplog.at_level(logging.INFO):
        jobs = list(LeverSource("dead-site", _RaisingHttp(_http_error(404))).discover())
    assert jobs == []
    records = [r for r in caplog.records if "dead-site" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None


def test_ashby_404_logs_compact_without_traceback(caplog):
    with caplog.at_level(logging.INFO):
        jobs = list(AshbySource("dead-board", _RaisingHttp(_http_error(404))).discover())
    assert jobs == []
    records = [r for r in caplog.records if "dead-board" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is None


class _FakeHttp:
    def __init__(self, data) -> None:
        self._data = data

    def get_json(self, url, **kwargs):
        return self._data


def test_lever_populates_ats_identity():
    http = _FakeHttp(
        [
            {
                "id": "abc-123",
                "text": "Backend Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                "descriptionPlain": "JD",
            },
        ]
    )
    job = list(LeverSource("acme", http).discover())[0]
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("lever", "acme", "abc-123")


def test_ashby_populates_ats_identity():
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "id": "xyz",
                    "title": "Backend Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/xyz",
                    "descriptionPlain": "JD",
                },
            ]
        }
    )
    job = list(AshbySource("acme", http).discover())[0]
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("ashby", "acme", "xyz")


def test_greenhouse_populates_ats_identity():
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "id": 456,
                    "title": "Backend Engineer",
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/456",
                    "content": "JD",
                },
            ]
        }
    )
    job = list(GreenhouseSource("acme", http).discover())[0]
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("greenhouse", "acme", "456")


def test_greenhouse_falls_back_to_its_own_board_for_unparseable_urls():
    # A board embedded on the employer's own domain reports an absolute_url
    # with no recognisable ATS host or board/job-id path shape, so nothing can
    # be parsed out of it; the adapter knows the board token and job id anyway.
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "id": 456,
                    "title": "Backend Engineer",
                    "absolute_url": "https://careers.acme.test/openings/456",
                    "content": "JD",
                },
            ]
        }
    )
    job = list(GreenhouseSource("acme", http).discover())[0]
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("greenhouse", "acme", "456")


def test_adapter_identity_matches_the_url_when_the_two_disagree():
    # The board slug the adapter was constructed with can differ in case from
    # the one in the posting URL. The URL wins, so the identity agrees with
    # what every other code path derives from that same URL.
    http = _FakeHttp(
        [
            {
                "id": "abc-123",
                "text": "Backend Engineer",
                "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                "descriptionPlain": "JD",
            },
        ]
    )
    job = list(LeverSource("ACME", http).discover())[0]
    assert job.ats_board == "acme"


def test_ashby_fetch_description_matches_by_url():
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
                    "descriptionPlain": "Full JD text",
                },
            ]
        }
    )
    result = ashby.fetch_description("acme", "https://jobs.ashbyhq.com/acme/abc", http)
    assert result == "Full JD text"


def test_ashby_fetch_description_returns_none_when_url_not_found():
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
                    "descriptionPlain": "Full JD text",
                },
            ]
        }
    )
    result = ashby.fetch_description("acme", "https://jobs.ashbyhq.com/acme/does-not-exist", http)
    assert result is None


def test_lever_fetch_description_matches_by_url():
    http = _FakeHttp(
        [
            {
                "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                "descriptionPlain": "Full JD text",
            },
        ]
    )
    result = lever.fetch_description("acme", "https://jobs.lever.co/acme/abc-123", http)
    assert result == "Full JD text"


def test_lever_fetch_description_returns_none_when_url_not_found():
    http = _FakeHttp(
        [
            {
                "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                "descriptionPlain": "Full JD text",
            },
        ]
    )
    result = lever.fetch_description("acme", "https://jobs.lever.co/acme/does-not-exist", http)
    assert result is None


def test_greenhouse_fetch_description_matches_by_url():
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/456",
                    "content": "Full JD text",
                },
            ]
        }
    )
    result = greenhouse.fetch_description(
        "acme", "https://boards.greenhouse.io/acme/jobs/456", http
    )
    assert result == "Full JD text"


def test_greenhouse_fetch_description_returns_none_when_url_not_found():
    http = _FakeHttp(
        {
            "jobs": [
                {
                    "absolute_url": "https://boards.greenhouse.io/acme/jobs/456",
                    "content": "Full JD text",
                },
            ]
        }
    )
    result = greenhouse.fetch_description(
        "acme", "https://boards.greenhouse.io/acme/jobs/does-not-exist", http
    )
    assert result is None

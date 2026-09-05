import json
from datetime import datetime, timezone

import pytest

from job_hunter.models import Job
from job_hunter.sources.company_watch import CompanyWatchSource
from job_hunter.store import JobStore


class FakeHttp:
    def __init__(self, json_data):
        self.json_data = json_data
        self.calls = []

    def get_json(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.json_data


def _watch(store, company_name, ats_identifier):
    return store.upsert_company_watch(
        company_name=company_name,
        careers_url="",
        ats_provider="greenhouse",
        ats_identifier=ats_identifier,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )


def test_greenhouse_watch_rewrites_only_source_and_records_success(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    _watch(store, "Acme", "acme")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    employer_url = "https://boards.greenhouse.io/acme/jobs/555"
    http = FakeHttp(
        {
            "jobs": [
                {
                    "id": 555,
                    "title": "Senior Product Engineer",
                    "location": {"name": "Remote - Europe"},
                    "absolute_url": employer_url,
                    "content": "<p>React TypeScript</p>",
                }
            ]
        }
    )

    jobs = CompanyWatchSource(store, http, now=lambda: now).discover()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "watch:greenhouse"
    assert job.source_job_id == "555"
    assert job.url == employer_url
    assert job.company == "acme"
    assert job.title == "Senior Product Engineer"
    row = store.get_company_watch("Acme")
    assert row["last_successful_check_at"] == "2026-08-31T12:00:00+00:00"
    assert row["last_verified_at"] == "2026-08-31T12:00:00+00:00"


def test_company_failure_is_recorded_without_skipping_later_watch(
    tmp_path, monkeypatch
):
    import job_hunter.sources.company_watch as company_watch

    store = JobStore(tmp_path / "state.sqlite3")
    failed_id = _watch(store, "Broken", "broken")
    healthy_id = _watch(store, "Healthy", "healthy")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    class ControlledGreenhouseSource:
        def __init__(self, token, http):
            self.token = token

        def discover(self):
            if self.token == "broken":
                raise RuntimeError("board unavailable")
            return [
                Job(
                    source="greenhouse",
                    source_job_id="healthy-1",
                    title="Frontend Engineer",
                    company="healthy",
                    url="https://boards.greenhouse.io/healthy/jobs/healthy-1",
                )
            ]

    monkeypatch.setitem(
        company_watch._ATS_SOURCE_TYPES,
        "greenhouse",
        ControlledGreenhouseSource,
    )

    jobs = CompanyWatchSource(store, FakeHttp({}), now=lambda: now).discover()

    assert [job.source_job_id for job in jobs] == ["healthy-1"]
    assert jobs[0].source == "watch:greenhouse"
    failed = store.get_company_watch("Broken")
    healthy = store.get_company_watch("Healthy")
    assert failed["id"] == failed_id
    assert failed["consecutive_failures"] == 1
    assert failed["last_successful_check_at"] is None
    assert healthy["id"] == healthy_id
    assert healthy["consecutive_failures"] == 0
    assert healthy["last_successful_check_at"] == "2026-08-31T12:00:00+00:00"


def test_swallowed_ats_http_failure_updates_health_and_later_watch_runs(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    _watch(store, "Broken", "broken")
    _watch(store, "Healthy", "healthy")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    class BoardHttp:
        def __init__(self):
            self.calls = []

        def get_json(self, url, **kwargs):
            self.calls.append(url)
            if "/broken/" in url:
                raise RuntimeError("network down")
            return {
                "jobs": [
                    {
                        "id": 777,
                        "title": "Frontend Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": "https://boards.greenhouse.io/healthy/jobs/777",
                        "content": "React",
                    }
                ]
            }

    http = BoardHttp()

    jobs = CompanyWatchSource(store, http, now=lambda: now).discover()

    assert [job.source_job_id for job in jobs] == ["777"]
    assert len(http.calls) == 2
    failed = store.get_company_watch("Broken")
    healthy = store.get_company_watch("Healthy")
    assert failed["consecutive_failures"] == 1
    assert failed["last_successful_check_at"] is None
    assert healthy["consecutive_failures"] == 0
    assert healthy["last_successful_check_at"] == "2026-08-31T12:00:00+00:00"


def test_failure_health_write_error_does_not_abort_later_watch(
    tmp_path, monkeypatch
):
    import job_hunter.sources.company_watch as company_watch

    store = JobStore(tmp_path / "state.sqlite3")
    failed_id = _watch(store, "Broken", "broken")
    _watch(store, "Healthy", "healthy")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    class ControlledGreenhouseSource:
        def __init__(self, token, http):
            self.token = token

        def discover(self):
            if self.token == "broken":
                raise RuntimeError("board unavailable")
            return [
                Job(
                    source="greenhouse",
                    source_job_id="healthy-1",
                    title="Frontend Engineer",
                    company="healthy",
                )
            ]

    monkeypatch.setitem(
        company_watch._ATS_SOURCE_TYPES,
        "greenhouse",
        ControlledGreenhouseSource,
    )
    original_record_failure = store.record_watch_failure

    def record_failure(watch_id, checked_at):
        if watch_id == failed_id:
            raise OSError("database locked")
        original_record_failure(watch_id, checked_at)

    monkeypatch.setattr(store, "record_watch_failure", record_failure)

    jobs = CompanyWatchSource(store, FakeHttp({}), now=lambda: now).discover()

    assert [job.source_job_id for job in jobs] == ["healthy-1"]
    assert (
        store.get_company_watch("Healthy")["last_successful_check_at"]
        == "2026-08-31T12:00:00+00:00"
    )


def test_success_health_write_error_keeps_jobs_and_does_not_record_failure(
    tmp_path, monkeypatch
):
    import job_hunter.sources.company_watch as company_watch

    store = JobStore(tmp_path / "state.sqlite3")
    first_id = _watch(store, "First", "first")
    _watch(store, "Second", "second")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    class SuccessfulGreenhouseSource:
        def __init__(self, token, http):
            self.token = token

        def discover(self):
            return [
                Job(
                    source="greenhouse",
                    source_job_id=f"{self.token}-1",
                    title="Frontend Engineer",
                    company=self.token,
                )
            ]

    monkeypatch.setitem(
        company_watch._ATS_SOURCE_TYPES,
        "greenhouse",
        SuccessfulGreenhouseSource,
    )
    original_record_success = store.record_watch_success

    def record_success(watch_id, checked_at):
        if watch_id == first_id:
            raise OSError("database locked")
        original_record_success(watch_id, checked_at)

    monkeypatch.setattr(store, "record_watch_success", record_success)

    jobs = CompanyWatchSource(store, FakeHttp({}), now=lambda: now).discover()

    assert [job.source_job_id for job in jobs] == ["first-1", "second-1"]
    first = store.get_company_watch("First")
    second = store.get_company_watch("Second")
    assert first["consecutive_failures"] == 0
    assert first["last_successful_check_at"] is None
    assert second["last_successful_check_at"] == "2026-08-31T12:00:00+00:00"


def test_generic_watch_parses_postings_and_links_from_one_page_only(tmp_path):
    store = JobStore(tmp_path / "state.sqlite3")
    careers_url = "https://acme.test/careers"
    store.upsert_company_watch(
        company_name="Acme",
        careers_url=careers_url,
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    html = """
    <html><body>
      <script type="application/ld+json">
        {
          "@graph": [
            {
              "@type": "JobPosting",
              "title": "Senior Product Engineer",
              "description": "<p>React and TypeScript</p>",
              "hiringOrganization": {"name": "Acme Labs"},
              "jobLocationType": "TELECOMMUTE",
              "url": "/jobs/product-engineer"
            },
            {
              "@type": "JobPosting",
              "title": "Frontend Lead",
              "description": "Design systems",
              "url": "https://acme.test/jobs/frontend-lead"
            }
          ]
        }
      </script>
      <a href="https://boards.greenhouse.io/acme/jobs/999">View another role</a>
    </body></html>
    """

    class FakeResponse:
        text = html

        @staticmethod
        def raise_for_status():
            return None

    class OnePageHttp:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            if url != careers_url:
                raise AssertionError("generic watch must not recurse")
            return FakeResponse()

    http = OnePageHttp()
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    jobs = CompanyWatchSource(store, http, now=lambda: now).discover()

    assert http.calls == [careers_url]
    assert [(job.title, job.url) for job in jobs] == [
        ("Senior Product Engineer", "https://acme.test/jobs/product-engineer"),
        ("Frontend Lead", "https://acme.test/jobs/frontend-lead"),
        ("", "https://boards.greenhouse.io/acme/jobs/999"),
    ]
    assert jobs[0].source == "watch:generic"
    assert jobs[0].company == "Acme Labs"
    assert jobs[0].description == "React and TypeScript"
    assert jobs[0].remote is True
    assert jobs[1].company == "Acme"


@pytest.mark.parametrize(
    "job_type",
    [
        ["Thing", "JobPosting"],
        "https://schema.org/JobPosting",
        "http://schema.org/JobPosting",
        ["Thing", "https://schema.org/JobPosting"],
    ],
)
def test_generic_watch_accepts_compatible_json_ld_job_types(tmp_path, job_type):
    store = JobStore(tmp_path / "state.sqlite3")
    careers_url = "https://acme.test/careers"
    store.upsert_company_watch(
        company_name="Acme",
        careers_url=careers_url,
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="manual",
        confidence=1.0,
    )
    posting = {
        "@type": job_type,
        "title": "Frontend Engineer",
        "url": "/jobs/frontend-engineer",
    }
    html = (
        '<script type="application/ld+json">'
        f"{json.dumps(posting)}"
        "</script>"
    )

    class FakeResponse:
        text = html

        @staticmethod
        def raise_for_status():
            return None

    class OnePageHttp:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append(url)
            return FakeResponse()

    http = OnePageHttp()
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)

    jobs = CompanyWatchSource(store, http, now=lambda: now).discover()

    assert http.calls == [careers_url]
    assert [(job.title, job.url) for job in jobs] == [
        ("Frontend Engineer", "https://acme.test/jobs/frontend-engineer")
    ]


def test_company_only_watch_is_skipped_without_recording_failure(tmp_path, caplog):
    store = JobStore(tmp_path / "state.sqlite3")
    placeholder_id = store.upsert_company_watch(
        company_name="Distribusion Technologies",
        careers_url="",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="automatic",
        confidence=1.0,
    )
    _watch(store, "Healthy", "healthy")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
    http = FakeHttp(
        {
            "jobs": [
                {
                    "id": 42,
                    "title": "Engineer",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/healthy/jobs/42",
                    "content": "<p>Work</p>",
                }
            ]
        }
    )

    with caplog.at_level("WARNING"):
        jobs = CompanyWatchSource(store, http, now=lambda: now).discover()

    assert [job.source for job in jobs] == ["watch:greenhouse"]
    assert "Distribusion Technologies" not in caplog.text

    placeholder = store.get_company_watch("Distribusion Technologies")
    assert placeholder["id"] == placeholder_id
    assert placeholder["consecutive_failures"] == 0
    assert placeholder["paused_until"] is None
    assert placeholder["last_verified_at"] is None
    assert placeholder["last_successful_check_at"] is None

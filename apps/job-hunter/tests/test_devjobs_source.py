import pytest

from job_hunter.sources.devjobs import DevJobsSource


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


_LISTING_HTML = """
<html><body>
    <a href="/job-details/4458634930">Frontend Engineer</a>
</body></html>
"""


def _detail_html(work_mode: str) -> str:
    return f"""
    <html>
      <head><title>Frontend Engineer - Loora - Tel Aviv-Yafo | DevJobs</title></head>
      <body>
        <h3>Frontend Engineer</h3>
        <div>Job Type {work_mode}</div>
        <div>Location Tel Aviv-Yafo</div>
        <div>Skills</div><span>TypeScript</span><span>React</span>
        <p>Build and own our web products with React and TypeScript.</p>
      </body>
    </html>
    """


class FakeHttp:
    def __init__(self, listing_html=_LISTING_HTML, detail_html_by_id=None):
        self.listing_html = listing_html
        self.detail_html_by_id = detail_html_by_id or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "job-details" in url:
            job_id = url.rstrip("/").rsplit("/", 1)[-1]
            html = self.detail_html_by_id.get(job_id, _detail_html("Remote"))
            return FakeResponse(html)
        return FakeResponse(self.listing_html)


@pytest.mark.parametrize(
    "work_mode,expected_remote",
    [
        ("Remote", True),
        ("On-site", False),
        ("Hybrid", False),
        ("Freelance", None),
    ],
)
def test_devjobs_parses_listing_and_detail(work_mode, expected_remote):
    http = FakeHttp(detail_html_by_id={"4458634930": _detail_html(work_mode)})

    jobs = DevJobsSource(http).discover()

    # Frontend and Full Stack share the same listing fixture and thus the same
    # job id; cross-category dedup means the id is fetched only once.
    assert len(jobs) == 1
    job = jobs[0]
    assert job.title == "Frontend Engineer"
    assert job.company == "Loora"
    assert job.location == "Tel Aviv-Yafo"
    assert job.remote is expected_remote
    assert job.source == "devjobs"
    assert job.source_job_id == "4458634930"
    assert job.market_hint == "israel_remote"
    assert "Build and own our web products with React and TypeScript." in job.description


def test_devjobs_listing_failure_returns_empty_list():
    class FailingHttp(FakeHttp):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            raise RuntimeError("network down")

    jobs = DevJobsSource(FailingHttp()).discover()

    assert jobs == []


def test_devjobs_detail_failure_skips_only_that_posting():
    listing_html = """
    <html><body>
        <a href="/job-details/1">Job One</a>
        <a href="/job-details/2">Job Two</a>
    </body></html>
    """

    class PartiallyFailingHttp(FakeHttp):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if url.endswith("/job-details/1"):
                raise RuntimeError("detail fetch failed")
            if "job-details" in url:
                return FakeResponse(_detail_html("Remote"))
            return FakeResponse(self.listing_html)

    http = PartiallyFailingHttp(listing_html=listing_html)
    jobs = DevJobsSource(http).discover()

    # Both categories share the same listing fixture, so cross-category dedup
    # fetches each id only once: job 1 fails, job 2 succeeds.
    assert len(jobs) == 1
    assert all(job.source_job_id == "2" for job in jobs)


def test_devjobs_malformed_detail_title_is_skipped():
    malformed_html = """
    <html>
      <head><title>Not a valid title format | DevJobs</title></head>
      <body><p>Some content</p></body>
    </html>
    """
    http = FakeHttp(detail_html_by_id={"4458634930": malformed_html})

    jobs = DevJobsSource(http).discover()

    assert jobs == []


def test_devjobs_deduplicates_detail_ids_within_a_listing():
    listing_html = """
    <html><body>
        <a href="/job-details/4458634930">Frontend Engineer</a>
        <a href="/job-details/4458634930">Frontend Engineer</a>
    </body></html>
    """
    http = FakeHttp(listing_html=listing_html)

    jobs = DevJobsSource(http).discover()

    detail_calls = [call for call in http.calls if "job-details" in call[0]]
    # The listing's own duplicate anchor collapses to 1 id, and cross-category
    # dedup means the second category's identical listing adds nothing.
    assert len(detail_calls) == 1
    assert len(jobs) == 1


def test_devjobs_dedupes_job_ids_across_categories():
    frontend_html = """
    <html><body>
        <a href="/job-details/1">Job One</a>
    </body></html>
    """
    fullstack_html = """
    <html><body>
        <a href="/job-details/1">Job One</a>
        <a href="/job-details/2">Job Two</a>
    </body></html>
    """

    class PerCategoryHttp(FakeHttp):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "job-details" in url:
                job_id = url.rstrip("/").rsplit("/", 1)[-1]
                html = self.detail_html_by_id.get(job_id, _detail_html("Remote"))
                return FakeResponse(html)
            category = kwargs.get("params", {}).get("developerTypes")
            return FakeResponse(frontend_html if category == "Frontend" else fullstack_html)

    http = PerCategoryHttp()
    jobs = DevJobsSource(http).discover()

    detail_calls = [call for call in http.calls if "job-details" in call[0]]
    # Job 1 appears in both categories' listings but is only fetched once;
    # job 2 is unique to Full Stack and is fetched once too.
    assert len(detail_calls) == 2
    assert {job.source_job_id for job in jobs} == {"1", "2"}


def test_devjobs_caps_detail_requests_per_category():
    listing_html = """
    <html><body>
        <a href="/job-details/1">Job One</a>
        <a href="/job-details/2">Job Two</a>
        <a href="/job-details/3">Job Three</a>
    </body></html>
    """
    http = FakeHttp(listing_html=listing_html)

    DevJobsSource(http, max_jobs_per_category=2).discover()

    detail_calls = [call for call in http.calls if "job-details" in call[0]]
    # Frontend takes ids 1,2 (cap of 2); Full Stack sees the same listing but,
    # after cross-category dedup, only id 3 remains unseen.
    assert len(detail_calls) == 3

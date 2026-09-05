import pytest

from job_hunter.sources.wellfound import WellfoundListing, WellfoundSource


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


_LISTING_HTML = """
<a href="/jobs/4639071-frontend-engineer">Frontend Engineer</a>
<a href="/jobs/2404013-senior-frontend-engineer-remote-europe">Senior Frontend Engineer - Remote Europe</a>
"""


def _detail_html(extra_body: str = "") -> str:
    return f"""
    <html>
      <head><title>Frontend Engineer at Omnea • London | Wellfound</title></head>
      <body>
        <h1>Frontend Engineer</h1>
        <div>£90k – £160k</div>
        <div>Full Time</div>
        <div>Job Location</div><div>London</div>
        <div>Visa Sponsorship</div><div>Not Available</div>
        <div>Relocation Not Allowed</div>
        {extra_body}
        <h2>About the job</h2>
        <p>Own the design system and front-end architecture.</p>
      </body>
    </html>
    """


def _job_id_from_url(url: str) -> str:
    # e.g. https://wellfound.com/jobs/4639071-frontend-engineer -> 4639071
    tail = url.rsplit("/jobs/", 1)[-1]
    return tail.split("-", 1)[0]


class FakeHttp:
    def __init__(self, listing_html=_LISTING_HTML, detail_html_by_id=None):
        self.listing_html = listing_html
        self.detail_html_by_id = detail_html_by_id or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if "/jobs/" in url:
            job_id = _job_id_from_url(url)
            html = self.detail_html_by_id.get(job_id, _detail_html())
            return FakeResponse(html)
        return FakeResponse(self.listing_html)


def test_wellfound_parses_listing_and_detail():
    http = FakeHttp(detail_html_by_id={"4639071": _detail_html()})
    listings = [WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london")]

    jobs = WellfoundSource(http, listings).discover()

    assert len(jobs) == 2
    job = jobs[0]
    assert job.source == "wellfound"
    assert job.source_job_id == "4639071"
    assert job.title == "Frontend Engineer"
    assert job.company == "Omnea"
    assert job.location == "London"
    assert job.market_hint == "london"
    assert "£90k" in job.description
    assert "Not Available" in job.description
    assert job.remote is None  # no "Remote Work Policy" text present at all


@pytest.mark.parametrize(
    "policy_text,expected_remote",
    [
        ("Remote Work Policy Remote only", True),
        ("Remote Work Policy In office", False),
        ("Remote Work Policy Hybrid", None),
    ],
)
def test_wellfound_parses_remote_work_policy(policy_text, expected_remote):
    http = FakeHttp(detail_html_by_id={"4639071": _detail_html(extra_body=f"<div>{policy_text}</div>")})
    listings = [WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london")]

    jobs = WellfoundSource(http, listings).discover()

    job = next(j for j in jobs if j.source_job_id == "4639071")
    assert job.remote is expected_remote
    assert policy_text in job.description


def test_wellfound_detail_missing_at_and_bullet_leaves_company_and_location_empty():
    malformed_html = """
    <html>
      <head><title>Frontend Engineer | Wellfound</title></head>
      <body><h1>Frontend Engineer</h1><p>Some content</p></body>
    </html>
    """
    http = FakeHttp(detail_html_by_id={"4639071": malformed_html})
    listings = [WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london")]

    jobs = WellfoundSource(http, listings).discover()

    job = next(j for j in jobs if j.source_job_id == "4639071")
    assert job.title == "Frontend Engineer"
    assert job.company == ""
    assert job.location == ""


def test_wellfound_listing_failure_does_not_block_other_listings():
    class PartiallyFailingHttp(FakeHttp):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "role/l" in url and "london" in url:
                raise RuntimeError("network down")
            if "/jobs/" in url:
                job_id = _job_id_from_url(url)
                html = self.detail_html_by_id.get(job_id, _detail_html())
                return FakeResponse(html)
            return FakeResponse(self.listing_html)

    http = PartiallyFailingHttp()
    listings = [
        WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london"),
        WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/europe", market_id="germany_eu"),
    ]

    jobs = WellfoundSource(http, listings).discover()

    assert len(jobs) == 2
    assert all(job.market_hint == "germany_eu" for job in jobs)


def test_wellfound_detail_failure_skips_only_that_posting():
    listing_html = """
    <a href="/jobs/1-job-one">Job One</a>
    <a href="/jobs/2-job-two">Job Two</a>
    """

    class PartiallyFailingHttp(FakeHttp):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "/jobs/1-" in url:
                raise RuntimeError("detail fetch failed")
            if "/jobs/" in url:
                job_id = _job_id_from_url(url)
                html = self.detail_html_by_id.get(job_id, _detail_html())
                return FakeResponse(html)
            return FakeResponse(self.listing_html)

    http = PartiallyFailingHttp(listing_html=listing_html)
    listings = [WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london")]

    jobs = WellfoundSource(http, listings).discover()

    assert len(jobs) == 1
    assert jobs[0].source_job_id == "2"


def test_wellfound_deduplicates_links_within_one_page():
    listing_html = """
    <a href="/jobs/4639071-frontend-engineer">Frontend Engineer</a>
    <a href="/jobs/4639071-frontend-engineer">Frontend Engineer</a>
    """
    http = FakeHttp(listing_html=listing_html)
    listings = [WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london")]

    jobs = WellfoundSource(http, listings).discover()

    detail_calls = [call for call in http.calls if "/jobs/" in call[0]]
    assert len(detail_calls) == 1
    assert len(jobs) == 1


def test_wellfound_global_dedup_across_listings_keeps_first_market_hint():
    listing_a_html = '<a href="/jobs/4639071-frontend-engineer">Frontend Engineer</a>'
    listing_b_html = '<a href="/jobs/4639071-frontend-engineer">Frontend Engineer</a>'

    class MultiListingHttp(FakeHttp):
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if "germany_eu" in url or "europe" in url:
                return FakeResponse(listing_a_html)
            if "london" in url:
                return FakeResponse(listing_b_html)
            if "/jobs/" in url:
                job_id = _job_id_from_url(url)
                html = self.detail_html_by_id.get(job_id, _detail_html())
                return FakeResponse(html)
            return FakeResponse(self.listing_html)

    http = MultiListingHttp()
    listings = [
        WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/europe", market_id="germany_eu"),
        WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london"),
    ]

    jobs = WellfoundSource(http, listings).discover()

    assert len(jobs) == 1
    assert jobs[0].market_hint == "germany_eu"
    detail_calls = [call for call in http.calls if "/jobs/4639071" in call[0]]
    assert len(detail_calls) == 1


def test_fetch_job_sets_source_page_html_for_canonical_reuse():
    http = FakeHttp()
    listings = [WellfoundListing(url="https://wellfound.com/role/l/x/y", market_id="germany_eu")]
    source = WellfoundSource(http, listings)

    jobs = source.discover()

    assert len(jobs) == 2
    assert all(job.source_page_html for job in jobs)
    assert "Frontend Engineer" in jobs[0].source_page_html


def test_wellfound_caps_new_detail_urls_per_listing():
    listing_html = """
    <a href="/jobs/1-job-one">Job One</a>
    <a href="/jobs/2-job-two">Job Two</a>
    <a href="/jobs/3-job-three">Job Three</a>
    """
    http = FakeHttp(listing_html=listing_html)
    listings = [WellfoundListing(url="https://wellfound.com/role/l/frontend-engineer/london", market_id="london")]

    WellfoundSource(http, listings, max_jobs_per_listing=2).discover()

    detail_calls = [call for call in http.calls if "/jobs/" in call[0]]
    assert len(detail_calls) == 2

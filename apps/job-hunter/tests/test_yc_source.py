from job_hunter.models import GeminiQuotaSettings, SearchPolicy, Settings


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class FakeHttp:
    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(url)
        return self.responses[url]


def _job_links() -> str:
    return """
    <html><body>
      <a class="ycdc-card" href="/companies/acme/jobs/abc"
         data-company="Acme"
         data-title="Senior Product Engineer"
         data-location="Berlin, Germany">Senior Product Engineer</a>
      <a class="ycdc-card" href="/companies/acme/jobs/abc"
         data-company="Acme"
         data-title="Senior Product Engineer"
         data-location="Berlin, Germany">Senior Product Engineer</a>
    </body></html>
    """


def test_yc_source_maps_public_job_links_and_deduplicates_urls():
    from job_hunter.sources.yc import YCSource

    page = "https://www.ycombinator.com/jobs/role"
    jobs = YCSource(FakeHttp({page: FakeResponse(_job_links())}), [page]).discover()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "yc"
    assert job.company == "Acme"
    assert job.title == "Senior Product Engineer"
    assert job.location == "Berlin, Germany"
    assert job.url == "https://www.ycombinator.com/companies/acme/jobs/abc"


def test_yc_source_continues_to_later_public_page_after_failure():
    from job_hunter.sources.yc import YCSource

    unavailable_page = "https://www.ycombinator.com/jobs/role"
    valid_page = "https://www.ycombinator.com/jobs/location/berlin"
    jobs = YCSource(
        FakeHttp(
            {
                unavailable_page: FakeResponse(status_code=500),
                valid_page: FakeResponse(_job_links()),
            }
        ),
        [unavailable_page, valid_page],
    ).discover()

    assert [job.url for job in jobs] == [
        "https://www.ycombinator.com/companies/acme/jobs/abc"
    ]


def test_build_sources_includes_yc_for_configured_public_pages():
    from job_hunter.sources import build_sources

    settings = Settings(
        gemini_api_key="g",
        candidate_profile="profile",
        cover_letter_template="template",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=SearchPolicy(
            target_titles=[],
            positive_keywords=[],
            blocked_title_keywords=[],
            salary_floor_eur=90000,
            thresholds={},
            yc_job_pages=["https://www.ycombinator.com/jobs/role"],
        ),
        gemini_quota=GeminiQuotaSettings(rpm=10, tpm=250000, rpd=500),
    )

    sources = build_sources(settings, FakeHttp({}))

    assert [type(source).__name__ for source in sources].count("YCSource") == 1

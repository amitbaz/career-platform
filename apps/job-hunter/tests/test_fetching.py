from job_hunter.content_confidence import CANONICAL_EMPLOYER_PAGE, SOURCE_DETAIL_PAGE
from job_hunter.fetching import enrich_job, extract_job_from_html, extract_job_page_links
from job_hunter.models import Job


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, html):
        self._html = html

    def get(self, url, **kwargs):
        return FakeResponse(self._html)


_JOB_POSTING_HTML = """
<html><head>
<script type="application/ld+json">
{
  "@type": "JobPosting",
  "title": "Senior Product Engineer",
  "hiringOrganization": {"name": "Acme"},
  "jobLocationType": "TELECOMMUTE",
  "description": "<p>Loves React and TypeScript</p>"
}
</script>
</head><body></body></html>
"""

_PLAIN_BODY_HTML = """
<html><head><title>Senior Engineer at Acme</title></head>
<body><p>Some job description here with React</p></body></html>
"""


def test_extracts_jobposting_json_ld():
    html = '''<script type="application/ld+json">{"@type":"JobPosting","title":"Senior Product Engineer","description":"<p>React and TypeScript</p>","hiringOrganization":{"name":"Acme"},"jobLocationType":"TELECOMMUTE"}</script>'''
    data = extract_job_from_html(html)
    assert data["title"] == "Senior Product Engineer"
    assert data["company"] == "Acme"
    assert data["remote"] is True
    assert "React and TypeScript" in data["description"]


def test_extracts_json_ld_from_array():
    html = '''<script type="application/ld+json">[{"@type":"WebPage"},{"@type":"JobPosting","title":"Frontend Lead","description":"TypeScript","hiringOrganization":{"name":"Corp"}}]</script>'''
    data = extract_job_from_html(html)
    assert data["title"] == "Frontend Lead"


def test_falls_back_to_page_title():
    html = '''<html><head><title>Senior Engineer at Acme</title></head><body><p>Some job description here with React</p></body></html>'''
    data = extract_job_from_html(html)
    assert data.get("title") or data.get("description")


def test_extract_job_page_links_returns_canonical_json_ld_and_ats_anchors():
    html = '''
    <link rel="canonical" href="/jobs/frontend">
    <script type="application/ld+json">
      {"@type": "JobPosting", "url": "https://jobs.ashbyhq.com/acme/123"}
    </script>
    <a href="https://jobs.lever.co/acme/456">Apply</a>
    <a href="https://example.test/careers">Careers</a>
    '''

    assert extract_job_page_links(html, "https://example.test/posting") == [
        "https://example.test/jobs/frontend",
        "https://jobs.ashbyhq.com/acme/123",
        "https://jobs.lever.co/acme/456",
    ]


def test_extract_job_page_links_uses_nested_jobposting_url():
    html = '''
    <script type="application/ld+json">
      {"@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "url": "/jobs/1"}]}
    </script>
    <a href="https://boards.greenhouse.io/acme/jobs/789">Apply</a>
    '''

    assert extract_job_page_links(html, "https://example.test/posting") == [
        "https://example.test/jobs/1",
        "https://boards.greenhouse.io/acme/jobs/789",
    ]


def test_enrich_job_sets_canonical_employer_page_tier_from_json_ld():
    job = Job(source="duckduckgo", title="", url="https://example.com/jobs/1")
    http = FakeHttp(_JOB_POSTING_HTML)

    enrich_job(job, http)

    assert job.content_confidence == CANONICAL_EMPLOYER_PAGE


def test_enrich_job_sets_source_detail_page_tier_from_body_fallback():
    job = Job(source="duckduckgo", title="", url="https://example.com/jobs/1")
    http = FakeHttp(_PLAIN_BODY_HTML)

    enrich_job(job, http)

    assert job.content_confidence == SOURCE_DETAIL_PAGE


def test_enrich_job_does_not_touch_confidence_when_description_already_present():
    job = Job(
        source="hackernews",
        title="x",
        description="already have text",
        content_confidence="aggregator_text",
        url="https://example.com/x",
    )
    http = FakeHttp(_JOB_POSTING_HTML)

    enrich_job(job, http)

    assert job.content_confidence == "aggregator_text"

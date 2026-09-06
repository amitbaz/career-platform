import pytest

import job_hunter.canonical as canonical
from job_hunter.availability import CLOSED, UNCHECKED, UNVERIFIED, VERIFIED
from job_hunter.canonical import (
    CanonicalResolver,
    apply_ats_identity,
    fetch_authoritative_description,
    parse_supported_ats_url,
)
from job_hunter.models import AtsReference, Job


class _Response:
    def __init__(self, *, url: str, text: str = "", status_code: int = 200):
        self.url = url
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.response


def test_parse_lever_reference():
    ref = parse_supported_ats_url("https://jobs.lever.co/acme/abc-123")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("lever", "acme", "abc-123")


def test_parse_ashby_reference():
    ref = parse_supported_ats_url("https://jobs.ashbyhq.com/acme/xyz")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("ashby", "acme", "xyz")


def test_parse_greenhouse_reference():
    ref = parse_supported_ats_url("https://boards.greenhouse.io/acme/jobs/456")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("greenhouse", "acme", "456")


def test_parse_greenhouse_reference_on_job_boards_host():
    # Modern Greenhouse boards serve their postings from job-boards.greenhouse.io
    # rather than boards.greenhouse.io; both hosts are live and a board's
    # absolute_url returns one or the other. Same board and job id either way.
    ref = parse_supported_ats_url("https://job-boards.greenhouse.io/acme/jobs/456")
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("greenhouse", "acme", "456")


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.eu.greenhouse.io/acme/jobs/456",
        "https://job-boards.eu.greenhouse.io/acme/jobs/456",
    ],
)
def test_parse_greenhouse_reference_on_eu_data_region_hosts(url):
    # Organizations on Greenhouse's EU data region get the same legacy/modern
    # host pair under `.eu.`, with the same path shape. These are not covered
    # by the substring consumers either, since "boards.greenhouse.io" is not a
    # substring of "job-boards.eu.greenhouse.io".
    ref = parse_supported_ats_url(url)
    assert ref is not None
    assert (ref.provider, ref.board, ref.job_id) == ("greenhouse", "acme", "456")


def test_apply_ats_identity_derives_from_url():
    job = Job(source="search:brave", title="Backend Engineer", url="https://jobs.lever.co/acme/abc-123")
    apply_ats_identity(job)
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("lever", "acme", "abc-123")


def test_apply_ats_identity_prefers_url_over_fallback():
    job = Job(source="lever", title="Backend Engineer", url="https://jobs.lever.co/acme/abc-123")
    apply_ats_identity(job, AtsReference(provider="lever", board="ACME", job_id="abc-123"))
    assert job.ats_board == "acme"


def test_apply_ats_identity_uses_fallback_when_url_is_not_parseable():
    # A Greenhouse board embedded on the employer's own domain: no recognisable
    # ATS host, so only the adapter's fallback can attribute the posting.
    job = Job(
        source="greenhouse",
        title="Backend Engineer",
        url="https://careers.acme.test/openings/456",
    )
    apply_ats_identity(job, AtsReference(provider="greenhouse", board="acme", job_id="456"))
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("greenhouse", "acme", "456")


def test_apply_ats_identity_keeps_existing_identity():
    job = Job(
        source="lever",
        title="Backend Engineer",
        url="https://jobs.lever.co/other/xyz",
        ats_provider="lever",
        ats_board="acme",
        ats_job_id="abc-123",
    )
    apply_ats_identity(job, AtsReference(provider="lever", board="fallback", job_id="999"))
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("lever", "acme", "abc-123")


def test_apply_ats_identity_is_a_noop_without_evidence():
    job = Job(source="hackernews", title="Backend Engineer", url="https://acme.test/careers/1")
    apply_ats_identity(job)
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == (None, None, None)


def test_apply_ats_identity_reads_canonical_url_first():
    job = Job(
        source="search:brave",
        title="Backend Engineer",
        url="https://acme.test/careers/1",
        canonical_url="https://jobs.ashbyhq.com/acme/xyz",
    )
    apply_ats_identity(job)
    assert (job.ats_provider, job.ats_board, job.ats_job_id) == ("ashby", "acme", "xyz")


def test_direct_ats_url_wins_without_search():
    http = _Http(_Response(url="https://unused.test"))
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    result = resolver.resolve(
        Job(
            source="yc",
            title="Frontend Engineer",
            company="Acme",
            url="https://jobs.lever.co/acme/abc",
        )
    )
    assert result is not None
    assert result.url == "https://jobs.lever.co/acme/abc"
    assert result.confidence == 1.0
    assert http.calls == 0


def test_redirect_to_supported_ats_is_accepted():
    http = _Http(_Response(url="https://jobs.lever.co/acme/abc"))
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )
    assert result is not None
    assert result.method == "redirect"
    assert result.confidence == 0.98


def test_targeted_search_rejects_wrong_company():
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Other",
                url="https://jobs.lever.co/other/abc",
            )
        ],
        watch_target=lambda company: None,
    )
    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )
    assert result is None


def test_watch_target_matches_ats_board_and_exact_normalized_title():
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend-Engineer",
                company="Acme",
                url="https://jobs.lever.co/acme/abc",
            )
        ],
        watch_target=lambda company: AtsReference("lever", "acme", None),
    )
    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )
    assert result is not None
    assert result.confidence == 0.92
    assert result.method == "watch_target"


def test_embedded_supported_ats_url_resolves_at_095():
    http = _Http(
        _Response(
            url="https://board.test/job",
            text='<a href="https://jobs.ashbyhq.com/acme/abc">Apply</a>',
        )
    )
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )

    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )

    assert result is not None
    assert result.url == "https://jobs.ashbyhq.com/acme/abc"
    assert result.confidence == 0.95
    assert result.method == "embedded"


def test_targeted_search_exact_match_resolves_at_090():
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Acme Inc.",
                location="Berlin, Germany",
                url="https://careers.acme.test/jobs/frontend",
            )
        ],
        watch_target=lambda company: None,
    )

    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            location="Berlin",
            url="https://board.test/job",
        )
    )

    assert result is not None
    assert result.url == "https://careers.acme.test/jobs/frontend"
    assert result.confidence == 0.90
    assert result.method == "targeted_search"


def test_targeted_search_prefers_supported_ats_url_over_generic_result():
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Acme",
                url="https://careers.acme.test/jobs/frontend",
            ),
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Acme",
                url="https://jobs.lever.co/acme/abc",
            ),
        ],
        watch_target=lambda company: None,
    )

    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )

    assert result is not None
    assert result.url == "https://jobs.lever.co/acme/abc"
    assert result.ats == AtsReference("lever", "acme", "abc")


def test_watch_target_failure_does_not_block_targeted_search():
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Acme",
                url="https://careers.acme.test/jobs/frontend",
            )
        ],
        watch_target=lambda company: (_ for _ in ()).throw(RuntimeError("watch down")),
    )

    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )

    assert result is not None
    assert result.confidence == 0.90
    assert result.method == "targeted_search"


def test_extraction_failure_does_not_block_targeted_search(monkeypatch):
    def fail_extraction(html: str, base_url: str) -> list[str]:
        raise RuntimeError("invalid page")

    monkeypatch.setattr(canonical, "extract_job_page_links", fail_extraction)
    http = _Http(_Response(url="https://board.test/job", text="<html></html>"))
    resolver = CanonicalResolver(
        http,
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Acme",
                url="https://jobs.ashbyhq.com/acme/abc",
            )
        ],
        watch_target=lambda company: None,
    )

    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )

    assert result is not None
    assert result.confidence == 0.90
    assert result.method == "targeted_search"


def test_source_fetch_failure_does_not_block_targeted_search():
    class _FailingHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("aggregator unavailable")

    resolver = CanonicalResolver(
        _FailingHttp(),
        search_candidates=lambda job: [
            Job(
                source="duckduckgo",
                title="Frontend Engineer",
                company="Acme",
                url="https://jobs.ashbyhq.com/acme/abc",
            )
        ],
        watch_target=lambda company: None,
    )

    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://unavailable.test/job",
        )
    )

    assert result is not None
    assert result.url == "https://jobs.ashbyhq.com/acme/abc"
    assert result.confidence == 0.90
    assert result.method == "targeted_search"


def test_reuses_already_fetched_page_html_without_refetching():
    http = _Http(_Response(url="https://unused.test"))
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    result = resolver.resolve(
        Job(
            source="wellfound",
            title="Frontend Engineer",
            company="Acme",
            url="https://wellfound.com/jobs/123-frontend-engineer",
            source_page_html='<a href="https://jobs.ashbyhq.com/acme/abc">Apply</a>',
        )
    )
    assert result is not None
    assert result.url == "https://jobs.ashbyhq.com/acme/abc"
    assert result.method == "embedded"
    assert http.calls == 0


def test_resolution_failure_is_non_blocking():
    class _FailingHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("network down")

    resolver = CanonicalResolver(
        _FailingHttp(), search_candidates=lambda job: [], watch_target=lambda company: None
    )
    result = resolver.resolve(
        Job(
            source="board",
            title="Frontend Engineer",
            company="Acme",
            url="https://board.test/job",
        )
    )
    assert result is None


def test_resolve_marks_availability_verified_on_fetched_page_with_no_signal():
    http = _Http(_Response(url="https://board.test/job", text="<html><body>Apply now</body></html>"))
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    job = Job(source="board", title="Frontend Engineer", company="Acme", url="https://board.test/job")

    resolver.resolve(job)

    assert job.availability == VERIFIED


def test_resolve_marks_availability_closed_on_explicit_closure_signal():
    http = _Http(
        _Response(
            url="https://board.test/job",
            text="<html><body>This job posting has expired</body></html>",
        )
    )
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    job = Job(source="board", title="Frontend Engineer", company="Acme", url="https://board.test/job")

    resolver.resolve(job)

    assert job.availability == CLOSED


def test_resolve_marks_availability_unverified_on_fetch_failure():
    class _FailingHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("network down")

    resolver = CanonicalResolver(
        _FailingHttp(), search_candidates=lambda job: [], watch_target=lambda company: None
    )
    job = Job(source="board", title="Frontend Engineer", company="Acme", url="https://board.test/job")

    resolver.resolve(job)

    assert job.availability == UNVERIFIED


def test_resolve_leaves_availability_unchecked_for_direct_ats_url():
    http = _Http(_Response(url="https://unused.test"))
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    job = Job(
        source="yc",
        title="Frontend Engineer",
        company="Acme",
        url="https://jobs.lever.co/acme/abc",
    )

    resolver.resolve(job)

    assert job.availability == UNCHECKED


def test_resolve_leaves_availability_unchecked_for_cached_page_html():
    http = _Http(_Response(url="https://unused.test"))
    resolver = CanonicalResolver(
        http, search_candidates=lambda job: [], watch_target=lambda company: None
    )
    job = Job(
        source="wellfound",
        title="Frontend Engineer",
        company="Acme",
        url="https://wellfound.com/jobs/123-frontend-engineer",
        source_page_html='<a href="https://jobs.ashbyhq.com/acme/abc">Apply</a>',
    )

    resolver.resolve(job)

    assert job.availability == UNCHECKED


def test_fetch_authoritative_description_dispatches_by_provider(monkeypatch):
    ats = AtsReference(provider="ashby", board="acme", job_id="abc")
    called = {}

    def fake_fetch(board, url, http):
        called["args"] = (board, url)
        return "authoritative text"

    monkeypatch.setattr("job_hunter.sources.ashby.fetch_description", fake_fetch)
    result = fetch_authoritative_description(ats, "https://jobs.ashbyhq.com/acme/abc", http=object())
    assert result == "authoritative text"
    assert called["args"] == ("acme", "https://jobs.ashbyhq.com/acme/abc")


def test_fetch_authoritative_description_returns_none_for_unsupported_provider():
    ats = AtsReference(provider="unknown_provider", board="acme", job_id="abc")
    assert fetch_authoritative_description(ats, "https://example.com/x", http=object()) is None


def test_fetch_authoritative_description_swallows_fetch_errors(monkeypatch):
    ats = AtsReference(provider="ashby", board="acme", job_id="abc")

    def raising_fetch(board, url, http):
        raise RuntimeError("network error")

    monkeypatch.setattr("job_hunter.sources.ashby.fetch_description", raising_fetch)
    assert fetch_authoritative_description(ats, "https://jobs.ashbyhq.com/acme/abc", http=object()) is None

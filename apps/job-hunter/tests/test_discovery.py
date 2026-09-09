import logging
import uuid
from datetime import datetime, timezone

import pytest

from job_hunter.canonical import CanonicalResolver
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS, PARTIAL_UNKNOWN
from job_hunter.discovery import (
    DISCOVERY_PHASES,
    DiscoveryStats,
    _dedupe,
    _format_phase_cost,
    _format_source_cost,
    _merge_fields,
    budget_applied,
    collect_candidates,
)
from job_hunter.search_backend import SearchResponse
from job_hunter.models import (
    AtsReference,
    CandidatePreferences,
    CanonicalResolution,
    Evaluation,
    Job,
    SearchPolicy,
)
from job_hunter.postgres_store import DryRunStore, PostingBatch
from tests.market_fixtures import make_market_policy


class FakeSource:
    def __init__(self, jobs):
        self._jobs = jobs

    def discover(self):
        return self._jobs


class FakeResolver:
    def __init__(self, resolution):
        self._resolution = resolution

    def resolve(self, job):
        return self._resolution


class FailFirstResolver:
    def __init__(self):
        self.calls = 0

    def resolve(self, job):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("resolver unavailable")
        return None


class BrokenSource:
    def discover(self):
        raise RuntimeError("source is down")


class NoOpHttp:
    def get(self, url, **kwargs):
        raise AssertionError(f"unexpected enrichment fetch for {url!r}")


class DryRunDiscoveryStore:
    """Only the read that one no-resolver discovery run is allowed to use."""

    def __init__(self):
        self.evaluation_job_ids = []

    def needs_evaluation_bulk(self, job_ids):
        self.evaluation_job_ids.extend(job_ids)
        return {job_id: True for job_id in job_ids}

    def __getattr__(self, name):
        raise AssertionError(f"unexpected wrapped-store access: {name}")


class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, html):
        self._html = html
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
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


@pytest.fixture
def policy():
    return SearchPolicy(
        target_titles=["senior product engineer"],
        positive_keywords=["react"],
        blocked_title_keywords=["junior"],
        salary_floor_eur=90000,
        thresholds={"package": 75, "possible": 65},
        max_jobs_per_run=25,
    )


@pytest.fixture
def market_policy():
    return make_market_policy()


def test_collect_candidates_continues_after_source_failure(store, policy):
    broken = BrokenSource()
    good = FakeSource(
        [
            Job(
                source="x",
                source_job_id="1",
                title="Senior Product Engineer",
                description="React TypeScript",
                remote=True,
            )
        ]
    )
    result = collect_candidates([broken, good], store, NoOpHttp(), policy)
    assert result.stats.raw == 1
    assert len(result.eligible) == 1


def test_collect_candidates_completes_with_dry_run_store_and_one_job(policy):
    job = Job(
        source="test",
        source_job_id="1",
        title="Senior Product Engineer",
        description="React TypeScript",
        remote=True,
    )
    backing_store = DryRunDiscoveryStore()

    result = collect_candidates(
        [FakeSource([job])],
        DryRunStore(backing_store),
        NoOpHttp(),
        policy,
    )

    assert len(result.eligible) == 1
    assert result.stats.newly_discovered == 0
    assert backing_store.evaluation_job_ids == [result.eligible[0][0]]


def test_collect_candidates_collapses_same_canonical_url(store, policy):
    jobs = [
        Job(
            source="duckduckgo",
            title="Senior Product Engineer",
            url="https://jobs.ashbyhq.com/acme/1?utm_source=x",
        ),
        Job(
            source="ashby",
            source_job_id="1",
            title="Senior Product Engineer",
            company="Acme",
            url="https://jobs.ashbyhq.com/acme/1",
            description="React TypeScript",
            remote=True,
        ),
    ]
    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)
    assert result.stats.unique == 1
    assert len(result.eligible) == 1
    assert result.eligible[0][1].company == "Acme"


def test_collect_candidates_derives_ats_identity_from_a_non_ats_source(store, policy):
    job = Job(
        source="search:brave",
        title="Senior Product Engineer",
        company="Acme",
        url="https://jobs.lever.co/acme/abc-123",
        description="React TypeScript",
        remote=True,
    )
    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert len(result.eligible) == 1
    eligible_job = result.eligible[0][1]
    assert (eligible_job.ats_provider, eligible_job.ats_board, eligible_job.ats_job_id) == (
        "lever",
        "acme",
        "abc-123",
    )


def test_collect_candidates_dedupes_on_derived_ats_identity(store, policy):
    # Same posting, different URL spellings: only the ATS identity derived
    # from each URL can join these two into one candidate.
    jobs = [
        Job(
            source="search:brave",
            title="Senior Product Engineer",
            company="Acme",
            url="https://jobs.lever.co/acme/abc-123/apply",
            description="React TypeScript",
            remote=True,
        ),
        Job(
            source="lever",
            source_job_id="abc-123",
            title="Senior Product Engineer Remote",
            company="Acme Inc",
            url="https://jobs.lever.co/acme/abc-123",
            description="React TypeScript",
            remote=True,
        ),
    ]
    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)

    assert result.stats.unique == 1


def test_collect_candidates_enriches_url_only_job(store, policy):
    job = Job(source="duckduckgo", title="", url="https://example.com/jobs/1")
    http = FakeHttp(_JOB_POSTING_HTML)

    result = collect_candidates([FakeSource([job])], store, http, policy)

    assert http.calls == ["https://example.com/jobs/1"]
    assert len(result.eligible) == 1
    enriched = result.eligible[0][1]
    assert enriched.title == "Senior Product Engineer"
    assert enriched.company == "Acme"
    assert enriched.remote is True
    assert "React" in enriched.description


def test_collect_candidates_does_not_reenrich_job_with_description(store, policy):
    job = Job(
        source="ashby",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/1",
        description="React TypeScript",
        remote=True,
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert len(result.eligible) == 1


def test_collect_candidates_rejects_explicitly_closed_posting_before_eligible(store, policy):
    job = Job(source="duckduckgo", title="", url="https://example.com/jobs/1")
    html = "<html><body><h1>This job posting has expired</h1></body></html>"
    http = FakeHttp(html)

    result = collect_candidates([FakeSource([job])], store, http, policy)

    assert result.eligible == []
    assert result.stats.availability_rejected == 1


def test_collect_candidates_keeps_unverified_posting_eligible(store, policy):
    class TimingOutHttp:
        def get(self, url, **kwargs):
            raise RuntimeError("timeout")

    job = Job(
        source="duckduckgo",
        title="Senior Product Engineer",
        url="https://example.com/jobs/1",
    )

    result = collect_candidates([FakeSource([job])], store, TimingOutHttp(), policy)

    assert len(result.eligible) == 1
    assert result.eligible[0][1].availability == "unverified"
    assert result.stats.availability_rejected == 0


def test_collect_candidates_rejects_closed_posting_found_during_canonical_resolution(
    store, policy
):
    job = Job(
        source="duckduckgo",
        title="Senior Product Engineer",
        description="Loves React",
        url="https://board.test/job",
    )

    class ClosureResolver:
        def resolve(self, job):
            job.availability = "closed"
            return None

    result = collect_candidates(
        [FakeSource([job])], store, NoOpHttp(), policy, resolver=ClosureResolver()
    )

    assert result.eligible == []
    assert result.stats.availability_rejected == 1
    assert result.stats.rejected_by_source.get("duckduckgo") == 1


def test_collect_candidates_counts_prefilter_rejections(store, policy):
    irrelevant_job = Job(
        source="x",
        source_job_id="2",
        title="Junior QA Tester",
        description="manual testing",
    )

    result = collect_candidates([FakeSource([irrelevant_job])], store, NoOpHttp(), policy)

    assert result.stats.prefilter_rejected == 1
    assert result.eligible == []
    assert result.rediscovered_job_ids == []


def test_collect_candidates_counts_jobs_the_run_actually_inserted(store, policy):
    jobs = [
        Job(
            source="devjobs",
            source_job_id=str(index),
            title="Senior Product Engineer",
            company=f"Acme {index}",
            description="React TypeScript remote role",
            remote=True,
        )
        for index in (1, 2)
    ]

    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)

    assert result.stats.raw == 2
    assert result.stats.unique == 2
    assert result.stats.newly_discovered == 2


def test_collect_candidates_reports_zero_when_every_job_is_already_known(store, policy):
    def posting():
        # A fresh instance per run: collect_candidates mutates the jobs it is
        # given, so reusing one would make the second run a different posting.
        return Job(
            source="devjobs",
            source_job_id="1",
            title="Senior Product Engineer",
            company="Acme",
            description="React TypeScript remote role",
            remote=True,
        )

    first = collect_candidates([FakeSource([posting()])], store, NoOpHttp(), policy)
    second = collect_candidates([FakeSource([posting()])], store, NoOpHttp(), policy)

    assert first.stats.newly_discovered == 1
    # The figure is reported, not omitted, when a run discovers nothing new.
    assert second.stats.raw == 1
    assert second.stats.newly_discovered == 0


def test_collect_candidates_counts_a_row_canonical_resolution_inserts(
    store, policy, monkeypatch
):
    # Resolution rewrites the job's URL, so the upsert that follows resolves
    # identity again. With no company there is no fallback identity to match
    # on, so it lands on no existing row and inserts a second one -- an insert
    # neither earlier persist saw.
    job = Job(
        source="hackernews",
        title="Senior Product Engineer",
        url="https://news.ycombinator.com/item?id=1",
        description="React TypeScript remote role",
        remote=True,
    )
    resolution = CanonicalResolution(
        url="https://jobs.ashbyhq.com/acme/abc",
        ats=AtsReference(provider="ashby", board="acme", job_id="abc"),
        confidence=1.0,
        method="test",
    )
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: "The real authoritative JD",
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert result.stats.raw == 1
    assert result.stats.newly_discovered == 2


def test_collect_candidates_counts_a_job_two_sources_found_once(store, policy):
    jobs = [
        Job(
            source="duckduckgo",
            title="Senior Product Engineer",
            url="https://jobs.ashbyhq.com/acme/1?utm_source=x",
        ),
        Job(
            source="ashby",
            source_job_id="1",
            title="Senior Product Engineer",
            company="Acme",
            url="https://jobs.ashbyhq.com/acme/1",
            description="React TypeScript",
            remote=True,
        ),
    ]

    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)

    # Two raw rows, one logical job: only the row the upsert inserted counts.
    assert result.stats.raw == 2
    assert result.stats.unique == 1
    assert result.stats.newly_discovered == 1


def test_collect_candidates_does_not_count_a_job_that_could_not_be_persisted(
    store, policy, monkeypatch
):
    jobs = [
        Job(
            source="devjobs",
            source_job_id=str(index),
            title="Senior Product Engineer",
            company=f"Acme {index}",
            description="React TypeScript remote role",
            remote=True,
        )
        for index in (1, 2)
    ]
    real_upsert = store.upsert_logical_jobs

    def dropping_first(batch, **kwargs):
        results = real_upsert(batch, **kwargs)
        return [None, *results[1:]] if results else results

    monkeypatch.setattr(store, "upsert_logical_jobs", dropping_first)

    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)

    assert result.stats.newly_discovered == 1


def test_collect_candidates_reports_the_postings_each_batch_discovered(
    store, policy, monkeypatch
):
    """The count of newly discovered postings is reported per batch (#182).

    Both persistence phases stage a batch, so the figure accumulates across
    them rather than being read off one of them -- the same rule
    `newly_discovered` already follows, and for the same reason: a phase
    nobody accumulated is a phase nobody would notice going quiet.
    """
    jobs = [
        Job(
            source="devjobs",
            source_job_id=str(index),
            title="Senior Product Engineer",
            company=f"Acme {index}",
            description="React TypeScript remote role",
            remote=True,
        )
        for index in (1, 2)
    ]
    staged: list[int] = []

    def merge(batch_jobs):
        staged.append(len(batch_jobs))
        # Deliberately no posting ids: a fabricated one would be written into
        # job_hunter_jobs.posting_id as a foreign key naming no posting. What
        # is under test here is the count, and an empty mapping is exactly
        # what a store with no direct connection returns.
        return PostingBatch(newly_discovered=len(batch_jobs))

    monkeypatch.setattr(store, "merge_posting_batch", merge)

    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)

    # Two raw listings, then the two unique jobs they dedupe to.
    assert staged == [2, 2]
    assert result.stats.postings_discovered == 4


def test_collect_candidates_reports_no_postings_without_a_direct_connection(
    store, policy
):
    """The `store` fixture holds no ingestion connection, which is the point.

    Every posting is then resolved inside its own job upsert, exactly as
    before #182, and the run still reports what it discovered for this user.
    """
    job = Job(
        source="devjobs",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        description="React TypeScript remote role",
        remote=True,
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert result.stats.postings_discovered == 0
    assert result.stats.newly_discovered == 1


def test_collect_candidates_counts_jobs_by_bounded_source_label(store, policy):
    eligible_job = Job(
        source="devjobs",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        description="React TypeScript remote role",
        remote=True,
    )
    rejected_jobs = [
        Job(
            source="devjobs",
            source_job_id=str(index),
            title="Junior QA Tester",
            description="manual testing",
        )
        for index in (2, 3)
    ]

    result = collect_candidates(
        [FakeSource([eligible_job, *rejected_jobs])], store, NoOpHttp(), policy
    )

    assert result.stats.unique_by_source == {"devjobs": 3}
    assert result.stats.eligible_by_source == {"devjobs": 1}
    assert result.stats.rejected_by_source == {"devjobs": 2}


def test_raw_jobs_get_content_confidence_from_source(store, policy):
    job = Job(
        source="ashby",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://jobs.ashbyhq.com/acme/1",
        description="React TypeScript",
        remote=True,
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert len(result.eligible) == 1
    assert result.eligible[0][1].content_confidence == OFFICIAL_ATS


def test_dedupe_prefers_higher_confidence_description_over_longer_weaker_one():
    weak = Job(
        source="hackernews",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-1",
        description="a" * 500,  # long but low-trust
        content_confidence=AGGREGATOR_TEXT,
    )
    strong = Job(
        source="ashby",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-1",
        description="short but authoritative JD",
        content_confidence=OFFICIAL_ATS,
    )
    merged, _ = _dedupe([weak, strong])
    assert len(merged) == 1
    assert merged[0].description == "short but authoritative JD"
    assert merged[0].content_confidence == OFFICIAL_ATS


def test_dedupe_still_fills_empty_description_from_weaker_source():
    empty = Job(
        source="targeted_search",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-2",
        description="",
        content_confidence=PARTIAL_UNKNOWN,
    )
    weak = Job(
        source="hackernews",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-2",
        description="a comment about the role",
        content_confidence=AGGREGATOR_TEXT,
    )
    merged, _ = _dedupe([empty, weak])
    assert merged[0].description == "a comment about the role"
    assert merged[0].content_confidence == AGGREGATOR_TEXT


def test_merge_fields_does_not_overwrite_real_description_with_empty_one():
    # richer has real description text but an unset content_confidence tier,
    # which ranks worse than any real tier including the weaker job's.
    richer = Job(
        source="ashby",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-3",
        description="authoritative JD text",
        content_confidence="",
    )
    weaker = Job(
        source="hackernews",
        title="Senior Engineer",
        company="Acme",
        location="Remote",
        url="https://example.com/acme-3",
        description="",
        content_confidence=PARTIAL_UNKNOWN,
    )
    merged = _merge_fields(richer, weaker)
    assert merged.description == "authoritative JD text"
    assert merged.content_confidence == ""


def test_collect_candidates_excludes_already_evaluated_unchanged_job(store, policy):
    job = Job(
        source="ashby",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        description="React TypeScript remote role",
        remote=True,
        content_confidence=OFFICIAL_ATS,
    )
    job_id, _is_new, _changed = store.upsert_job(job)
    store.save_evaluation(
        job_id,
        Evaluation(
            job_id=job_id,
            total_score=90,
            scores={},
            decision="high_priority",
            hard_blockers=[],
            strengths=[],
            gaps=[],
            salary_note="",
            location_note="",
            rationale="",
            model="gemini-test",
            status="ok",
            content_confidence=OFFICIAL_ATS,
        ),
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert result.eligible == []
    assert result.rediscovered_job_ids == [job_id]


def test_collect_candidates_logs_cross_source_dedupe_metrics_without_job_content(
    store, policy, caplog
):
    sources = [
        FakeSource(
            [
                Job(
                    source=source,
                    title="Senior Product Engineer",
                    company="Acme",
                    location="Berlin",
                    url=url,
                    description="PRIVATE_GMAIL_BODY React TypeScript",
                    remote=True,
                )
            ]
        )
        for source, url in (
            ("gmail:linkedin", "https://linkedin.test/1"),
            ("yc", "https://yc.test/2"),
            ("specialist", "https://specialist.test/3"),
        )
    ]
    resolver = FakeResolver(
        CanonicalResolution(
            url="https://jobs.lever.co/acme/abc",
            ats=AtsReference(provider="lever", board="acme", job_id="abc"),
            confidence=1.0,
            method="test",
        )
    )

    with caplog.at_level(logging.INFO):
        result = collect_candidates(
            sources,
            store,
            NoOpHttp(),
            policy,
            resolver=resolver,
        )

    assert result.stats.raw == 3
    assert result.stats.unique == 1
    # Resolution runs per surviving unique candidate, not per raw source copy.
    assert result.stats.canonical_resolved == 1
    assert result.stats.canonical_unresolved == 0
    assert result.stats.cross_source_duplicates == 1
    assert store.count_jobs() == 1
    job_id = result.eligible[0][0]
    provenance = store.list_job_sources(job_id)
    assert {row["source"] for row in provenance} == {
        "gmail:linkedin",
        "yc",
        "specialist",
    }
    assert {row["source_url"] for row in provenance} == {
        "https://linkedin.test/1",
        "https://yc.test/2",
        "https://specialist.test/3",
    }
    assert "gmail=1 specialist=1 yc=1" in caplog.text
    assert "canonical_resolved=1" in caplog.text
    assert "canonical_unresolved=0" in caplog.text
    assert "cross_source_duplicates=1" in caplog.text
    assert "PRIVATE_GMAIL_BODY" not in caplog.text


def test_collect_candidates_does_not_count_same_source_duplicates_as_cross_source(
    store, policy
):
    jobs = [
        Job(
            source="yc",
            title="Senior Product Engineer",
            company="Acme",
            location="Berlin",
            url="https://yc.test/jobs/acme",
            description="React TypeScript",
            remote=True,
        ),
        Job(
            source="yc",
            title="Senior Product Engineer",
            company="Acme",
            location="Berlin",
            url="https://yc.test/jobs/acme",
            description="React TypeScript",
            remote=True,
        ),
    ]

    result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)

    assert result.stats.raw == 2
    assert result.stats.unique == 1
    assert result.stats.cross_source_duplicates == 0


def test_collect_candidates_late_canonicalization_keeps_history_job_id(store, policy):
    legacy_url = "https://aggregator.test/jobs/acme-frontend"
    canonical_url = "https://jobs.lever.co/acme/abc"
    legacy = Job(
        source="aggregator",
        source_job_id="legacy-1",
        title="Senior Product Engineer",
        company="Acme GmbH",
        location="Berlin",
        url=legacy_url,
        description="React TypeScript",
        remote=True,
        content_confidence=AGGREGATOR_TEXT,
    )
    legacy_id, _, _ = store.upsert_job(legacy)
    store.save_evaluation(
        legacy_id,
        Evaluation(
            job_id=legacy_id,
            total_score=90,
            scores={},
            decision="high_priority",
            hard_blockers=[],
            strengths=[],
            gaps=[],
            salary_note="",
            location_note="",
            rationale="",
            model="gemini-test",
            status="ok",
            content_confidence=AGGREGATOR_TEXT,
        ),
    )
    canonical_id, _, _ = store.upsert_job(
        Job(
            source="lever",
            source_job_id="abc",
            title="senior product engineer",
            company="ACME",
            location="Berlin",
            url=canonical_url,
            canonical_url=canonical_url,
            ats_provider="lever",
            ats_board="acme",
            ats_job_id="abc",
        )
    )
    resolution = CanonicalResolution(
        url=canonical_url,
        ats=AtsReference(provider="lever", board="acme", job_id="abc"),
        confidence=1.0,
        method="test",
    )

    result = collect_candidates(
        [FakeSource([legacy])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert canonical_id != legacy_id
    assert store.count_jobs() == 1
    assert result.eligible == []
    assert result.rediscovered_job_ids == [legacy_id]
    assert store.get_evaluation(legacy_id) is not None
    assert store.get_job(legacy_id).url == canonical_url

    rerun = collect_candidates(
        [FakeSource([legacy])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert store.count_jobs() == 1
    assert rerun.rediscovered_job_ids == [legacy_id]


def test_collect_candidates_counts_unresolved_canonical_urls(store, policy):
    job = Job(
        source="yc",
        title="Senior Product Engineer",
        company="Acme",
        url="https://yc.test/unresolved",
        description="React TypeScript",
        remote=True,
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(None),
    )

    assert result.stats.canonical_resolved == 0
    assert result.stats.canonical_unresolved == 1
    assert result.eligible[0][1].url == "https://yc.test/unresolved"


def test_canonical_resolution_does_not_overwrite_adapter_ats_identity(store, policy):
    # A modern Greenhouse posting carries authoritative identity from its own
    # adapter, but its job-boards.greenhouse.io URL does not parse, so it still
    # reaches the resolver. The resolver's weaker embedded-link branch takes
    # the first ATS anchor on the page with no company or title check, so it
    # can point at an entirely different posting -- which must not be allowed
    # to relabel this job and merge it into that posting's stored row.
    job = Job(
        source="greenhouse",
        source_job_id="456",
        title="Senior Product Engineer",
        company="Acme",
        url="https://job-boards.greenhouse.io/acme/jobs/456",
        description="React TypeScript",
        remote=True,
        ats_provider="greenhouse",
        ats_board="acme",
        ats_job_id="456",
    )
    resolution = CanonicalResolution(
        url="https://jobs.lever.co/unrelated/other-posting",
        ats=AtsReference(provider="lever", board="unrelated", job_id="other-posting"),
        confidence=0.95,
        method="embedded",
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    eligible_job = result.eligible[0][1]
    assert (eligible_job.ats_provider, eligible_job.ats_board, eligible_job.ats_job_id) == (
        "greenhouse",
        "acme",
        "456",
    )


def test_canonical_resolution_still_attributes_an_unattributed_job(store, policy):
    job = Job(
        source="hackernews",
        title="Senior Product Engineer",
        company="Acme",
        url="https://news.ycombinator.com/item?id=1",
        description="React TypeScript",
        remote=True,
    )
    resolution = CanonicalResolution(
        url="https://jobs.ashbyhq.com/acme/abc",
        ats=AtsReference(provider="ashby", board="acme", job_id="abc"),
        confidence=1.0,
        method="test",
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    eligible_job = result.eligible[0][1]
    assert (eligible_job.ats_provider, eligible_job.ats_board, eligible_job.ats_job_id) == (
        "ashby",
        "acme",
        "abc",
    )


def test_canonical_resolution_upgrades_description_when_ats_found(
    store, policy, monkeypatch
):
    job = Job(
        source="hackernews",
        title="Senior Product Engineer",
        company="Acme",
        location="Berlin",
        url="https://news.ycombinator.com/item?id=1",
        description="original weak text React TypeScript",
        remote=True,
    )
    resolution = CanonicalResolution(
        url="https://jobs.ashbyhq.com/acme/abc",
        ats=AtsReference(provider="ashby", board="acme", job_id="abc"),
        confidence=1.0,
        method="test",
    )
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: "The real authoritative JD",
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert result.eligible[0][1].description == "The real authoritative JD"
    assert result.eligible[0][1].content_confidence == OFFICIAL_ATS


def test_canonical_resolution_skips_description_fetch_when_already_official_ats(
    store, policy, monkeypatch
):
    job = Job(
        source="ashby",
        title="Senior Product Engineer",
        company="Acme",
        location="Berlin",
        url="https://jobs.ashbyhq.com/acme/abc",
        description="already authoritative React TypeScript",
        remote=True,
    )
    resolution = CanonicalResolution(
        url="https://jobs.ashbyhq.com/acme/abc",
        ats=AtsReference(provider="ashby", board="acme", job_id="abc"),
        confidence=1.0,
        method="direct",
    )
    calls = []
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: calls.append(1) or "should not be used",
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert calls == []
    assert result.eligible[0][1].description == "already authoritative React TypeScript"


def test_canonical_resolution_keeps_existing_description_when_fetch_fails(
    store, policy, monkeypatch
):
    job = Job(
        source="hackernews",
        title="Senior Product Engineer",
        company="Acme",
        location="Berlin",
        url="https://news.ycombinator.com/item?id=1",
        description="original weak text React TypeScript",
        remote=True,
    )
    resolution = CanonicalResolution(
        url="https://jobs.ashbyhq.com/acme/abc",
        ats=AtsReference(provider="ashby", board="acme", job_id="abc"),
        confidence=1.0,
        method="test",
    )
    monkeypatch.setattr(
        "job_hunter.discovery.fetch_authoritative_description",
        lambda ats, url, http: None,
    )

    result = collect_candidates(
        [FakeSource([job])],
        store,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert result.eligible[0][1].description == "original weak text React TypeScript"


def test_resolver_exception_preserves_candidate_and_continues_collection(store, policy):
    first = Job(
        source="first",
        title="Senior Product Engineer",
        company="Acme",
        url="https://first.test/jobs/1",
        description="React TypeScript",
        remote=True,
    )
    second = Job(
        source="second",
        title="Senior Product Engineer",
        company="Beta",
        url="https://second.test/jobs/2",
        description="React TypeScript",
        remote=True,
    )

    result = collect_candidates(
        [FakeSource([first]), FakeSource([second])],
        store,
        NoOpHttp(),
        policy,
        resolver=FailFirstResolver(),
    )

    assert result.stats.raw == 2
    assert result.stats.unique == 2
    assert result.stats.canonical_unresolved == 2
    assert first.url == "https://first.test/jobs/1"
    assert {job.source for _job_id, job in result.eligible} == {"first", "second"}
    assert store.count_jobs() == 2
    # The source and the link are the advertisement's, so they are read off
    # the postings the caller holds membership rows for (#178).
    stored_urls = {
        row["posting"]["source"]: row["posting"]["url"]
        for row in store.client.select(
            "job_hunter_jobs",
            params={"select": "posting:job_hunter_postings!inner(source,url)"},
        )
    }
    assert stored_urls == {
        "first": "https://first.test/jobs/1",
        "second": "https://second.test/jobs/2",
    }


class CountingResolver:
    def __init__(self, resolution=None):
        self.calls = []
        self._resolution = resolution

    def resolve(self, job):
        self.calls.append(job.title)
        return self._resolution


class SourceCountingResolver:
    def __init__(self, resolution=None):
        self.calls = []
        self._resolution = resolution

    def resolve(self, job):
        self.calls.append(job.source)
        return self._resolution


def test_collect_candidates_skips_canonical_resolution_for_prefiltered_jobs(
    store, policy
):
    off_target = Job(
        source="arbeitnow",
        source_job_id="1",
        title="Fachärztin / Facharzt für Allgemeinmedizin",
        company="PraxisEins",
        url="https://arbeitnow.test/jobs/1",
        description="Praxis in Berlin",
        remote=True,
    )
    eligible_job = Job(
        source="arbeitnow",
        source_job_id="2",
        title="Senior Product Engineer",
        company="Acme",
        url="https://arbeitnow.test/jobs/2",
        description="React TypeScript",
        remote=True,
    )
    resolver = CountingResolver()

    result = collect_candidates(
        [FakeSource([off_target, eligible_job])],
        store,
        NoOpHttp(),
        policy,
        resolver=resolver,
    )

    assert resolver.calls == ["Senior Product Engineer"]
    assert result.stats.profession_rejected == 1
    assert len(result.eligible) == 1


def test_collect_candidates_caps_canonical_resolutions_per_run(store, policy):
    # Same title/description so `_title_fit`/`_strength_evidence` tie --
    # only `source_quality` (via `job.source`) differs, so rank order is
    # deterministic: remotive (7) > hackernews (5) > duckduckgo (3,
    # default -- ranking.py's 7-point tier already includes arbeitnow
    # alongside remotive, so duckduckgo is the genuinely-default source
    # here, not arbeitnow). Lowest-ranked sources are discovered FIRST on
    # purpose: under the old discovery-order behavior they'd win the
    # 2-slot shortlist; under rank-order bounding they must lose it.
    jobs = [
        Job(
            source=source,
            source_job_id=str(index),
            title="Senior Product Engineer",
            company=f"Acme {index}",
            url=f"https://{source}.test/jobs/{index}",
            description="React TypeScript",
            remote=True,
        )
        for index, source in enumerate(
            ["duckduckgo", "duckduckgo", "hackernews", "hackernews", "remotive"]
        )
    ]
    policy.max_canonical_resolutions_per_run = 2
    resolver = SourceCountingResolver()

    result = collect_candidates(
        [FakeSource(jobs)], store, NoOpHttp(), policy, resolver=resolver
    )

    # Only the top-2-ranked jobs (by source_quality) get the expensive
    # resolution attempt, regardless of discovery order: remotive (idx 4,
    # score 7) and the higher-tie-broken hackernews (idx 2, "Acme 2" <
    # "Acme 3" beats the other hackernews at idx 3). Pass 2 then resolves
    # in ORIGINAL discovery order among the shortlisted two, so idx 2
    # (hackernews) is called before idx 4 (remotive) -- NOT rank order.
    assert resolver.calls == ["hackernews", "remotive"]
    assert result.stats.canonical_budget_exhausted == 3
    assert len(result.eligible) == 5


def test_collect_candidates_does_not_charge_budget_for_already_ats_urls(
    store, policy
):
    already_ats = Job(
        source="arbeitnow",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://jobs.lever.co/acme/abc123",
        description="React TypeScript",
        remote=True,
    )
    needs_resolution = Job(
        source="arbeitnow",
        source_job_id="2",
        title="Senior Product Engineer",
        company="Beta",
        url="https://aggregator.test/jobs/2",
        description="React TypeScript",
        remote=True,
    )
    policy.max_canonical_resolutions_per_run = 1
    resolver = CanonicalResolver(NoOpHttp(), lambda job: [], lambda company: None)

    result = collect_candidates(
        [FakeSource([already_ats, needs_resolution])],
        store,
        NoOpHttp(),
        policy,
        resolver=resolver,
    )

    # The already-ATS job resolves for free (method="direct", no network call),
    # so it must not consume the single resolution slot: the second job still
    # gets its resolution attempt instead of being counted as budget-exhausted.
    assert result.stats.canonical_resolved == 1
    assert result.stats.canonical_unresolved == 1
    assert result.stats.canonical_budget_exhausted == 0


def test_collect_candidates_does_not_charge_budget_for_job_boards_greenhouse_urls(
    store, policy
):
    # A job-boards.greenhouse.io posting is already on an employer ATS URL, so
    # it must resolve locally like any other supported ATS host instead of
    # spending a shortlist slot and a network attempt rediscovering itself.
    already_ats = Job(
        source="greenhouse",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://job-boards.greenhouse.io/acme/jobs/456",
        description="React TypeScript",
        remote=True,
    )
    needs_resolution = Job(
        source="arbeitnow",
        source_job_id="2",
        title="Senior Product Engineer",
        company="Beta",
        url="https://aggregator.test/jobs/2",
        description="React TypeScript",
        remote=True,
    )
    policy.max_canonical_resolutions_per_run = 1
    resolver = CanonicalResolver(NoOpHttp(), lambda job: [], lambda company: None)

    result = collect_candidates(
        [FakeSource([already_ats, needs_resolution])],
        store,
        NoOpHttp(),
        policy,
        resolver=resolver,
    )

    assert result.stats.canonical_resolved == 1
    assert result.stats.canonical_unresolved == 1
    assert result.stats.canonical_network_attempts == 1
    assert result.stats.canonical_budget_exhausted == 0


def test_collect_candidates_still_canonicalizes_eligible_jobs(store, policy):
    job = Job(
        source="aggregator",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://aggregator.test/jobs/1",
        description="React TypeScript",
        remote=True,
    )
    resolver = CountingResolver(
        CanonicalResolution(
            url="https://jobs.lever.co/acme/abc",
            ats=AtsReference(provider="lever", board="acme", job_id="abc"),
            confidence=0.9,
            method="targeted_search",
        )
    )

    result = collect_candidates(
        [FakeSource([job])], store, NoOpHttp(), policy, resolver=resolver
    )

    assert result.stats.canonical_resolved == 1
    resolved = result.eligible[0][1]
    assert resolved.url == "https://jobs.lever.co/acme/abc"
    assert resolved.canonical_url == "https://jobs.lever.co/acme/abc"
    assert resolved.ats_provider == "lever"
    assert resolved.original_url == "https://aggregator.test/jobs/1"


def test_collect_candidates_emits_one_entry_when_canonicalization_merges_jobs(
    store, policy
):
    jobs = [
        Job(
            source="aggregator",
            source_job_id="1",
            title="Senior Product Engineer",
            company="Acme",
            location="Berlin",
            url="https://aggregator.test/jobs/1",
            description="React TypeScript",
            remote=True,
        ),
        Job(
            source="specialist",
            source_job_id="2",
            title="Senior Product Engineer",
            company="Acme GmbH",
            location="Remote",
            url="https://specialist.test/jobs/2",
            description="React TypeScript",
            remote=True,
        ),
    ]
    resolver = CountingResolver(
        CanonicalResolution(
            url="https://jobs.lever.co/acme/abc",
            ats=AtsReference(provider="lever", board="acme", job_id="abc"),
            confidence=0.9,
            method="targeted_search",
        )
    )

    result = collect_candidates(
        [FakeSource(jobs)], store, NoOpHttp(), policy, resolver=resolver
    )

    assert result.stats.unique == 2
    assert len(result.eligible) == 1


def test_collect_candidates_attributes_market_before_prefilter(store, market_policy):
    source = FakeSource([
        Job(
            source="fake",
            title="Senior Frontend Engineer",
            location="London - Hybrid",
            remote=False,
            description="React TypeScript. Visa sponsorship available.",
        )
    ])
    result = collect_candidates([source], store, NoOpHttp(), market_policy)
    assert len(result.eligible) == 1
    job_id, job = result.eligible[0]
    assert job.market_id == "london"
    assert store.get_job(job_id).market_id == "london"
    assert result.stats.eligible_by_market == {"london": 1}


def test_collect_candidates_prioritizes_resolution_by_preferences_not_discovery_order(
    store, policy
):
    policy.max_jobs_per_run = 1  # shortlist = 2
    # All three share source/URL host, so only the profile-driven rank score
    # (not source_quality) can explain who's shortlisted. Discovery order is
    # poor_fit, third_job (also a poor fit), strong_fit -- the strong fit is
    # discovered LAST, on purpose: discovery-order bounding would shortlist
    # poor_fit and third_job (the first two seen) and exclude strong_fit;
    # rank-based bounding must do the opposite and exclude third_job instead.
    poor_fit = Job(
        source="arbeitnow",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://arbeitnow.test/jobs/1",
        description="React TypeScript",
        remote=True,
    )
    third_job = Job(
        source="arbeitnow",
        source_job_id="3",
        title="Senior Product Engineer",
        company="Gamma",
        url="https://arbeitnow.test/jobs/3",
        description="React TypeScript",
        remote=True,
    )
    strong_fit = Job(
        source="arbeitnow",
        source_job_id="2",
        title="Staff Frontend Engineer",
        company="Beta",
        url="https://arbeitnow.test/jobs/2",
        description="React TypeScript design system ownership",
        remote=True,
    )
    preferences = CandidatePreferences(
        preferred_roles=["staff frontend engineer"],
        preferred_seniority=["staff"],
        must_have_signals=["design system"],
        nice_to_have_signals=[],
        preferred_locations=[],
        avoid_signals=[],
        summary="",
    )

    class JobIdCountingResolver:
        """Records job.source_job_id per resolve() call -- distinguishes
        which of the three same-source jobs was actually resolved, which
        SourceCountingResolver (keyed on job.source) cannot do here."""

        def __init__(self):
            self.calls = []

        def resolve(self, job):
            self.calls.append(job.source_job_id)
            return None

    resolver = JobIdCountingResolver()

    result = collect_candidates(
        [FakeSource([poor_fit, third_job, strong_fit])],
        store,
        NoOpHttp(),
        policy,
        resolver=resolver,
        preferences=preferences,
    )

    # profile_priority_score gives strong_fit (exact role/seniority match
    # plus the must-have "design system" signal) 73, and poor_fit/third_job
    # (identical except company name) 26 each, tied but broken by company
    # name ("Acme" < "Gamma") in poor_fit's favor. Shortlist = top 2 by
    # score: strong_fit and poor_fit. Pass 2 then resolves in ORIGINAL
    # discovery order among the shortlisted -- poor_fit (seen 1st), then
    # strong_fit (seen 3rd, last) -- skipping third_job (seen 2nd) entirely.
    assert resolver.calls == ["1", "2"]
    assert result.stats.canonical_network_attempts == 2
    assert result.stats.canonical_budget_exhausted == 1
    assert len(result.eligible) == 3


def test_collect_candidates_shortlists_diversity_floor_sources_for_resolution(
    store, policy
):
    # 8 remotive jobs + 2 hackernews jobs, all otherwise identical, so
    # source_quality (remotive=7 > hackernews=5, ranking.py) is the only
    # thing that separates their rank scores -- every remotive job outranks
    # every hackernews job. max_jobs_per_run=2 makes shortlist_limit = 4
    # (2 * _CANONICAL_SHORTLIST_MULTIPLIER). A flat top-4-by-rank slice
    # would pick 4 remotive jobs and shortlist zero hackernews jobs. But
    # select_diverse_candidates, with the default source_minimum_per_run=2,
    # guarantees hackernews (the only other source, with exactly 2 jobs) both
    # of its slots -- so both hackernews jobs get shortlisted alongside the
    # top 2 remotive jobs, matching what final selection's own diversity
    # floor would protect downstream.
    policy.max_jobs_per_run = 2  # shortlist_limit = 4

    remotive_companies = [
        "Aaa Corp", "Bbb Corp", "Ccc Corp", "Ddd Corp",
        "Eee Corp", "Fff Corp", "Ggg Corp", "Hhh Corp",
    ]
    remotive_jobs = [
        Job(
            source="remotive",
            source_job_id=f"r{index + 1}",
            title="Senior Product Engineer",
            company=company,
            url=f"https://remotive.test/jobs/r{index + 1}",
            description="React TypeScript",
            remote=True,
        )
        for index, company in enumerate(remotive_companies)
    ]
    hackernews_jobs = [
        Job(
            source="hackernews",
            source_job_id=job_id,
            title="Senior Product Engineer",
            company=company,
            url=f"https://hackernews.test/jobs/{job_id}",
            description="React TypeScript",
            remote=True,
        )
        for job_id, company in [("h1", "Zzz Inc"), ("h2", "Yyy Inc")]
    ]
    preferences = CandidatePreferences(
        preferred_roles=["senior product engineer"],
        preferred_seniority=["senior"],
        must_have_signals=[],
        nice_to_have_signals=[],
        preferred_locations=[],
        avoid_signals=[],
        summary="",
    )

    class JobIdCountingResolver:
        def __init__(self):
            self.calls = []

        def resolve(self, job):
            self.calls.append(job.source_job_id)
            return None

    resolver = JobIdCountingResolver()

    result = collect_candidates(
        [FakeSource([*remotive_jobs, *hackernews_jobs])],
        store,
        NoOpHttp(),
        policy,
        resolver=resolver,
        preferences=preferences,
    )

    # Top 2 remotive by rank (alphabetically-first companies, since score
    # ties within a source) plus both hackernews jobs, protected by the
    # diversity floor.
    assert set(resolver.calls) == {"r1", "r2", "h1", "h2"}
    assert result.stats.canonical_network_attempts == 4
    assert result.stats.canonical_budget_exhausted == 6
    assert len(result.eligible) == 10


def test_collect_candidates_rejects_onsite_israel_job(store, market_policy):
    source = FakeSource([
        Job(
            source="fake",
            title="Senior Product Engineer",
            location="Tel Aviv - onsite",
            remote=False,
            description="React TypeScript. Onsite role in our Tel Aviv office.",
        )
    ])

    result = collect_candidates([source], store, NoOpHttp(), market_policy)

    assert result.eligible == []
    assert result.stats.rejected_by_market == {"israel_remote": 1}


def test_collect_candidates_survives_unattributed_uncertainty_for_remote_job(
    store, market_policy
):
    source = FakeSource([
        Job(
            source="fake",
            title="Senior Product Engineer",
            location="",
            remote=True,
            description="React TypeScript. Fully remote role.",
        )
    ])

    result = collect_candidates([source], store, NoOpHttp(), market_policy)

    # No location evidence at all falls back to the first enabled market
    # rather than being dropped -- attribution uncertainty alone must never
    # reject a job.
    assert len(result.eligible) == 1
    job_id, job = result.eligible[0]
    assert job.market_id == "germany_eu"
    assert result.stats.eligible_by_market == {"germany_eu": 1}


def test_collect_candidates_teaches_ats_board_even_for_backend_only_role(store, policy):
    # job_hunter_ats_boards is shared and has no delete policy (#203), so
    # the board identifier is salted unique to this test run rather than
    # asserting store.count_ats_boards() -- a global count over every
    # board any test or concurrent worktree has ever registered.
    board = f"example-{uuid.uuid4().hex[:8]}"
    job = Job(
        source="feed",
        title="Backend Engineer",
        company="Example",
        url=f"https://jobs.ashbyhq.com/{board}/backend-1",
        description="Python backend services",
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert result.eligible == []
    due = store.list_due_ats_boards(datetime.now(timezone.utc))
    assert [e.board_identifier for e in due if e.board_identifier == board] == [board]
    assert result.stats.ats_boards_discovered == 1


def test_collect_candidates_teaches_ats_board_from_canonical_resolution(store, policy):
    board = f"acme-{uuid.uuid4().hex[:8]}"
    job = Job(
        source="aggregator",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://aggregator.test/jobs/1",
        description="React TypeScript",
        remote=True,
    )
    resolver = FakeResolver(
        CanonicalResolution(
            url=f"https://boards.greenhouse.io/{board}/jobs/123",
            ats=AtsReference(provider="greenhouse", board=board, job_id="123"),
            confidence=0.9,
            method="targeted_search",
        )
    )

    result = collect_candidates(
        [FakeSource([job])], store, NoOpHttp(), policy, resolver=resolver
    )

    assert len(result.eligible) == 1
    assert result.stats.ats_boards_discovered == 1
    due = store.list_due_ats_boards(datetime.now(timezone.utc))
    assert [
        (entry.provider, entry.board_identifier)
        for entry in due
        if entry.board_identifier == board
    ] == [("greenhouse", board)]


def test_collect_candidates_counts_one_eligible_per_canonical_duplicate_group_by_market(
    store, market_policy
):
    jobs = [
        Job(
            source="aggregator",
            source_job_id="1",
            title="Senior Product Engineer",
            company="Acme",
            location="London",
            url="https://aggregator.test/jobs/1",
            description="React TypeScript. Visa sponsorship available.",
            remote=False,
        ),
        Job(
            source="specialist",
            source_job_id="2",
            title="Senior Product Engineer",
            company="Acme GmbH",
            location="London - Hybrid",
            url="https://specialist.test/jobs/2",
            description="React TypeScript. Visa sponsorship available.",
            remote=False,
        ),
    ]
    resolver = CountingResolver(
        CanonicalResolution(
            url="https://jobs.lever.co/acme/abc",
            ats=AtsReference(provider="lever", board="acme", job_id="abc"),
            confidence=0.9,
            method="targeted_search",
        )
    )

    result = collect_candidates(
        [FakeSource(jobs)], store, NoOpHttp(), market_policy, resolver=resolver
    )

    assert result.stats.unique == 2
    assert len(result.eligible) == 1
    assert result.stats.eligible_by_market == {"london": 1}


def test_collect_candidates_bounds_expensive_resolution_below_eligible_count(
    store, policy
):
    policy.max_jobs_per_run = 5
    policy.max_canonical_resolutions_per_run = 80  # not the binding constraint
    jobs = [
        Job(
            source="arbeitnow",
            source_job_id=str(index),
            title="Senior Product Engineer",
            company=f"Acme {index}",
            url=f"https://arbeitnow.test/jobs/{index}",
            description="React TypeScript",
            remote=True,
        )
        for index in range(20)
    ]
    resolver = CountingResolver()

    result = collect_candidates(
        [FakeSource(jobs)], store, NoOpHttp(), policy, resolver=resolver
    )

    # Shortlist = max_jobs_per_run * 2 = 10, well below the 20 eligible jobs
    # and below the flat 80 ceiling -- this is the production regression
    # from issue #29 (eligible=55 with max_jobs_per_run=35 today).
    assert len(resolver.calls) == 10
    assert len(result.eligible) == 20
    assert result.stats.canonical_network_attempts == 10
    assert result.stats.canonical_budget_exhausted == 10


def test_collect_candidates_always_resolves_already_ats_urls_outside_shortlist(
    store, policy
):
    policy.max_jobs_per_run = 1  # shortlist = 2, far below 10 ATS jobs below
    jobs = [
        Job(
            source="arbeitnow",
            source_job_id=str(index),
            title="Senior Product Engineer",
            company=f"Acme {index}",
            url=f"https://jobs.lever.co/acme-{index}/abc",
            description="React TypeScript",
            remote=True,
        )
        for index in range(10)
    ]
    resolver = CountingResolver()

    result = collect_candidates(
        [FakeSource(jobs)], store, NoOpHttp(), policy, resolver=resolver
    )

    # Every already-ATS URL resolves for free regardless of shortlist size.
    assert len(resolver.calls) == 10
    assert result.stats.canonical_network_attempts == 0
    assert result.stats.canonical_budget_exhausted == 0


class CountingClient:
    """Delegates to a real SupabaseClient, counting requests by kind.

    The bug this suite guards against is a request count that grows with the
    job count. Asserting on returned data would not catch its return, so this
    counts calls instead -- specifically the underlying HTTP-shaped client
    calls (rpc/select/update/...), not the higher-level store methods, so a
    store method that quietly reverts to looping internally still shows up
    here.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[str] = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def recording(*args, **kwargs):
            label = args[0] if args else name
            self.calls.append(f"{name}:{label}")
            return attribute(*args, **kwargs)

        return recording


def test_collect_candidates_request_count_does_not_grow_with_job_count(
    supabase_client, policy
):
    """Twenty jobs must not cost twenty times what one job costs."""
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> int:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="test",
                source_job_id=f"count-{job_count}-{i}",
                url=f"https://example.test/count-{job_count}-{i}",
                company="Acme",
                title="Frontend Engineer",
                location="Remote",
                remote=True,
                description="A frontend engineering role working in React.",
            )
            for i in range(job_count)
        ]
        collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)
        return len(client.calls)

    one_job = run_with(1)
    twenty_jobs = run_with(20)

    # Batched, the marginal cost of 19 more jobs is zero extra round trips.
    # A per-job design would put this at roughly 20x.
    assert twenty_jobs <= one_job + 2, (
        f"{twenty_jobs} requests for 20 jobs vs {one_job} for 1 -- "
        "discovery persistence is scaling with the job count again"
    )


def test_collect_candidates_request_count_does_not_grow_with_rejected_job_count(
    supabase_client, policy
):
    """The prefilter-rejection path must stay batched too.

    The base request-count test above only exercises jobs that survive to
    `eligible`, so it never touches `set_job_statuses` -- the batched write
    for jobs rejected as closed or prefiltered. Every job here is rejected
    by the legacy remote-only hard blocker (`policy` carries no markets, so
    `job.remote is False` always trips it), forcing that path to run for
    all twenty jobs and proving it doesn't cost one request per job either.
    """
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> int:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="test",
                source_job_id=f"rejected-{job_count}-{i}",
                url=f"https://example.test/rejected-{job_count}-{i}",
                company=f"Acme {i}",
                title="Frontend Engineer",
                location="Office",
                remote=False,
                description="A frontend engineering role working in React.",
            )
            for i in range(job_count)
        ]
        result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)
        assert result.stats.availability_rejected == 0
        assert result.stats.prefilter_rejected == job_count, (
            "sanity check: this test's jobs must actually take the "
            "rejected/set_job_statuses path, not some other one"
        )
        return len(client.calls)

    one_job = run_with(1)
    twenty_jobs = run_with(20)

    assert twenty_jobs <= one_job + 2, (
        f"{twenty_jobs} requests for 20 rejected jobs vs {one_job} for 1 -- "
        "set_job_statuses is scaling with the rejected job count"
    )


class _PerJobAtsResolver:
    """Resolves every job to its own ATS posting, making no store calls itself."""

    def __init__(self):
        self.calls: list[str] = []

    def resolve(self, job):
        self.calls.append(job.source_job_id)
        board = f"acme-{job.source_job_id}"
        return CanonicalResolution(
            url=f"https://jobs.lever.co/{board}/abc",
            ats=AtsReference(provider="lever", board=board, job_id="abc"),
            confidence=0.9,
            method="already_ats",
        )


def test_collect_candidates_resolver_tail_costs_only_what_it_resolves(
    supabase_client, policy
):
    """Record the cost of the one path `collect_candidates` did NOT batch.

    The two request-count tests above pass no resolver, so the whole
    canonical-resolution tail (`discovery.py`'s final loop over
    `prefiltered`) is switched off in them -- while `pipeline.py` always
    passes one. That tail is still per-job, and this test exists so a reader
    of this file learns what it costs rather than inferring from the tests
    above that discovery is batched end to end.

    Every job here reaches the resolver on a supported ATS URL and is
    resolved to a *different* board, so identity really changes and the
    branch must persist it: `upsert_logical_job`, optionally
    `set_job_market`, and `needs_evaluation`, sequentially per job. Board
    registration is no longer among them -- #160 flushes it once for the run
    -- and neither is eligibility recording, which #151 took out of the loop.

    Its counterpart below pins the other half of #160: a job resolved to what
    it already was costs nothing here at all.

    The bounds are deliberately loose. This is a shape assertion, not a
    budget: it fails if the tail gets materially more expensive, and it also
    fails if someone batches it -- in which case the fix is to update this
    test and `apps/job-hunter/AGENTS.md`, which documents this path as
    per-job.
    """
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> int:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="arbeitnow",
                source_job_id=f"tail-{job_count}-{i}",
                title="Senior Product Engineer",
                company=f"Acme {job_count} {i}",
                url=f"https://jobs.lever.co/acme-tail-{job_count}-{i}/abc",
                description="React TypeScript remote role.",
                remote=True,
            )
            for i in range(job_count)
        ]
        resolver = _PerJobAtsResolver()
        result = collect_candidates(
            [FakeSource(jobs)], store, NoOpHttp(), policy, resolver=resolver
        )
        assert len(resolver.calls) == job_count, (
            "sanity check: every already-ATS job must reach the resolve "
            "branch, or this test is measuring the wrong path"
        )
        assert len(result.eligible) == job_count, (
            "sanity check: these jobs must become eligible, or this test is "
            "measuring a shorter path than the one it names"
        )
        return len(client.calls)

    one_job = run_with(1)
    five_jobs = run_with(5)
    marginal_per_job = (five_jobs - one_job) / 4

    assert marginal_per_job >= 1, (
        f"the resolver tail now costs {marginal_per_job} requests per extra "
        f"job that resolved to a new identity ({five_jobs} for 5 vs "
        f"{one_job} for 1). If it was batched, that is good news -- update "
        "this test and apps/job-hunter/AGENTS.md, which documents this path "
        "as per-job."
    )
    assert marginal_per_job <= 6, (
        f"the resolver tail now costs {marginal_per_job} requests per extra "
        f"job ({five_jobs} for 5 vs {one_job} for 1), up from the two #160 "
        "left it at. This path is sequential and ungated for already-ATS "
        "URLs, so growth here scales straight into the daily run's wall "
        "clock."
    )


def test_collect_candidates_resolver_tail_is_free_for_already_canonical_jobs(
    supabase_client, policy
):
    """#160: a job resolved to what it already was costs no request at all.

    Same measurement as the test above, with the one difference that decides
    the cost: these jobs resolve to their own URL. Everything the branch
    would write back -- the row, its market, its board, the re-read of
    `needs_evaluation` -- the batched phases already wrote earlier in the
    same run, so the marginal cost of another such job is zero.

    All five share one board so board registration stays constant between
    the two runs and cannot hide a per-job cost in the tail.
    """
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> int:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="lever",
                source_job_id=f"canonical-{job_count}-{i}",
                title="Senior Product Engineer",
                company=f"Acme {job_count} {i}",
                # A distinct board per invocation, not shared with the other
                # call to run_with(): job_hunter_ats_boards is shared and
                # has no delete policy (#203), and a per-user
                # job_hunter_ats_registry row is now only written on a
                # user's *first* sighting of a board -- reusing "acme"
                # across both invocations would make the second call's
                # count reflect an already-registered board rather than
                # the board-count invariant this test means to pin.
                url=f"https://jobs.lever.co/acme-{job_count}/canonical-{job_count}-{i}",
                description="React TypeScript remote role.",
                remote=True,
            )
            for i in range(job_count)
        ]
        result = collect_candidates(
            [FakeSource(jobs)], store, NoOpHttp(), policy, resolver=_direct_resolver()
        )
        assert result.stats.canonical_unchanged == job_count, (
            "sanity check: every job must reach the resolve branch and change "
            "nothing, or this test is measuring the wrong path"
        )
        assert len(result.eligible) == job_count, (
            "sanity check: these jobs must become eligible, or this test is "
            "measuring a shorter path than the one it names"
        )
        return len(client.calls)

    one_job = run_with(1)
    five_jobs = run_with(5)

    assert five_jobs == one_job, (
        f"five already-canonical jobs cost {five_jobs} requests against "
        f"{one_job} for one. The resolution phase is meant to scale with the "
        "jobs that resolved to something new, not with the jobs that reach it."
    )


def test_collect_candidates_ats_board_registration_does_not_grow_with_job_count(
    supabase_client, policy
):
    """Twenty jobs on one ATS board must cost the same registry traffic as one.

    `upsert_ats_boards` is documented as O(distinct boards), collapsing
    thousands of sightings to dozens of registrations -- this pins that:
    twenty jobs all pointing at the same (provider, board) must not cost
    any more ATS-registry requests than a single job on that board.

    Jobs are rejected by the legacy remote-only hard blocker (`remote=False`,
    no markets configured) so none reach `eligible`. That keeps this test
    isolated to `upsert_ats_boards` (design step 6's board-sighting write):
    an eligible ATS job also touches `job_hunter_ats_registry` when its
    eligibility is recorded, and mixing the two would leave neither
    pinned down. That write is batched too, and has its own test below.
    """
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> int:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="test",
                source_job_id=f"board-{job_count}-{i}",
                # See the note in the resolver-tail test above: a distinct
                # board per invocation, since #203 made a per-user
                # registry row a one-time write on first sighting.
                url=f"https://jobs.lever.co/acme-{job_count}/board-{job_count}-{i}",
                company="Acme",
                title=f"Frontend Engineer {i}",
                location="Office",
                remote=False,
                description="A frontend engineering role working in React.",
            )
            for i in range(job_count)
        ]
        result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)
        assert result.eligible == [], (
            "sanity check: these jobs must be rejected, not eligible, or the "
            "eligibility write would contaminate this test's count"
        )
        return sum(
            1 for call in client.calls if "job_hunter_ats_registry" in call
        )

    one_job = run_with(1)
    twenty_jobs = run_with(20)

    assert one_job > 0, (
        "sanity check: registering the single job's board must actually "
        "touch job_hunter_ats_registry, or this test proves nothing"
    )
    assert twenty_jobs == one_job, (
        f"{twenty_jobs} ATS-registry requests for 20 jobs on one board vs "
        f"{one_job} for 1 -- ATS board registration is scaling with the "
        "job count instead of the distinct board count"
    )


def test_collect_candidates_harvests_ats_board_with_observed_not_attributed_market(
    store, market_policy
):
    """ATS board harvesting must see the pre-attribution market hint.

    The job's query-time hint points at "london", but its description/
    location match "germany_eu" on stronger evidence, so full attribution
    (which runs after board harvesting is decided) overwrites job.market_id
    to "germany_eu". If harvesting were moved after attribution -- or if the
    batched rewrite plumbed the wrong hint through -- the registry would
    record "germany_eu" instead of the observed "london", silently losing
    the invariant this test guards.
    """
    job = Job(
        source="ashby",
        source_job_id="1",
        title="Senior Product Engineer",
        company="Acme",
        location="Berlin",
        url="https://jobs.lever.co/acme/observed-hint-job",
        description="React TypeScript remote role based in Berlin.",
        remote=True,
        market_hint="london",
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), market_policy)

    # Sanity check: full attribution did in fact override the hint, so this
    # test is actually exercising the divergence it claims to.
    assert result.eligible[0][1].market_id == "germany_eu"

    boards = store.list_due_ats_boards(datetime.now(timezone.utc))
    matching = [b for b in boards if b.board_identifier == "acme" and b.provider == "lever"]
    assert len(matching) == 1
    assert matching[0].market_hint == "london"


def test_collect_candidates_rediscovered_job_ids_membership_survives_batching(
    store, policy
):
    """rediscovered_job_ids must name exactly the already-evaluated jobs.

    Two jobs already carry a saved evaluation (so needs_evaluation is False
    for them) and two are new. Run together, batched evaluation-need lookups
    must still map each id back to its own job rather than, e.g., collapsing
    to all-or-nothing for the whole chunk.
    """
    already_evaluated = []
    for i in range(2):
        job = Job(
            source="ashby",
            source_job_id=f"seen-{i}",
            title="Senior Product Engineer",
            company=f"Seen {i}",
            description="React TypeScript remote role",
            remote=True,
            content_confidence=OFFICIAL_ATS,
        )
        job_id, _is_new, _changed = store.upsert_job(job)
        store.save_evaluation(
            job_id,
            Evaluation(
                job_id=job_id,
                total_score=90,
                scores={},
                decision="high_priority",
                hard_blockers=[],
                strengths=[],
                gaps=[],
                salary_note="",
                location_note="",
                rationale="",
                model="gemini-test",
                status="ok",
                content_confidence=OFFICIAL_ATS,
            ),
        )
        already_evaluated.append((job, job_id))

    new_jobs = [
        Job(
            source="ashby",
            source_job_id=f"new-{i}",
            title="Senior Product Engineer",
            company=f"New {i}",
            description="React TypeScript remote role",
            remote=True,
        )
        for i in range(2)
    ]

    result = collect_candidates(
        [FakeSource([job for job, _id in already_evaluated] + new_jobs)],
        store,
        NoOpHttp(),
        policy,
    )

    expected_rediscovered_ids = {job_id for _job, job_id in already_evaluated}
    assert set(result.rediscovered_job_ids) == expected_rediscovered_ids
    assert len(result.rediscovered_job_ids) == 2
    assert len(result.eligible) == 2


class FakeClock:
    """A monotonic clock the test drives instead of waiting on a real one.

    Reading it advances time by `tick`, standing in for the work discovery
    does between sources; a source consumes time explicitly by calling
    `advance`, so what lands in the statistics is exactly what the test made
    that source cost.
    """

    def __init__(self, tick: float = 0.0) -> None:
        self._now = 0.0
        self._tick = tick

    def __call__(self) -> float:
        self._now += self._tick
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class CountingHttp:
    """Stands in for the shared `HttpClient`, counting requests as it does.

    Given a clock, each request also costs `seconds_per_request` on it, which
    is how a test prices work that discovery does outside any source -- the
    enrichment fetch in particular, which no source's figure can ever cover.
    """

    def __init__(self, clock=None, seconds_per_request: float = 0.0) -> None:
        self.request_count = 0
        self._clock = clock
        self._seconds_per_request = seconds_per_request

    def _charge(self) -> None:
        self.request_count += 1
        if self._clock is not None and self._seconds_per_request:
            self._clock.advance(self._seconds_per_request)

    def get(self, url, **kwargs):
        self._charge()
        return FakeResponse("")

    def get_json(self, url, **kwargs):
        self._charge()
        return {}


class ClockedStore:
    """Wraps a real store, pricing chosen methods on the virtual clock.

    Discovery's persistence is where a run's time can hide without any
    source accounting for it, and the real store against a local stack is
    too fast to make that visible. This charges the named methods instead,
    so a test can assert which phase the cost landed in rather than how long
    a round trip happened to take.
    """

    def __init__(self, inner, clock, seconds_by_method: dict[str, float]) -> None:
        self._inner = inner
        self._clock = clock
        self._seconds_by_method = seconds_by_method

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        seconds = self._seconds_by_method.get(name)
        if seconds is None or not callable(attribute):
            return attribute

        def charged(*args, **kwargs):
            self._clock.advance(seconds)
            return attribute(*args, **kwargs)

        return charged


class CostlySource:
    """A fake source that consumes a chosen amount of time and requests.

    `FakeSource` above cannot express either, which is the whole subject
    here; everything else about it is that fixture with a price tag.
    """

    def __init__(
        self,
        jobs,
        clock,
        http,
        *,
        seconds=0.0,
        requests=0,
        label=None,
        raises=False,
    ) -> None:
        self._jobs = jobs
        self._clock = clock
        self._http = http
        self._seconds = seconds
        self._requests = requests
        self._raises = raises
        if label is not None:
            self.source_label = label

    def discover(self):
        self._clock.advance(self._seconds)
        for _ in range(self._requests):
            self._http.get("https://example.test/listing")
        if self._raises:
            raise RuntimeError("source is down")
        return self._jobs


def _costed_job(source: str, job_id: str) -> Job:
    return Job(
        source=source,
        source_job_id=job_id,
        title="Senior Product Engineer",
        description="React TypeScript",
        remote=True,
    )


class IncrementalCostlySource:
    """A costed source shaped like the real ones: it yields, job by job.

    `CostlySource` above returns a finished list, so it spends everything
    inside the `discover()` call. Every real source yields instead (issue
    #122), spending nothing there and everything during iteration -- so a
    cost measurement wrapped around the call alone would score all of them
    at zero while `CostlySource` kept passing. This double exists to make
    that difference visible to the suite.
    """

    def __init__(self, jobs, clock, http, *, seconds_per_job, label) -> None:
        self._jobs = jobs
        self._clock = clock
        self._http = http
        self._seconds_per_job = seconds_per_job
        self.source_label = label

    def discover(self):
        for job in self._jobs:
            self._clock.advance(self._seconds_per_job)
            self._http.get("https://example.test/listing")
            yield job


def test_collect_candidates_costs_a_yielding_source_by_what_it_spends(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    source = IncrementalCostlySource(
        [_costed_job("incremental", "1"), _costed_job("incremental", "2")],
        clock,
        http,
        seconds_per_job=5.0,
        label="incremental",
    )

    result = collect_candidates([source], store, http, policy, clock=clock)

    # Both jobs' work happened during iteration, after discover() returned.
    assert result.stats.elapsed_by_source == {"incremental": 10.0}
    assert result.stats.requests_by_source == {"incremental": 2}


def test_collect_candidates_records_each_source_elapsed_time_and_requests(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    cheap = CostlySource(
        [_costed_job("cheap", "1")], clock, http, seconds=2.0, requests=1, label="cheap"
    )
    expensive = CostlySource(
        [_costed_job("expensive", "2")],
        clock,
        http,
        seconds=30.0,
        requests=4,
        label="expensive",
    )

    result = collect_candidates([cheap, expensive], store, http, policy, clock=clock)

    assert result.stats.elapsed_by_source == {"cheap": 2.0, "expensive": 30.0}
    assert result.stats.requests_by_source == {"cheap": 1, "expensive": 4}


def test_collect_candidates_reports_the_cost_of_a_source_that_yields_nothing(
    store, policy
):
    clock = FakeClock()
    http = CountingHttp()
    barren = CostlySource([], clock, http, seconds=12.0, requests=3, label="barren")

    result = collect_candidates([barren], store, http, policy, clock=clock)

    assert result.stats.per_source == {}
    assert result.stats.elapsed_by_source == {"barren": 12.0}
    assert result.stats.requests_by_source == {"barren": 3}


def test_collect_candidates_reports_what_a_failing_source_spent_before_failing(
    store, policy
):
    clock = FakeClock()
    http = CountingHttp()
    broken = CostlySource(
        [], clock, http, seconds=5.0, requests=2, label="broken", raises=True
    )
    good = CostlySource(
        [_costed_job("good", "1")], clock, http, seconds=1.0, requests=1, label="good"
    )

    result = collect_candidates([broken, good], store, http, policy, clock=clock)

    assert result.stats.elapsed_by_source == {"broken": 5.0, "good": 1.0}
    assert result.stats.requests_by_source == {"broken": 2, "good": 1}
    # Failure isolation is unchanged: the broken source contributes nothing
    # and the source after it still runs.
    assert result.stats.raw == 1
    assert len(result.eligible) == 1


def test_collect_candidates_reports_total_time_beyond_the_sum_of_its_sources(
    store, policy
):
    # A non-zero tick stands in for the work discovery does around the
    # sources -- dedupe, persistence, prefilter -- which is exactly the time
    # the sum of the per-source figures cannot account for.
    clock = FakeClock(tick=1.0)
    http = CountingHttp()
    sources = [
        CostlySource(
            [_costed_job("a", "1")], clock, http, seconds=10.0, requests=1, label="a"
        ),
        CostlySource(
            [_costed_job("b", "2")], clock, http, seconds=20.0, requests=1, label="b"
        ),
    ]

    result = collect_candidates(sources, store, http, policy, clock=clock)

    stats = result.stats
    assert stats.elapsed_by_source["a"] >= 10.0
    assert stats.elapsed_by_source["b"] >= 20.0
    assert stats.total_elapsed_seconds > sum(stats.elapsed_by_source.values())


class SilentSearchBackend:
    """A search backend that answers every query with nothing."""

    name = "silent"

    def search(self, query):
        return SearchResponse(hits=[], backend=self.name)


def test_collect_candidates_measures_every_source_type_comparably(store, policy):
    """Every kind of source in use is measured, and none is merged into another.

    One instance of each shape the pipeline actually runs -- a feed, two
    boards of the same ATS provider, a targeted search, a watchlist source
    and staged email -- so that a source type cannot go silently unmeasured.
    """
    from job_hunter.sources.company_watch import CompanyWatchSource
    from job_hunter.sources.gmail_staged import GmailStagedSource
    from job_hunter.sources.learned_ats import LearnedAtsSource
    from job_hunter.sources.lever import LeverSource
    from job_hunter.sources.remotive import RemotiveSource
    from job_hunter.sources.targeted_search import TargetedSearchSource

    clock = FakeClock(tick=1.0)
    http = CountingHttp()
    sources = [
        RemotiveSource(http),
        LeverSource("acme", http),
        LeverSource("globex", http),
        TargetedSearchSource(SilentSearchBackend(), ["senior product engineer"]),
        CompanyWatchSource(store, http),
        LearnedAtsSource(store, http, limit=5, market_order=[]),
        GmailStagedSource(store),
    ]

    result = collect_candidates(sources, store, http, policy, clock=clock)

    assert set(result.stats.elapsed_by_source) == {
        "remotive",
        "lever:acme",
        "lever:globex",
        "targeted_search",
        "company_watch",
        "learned_ats",
        "gmail",
    }
    assert set(result.stats.requests_by_source) == set(result.stats.elapsed_by_source)
    # The three adapters that fetch over the shared client are the three that
    # report requests; the store-backed ones reach Postgres through their own
    # client and so report none of it here.
    assert result.stats.requests_by_source["remotive"] == 1
    assert result.stats.requests_by_source["lever:acme"] == 1
    assert result.stats.requests_by_source["lever:globex"] == 1


def test_collect_candidates_keeps_indistinguishable_sources_separate(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    first = CostlySource([], clock, http, seconds=3.0)
    second = CostlySource([], clock, http, seconds=7.0)

    result = collect_candidates([first, second], store, http, policy, clock=clock)

    assert sorted(result.stats.elapsed_by_source.values()) == [3.0, 7.0]


class BudgetedSource:
    """A source whose every unit of work costs a fixed amount of time.

    Shaped like the real ones: a unit is one fetch followed by one yield, so
    the only place the caller can stop it is between units. It records what
    it actually ran, which is how the tests tell "stopped between units" from
    "stopped mid-unit".
    """

    def __init__(self, jobs, clock, http, *, seconds_per_job, label) -> None:
        self._jobs = jobs
        self._clock = clock
        self._http = http
        self._seconds_per_job = seconds_per_job
        self.source_label = label
        self.units_started = 0
        self.units_completed = 0
        self.closed_cleanly = False

    def discover(self):
        try:
            for job in self._jobs:
                self.units_started += 1
                self._clock.advance(self._seconds_per_job)
                self._http.get("https://example.test/listing")
                self.units_completed += 1
                yield job
        except GeneratorExit:
            # Raised at the `yield`, never inside the fetch above: that is
            # the guarantee "cut off between units, never mid-request" means.
            self.closed_cleanly = True
            raise


def _budget_policy(policy, seconds):
    policy.source_time_budget_seconds = seconds
    return policy


def test_collect_candidates_cuts_off_a_source_that_exceeds_its_budget(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )

    result = collect_candidates(
        [source], store, http, _budget_policy(policy, 25.0), clock=clock
    )

    # Three units fit: the budget is only checked between them, so the unit
    # that crosses it still finishes rather than being torn up mid-request.
    assert source.units_started == 3
    assert result.stats.raw == 3
    assert result.stats.source_outcomes == {"slow": "cut_off"}


def test_a_cut_off_source_is_stopped_between_units_never_mid_request(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )

    collect_candidates([source], store, http, _budget_policy(policy, 25.0), clock=clock)

    assert source.units_started == source.units_completed
    assert source.closed_cleanly


def test_a_cut_off_sources_jobs_flow_through_the_pipeline(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )

    result = collect_candidates(
        [source], store, http, _budget_policy(policy, 25.0), clock=clock
    )

    # Partial results are kept, not discarded: a cut-off board contributes
    # what it harvested rather than nothing at all.
    assert len(result.eligible) == 3
    assert result.stats.eligible == 3
    assert result.stats.per_source == {"slow": 3}


def test_sources_after_a_cut_off_source_still_run_and_are_measured(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    slow = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )
    later = BudgetedSource(
        [_costed_job("later", "1")], clock, http, seconds_per_job=2.0, label="later"
    )

    result = collect_candidates(
        [slow, later], store, http, _budget_policy(policy, 25.0), clock=clock
    )

    assert later.units_completed == 1
    assert result.stats.elapsed_by_source == {"slow": 30.0, "later": 2.0}
    assert result.stats.requests_by_source == {"slow": 3, "later": 1}
    assert result.stats.source_outcomes == {"slow": "cut_off", "later": "completed"}


def test_the_cost_of_a_cut_off_source_covers_the_work_it_did(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )

    result = collect_candidates(
        [source], store, http, _budget_policy(policy, 25.0), clock=clock
    )

    assert result.stats.elapsed_by_source == {"slow": 30.0}
    assert result.stats.requests_by_source == {"slow": 3}


def test_a_source_that_raises_is_recorded_as_failed_not_cut_off(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    broken = CostlySource(
        [], clock, http, seconds=5.0, requests=2, label="broken", raises=True
    )
    good = CostlySource(
        [_costed_job("good", "1")], clock, http, seconds=1.0, requests=1, label="good"
    )

    result = collect_candidates(
        [broken, good], store, http, _budget_policy(policy, 600.0), clock=clock
    )

    assert result.stats.source_outcomes == {"broken": "failed", "good": "completed"}
    assert result.stats.raw == 1


def test_a_source_whose_single_unit_outlasts_the_budget_reports_it_unbounded(
    store, policy
):
    """One indivisible fetch cannot be bounded, and the report says so.

    The budget can only cut between units. A source that spends its whole
    cost inside one unit has no earlier boundary to be stopped at, so the
    existing request timeout is what bounds it, not this budget. That is
    derived from the source's longest unit rather than declared per adapter,
    so it stays true for an adapter nobody has annotated.
    """
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("indivisible", "1"), _costed_job("indivisible", "2")],
        clock,
        http,
        seconds_per_job=100.0,
        label="indivisible",
    )

    result = collect_candidates(
        [source], store, http, _budget_policy(policy, 25.0), clock=clock
    )

    stats = result.stats
    assert stats.longest_step_by_source == {"indivisible": 100.0}
    assert stats.source_outcomes == {"indivisible": "cut_off"}
    # The one unit that ran overshot the budget on its own, so the budget
    # never had a boundary early enough to help.
    assert budget_applied(stats, "indivisible", 25.0) is False
    assert stats.raw == 1


def test_a_divisible_source_cut_off_is_not_reported_unbounded(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )

    result = collect_candidates(
        [source], store, http, _budget_policy(policy, 25.0), clock=clock
    )

    assert budget_applied(result.stats, "slow", 25.0) is True


def test_source_cost_log_distinguishes_cut_off_failed_and_unbounded(store, policy):
    clock = FakeClock()
    http = CountingHttp()
    slow = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(5)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )
    indivisible = BudgetedSource(
        [_costed_job("indivisible", "1")],
        clock,
        http,
        seconds_per_job=100.0,
        label="indivisible",
    )
    broken = CostlySource(
        [], clock, http, seconds=1.0, requests=1, label="broken", raises=True
    )
    fine = BudgetedSource(
        [_costed_job("fine", "1")], clock, http, seconds_per_job=1.0, label="fine"
    )

    result = collect_candidates(
        [slow, indivisible, broken, fine],
        store,
        http,
        _budget_policy(policy, 25.0),
        clock=clock,
    )

    rendered = _format_source_cost(result.stats, 25.0)
    assert "slow=30.0s/3req(cut_off)" in rendered
    assert "indivisible=100.0s/1req(cut_off,budget_not_applicable)" in rendered
    assert "broken=1.0s/1req(failed)" in rendered
    assert "fine=1.0s/1req" in rendered
    assert "fine=1.0s/1req(" not in rendered


def test_a_generous_budget_leaves_a_run_unchanged(store, policy):
    """The default is loose on purpose: the first run must still measure reality."""
    clock = FakeClock()
    http = CountingHttp()
    jobs = [_costed_job("slow", str(index)) for index in range(5)]
    source = BudgetedSource(jobs, clock, http, seconds_per_job=10.0, label="slow")

    result = collect_candidates([source], store, http, policy, clock=clock)

    assert source.units_completed == 5
    assert result.stats.raw == 5
    assert result.stats.source_outcomes == {"slow": "completed"}


def test_a_non_positive_budget_means_no_budget(store, policy):
    """Zero is "unbounded", not "cut everything off immediately".

    Reading a missing or zeroed setting as a zero-second budget would
    silently stop every source at its first unit -- the most destructive
    possible interpretation of an unset value.
    """
    clock = FakeClock()
    http = CountingHttp()
    source = BudgetedSource(
        [_costed_job("slow", str(index)) for index in range(3)],
        clock,
        http,
        seconds_per_job=10.0,
        label="slow",
    )

    result = collect_candidates(
        [source], store, http, _budget_policy(policy, 0.0), clock=clock
    )

    assert result.stats.raw == 3
    assert result.stats.source_outcomes == {"slow": "completed"}


def _undescribed_job(source: str, job_id: str) -> Job:
    """A job the enrichment pass will fetch: it has a URL and no description."""
    return Job(
        source=source,
        source_job_id=job_id,
        title="Senior Product Engineer",
        description="",
        url=f"https://example.test/jobs/{job_id}",
        remote=True,
    )


def test_collect_candidates_attributes_every_phase_of_its_own_time(store, policy):
    """The phases partition the run: they sum to the total, with no remainder.

    This is the property that would have caught the 2671 unattributed
    seconds in run 34201733339 the moment they appeared.
    """
    # A non-zero tick prices the work discovery does between the phases, so
    # this cannot pass by every phase being free.
    clock = FakeClock(tick=0.5)
    http = CountingHttp()
    source = CostlySource(
        [_costed_job("a", "1")], clock, http, seconds=10.0, requests=1, label="a"
    )

    result = collect_candidates([source], store, http, policy, clock=clock)

    stats = result.stats
    assert set(stats.elapsed_by_phase) == set(DISCOVERY_PHASES)
    assert sum(stats.elapsed_by_phase.values()) == pytest.approx(
        stats.total_elapsed_seconds
    )
    assert stats.elapsed_by_phase["sources"] >= 10.0


def test_collect_candidates_reports_a_phase_that_cost_nothing(store, policy):
    """A phase absent from the figures is indistinguishable from an unmeasured one."""
    clock = FakeClock()
    http = CountingHttp()
    source = CostlySource(
        [_costed_job("a", "1")], clock, http, seconds=1.0, requests=0, label="a"
    )

    result = collect_candidates([source], store, http, policy, clock=clock)

    # No resolver was supplied, so canonical resolution genuinely cost
    # nothing -- and is still reported.
    assert result.stats.elapsed_by_phase["canonical"] == 0.0
    assert set(result.stats.elapsed_by_phase) == set(DISCOVERY_PHASES)


def test_collect_candidates_charges_enrichment_rather_than_a_source(store, policy):
    """The enrichment fetch belongs to no source, and now says so."""
    clock = FakeClock()
    http = CountingHttp(clock=clock, seconds_per_request=7.0)
    source = CostlySource(
        [_undescribed_job("a", "1")], clock, http, seconds=1.0, requests=0, label="a"
    )

    result = collect_candidates([source], store, http, policy, clock=clock)

    stats = result.stats
    # The source cost one second; the fetch discovery made on its behalf,
    # after the source had finished, cost seven.
    assert stats.elapsed_by_source == {"a": 1.0}
    assert stats.elapsed_by_phase["enrich"] >= 7.0
    assert sum(stats.elapsed_by_phase.values()) == pytest.approx(
        stats.total_elapsed_seconds
    )


def test_collect_candidates_charges_persistence_to_its_own_phase(store, policy):
    """The two bulk upserts are separate phases: raw copies, then unique jobs."""
    clock = FakeClock()
    http = CountingHttp()
    clocked = ClockedStore(store, clock, {"upsert_logical_jobs": 4.0})
    source = CostlySource(
        [_costed_job("a", "1")], clock, http, seconds=0.0, requests=0, label="a"
    )

    result = collect_candidates([source], clocked, http, policy, clock=clock)

    stats = result.stats
    assert stats.elapsed_by_phase["raw_persist"] >= 4.0
    assert stats.elapsed_by_phase["unique_persist"] >= 4.0
    assert stats.elapsed_by_source == {"a": 0.0}


def test_phase_figures_still_partition_the_run_when_a_source_fails(store, policy):
    """Failure isolation must not open a hole in the accounting."""
    clock = FakeClock(tick=0.5)
    http = CountingHttp()
    broken = CostlySource(
        [], clock, http, seconds=5.0, requests=1, label="broken", raises=True
    )
    good = CostlySource(
        [_costed_job("good", "1")], clock, http, seconds=1.0, requests=1, label="good"
    )

    result = collect_candidates([broken, good], store, http, policy, clock=clock)

    stats = result.stats
    assert sum(stats.elapsed_by_phase.values()) == pytest.approx(
        stats.total_elapsed_seconds
    )
    assert stats.elapsed_by_phase["sources"] >= 6.0


def test_format_phase_cost_renders_every_phase_dearest_first():
    stats = DiscoveryStats()
    stats.elapsed_by_phase = {phase: 0.0 for phase in DISCOVERY_PHASES}
    stats.elapsed_by_phase["enrich"] = 1500.0
    stats.elapsed_by_phase["eligible"] = 900.0

    rendered = _format_phase_cost(stats)

    assert rendered.startswith("enrich=1500.0s eligible=900.0s")
    # A free phase is still rendered: a missing one would read as unmeasured.
    assert "dedupe=0.0s" in rendered


def test_recording_eligibility_does_not_scale_with_the_eligible_jobs(
    supabase_client, policy
):
    """Twenty eligible jobs on one board cost no more registry writes than one.

    The defect this guards against is a request count that grows with the
    job count: the per-job version did a read then a write for every
    eligible job, roughly 2,700 serial round trips in run 34201733339 to
    increment a counter on a few dozen rows (#151). Counting the calls is
    the only way to see that -- the data it writes is identical either way.
    """
    from job_hunter.postgres_store import PostgresJobStore

    def run_with(job_count: int) -> tuple[int, int]:
        client = CountingClient(supabase_client)
        store = PostgresJobStore(client)
        jobs = [
            Job(
                source="arbeitnow",
                source_job_id=f"eligible-{job_count}-{index}",
                title="Senior Product Engineer",
                company=f"Acme {job_count} {index}",
                # See the note in the resolver-tail test above: a distinct
                # board per invocation, since #203 made a per-user
                # registry row a one-time write on first sighting.
                url=f"https://jobs.lever.co/acme-{job_count}/eligible-{job_count}-{index}",
                description="React TypeScript remote role.",
                remote=True,
            )
            for index in range(job_count)
        ]
        result = collect_candidates([FakeSource(jobs)], store, NoOpHttp(), policy)
        assert len(result.eligible) == job_count, (
            "sanity check: these jobs must become eligible, or nothing "
            "records their eligibility and this test proves nothing"
        )
        return sum(1 for call in client.calls if "job_hunter_ats_registry" in call), sum(
            1
            for call in client.calls
            if "job_hunter_record_ats_eligible_jobs" in call
        )

    one_registry_calls, one_flush = run_with(1)
    twenty_registry_calls, twenty_flush = run_with(20)

    assert twenty_registry_calls == one_registry_calls, (
        f"twenty eligible jobs on one board cost {twenty_registry_calls} "
        f"ATS-registry requests against {one_registry_calls} for a single "
        "job; the eligibility write is scaling with the job count again"
    )
    # One flush per run, whatever the run found.
    assert one_flush == 1
    assert twenty_flush == 1


def test_a_failure_recording_eligibility_does_not_cost_the_run(store, policy, caplog):
    """Learning the registry is opportunistic; the candidates are not."""

    class FailingEligibilityStore:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def record_ats_eligible_jobs(self, sightings, now):
            raise RuntimeError("registry is down")

    job = Job(
        source="arbeitnow",
        source_job_id="eligible-1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://jobs.lever.co/acme/eligible-1",
        description="React TypeScript remote role.",
        remote=True,
    )

    with caplog.at_level(logging.ERROR):
        result = collect_candidates(
            [FakeSource([job])], FailingEligibilityStore(store), NoOpHttp(), policy
        )

    assert len(result.eligible) == 1
    assert "recording 1 ATS-eligible job(s) failed" in caplog.text


class RecordingStore:
    """Wraps a store and records every method call a run makes through it.

    Since #182 the canonical-resolution tail defers its writes into the same
    batch methods the persistence phases use, so a name no longer names a
    path: what distinguishes them is how many times a method was called and
    with how many jobs. A tail that has gone back to writing per job shows up
    as a `upsert_logical_job` (singular) call, which is what the assertions
    below watch for.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        def recorder(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return attribute(*args, **kwargs)

        return recorder

    def count(self, name: str) -> int:
        return sum(1 for call_name, _args, _kwargs in self.calls if call_name == name)

    def registered_boards(self) -> list[tuple[str, str]]:
        """Every ATS board this run registered, batched and single-job paths alike.

        `upsert_ats_boards` collapses its sightings to one registration per
        distinct board before writing, so counting distinct boards per call is
        counting registrations.
        """
        boards: list[tuple[str, str]] = []
        for name, args, kwargs in self.calls:
            if name == "upsert_ats_boards":
                seen: list[tuple[str, str]] = []
                for provider, board, _company, _market_hint in args[0]:
                    if (provider, board) not in seen:
                        seen.append((provider, board))
                boards.extend(seen)
            elif name == "upsert_ats_board":
                boards.append((kwargs["provider"], kwargs["board_identifier"]))
        return boards


def _direct_resolver() -> CanonicalResolver:
    """A real resolver whose expensive halves would fail if they were reached."""

    def unreachable_search(job):
        raise AssertionError("a job already on an ATS URL must not be searched for")

    def unreachable_watch(company):
        raise AssertionError("a job already on an ATS URL must not consult the watchlist")

    return CanonicalResolver(NoOpHttp(), unreachable_search, unreachable_watch)


def test_canonical_resolution_writes_nothing_when_the_url_is_already_canonical(
    store, policy
):
    # The batched persistence phase already wrote this row, registered its
    # board and answered needs_evaluation for it minutes earlier in the same
    # run. Resolution decides nothing new, so it must restate none of that.
    job = Job(
        source="lever",
        source_job_id="abc",
        title="Senior Product Engineer",
        company="Acme",
        url="https://jobs.lever.co/acme/abc",
        description="React TypeScript remote role.",
        remote=True,
    )
    recording = RecordingStore(store)

    result = collect_candidates(
        [FakeSource([job])],
        recording,
        NoOpHttp(),
        policy,
        resolver=_direct_resolver(),
    )

    assert len(result.eligible) == 1
    assert result.stats.canonical_resolved == 1
    assert result.stats.canonical_unchanged == 1
    assert recording.count("upsert_logical_job") == 0
    assert recording.count("set_job_market") == 0
    assert recording.count("needs_evaluation") == 0
    # Registered once, by the batched phase, and not restated here -- so the
    # board counts once in the stat too.
    assert recording.registered_boards() == [("lever", "acme")]
    assert result.stats.ats_boards_discovered == 1


def test_canonical_resolution_registers_each_board_once_per_run(store, policy):
    # Two postings on one board, one of them reaching the resolver on a URL
    # that already parses: the board is registered by the batched phase and
    # never again.
    jobs = [
        Job(
            source="lever",
            source_job_id=job_id,
            title="Senior Product Engineer",
            company=company,
            url=f"https://jobs.lever.co/acme/{job_id}",
            description="React TypeScript remote role.",
            remote=True,
        )
        for job_id, company in (("abc", "Acme"), ("def", "Acme Labs"))
    ]
    recording = RecordingStore(store)

    collect_candidates(
        [FakeSource(jobs)],
        recording,
        NoOpHttp(),
        policy,
        resolver=_direct_resolver(),
    )

    assert recording.registered_boards() == [("lever", "acme")]


def test_canonical_resolution_persists_a_job_that_resolved_to_a_new_url(store, policy):
    # The other half of the branch: identity really did change here, so every
    # write the branch makes is still made.
    job = Job(
        source="hackernews",
        source_job_id="hn-1",
        title="Senior Product Engineer",
        company="Acme",
        url="https://news.ycombinator.com/item?id=1",
        description="React TypeScript remote role.",
        remote=True,
    )
    resolution = CanonicalResolution(
        url="https://jobs.lever.co/acme/abc",
        ats=AtsReference(provider="lever", board="acme", job_id="abc"),
        confidence=1.0,
        method="redirect",
    )
    recording = RecordingStore(store)

    result = collect_candidates(
        [FakeSource([job])],
        recording,
        NoOpHttp(),
        policy,
        resolver=FakeResolver(resolution),
    )

    assert result.stats.canonical_resolved == 1
    assert result.stats.canonical_unchanged == 0
    # Still persisted -- but in the batch the loop defers to, not one call per
    # job (#182). Nothing in the tail reaches a single-job write any more.
    assert recording.count("upsert_logical_job") == 0
    assert recording.count("needs_evaluation") == 0
    assert recording.count("set_job_market") == 0
    assert result.eligible[0][1].url == "https://jobs.lever.co/acme/abc"
    # The batched phase saw only the Hacker News URL, so this board is new.
    assert recording.registered_boards() == [("lever", "acme")]
    assert result.stats.ats_boards_discovered == 1


def test_canonical_resolution_writes_once_for_the_whole_run(store, policy):
    """The measurement #182 exists to move.

    In run 34289288702 the canonical phase cost 1170.8s for 1,221 resolutions
    against 81 network attempts: three PostgREST round trips per resolved job,
    made one job at a time. Every one of those writes is now deferred into one
    staged posting merge and three bulk calls for the whole run, so the phase's
    cost stops scaling with the number of jobs it resolved.
    """
    jobs = [
        Job(
            source="hackernews",
            source_job_id=f"hn-{index}",
            title="Senior Product Engineer",
            company=f"Acme {index}",
            url=f"https://news.ycombinator.com/item?id={index}",
            description="React TypeScript remote role.",
            remote=True,
        )
        for index in (1, 2, 3)
    ]

    def resolve(job):
        suffix = job.source_job_id.removeprefix("hn-")
        return CanonicalResolution(
            url=f"https://jobs.lever.co/acme{suffix}/abc",
            ats=AtsReference(provider="lever", board=f"acme{suffix}", job_id="abc"),
            confidence=1.0,
            method="redirect",
        )

    class PerJobResolver:
        def resolve(self, job):
            return resolve(job)

    recording = RecordingStore(store)

    result = collect_candidates(
        [FakeSource(jobs)], recording, NoOpHttp(), policy, resolver=PerJobResolver()
    )

    assert result.stats.canonical_resolved == 3
    assert len(result.eligible) == 3
    # Three resolutions, one batch: the two persistence phases plus the tail.
    assert recording.count("upsert_logical_jobs") == 3
    assert recording.count("merge_posting_batch") == 3
    assert recording.count("needs_evaluation_bulk") == 2
    assert recording.count("set_job_markets") == 2
    # And nothing per job.
    assert recording.count("upsert_logical_job") == 0
    assert recording.count("needs_evaluation") == 0
    assert recording.count("set_job_market") == 0


def test_canonical_resolution_registers_a_newly_resolved_board_once_for_two_jobs(
    store, policy
):
    jobs = [
        Job(
            source="hackernews",
            source_job_id=f"hn-{index}",
            title="Senior Product Engineer",
            company=f"Acme {index}",
            url=f"https://news.ycombinator.com/item?id={index}",
            description="React TypeScript remote role.",
            remote=True,
        )
        for index in (1, 2)
    ]

    class PerJobResolver:
        def resolve(self, job):
            job_id = job.source_job_id.split("-")[-1]
            return CanonicalResolution(
                url=f"https://jobs.lever.co/acme/{job_id}",
                ats=AtsReference(provider="lever", board="acme", job_id=job_id),
                confidence=1.0,
                method="redirect",
            )

    recording = RecordingStore(store)

    result = collect_candidates(
        [FakeSource(jobs)],
        recording,
        NoOpHttp(),
        policy,
        resolver=PerJobResolver(),
    )

    assert result.stats.canonical_resolved == 2
    assert recording.registered_boards() == [("lever", "acme")]
    assert result.stats.ats_boards_discovered == 1

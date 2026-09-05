import logging
from datetime import datetime, timezone

import pytest

from job_hunter.canonical import CanonicalResolver
from job_hunter.content_confidence import AGGREGATOR_TEXT, OFFICIAL_ATS, PARTIAL_UNKNOWN
from job_hunter.discovery import _dedupe, _merge_fields, collect_candidates
from job_hunter.models import (
    AtsReference,
    CandidatePreferences,
    CanonicalResolution,
    Evaluation,
    Job,
    SearchPolicy,
)
from job_hunter.store import JobStore
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
def store(tmp_path):
    return JobStore(tmp_path / "state.sqlite3")


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
    stored_urls = {
        row["source"]: row["url"]
        for row in store._conn.execute("SELECT source, url FROM jobs")
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
    job = Job(
        source="feed",
        title="Backend Engineer",
        company="Example",
        url="https://jobs.ashbyhq.com/example/backend-1",
        description="Python backend services",
    )

    result = collect_candidates([FakeSource([job])], store, NoOpHttp(), policy)

    assert result.eligible == []
    assert store.count_ats_boards() == 1
    assert result.stats.ats_boards_discovered == 1


def test_collect_candidates_teaches_ats_board_from_canonical_resolution(store, policy):
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
            url="https://boards.greenhouse.io/acme/jobs/123",
            ats=AtsReference(provider="greenhouse", board="acme", job_id="123"),
            confidence=0.9,
            method="targeted_search",
        )
    )

    result = collect_candidates(
        [FakeSource([job])], store, NoOpHttp(), policy, resolver=resolver
    )

    assert len(result.eligible) == 1
    assert store.count_ats_boards() == 1
    assert result.stats.ats_boards_discovered == 1
    due = store.list_due_ats_boards(datetime.now(timezone.utc))
    assert [(entry.provider, entry.board_identifier) for entry in due] == [
        ("greenhouse", "acme")
    ]


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

# Bound Canonical Resolution Work Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop spending canonical-resolution network work (page refetch + paid search) on every eligible candidate, and instead bound it to the highest-ranked candidates that could realistically survive this run's `max_jobs_per_run` selection — while preserving ATS learning, dedupe/persistence, watch promotion, and fail-open behavior, and adding telemetry to measure the change (issue #29).

**Architecture:** Split `collect_candidates()` into two passes over each run's unique jobs. Pass 1 keeps doing today's cheap, local-only work (dedupe, enrich, market attribution, prefilter) and collects every survivor into a `prefiltered` list instead of resolving canonically inline. Between the passes, rank that list with the existing `rank_jobs()` (now given the candidate's `preferences`, loaded earlier in `pipeline.py` than it is today) and take the top `min(max_canonical_resolutions_per_run, max_jobs_per_run * 2)` candidates that actually need network resolution as a shortlist. Pass 2 replays today's resolution-and-append logic per job, but only calls the resolver's expensive path for shortlisted jobs — an already-ATS-hosted URL still resolves for free and unconditionally, since that costs no network call. Separately, give `Job` an in-memory `source_page_html` field so a source adapter that already fetched the full page (Wellfound today) can hand it to `CanonicalResolver.resolve()` and skip a redundant refetch of the same URL.

**Tech Stack:** Python, pytest, existing `job_hunter` package (`discovery.py`, `canonical.py`, `pipeline.py`, `ranking.py`, `models.py`, `sources/wellfound.py`).

---

## Context for the implementing engineer

- Today, `collect_candidates()` (`src/job_hunter/discovery.py`) resolves canonical URLs for *every* job that survives prefiltering, before ranking or the `max_jobs_per_run` cap ever run (`pipeline.py` calls `rank_jobs`/`_select_candidates` only after `collect_candidates` returns). A flat per-run counter, `policy.max_canonical_resolutions_per_run` (default 80), is the only existing bound, and it's applied in raw discovery order, not by which jobs would actually be selected.
- `CanonicalResolver.resolve()` (`src/job_hunter/canonical.py`) has one free/local path (URL already matches a supported ATS host: `jobs.lever.co`, `jobs.ashbyhq.com`, `boards.greenhouse.io` — returns instantly, no network) and an expensive path for everything else (fetch `job.url`, then maybe one paid/public search call). The existing budget counter only ever gates the expensive path — already-ATS jobs always call `resolve()` regardless of budget, because it's free.
- Canonical resolution's success also sets `job.ats_provider`/`ats_board`/`ats_job_id` on the `Job` object itself (not just the ATS registry table). Those object fields — not the registry — are what `watchlist.promote_company()` (`src/job_hunter/watchlist.py:64-73`) and `store.upsert_logical_job`'s identity resolution (`src/job_hunter/store.py:651-659`) read. This is why the free/direct resolution path must keep running unconditionally for every already-ATS-hosted job, even ones excluded from the new shortlist — skipping it would silently degrade watch promotion and dedupe for jobs that cost nothing to resolve correctly.
- `rank_jobs()` (`src/job_hunter/ranking.py`) is pure in-memory scoring — it never needs canonical data to run. It reads `preferences: CandidatePreferences | None`, which today is loaded via `get_candidate_context(...)` in `pipeline.py` *after* `collect_candidates()` returns. This plan moves that load earlier so discovery can rank before resolving.
- `Job` (`src/job_hunter/models.py`) is a plain `@dataclass(slots=True)`. `store.py`'s persistence code reads named fields explicitly (`job.url`, `job.company`, etc.) rather than serializing the whole dataclass, so adding a new field is safe — nothing will try to persist it unless explicitly wired to.

---

### Task 1: Write failing tests for rank-based canonical-resolution bounding

**Files:**
- Modify: `tests/test_discovery.py`

**Step 1: Rewrite the existing flat-order test to prove rank order, not discovery order**

Replace the current `test_collect_candidates_caps_canonical_resolutions_per_run` (currently asserts only a *count*, and happens to pass under both old and new logic because of incidental alphabetical/company-name tie-breaking — this rewrite makes the rank-basis explicit and would fail under the current iteration-order implementation):

```python
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
```

(The two lowest-ranked `duckduckgo` jobs and the losing `hackernews` tie-break are the three excluded from the 2-slot shortlist. `CountingResolver`, the existing fixture, records `job.title` per call — useless here since every job shares the same title. Add a sibling fixture near it that records `job.source` instead, and use it for every test in this task that needs to know *which* job was resolved rather than just how many:

```python
class SourceCountingResolver:
    def __init__(self, resolution=None):
        self.calls = []
        self._resolution = resolution

    def resolve(self, job):
        self.calls.append(job.source)
        return self._resolution
```

**Step 2: Add a production-shaped bounding test**

```python
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
```

**Step 3: Add a test proving already-ATS jobs are never gated by the shortlist**

```python
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
```

**Step 4: Run the new tests and confirm they fail**

Run: `pytest tests/test_discovery.py -k "caps_canonical_resolutions_per_run or bounds_expensive_resolution or always_resolves_already_ats" -v`

Expected: the rewritten `test_collect_candidates_caps_canonical_resolutions_per_run` and the two new tests FAIL (current code resolves in raw iteration order and has no `canonical_network_attempts` stat field at all — this will error with `AttributeError: 'DiscoveryStats' object has no attribute 'canonical_network_attempts'`).

**Step 5: Commit**

```bash
git add tests/test_discovery.py
git commit -m "test: add failing tests for rank-based canonical-resolution bounding"
```

---

### Task 2: Implement rank-based canonical-resolution shortlisting in discovery.py

**Files:**
- Modify: `src/job_hunter/discovery.py`

**Step 1: Add the new stats fields, constant, and imports**

In `src/job_hunter/discovery.py`, update the imports (around line 8-13):

```python
from job_hunter.ats_registry import harvest_ats_board
from job_hunter.canonical import CanonicalResolver, parse_supported_ats_url
from job_hunter.fetching import enrich_job
from job_hunter.http import HttpClient
from job_hunter.job_identity import job_fallback_identity
from job_hunter.market_policy import attribute_market, market_by_id
from job_hunter.models import CandidatePreferences, Job, SearchPolicy
from job_hunter.normalize import canonicalize_url
from job_hunter.prefilter import prefilter_job
from job_hunter.ranking import rank_jobs
from job_hunter.store import JobStore
```

Add a module constant next to `_ATS_HOSTS` (around line 20):

```python
_ATS_HOSTS = ("jobs.ashbyhq.com", "jobs.lever.co", "boards.greenhouse.io")

# How many candidates beyond max_jobs_per_run to keep in the canonical
# resolution shortlist, to absorb resolution failures and any reordering
# that happens once resolved data (e.g. source_quality) feeds back into
# ranking. Fixed rather than configurable -- see docs/superpowers/plans/
# 2026-09-03-bound-canonical-resolution.md for why.
_CANONICAL_SHORTLIST_MULTIPLIER = 2
```

Add two fields to `DiscoveryStats` (around line 36):

```python
    canonical_budget_exhausted: int = 0
    canonical_network_attempts: int = 0
    canonical_shortlist_limit: int = 0
```

**Step 2: Restructure `collect_candidates` into two passes**

Replace everything from `eligible: list[tuple[int, Job]] = []` (current line 292) through the final `return DiscoveryResult(...)` (current line 419) with:

```python
    prefiltered: list[tuple[int, Job]] = []
    rediscovered_job_ids: list[int] = []

    for job in unique_jobs:
        observed_market_id = _cheap_market_attribution(job, policy)
        if _harvest_ats_board_safely(store, job, market_hint=observed_market_id):
            stats.ats_boards_discovered += 1
        if job.url and not job.description:
            enrich_job(job, http)

        job_id, _is_new, _description_changed = store.upsert_logical_job(job)

        job.market_id = attribute_market(job, policy.markets) if policy.markets else None
        _record_reattribution(stats, observed_market_id, job.market_id)
        if job.market_id:
            store.set_job_market(job_id, job.market_id)
        market_key = job.market_id or _UNATTRIBUTED
        source_label = metric_source_label(job.source)
        _bump(stats.unique_by_market, market_key)
        _bump(stats.unique_by_source, source_label)

        if not store.needs_evaluation(job_id):
            rediscovered_job_ids.append(job_id)
            continue

        market = market_by_id(policy, job.market_id) if job.market_id else None
        prefilter_result = prefilter_job(job, policy, market)
        if not prefilter_result.should_evaluate:
            if prefilter_result.reason_code == "off_target_profession":
                stats.profession_rejected += 1
            else:
                stats.prefilter_rejected += 1
            _bump(stats.rejected_by_market, market_key)
            _bump(stats.rejected_by_source, source_label)
            continue

        prefiltered.append((job_id, job))

    # Canonical resolution costs a page fetch plus a public search for jobs
    # not already on a supported ATS host, so that expensive path only runs
    # for the highest-ranked prefiltered candidates: the ones that could
    # realistically survive ranking/selection this run. The shortlist size
    # is whichever is smaller, the flat per-run ceiling or max_jobs_per_run
    # times the slack multiplier. Already-supported-ATS URLs resolve locally
    # at zero network cost, so they are never gated by this shortlist.
    shortlisted_ids: set[int] = set()
    if resolver is not None and prefiltered:
        shortlist_limit = min(
            policy.max_canonical_resolutions_per_run,
            max(0, policy.max_jobs_per_run) * _CANONICAL_SHORTLIST_MULTIPLIER,
        )
        stats.canonical_shortlist_limit = shortlist_limit
        ranked_prefiltered = rank_jobs(prefiltered, policy, preferences)
        needing_resolution = [
            ranked_job_id
            for ranked_job_id, ranked_job, _score in ranked_prefiltered
            if ranked_job.url and parse_supported_ats_url(ranked_job.url) is None
        ]
        shortlisted_ids = set(needing_resolution[:shortlist_limit])

    eligible: list[tuple[int, Job]] = []
    eligible_job_ids: set[int] = set()

    for job_id, job in prefiltered:
        if resolver is not None and job.url:
            already_ats_url = parse_supported_ats_url(job.url) is not None
            if not already_ats_url and job_id not in shortlisted_ids:
                stats.canonical_budget_exhausted += 1
            else:
                if not already_ats_url:
                    stats.canonical_network_attempts += 1
                try:
                    resolution = resolver.resolve(job)
                except Exception:
                    logger.exception(
                        "canonical resolution failed: source=%s",
                        metric_source_label(job.source),
                    )
                    resolution = None
                if resolution is None:
                    stats.canonical_unresolved += 1
                else:
                    stats.canonical_resolved += 1
                    job.canonical_url = resolution.url
                    job.url = resolution.url
                    if resolution.ats is not None:
                        job.ats_provider = resolution.ats.provider
                        job.ats_board = resolution.ats.board
                        job.ats_job_id = resolution.ats.job_id
                        if _harvest_ats_board_safely(store, job):
                            stats.ats_boards_discovered += 1
                    # Canonical resolution can surface stronger, directly
                    # observed location evidence than the query-time hint that
                    # seeded the earlier attribution above, so re-run it
                    # before the final append. Attribution uncertainty alone
                    # (i.e. falling back to the first enabled market) must
                    # never drop a job -- only prefilter/eligibility do that.
                    previous_market_id = job.market_id
                    job.market_id = (
                        attribute_market(job, policy.markets) if policy.markets else None
                    )
                    _record_reattribution(stats, previous_market_id, job.market_id)
                    # Late canonicalization may consolidate stored rows; use
                    # the store's history-preserving survivor ID downstream.
                    job_id, _is_new, _description_changed = store.upsert_logical_job(job)
                    if job.market_id:
                        store.set_job_market(job_id, job.market_id)
                    if not store.needs_evaluation(job_id):
                        rediscovered_job_ids.append(job_id)
                        continue

        if job_id in eligible_job_ids:
            continue
        eligible_job_ids.add(job_id)
        eligible.append((job_id, job))
        _bump(stats.eligible_by_market, job.market_id or _UNATTRIBUTED)
        _bump(stats.eligible_by_source, metric_source_label(job.source))
        if job.ats_provider and job.ats_board:
            try:
                store.record_ats_eligible_job(
                    job.ats_provider, job.ats_board, datetime.now(timezone.utc)
                )
            except Exception:
                logger.exception(
                    "recording ATS-eligible job failed: source=%s",
                    metric_source_label(job.source),
                )

    stats.eligible = len(eligible)
    logger.info(
        "discovery source contribution: %s canonical_resolved=%s "
        "canonical_unresolved=%s canonical_budget_exhausted=%s "
        "canonical_network_attempts=%s canonical_shortlist_limit=%s "
        "cross_source_duplicates=%s",
        _format_source_contribution(stats.per_source),
        stats.canonical_resolved,
        stats.canonical_unresolved,
        stats.canonical_budget_exhausted,
        stats.canonical_network_attempts,
        stats.canonical_shortlist_limit,
        stats.cross_source_duplicates,
    )

    return DiscoveryResult(
        eligible=eligible,
        rediscovered_job_ids=rediscovered_job_ids,
        stats=stats,
    )
```

**Step 3: Add the `preferences` parameter to `collect_candidates`**

Change the function signature (current lines 252-258):

```python
def collect_candidates(
    sources: list,
    store: JobStore,
    http: HttpClient,
    policy: SearchPolicy,
    resolver: CanonicalResolver | None = None,
    preferences: CandidatePreferences | None = None,
) -> DiscoveryResult:
```

**Step 4: Run the discovery test suite**

Run: `pytest tests/test_discovery.py -v`

Expected: ALL PASS, including the three tests from Task 1 and every pre-existing test in the file (per the trace in this plan's design notes, every other existing test's job count is small enough to sit entirely inside the default shortlist limit of `min(80, 25*2=50)`, so their outcomes are unaffected by the ordering change).

**Step 5: Commit**

```bash
git add src/job_hunter/discovery.py
git commit -m "feat: bound canonical resolution to a rank-based shortlist"
```

---

### Task 3: Thread candidate preferences into discovery from pipeline.py

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_discovery.py`
- Modify: `tests/test_pipeline.py`

**Step 1: Write a failing test proving discovery uses preferences to prioritize resolution**

Add to `tests/test_discovery.py` (needs `CandidatePreferences` imported — add it to the existing `from job_hunter.models import (...)` block):

```python
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
```

**Step 2: Run it and confirm it passes**

Run: `pytest tests/test_discovery.py::test_collect_candidates_prioritizes_resolution_by_preferences_not_discovery_order -v`

Expected: PASS — `collect_candidates` already accepts and threads `preferences` as of Task 2, so this test needs no new production code; it exists to pin the contract (preferences drive shortlist composition, not discovery order) with a regression test before touching `pipeline.py` in the next step.

**Step 3: Reorder `run_pipeline` so `preferences` is available before `collect_candidates` runs**

In `src/job_hunter/pipeline.py`, move the candidate-context block. Current order (~lines 675-706):

```python
    summary = RunSummary()
    digest_items: list[DigestItem] = []
    pdf_deliveries: list[tuple[int, Path, DigestItem]] = []
    out_dir = cover_letter_output_dir(settings)
    discovery = collect_candidates(
        sources,
        store,
        http,
        settings.policy,
        resolver=resolver,
    )
    search_planned, search_attempted, search_succeeded, search_results = (
        _aggregate_targeted_search_stats(base_sources)
    )
    watch_checks, watch_paused = _watch_check_outcomes(store, due_watches)
    try:
        candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, gemini, store)
    except (GeminiBudgetExceeded, GeminiQuotaPaused):
        candidate_context = None
        logger.warning(
            "candidate context load deferred by Gemini quota; evaluation and cover letters "
            "will be deferred this run"
        )
    else:
        logger.info(
            "profile extraction: source=%s error=%s",
            candidate_context.source,
            candidate_context.load_error or "none",
        )
    preferences = candidate_context.preferences if candidate_context is not None else None
    summary.skipped += discovery.stats.prefilter_rejected + discovery.stats.profession_rejected
    ranked = rank_jobs(discovery.eligible, settings.policy, preferences)
    selected = _select_candidates(ranked, settings.policy, preferences)
```

Replace with:

```python
    summary = RunSummary()
    digest_items: list[DigestItem] = []
    pdf_deliveries: list[tuple[int, Path, DigestItem]] = []
    out_dir = cover_letter_output_dir(settings)
    try:
        candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, gemini, store)
    except (GeminiBudgetExceeded, GeminiQuotaPaused):
        candidate_context = None
        logger.warning(
            "candidate context load deferred by Gemini quota; evaluation and cover letters "
            "will be deferred this run"
        )
    else:
        logger.info(
            "profile extraction: source=%s error=%s",
            candidate_context.source,
            candidate_context.load_error or "none",
        )
    preferences = candidate_context.preferences if candidate_context is not None else None
    discovery = collect_candidates(
        sources,
        store,
        http,
        settings.policy,
        resolver=resolver,
        preferences=preferences,
    )
    search_planned, search_attempted, search_succeeded, search_results = (
        _aggregate_targeted_search_stats(base_sources)
    )
    watch_checks, watch_paused = _watch_check_outcomes(store, due_watches)
    summary.skipped += discovery.stats.prefilter_rejected + discovery.stats.profession_rejected
    ranked = rank_jobs(discovery.eligible, settings.policy, preferences)
    selected = _select_candidates(ranked, settings.policy, preferences)
```

**Step 4: Add a pipeline-level regression test proving the wiring**

Add to `tests/test_pipeline.py`, near `test_pipeline_loads_candidate_context_once_without_logging_profile`:

```python
def test_pipeline_passes_loaded_preferences_into_discovery(settings, monkeypatch):
    job = _job()
    store = JobStore(settings.db_path)
    gemini = FakeGemini()
    telegram = FakeTelegram()
    captured = {}

    real_collect_candidates = job_hunter.pipeline.collect_candidates

    def capturing_collect_candidates(*args, **kwargs):
        captured["preferences"] = kwargs.get("preferences")
        return real_collect_candidates(*args, **kwargs)

    monkeypatch.setattr(
        "job_hunter.pipeline.collect_candidates", capturing_collect_candidates
    )

    run_pipeline(settings, sources=[FakeSource([job])], store=store, gemini=gemini, telegram=telegram)

    assert captured["preferences"] is not None
    assert captured["preferences"] == _candidate_context().preferences
```

Check the top of `tests/test_pipeline.py` for how `job_hunter.pipeline` is imported (it may already be imported as a module, or only specific names are imported via `from job_hunter.pipeline import run_pipeline`) — if only names are imported, add `import job_hunter.pipeline` alongside the existing imports so `job_hunter.pipeline.collect_candidates` resolves.

**Step 5: Run both test files**

Run: `pytest tests/test_discovery.py tests/test_pipeline.py -v`

Expected: ALL PASS.

**Step 6: Commit**

```bash
git add src/job_hunter/pipeline.py tests/test_discovery.py tests/test_pipeline.py
git commit -m "feat: rank canonical-resolution shortlist using loaded candidate preferences"
```

---

### Task 4: Reuse already-fetched page content in canonical resolution (Wellfound)

**Files:**
- Modify: `src/job_hunter/models.py`
- Modify: `src/job_hunter/canonical.py`
- Modify: `src/job_hunter/sources/wellfound.py`
- Modify: `tests/test_canonical.py`
- Modify: `tests/test_wellfound_source.py`

**Step 1: Write a failing test for canonical.py reusing pre-fetched HTML**

Add to `tests/test_canonical.py`:

```python
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
```

**Step 2: Run and confirm it fails**

Run: `pytest tests/test_canonical.py::test_reuses_already_fetched_page_html_without_refetching -v`

Expected: FAIL with `TypeError: Job.__init__() got an unexpected keyword argument 'source_page_html'`.

**Step 3: Add the field to `Job`**

In `src/job_hunter/models.py`, add to the `Job` dataclass (after `market_id`, around line 95):

```python
@dataclass(slots=True)
class Job:
    source: str
    title: str
    company: str = ""
    location: str = ""
    url: str = ""
    description: str = ""
    source_job_id: str | None = None
    remote: bool | None = None
    original_url: str = ""
    canonical_url: str = ""
    ats_provider: str | None = None
    ats_board: str | None = None
    ats_job_id: str | None = None
    market_hint: str | None = None
    market_id: str | None = None
    source_page_html: str = ""
```

**Step 4: Make `CanonicalResolver.resolve()` reuse it**

In `src/job_hunter/canonical.py`, replace the network-fetch block (current lines 63-71):

```python
        response_url = ""
        response_text = ""
        try:
            response = self._http.get(job.url)
            response.raise_for_status()
            response_url = response.url
            response_text = response.text
        except Exception:
            pass
```

with:

```python
        response_url = ""
        response_text = ""
        if job.source_page_html:
            # The source adapter already fetched this exact URL during
            # discovery (e.g. Wellfound); reuse its page content instead of
            # refetching. Redirect-based ATS detection doesn't apply here
            # since no HTTP round trip happened, but embedded-link detection
            # still works against the cached HTML below.
            response_url = job.url
            response_text = job.source_page_html
        else:
            try:
                response = self._http.get(job.url)
                response.raise_for_status()
                response_url = response.url
                response_text = response.text
            except Exception:
                pass
```

**Step 5: Run the canonical test suite**

Run: `pytest tests/test_canonical.py -v`

Expected: ALL PASS.

**Step 6: Wire Wellfound to set the field**

In `src/job_hunter/sources/wellfound.py`, update `_parse_detail` (current lines 146-177) to pass the fetched HTML through:

```python
def _parse_detail(html: str, job_id: str, detail_url: str, market_id: str) -> Job:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("title")
    raw_title = title_tag.get_text(" ", strip=True) if title_tag else ""
    raw_title = raw_title.removesuffix(_TITLE_SUFFIX).strip()

    company = ""
    location = ""
    if " at " in raw_title and " • " in raw_title:
        _, rest = raw_title.split(" at ", 1)
        company_part, location_part = rest.split(" • ", 1)
        company = company_part.strip()
        location = location_part.strip()

    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 is not None else raw_title.split(" at ", 1)[0].strip()

    body = soup.find("body")
    description = " ".join(body.get_text(" ", strip=True).split()) if body else ""

    return Job(
        source="wellfound",
        source_job_id=job_id,
        title=title,
        company=company,
        location=location,
        url=detail_url,
        description=description,
        remote=_parse_work_mode(description),
        market_hint=market_id,
        source_page_html=html,
    )
```

**Step 7: Add a Wellfound test proving the field is populated**

Add to `tests/test_wellfound_source.py`:

```python
def test_fetch_job_sets_source_page_html_for_canonical_reuse():
    http = FakeHttp()
    listings = [WellfoundListing(url="https://wellfound.com/role/l/x/y", market_id="germany_eu")]
    source = WellfoundSource(http, listings)

    jobs = source.discover()

    assert len(jobs) == 2
    assert all(job.source_page_html for job in jobs)
    assert "Frontend Engineer" in jobs[0].source_page_html
```

**Step 8: Run the full test suite for the touched files**

Run: `pytest tests/test_canonical.py tests/test_wellfound_source.py tests/test_discovery.py -v`

Expected: ALL PASS.

**Step 9: Commit**

```bash
git add src/job_hunter/models.py src/job_hunter/canonical.py src/job_hunter/sources/wellfound.py tests/test_canonical.py tests/test_wellfound_source.py
git commit -m "perf: reuse Wellfound's already-fetched page HTML in canonical resolution"
```

---

### Task 5: Extend production-facing telemetry

**Files:**
- Modify: `src/job_hunter/pipeline.py`
- Modify: `tests/test_pipeline.py`

**Step 1: Write a failing test for the enriched discovery summary log line**

Find the existing pipeline test that checks the `"discovery: raw=..."` log line (search `tests/test_pipeline.py` for `deferred_by_budget`) and read its surrounding context first — extend its assertions (or add a new small test if the existing one is narrowly scoped) to also check:

```python
    assert "canonical_network_attempts=" in caplog.text
```

**Step 2: Run it and confirm it fails**

Run: `pytest tests/test_pipeline.py -k deferred_by_budget -v` (adjust `-k` to match whatever the real test name is once you've located it)

Expected: FAIL — the substring isn't in today's log line.

**Step 3: Add the field to the log line**

In `src/job_hunter/pipeline.py`, update the log call around current lines 714-724:

```python
    logger.info(
        "discovery: raw=%s unique=%s prefilter_rejected=%s profession_rejected=%s "
        "eligible=%s selected=%s deferred_by_budget=%s canonical_network_attempts=%s sources=%s",
        discovery.stats.raw,
        discovery.stats.unique,
        discovery.stats.prefilter_rejected,
        discovery.stats.profession_rejected,
        discovery.stats.eligible,
        len(selected),
        deferred_by_budget,
        discovery.stats.canonical_network_attempts,
        len(eligible_source_counts),
    )
```

**Step 4: Run the full pipeline test suite**

Run: `pytest tests/test_pipeline.py -v`

Expected: ALL PASS.

**Step 5: Commit**

```bash
git add src/job_hunter/pipeline.py tests/test_pipeline.py
git commit -m "feat: log canonical network attempts in the per-run discovery summary"
```

---

### Task 6: Full verification pass

**Files:** none (verification only)

**Step 1: Run the entire test suite**

Run: `pytest -q`

Expected: all tests pass, no regressions anywhere in the suite (not just the files touched above — `store.py`, `watchlist.py`, and `ats_registry.py` behavior is exercised indirectly through `test_discovery.py` and `test_pipeline.py`, but re-run everything to be sure).

**Step 2: Re-check the issue's acceptance criteria against what was built**

Go through issue #29's acceptance-criteria checklist one by one and confirm each is met by the tests added in Tasks 1-5:
- Tests document required-before-selection vs. can-happen-after canonical outputs: Task 1/3 tests.
- Production-shaped scenario bounded: Task 1 Step 2 test.
- Network attempts demonstrably bounded: `canonical_network_attempts` stat + Task 1 tests.
- Redundant fetching removed where safe, covered by tests: Task 4.
- ATS harvesting/registry, dedupe/persistence, company-watch, fail-open all preserved: covered implicitly by the full existing `test_discovery.py`/`test_canonical.py`/`test_pipeline.py` suites passing unchanged in Task 2 Step 4 and Task 6 Step 1.
- Final URLs at least as reliable: unchanged resolution logic, only its trigger condition changed.
- Canonical telemetry measurable: Task 2 (`canonical_network_attempts`, `canonical_shortlist_limit`) + Task 5 (pipeline summary log).
- Full suite passes: Task 6 Step 1.

**Step 3: Update the issue**

Leave a comment on issue #29 (or note in the PR description) summarizing the mechanism and pointing at the new stats fields for anyone comparing before/after production logs.

No commit for this task — it's verification only.

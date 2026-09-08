from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from job_hunter import content_confidence
from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.availability import CLOSED
from job_hunter.ats_registry import ats_board_reference, harvest_ats_board
from job_hunter.canonical import (
    CanonicalResolver,
    apply_ats_identity,
    fetch_authoritative_description,
    parse_supported_ats_url,
)
from job_hunter.fetching import enrich_job
from job_hunter.http import HttpClient
from job_hunter.job_identity import job_fallback_identity
from job_hunter.market_policy import attribute_market, market_by_id
from job_hunter.models import CandidatePreferences, Job, SearchPolicy
from job_hunter.normalize import canonicalize_url
from job_hunter.prefilter import prefilter_job
from job_hunter.ranking import rank_jobs, select_diverse_candidates
from job_hunter.postgres_store import PostgresJobStore

logger = logging.getLogger(__name__)

# Matched as a substring of the URL rather than against the parsed hostname, so
# that an aggregator's redirect wrapper around an ATS posting still counts as
# the richer record when a dedup cluster picks its winner. See ats_hosts.
_ATS_HOSTS = tuple(SUPPORTED_ATS_HOSTS)

# How many candidates beyond max_jobs_per_run to keep in the canonical
# resolution shortlist, to absorb resolution failures and any reordering
# that happens once resolved data (e.g. source_quality) feeds back into
# ranking. Fixed rather than configurable -- see docs/superpowers/plans/
# 2026-09-03-bound-canonical-resolution.md for why.
_CANONICAL_SHORTLIST_MULTIPLIER = 2

# Bucket key for jobs that could not be tied to any configured market (or when
# no markets are configured at all). Kept distinct from real market ids so
# per-market observability never silently merges unattributed jobs into a
# real market's counters.
_UNATTRIBUTED = "unattributed"

# How a source's run ended, as recorded in `DiscoveryStats.source_outcomes`.
# "Cut off" and "failed" are kept apart deliberately: a slow source needs a
# schedule, a smaller unit of work, or to run alongside others, while a broken
# one needs fixing or removing, and one log line cannot prompt both.
SOURCE_COMPLETED = "completed"
SOURCE_CUT_OFF = "cut_off"
SOURCE_FAILED = "failed"

# The phases of `collect_candidates`, in the order they run. Each one can
# independently dominate a run, and the per-source figures above measure only
# the first of them -- in the run that prompted this (34201733339) the sources
# summed to 100.1s of a 2771.2s discovery, leaving 96% of it attributed to
# nothing at all.
PHASE_SOURCES = "sources"
PHASE_RAW_PERSIST = "raw_persist"
PHASE_DEDUPE = "dedupe"
PHASE_ENRICH = "enrich"
PHASE_UNIQUE_PERSIST = "unique_persist"
PHASE_PREFILTER = "prefilter"
PHASE_CANONICAL = "canonical"
PHASE_ELIGIBLE = "eligible"
# Everything not inside one of the phases above: the in-memory bookkeeping
# between them. It exists so the phases sum to the total exactly. A large
# `other` is itself a finding -- it means real work has grown somewhere no
# phase covers, which is the failure this instrumentation exists to prevent.
PHASE_OTHER = "other"

DISCOVERY_PHASES = (
    PHASE_SOURCES,
    PHASE_RAW_PERSIST,
    PHASE_DEDUPE,
    PHASE_ENRICH,
    PHASE_UNIQUE_PERSIST,
    PHASE_PREFILTER,
    PHASE_CANONICAL,
    PHASE_ELIGIBLE,
    PHASE_OTHER,
)


@dataclass(slots=True)
class DiscoveryStats:
    raw: int = 0
    unique: int = 0
    canonical_resolved: int = 0
    canonical_unresolved: int = 0
    cross_source_duplicates: int = 0
    canonical_budget_exhausted: int = 0
    canonical_network_attempts: int = 0
    canonical_shortlist_limit: int = 0
    prefilter_rejected: int = 0
    profession_rejected: int = 0
    availability_rejected: int = 0
    eligible: int = 0
    ats_boards_discovered: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    raw_by_market: dict[str, int] = field(default_factory=dict)
    unique_by_market: dict[str, int] = field(default_factory=dict)
    rejected_by_market: dict[str, int] = field(default_factory=dict)
    eligible_by_market: dict[str, int] = field(default_factory=dict)
    reattributed_by_market: dict[str, int] = field(default_factory=dict)
    unique_by_source: dict[str, int] = field(default_factory=dict)
    rejected_by_source: dict[str, int] = field(default_factory=dict)
    eligible_by_source: dict[str, int] = field(default_factory=dict)
    # The cost side of the yield figures above, keyed by source instance
    # (see `source_cost_label`) rather than by the source string its jobs
    # carry, so that a source producing nothing is still reported and two
    # boards of the same ATS provider stay distinguishable.
    elapsed_by_source: dict[str, float] = field(default_factory=dict)
    requests_by_source: dict[str, int] = field(default_factory=dict)
    # The longest single unit of work a source ran -- one feed page, one ATS
    # board, one watched company, one search query. It is the granularity at
    # which the time budget can cut, so comparing it against the budget says
    # whether the budget could have bounded that source at all. Derived from
    # what the source did rather than declared per adapter, so it stays true
    # for an adapter nobody annotated.
    longest_step_by_source: dict[str, float] = field(default_factory=dict)
    # How each source's run ended: SOURCE_COMPLETED, SOURCE_CUT_OFF or
    # SOURCE_FAILED, keyed like the cost figures above.
    source_outcomes: dict[str, str] = field(default_factory=dict)
    # Wall-clock time inside `collect_candidates`. Deliberately not derived
    # from `elapsed_by_source`: the difference between the two is the work
    # happening around the sources rather than inside them, and that gap is
    # the finding this instrumentation exists to expose.
    total_elapsed_seconds: float = 0.0
    # Where that total went, keyed by the phase names in `DISCOVERY_PHASES`.
    # Every phase is present even when it cost nothing, so a phase can never
    # go silently unmeasured, and the values sum to `total_elapsed_seconds`
    # so no remainder can hide between them. The `sources` phase is the whole
    # source loop and so exceeds the sum of `elapsed_by_source`: the
    # difference is the per-job work the loop body does around each source.
    elapsed_by_phase: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class DiscoveryResult:
    eligible: list[tuple[str, Job]]
    rediscovered_job_ids: list[str]
    stats: DiscoveryStats


def candidate_url_key(job: Job) -> str | None:
    url = job.canonical_url or job.url
    if not url:
        return None
    return canonicalize_url(url)


def candidate_identity_key(job: Job) -> str:
    return job_fallback_identity(job.company, job.title, job.location) or ""


def candidate_ats_key(job: Job) -> tuple[str, str, str] | None:
    if not job.ats_provider or not job.ats_board or not job.ats_job_id:
        return None
    return (job.ats_provider, job.ats_board, job.ats_job_id)


def _is_ats_url(job: Job) -> bool:
    url = (job.url or "").lower()
    return any(host in url for host in _ATS_HOSTS)


def _richness_key(job: Job) -> tuple[bool, int, bool, bool, bool]:
    tier_score = len(content_confidence.TIERS) - 1 - content_confidence.tier_rank(
        job.content_confidence
    )
    return (
        _is_ats_url(job),
        tier_score,
        bool(job.company),
        bool(job.location),
        job.remote is not None,
    )


def _merge_fields(richer: Job, weaker: Job) -> Job:
    if not richer.title and weaker.title:
        richer.title = weaker.title
    if not richer.company and weaker.company:
        richer.company = weaker.company
    if not richer.location and weaker.location:
        richer.location = weaker.location
    if weaker.description and content_confidence.tier_rank(
        weaker.content_confidence
    ) < content_confidence.tier_rank(richer.content_confidence):
        richer.description = weaker.description
        richer.content_confidence = weaker.content_confidence
    if richer.remote is None and weaker.remote is not None:
        richer.remote = weaker.remote
    if not richer.url and weaker.url:
        richer.url = weaker.url
    if not richer.source_job_id and weaker.source_job_id:
        richer.source_job_id = weaker.source_job_id
    if not richer.original_url and weaker.original_url:
        richer.original_url = weaker.original_url
    if not richer.canonical_url and weaker.canonical_url:
        richer.canonical_url = weaker.canonical_url
    if not richer.ats_provider and weaker.ats_provider:
        richer.ats_provider = weaker.ats_provider
    if not richer.ats_board and weaker.ats_board:
        richer.ats_board = weaker.ats_board
    if not richer.ats_job_id and weaker.ats_job_id:
        richer.ats_job_id = weaker.ats_job_id
    return richer


def _dedupe(jobs: list[Job]) -> tuple[list[Job], int]:
    """
    Collapse in-run duplicates. Two jobs are the same candidate when their
    canonical URLs, ATS identities, or exact source-independent fallback
    identities match. Union-find lets one record bridge multiple strong keys.
    """
    n = len(jobs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    url_seen: dict[str, int] = {}
    ats_seen: dict[tuple[str, str, str], int] = {}
    identity_seen: dict[str, int] = {}

    for i, job in enumerate(jobs):
        url_key = candidate_url_key(job)
        if url_key:
            if url_key in url_seen:
                union(i, url_seen[url_key])
            else:
                url_seen[url_key] = i

        ats_key = candidate_ats_key(job)
        if ats_key:
            if ats_key in ats_seen:
                union(i, ats_seen[ats_key])
            else:
                ats_seen[ats_key] = i

        identity_key = candidate_identity_key(job)
        if identity_key:
            if identity_key in identity_seen:
                union(i, identity_seen[identity_key])
            else:
                identity_seen[identity_key] = i

    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)

    ordered_roots = sorted(clusters, key=lambda root: min(clusters[root]))

    merged: list[Job] = []
    cross_source_duplicate_groups = 0
    for root in ordered_roots:
        cluster = clusters[root]
        if len({metric_source_label(jobs[index].source) for index in cluster}) > 1:
            cross_source_duplicate_groups += 1
        cluster_jobs = sorted(
            (jobs[i] for i in cluster), key=_richness_key, reverse=True
        )
        winner = cluster_jobs[0]
        for weaker in cluster_jobs[1:]:
            winner = _merge_fields(winner, weaker)
        merged.append(winner)

    return merged, cross_source_duplicate_groups


def source_cost_label(source) -> str:
    """Return a stable label identifying one source *instance* in run stats.

    Cost is recorded per source object, not per source string: several
    instances of the same adapter (one per configured ATS board) run as
    separate sources, and a source that yields no jobs has no string to be
    keyed by at all. Adapters declare a `source_label` -- ATS adapters
    include their board, so `lever:acme` and `lever:globex` stay apart --
    and anything without one (a test double, a source added without a
    label) falls back to its class name so it is still measured rather than
    silently missing.
    """
    declared = getattr(source, "source_label", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return type(source).__name__


def _distinct_label(source, taken: set[str]) -> str:
    """Return `source`'s cost label, suffixed if another source already took it.

    Merging two sources under one key would hide one of them, which is the
    failure mode this instrumentation exists to remove.
    """
    label = source_cost_label(source)
    if label not in taken:
        taken.add(label)
        return label
    suffix = 2
    while f"{label}#{suffix}" in taken:
        suffix += 1
    unique = f"{label}#{suffix}"
    taken.add(unique)
    return unique


def _client_request_count(http) -> int:
    """Return the shared client's request counter, or 0 if it has none.

    Test doubles and any client that does not count are reported as zero
    requests rather than crashing discovery over instrumentation.
    """
    count = getattr(http, "request_count", 0)
    return count if isinstance(count, int) else 0


def metric_source_label(source: str) -> str:
    """Return a bounded source label suitable for metrics and logs."""
    if source.startswith("gmail:"):
        return "gmail"
    return source


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _harvest_ats_board_safely(
    store: PostgresJobStore,
    job: Job,
    market_hint: str | None = None,
    denylist: frozenset[str] = frozenset(),
) -> bool:
    """Learn a job's ATS board without letting a registry write drop the job.

    Returns True only when the harvest created a new registry entry.
    """
    try:
        return harvest_ats_board(store, job, market_hint=market_hint, denylist=denylist)
    except Exception:
        logger.exception(
            "ATS board harvesting failed: source=%s", metric_source_label(job.source)
        )
        return False


def _ats_board_reference_safely(
    job: Job,
    market_hint: str | None = None,
    denylist: frozenset[str] = frozenset(),
) -> tuple[str, str, str, str] | None:
    """Extract a job's ATS board reference without letting a bad URL drop the run.

    `ats_board_reference` has no store call to fail, but URL parsing itself
    (`parse_supported_ats_url`, reached via `extract_ats_reference`) can raise
    on malformed input -- an unterminated IPv6 literal, for one. Design step 4
    runs for every job in the run, unguarded, so a single bad URL here would
    abort the whole daily run instead of being skipped, exactly the failure
    class this branch exists to remove.
    """
    try:
        return ats_board_reference(job, market_hint=market_hint, denylist=denylist)
    except Exception:
        logger.exception(
            "ATS board reference extraction failed: source=%s",
            metric_source_label(job.source),
        )
        return None


def _record_reattribution(
    stats: DiscoveryStats,
    before: str | None,
    after: str | None,
) -> None:
    if before == after:
        return
    _bump(stats.reattributed_by_market, after or _UNATTRIBUTED)


def _cheap_market_attribution(job: Job, policy: SearchPolicy) -> str | None:
    """Attribute a market as cheaply as possible for raw-stage observability.

    A query-time hint is free; otherwise fall back to the same evidence-based
    attribution used later, using whatever fields the raw job already carries
    (this never performs network I/O, so it's still cheap at this stage).
    """
    if job.market_hint:
        return job.market_hint
    if not policy.markets:
        return None
    return attribute_market(job, policy.markets)


class _PhaseLedger:
    """Charges every moment of a discovery pass to exactly one phase.

    A stopwatch rather than a set of independent timers: time is charged to
    whichever phase is currently on the stack, so the phases partition the
    run instead of sampling it. That is what lets the figures sum to the
    total -- the property that would have made the 2671 unattributed seconds
    in run 34138786671's successor visible the moment they appeared, instead
    of after two wrong conclusions drawn from the log lines around them.

    Phases nest: canonical resolution runs inside the eligible pass, and the
    inner phase is charged for its own time while the outer one keeps the
    rest. Reading the clock is the only side effect, so an injected clock
    drives this exactly as it drives the per-source figures.
    """

    def __init__(self, clock: Callable[[], float], started_at: float) -> None:
        self._clock = clock
        self._last = started_at
        self._stack = [PHASE_OTHER]
        self.elapsed: dict[str, float] = {phase: 0.0 for phase in DISCOVERY_PHASES}

    def _charge(self) -> float:
        now = self._clock()
        self.elapsed[self._stack[-1]] += max(0.0, now - self._last)
        self._last = now
        return now

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        self._charge()
        self._stack.append(name)
        try:
            yield
        finally:
            self._charge()
            self._stack.pop()

    def close(self) -> float:
        """Charge the remaining time and return the clock reading it ended on.

        The caller takes the total from this return value rather than reading
        the clock again, so that the phases sum to the total exactly instead
        of to the total minus one more read.
        """
        return self._charge()


def _format_seconds(seconds: float) -> str:
    return f"{seconds:.1f}s"


def _format_phase_cost(stats: DiscoveryStats) -> str:
    """Render where a discovery pass spent its time, dearest phase first.

    Only a rendering: the figures live on `DiscoveryStats.elapsed_by_phase`,
    so a test asserts them without reading a log line. Every phase is
    rendered, including the free ones -- a phase missing from the line would
    be indistinguishable from a phase nobody thought to measure.
    """
    if not stats.elapsed_by_phase:
        return "none"
    ordered = sorted(
        stats.elapsed_by_phase.items(), key=lambda item: item[1], reverse=True
    )
    return " ".join(
        f"{phase}={_format_seconds(elapsed)}" for phase, elapsed in ordered
    )


def budget_applied(
    stats: DiscoveryStats, label: str, budget_seconds: float | None
) -> bool:
    """Return whether the time budget could actually bound this source.

    The budget is checked between the units of work a source iterates over,
    so it can only bound a source whose units are smaller than it. A source
    whose work is one indivisible fetch -- or whose single unit outlasts the
    whole budget -- has no earlier boundary to be stopped at, and is bounded
    by the existing request timeout instead. Saying so in the report matters:
    a source that overran despite the budget and a source the budget could
    never have caught call for different responses.
    """
    if not budget_seconds or budget_seconds <= 0:
        return False
    return stats.longest_step_by_source.get(label, 0.0) <= budget_seconds


def _format_source_cost(stats: DiscoveryStats, budget_seconds: float | None) -> str:
    """Render each source's cost and how its run ended, dearest first.

    Only a rendering: the figures themselves live on `DiscoveryStats`, so a
    test can assert them without reading a log line. A source that finished
    within its budget carries no marker, so the markers that do appear are
    the ones worth reading.

    `budget_seconds` is required rather than defaulted: omitting it would make
    `budget_applied` answer False for everything and render every cut-off
    source as one the budget could not have caught, which is the opposite of
    what that marker is for.
    """
    if not stats.elapsed_by_source:
        return "none"
    ordered = sorted(
        stats.elapsed_by_source.items(), key=lambda item: item[1], reverse=True
    )
    rendered = []
    for label, elapsed in ordered:
        markers = []
        outcome = stats.source_outcomes.get(label)
        if outcome in (SOURCE_CUT_OFF, SOURCE_FAILED):
            markers.append(outcome)
        if outcome == SOURCE_CUT_OFF and not budget_applied(
            stats, label, budget_seconds
        ):
            markers.append("budget_not_applicable")
        suffix = f"({','.join(markers)})" if markers else ""
        requests = stats.requests_by_source.get(label, 0)
        rendered.append(
            f"{label}={_format_seconds(elapsed)}/{requests}req{suffix}"
        )
    return " ".join(rendered)


def _format_source_contribution(per_source: dict[str, int]) -> str:
    """Render compact source totals without including any job content."""
    metric_counts: dict[str, int] = {}
    for source, count in per_source.items():
        label = metric_source_label(source)
        metric_counts[label] = metric_counts.get(label, 0) + count
    if not metric_counts:
        return "none"
    return " ".join(
        f"{source}={count}" for source, count in sorted(metric_counts.items())
    )


def _iter_source_jobs(
    source,
    http: HttpClient,
    stats: DiscoveryStats,
    label: str,
    clock: Callable[[], float],
    budget_seconds: float | None = None,
) -> Iterator[Job]:
    """Yield one source's jobs, bounding it, costing it and isolating failures.

    Sources hand jobs back incrementally, so all three of this function's jobs
    -- failure isolation, cost accounting and the time budget -- have to
    follow the work into the iteration rather than sitting around the call
    that starts it.

    *Failures* can surface at any point during iteration. The iterator is
    therefore driven by hand: that keeps the `except` around the source's own
    work alone, so a bug in the caller's per-job handling still propagates
    instead of being mistaken for a dead source. Jobs the source produced
    before it failed have already been handed over and stay handed over --
    handled exactly as they are when the source succeeds. That is a
    deliberate reading of "unchanged" from issue #122: before sources yielded,
    a source raising part way contributed nothing, because its whole list was
    discarded. No source in the tree can reach that path today -- each either
    catches its own errors or raises on its first request, before yielding --
    so no run's job set changes.

    *Cost* is charged per `next()`, not per `discover()` call, which after
    #122 does no work at all and would have measured every source at zero.
    Only time spent inside the source counts: the caller's own per-job
    handling happens between `next()` calls and is deliberately excluded, so
    the figure still means "what this source cost". The running totals are
    written to `stats` after every step rather than once at the end, so a
    caller that abandons the drain part way still sees what the source spent
    before it stopped.

    *The budget* is checked between steps and never inside one, so a source is
    cut off between the units it already iterates over -- a feed page, an ATS
    board, a watched company, a search query -- and never mid-request: a
    half-parsed HTTP response is worse than a source that overruns slightly.
    Whatever it produced before that point has already been handed over and is
    kept, which is what makes the budget worth having: a large board that
    contributes some of its harvest beats one that contributes none. Sources
    scheduled after it run and are measured as usual.

    Two consequences are worth stating. A source whose own unit outlasts the
    whole budget cannot be bounded by it -- see `budget_applied`, which is how
    the report says so. And a source that overruns on its *last* unit is
    recorded as cut off rather than completed, because from outside there is
    no way to tell "nothing left" from "one more unit" without paying for that
    unit, and paying it is the thing the budget exists to refuse.
    """
    elapsed = 0.0
    requests = 0
    longest_step = 0.0

    def bracket(step):
        """Run one step of the source's own work, charging it to `label`."""
        nonlocal elapsed, requests, longest_step
        started_at = clock()
        requests_before = _client_request_count(http)
        try:
            return step()
        finally:
            step_elapsed = max(0.0, clock() - started_at)
            elapsed += step_elapsed
            longest_step = max(longest_step, step_elapsed)
            requests += max(0, _client_request_count(http) - requests_before)
            # Written every step, and always at least once, so a source that
            # yields nothing -- or raises immediately -- is still reported
            # rather than missing from the cost table.
            stats.elapsed_by_source[label] = elapsed
            stats.requests_by_source[label] = requests
            stats.longest_step_by_source[label] = longest_step

    def over_budget() -> bool:
        # A missing or non-positive budget means no budget. Reading it as a
        # zero-second one would stop every source at its first unit, which is
        # the most destructive possible reading of an unset setting.
        if not budget_seconds or budget_seconds <= 0:
            return False
        return elapsed >= budget_seconds

    try:
        jobs = bracket(lambda: iter(source.discover()))
    except Exception:
        # Failure isolation is unchanged -- the run continues with the next
        # source -- but what this one spent before failing is still reported,
        # since a source that burns the run and then raises is exactly what
        # the cost figures exist to expose.
        logger.exception("source discovery failed: %r", source)
        stats.source_outcomes[label] = SOURCE_FAILED
        return

    try:
        while True:
            if over_budget():
                stats.source_outcomes[label] = SOURCE_CUT_OFF
                logger.warning(
                    "source cut off by its time budget: source=%s "
                    "elapsed=%s budget=%s longest_step=%s budget_applied=%s",
                    label,
                    _format_seconds(elapsed),
                    _format_seconds(budget_seconds),
                    _format_seconds(longest_step),
                    budget_applied(stats, label, budget_seconds),
                )
                return
            try:
                job = bracket(lambda: next(jobs))
            except StopIteration:
                stats.source_outcomes[label] = SOURCE_COMPLETED
                return
            except Exception:
                logger.exception("source discovery failed: %r", source)
                stats.source_outcomes[label] = SOURCE_FAILED
                return
            yield job
    finally:
        # Closing a generator raises GeneratorExit at whichever `yield` it is
        # parked on, which is by definition a boundary between its units of
        # work -- so a cut-off source unwinds its own `finally` blocks rather
        # than being abandoned mid-iteration. Sources that hand back a plain
        # iterator have nothing to close.
        close = getattr(jobs, "close", None)
        if callable(close):
            try:
                bracket(close)
            except Exception:
                logger.exception("closing source iterator failed: %r", source)


def collect_candidates(
    sources: list,
    store: PostgresJobStore,
    http: HttpClient,
    policy: SearchPolicy,
    resolver: CanonicalResolver | None = None,
    preferences: CandidatePreferences | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DiscoveryResult:
    """Discover, canonicalize, deduplicate, persist, and prefilter jobs.

    Source failures and unresolved canonical lookups remain non-fatal. When no
    resolver is supplied, candidates retain their source URLs and canonical
    counters stay at zero.

    Records what each source cost -- elapsed time and network requests -- in
    `DiscoveryStats`, alongside the yield counts it already collects, and the
    total time this function took. `clock` returns monotonic seconds and is
    injected so tests drive timing without waiting; requests are counted at
    the shared `http` client (see `HttpClient.request_count`) and attributed
    to whichever source is running, because the sources differ too much in
    how they issue requests for each to count its own.

    Each source is also bounded by `policy.source_time_budget_seconds`, so no
    one source can consume the run. A source that overruns is cut off between
    its units of work, whatever it produced is kept and flows on normally, and
    the remaining sources run as usual -- see `_iter_source_jobs`.
    """
    started_at = clock()
    stats = DiscoveryStats()
    ledger = _PhaseLedger(clock, started_at)
    raw_jobs: list[Job] = []
    denylist = frozenset(policy.learned_ats_denylist)
    taken_labels: set[str] = set()

    budget_seconds = policy.source_time_budget_seconds

    with ledger.phase(PHASE_SOURCES):
        for source in sources:
            label = _distinct_label(source, taken_labels)
            for job in _iter_source_jobs(
                source, http, stats, label, clock, budget_seconds
            ):
                stats.raw += 1
                stats.per_source[job.source] = stats.per_source.get(job.source, 0) + 1
                if job.url:
                    job.original_url = job.original_url or job.url
                # Attribute before the first persist and before _dedupe, so a
                # job from any source that happens to carry a supported ATS
                # URL is stored with its identity and can be matched on the
                # strongest dedup key this run rather than only on its URL.
                apply_ats_identity(job)
                job.content_confidence = content_confidence.infer_content_confidence(
                    job.source, job.description
                )
                raw_market_id = _cheap_market_attribution(job, policy)
                _bump(stats.raw_by_market, raw_market_id or _UNATTRIBUTED)
                raw_jobs.append(job)

    # Persist every source copy before collapsing the run so provenance is
    # retained even when only one representative continues to evaluation.
    with ledger.phase(PHASE_RAW_PERSIST):
        store.upsert_logical_jobs(raw_jobs)

    with ledger.phase(PHASE_DEDUPE):
        unique_jobs, stats.cross_source_duplicates = _dedupe(raw_jobs)
        stats.unique = len(unique_jobs)

    prefiltered: list[tuple[str, Job]] = []
    rediscovered_job_ids: list[str] = []

    # Design step 4 (network work on unique jobs). Board references are
    # captured here, while each job still carries the market hint it was
    # observed with -- the attribution in step 6 overwrites job.market_id.
    board_sightings: list[tuple[str, str, str, str]] = []
    observed_markets: list[str | None] = []
    with ledger.phase(PHASE_ENRICH):
        for job in unique_jobs:
            observed_market_id = _cheap_market_attribution(job, policy)
            observed_markets.append(observed_market_id)
            reference = _ats_board_reference_safely(
                job, market_hint=observed_market_id, denylist=denylist
            )
            if reference is not None:
                board_sightings.append(reference)
            if job.url and not job.description:
                enrich_job(job, http)

    # Design steps 5 and 6 (batch-upsert the unique jobs, then batch the
    # remaining reads and writes): one batch of writes and one batch of reads
    # for the whole run.
    with ledger.phase(PHASE_UNIQUE_PERSIST):
        stats.ats_boards_discovered += store.upsert_ats_boards(board_sightings)
        upserted = store.upsert_logical_jobs(unique_jobs)

        persisted: list[tuple[str, Job, str | None]] = []
        skipped_count = 0
        # strict=True: these three lists are built one entry per unique job
        # and must stay that way. A store whose batch upsert returns a
        # shorter list (DryRunStore._synthesize("list") returns []) would
        # otherwise make collect_candidates silently return zero candidates.
        for job, observed_market_id, result in zip(
            unique_jobs, observed_markets, upserted, strict=True
        ):
            if result is None:
                # upsert_logical_jobs already logged why. Dropping the job here is
                # the only option: everything downstream is keyed by its id. It
                # still counts in stats.unique (set above, from _dedupe's output)
                # but never reaches unique_by_market/unique_by_source or
                # _record_reattribution below -- logged so that gap is visible
                # rather than a silent mismatch against the discovery: log line.
                skipped_count += 1
                continue
            persisted.append((result[0], job, observed_market_id))

        if skipped_count:
            logger.warning(
                "discovery dropped %s job(s) that could not be persisted; "
                "stats.unique will not equal the sum of unique_by_market/unique_by_source",
                skipped_count,
            )

        market_updates: list[tuple[str, str | None]] = []
        for job_id, job, observed_market_id in persisted:
            job.market_id = attribute_market(job, policy.markets) if policy.markets else None
            _record_reattribution(stats, observed_market_id, job.market_id)
            if job.market_id:
                market_updates.append((job_id, job.market_id))
        store.set_job_markets(market_updates)

        evaluation_needed = store.needs_evaluation_bulk([job_id for job_id, _job, _hint in persisted])

    # Design step 7 (prefilter and count): mostly pure -- the only I/O left
    # here is collecting the terminal-status pairs for jobs rejected this
    # run, flushed once after the loop.
    # That write must survive: job_hunter_gmail_candidate_complete
    # (20260907104935_job_hunter_gmail_candidate_eligibility.sql) treats a
    # job whose status is "rejected" or "closed" as complete regardless of
    # whether it has an evaluation, which is what stops a rejected public
    # job's Gmail twin being re-emitted as an inbound candidate forever.
    # (It is NOT read by needs_evaluation/needs_evaluation_bulk, which only
    # look at the evaluations table -- a rejected job with no evaluation row
    # still answers needs=True next run and is correctly re-evaluated.)
    with ledger.phase(PHASE_PREFILTER):
        status_updates: list[tuple[str, str]] = []
        for job_id, job, _observed_market_id in persisted:
            market_key = job.market_id or _UNATTRIBUTED
            source_label = metric_source_label(job.source)
            _bump(stats.unique_by_market, market_key)
            _bump(stats.unique_by_source, source_label)

            if not evaluation_needed[job_id]:
                rediscovered_job_ids.append(job_id)
                continue

            if job.availability == CLOSED:
                status_updates.append((job_id, "closed"))
                stats.availability_rejected += 1
                _bump(stats.rejected_by_market, market_key)
                _bump(stats.rejected_by_source, source_label)
                continue

            market = market_by_id(policy, job.market_id) if job.market_id else None
            prefilter_result = prefilter_job(job, policy, market)
            if not prefilter_result.should_evaluate:
                status_updates.append((job_id, "rejected"))
                if prefilter_result.reason_code == "off_target_profession":
                    stats.profession_rejected += 1
                else:
                    stats.prefilter_rejected += 1
                _bump(stats.rejected_by_market, market_key)
                _bump(stats.rejected_by_source, source_label)
                continue

            prefiltered.append((job_id, job))

        store.set_job_statuses(status_updates)

    # Canonical resolution costs a page fetch plus a public search for jobs
    # not already on a supported ATS host, so that expensive path only runs
    # for the highest-ranked prefiltered candidates: the ones that could
    # realistically survive ranking/selection this run. The shortlist size
    # is whichever is smaller, the flat per-run ceiling or max_jobs_per_run
    # times the slack multiplier. Already-supported-ATS URLs resolve locally
    # at zero network cost, so they are never gated by this shortlist -- and
    # never consume a shortlist slot either, since they're filtered out
    # before the slot count is applied below.
    with ledger.phase(PHASE_CANONICAL):
        shortlisted_ids: set[str] = set()
        if resolver is not None and prefiltered:
            shortlist_limit = max(
                0,
                min(
                    policy.max_canonical_resolutions_per_run,
                    max(0, policy.max_jobs_per_run) * _CANONICAL_SHORTLIST_MULTIPLIER,
                ),
            )
            stats.canonical_shortlist_limit = shortlist_limit
            ranked_prefiltered = rank_jobs(prefiltered, policy, preferences)
            needing_resolution_ranked = [
                item
                for item in ranked_prefiltered
                if item[1].url and parse_supported_ats_url(item[1].url) is None
            ]
            # Mirror pipeline._select_candidates's own strategy here: a flat
            # top-N slice when there's no candidate profile to diversify by,
            # diversity-aware selection when there is. Final selection
            # guarantees every source a minimum_per_source floor regardless of
            # global rank, so a flat rank slice here could shortlist zero
            # candidates from a source that final selection still picks --
            # leaving those jobs unresolved even though they ship.
            try:
                if preferences is None:
                    shortlist = needing_resolution_ranked[:shortlist_limit]
                else:
                    shortlist = select_diverse_candidates(
                        needing_resolution_ranked,
                        limit=shortlist_limit,
                        minimum_per_source=policy.source_minimum_per_run,
                        max_share=policy.source_max_share,
                    )
            except Exception:
                logger.exception(
                    "canonical shortlist selection failed; falling back to global rank"
                )
                shortlist = needing_resolution_ranked[:shortlist_limit]
            shortlisted_ids = {item[0] for item in shortlist}

    eligible: list[tuple[str, Job]] = []
    eligible_job_ids: set[str] = set()
    # One entry per eligible job on a supported ATS board; the store collapses
    # them to one entry per board before it writes.
    eligible_sightings: list[tuple[str, str]] = []

    with ledger.phase(PHASE_ELIGIBLE):
        for job_id, job in prefiltered:
            if resolver is not None and job.url:
                already_ats_url = parse_supported_ats_url(job.url) is not None
                if not already_ats_url and job_id not in shortlisted_ids:
                    stats.canonical_budget_exhausted += 1
                else:
                    with ledger.phase(PHASE_CANONICAL):
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
                        if job.availability == CLOSED:
                            store.set_job_status(job_id, "closed")
                            stats.availability_rejected += 1
                            _bump(stats.rejected_by_market, job.market_id or _UNATTRIBUTED)
                            _bump(stats.rejected_by_source, metric_source_label(job.source))
                            continue
                        if resolution is None:
                            stats.canonical_unresolved += 1
                        else:
                            stats.canonical_resolved += 1
                            job.canonical_url = resolution.url
                            job.url = resolution.url
                            if resolution.ats is not None:
                                # Fill, never relabel. A job that reached the resolver
                                # can already carry authoritative identity from its own
                                # adapter (an ATS posting whose URL does not parse, such
                                # as a board embedded on the employer's domain), and the
                                # resolver's weaker branches -- an embedded link is the
                                # first ATS anchor on the page, with no company or title
                                # check -- can point at a different posting entirely.
                                # Overwriting here would merge this job into that
                                # posting's stored row on the ATS dedup key.
                                apply_ats_identity(job, resolution.ats)
                                if _harvest_ats_board_safely(store, job, denylist=denylist):
                                    stats.ats_boards_discovered += 1
                                if job.content_confidence != content_confidence.OFFICIAL_ATS:
                                    authoritative = fetch_authoritative_description(
                                        resolution.ats, resolution.url, http
                                    )
                                    if authoritative:
                                        job.description = authoritative
                                        job.content_confidence = content_confidence.OFFICIAL_ATS
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
                eligible_sightings.append((job.ats_provider, job.ats_board))

        # Flushed once, like the status writes the prefilter pass collects.
        # Recording this per job was two round trips each -- roughly 2,700 of
        # them in run 34201733339 -- to increment a counter on a few dozen
        # rows. Learning the registry stays opportunistic: a failure is
        # logged and skipped, and the run continues with its candidates.
        if eligible_sightings:
            try:
                store.record_ats_eligible_jobs(
                    eligible_sightings, datetime.now(timezone.utc)
                )
            except Exception:
                logger.exception(
                    "recording %s ATS-eligible job(s) failed",
                    len(eligible_sightings),
                )

    stats.eligible = len(eligible)
    # The total comes from the ledger's own last reading rather than a fresh
    # one, so the phases sum to it exactly and no remainder can appear
    # between the last phase and this line.
    stats.total_elapsed_seconds = max(0.0, ledger.close() - started_at)
    stats.elapsed_by_phase = dict(ledger.elapsed)
    logger.info(
        "discovery source cost: total=%s %s",
        _format_seconds(stats.total_elapsed_seconds),
        _format_source_cost(stats, budget_seconds),
    )
    logger.info(
        "discovery phase cost: total=%s %s",
        _format_seconds(stats.total_elapsed_seconds),
        _format_phase_cost(stats),
    )
    logger.info(
        "discovery source contribution: %s canonical_resolved=%s "
        "canonical_unresolved=%s canonical_budget_exhausted=%s "
        "canonical_network_attempts=%s canonical_shortlist_limit=%s "
        "cross_source_duplicates=%s availability_rejected=%s",
        _format_source_contribution(stats.per_source),
        stats.canonical_resolved,
        stats.canonical_unresolved,
        stats.canonical_budget_exhausted,
        stats.canonical_network_attempts,
        stats.canonical_shortlist_limit,
        stats.cross_source_duplicates,
        stats.availability_rejected,
    )

    return DiscoveryResult(
        eligible=eligible,
        rediscovered_job_ids=rediscovered_job_ids,
        stats=stats,
    )

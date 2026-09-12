"""Resolve public job-listing URLs to higher-confidence employer postings."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from engine.ats_hosts import SUPPORTED_ATS_HOSTS
from engine.availability import CLOSED, UNVERIFIED, VERIFIED, detect_closure
from engine.fetching import extract_job_page_links
from engine.job_identity import (
    locations_compatible,
    normalize_company_name,
    normalize_job_title,
)
from engine.models import AtsReference, CanonicalResolution, Job

if TYPE_CHECKING:
    from engine.http import HttpClient


def parse_supported_ats_url(url: str) -> AtsReference | None:
    """Return the supported ATS reference encoded by a public job URL, if any."""
    parsed = urlparse(url)
    host = parsed.hostname.lower() if parsed.hostname else ""
    parts = [part for part in parsed.path.split("/") if part]

    provider = SUPPORTED_ATS_HOSTS.get(host)
    if provider in ("lever", "ashby") and len(parts) >= 2:
        return AtsReference(provider=provider, board=parts[0], job_id=parts[1])
    if provider == "greenhouse" and len(parts) >= 3 and parts[1] == "jobs":
        return AtsReference(provider=provider, board=parts[0], job_id=parts[2])
    return None


def apply_ats_identity(job: Job, fallback: AtsReference | None = None) -> bool:
    """Populate a job's empty ATS identity fields in place.

    Evidence is taken from the job's own URLs first (``canonical_url``, then
    ``url``, then ``original_url``), and only then from ``fallback`` — the
    reference a caller knows independently of the URL, such as an ATS adapter
    that was constructed with a board identifier and read the job id out of
    the listing payload.

    URLs win over ``fallback`` so that identity always agrees with what every
    other code path derives from the same URL (``extract_ats_reference``,
    ``discovery.candidate_ats_key``, ``JobStore._find_job_ids_by_ats``). A
    board slug spelled differently by the adapter and by the posting URL would
    otherwise split the dedup key instead of joining it.

    Already-populated fields are never overwritten: this fills gaps, it does
    not relabel. That is deliberately the opposite of the store's own update
    rule, where an incoming non-empty value wins (`upsert_job` coalesces onto
    the incoming value, `_update_logical_job` uses `job.ats_provider or
    row[...]`). The store is arbitrating between two sightings of one posting;
    this helper is arbitrating between evidence about one in-memory job, where
    whatever attributed it first saw it more directly than any later guess.
    Returns True when any field was filled.
    """
    if job.ats_provider and job.ats_board and job.ats_job_id:
        return False

    reference: AtsReference | None = None
    for url in (job.canonical_url, job.url, job.original_url):
        if not url:
            continue
        reference = parse_supported_ats_url(url)
        if reference is not None:
            break
    if reference is None:
        reference = fallback
    if reference is None:
        return False

    filled = False
    if not job.ats_provider and reference.provider:
        job.ats_provider = reference.provider
        filled = True
    if not job.ats_board and reference.board:
        job.ats_board = reference.board
        filled = True
    if not job.ats_job_id and reference.job_id:
        job.ats_job_id = reference.job_id
        filled = True
    return filled


class CanonicalResolver:
    """Resolve a job to a public canonical URL without blocking the pipeline on errors."""

    def __init__(
        self,
        http: HttpClient,
        search_candidates: Callable[[Job], list[Job]],
        watch_target: Callable[[str], AtsReference | None],
    ) -> None:
        self._http = http
        self._search_candidates = search_candidates
        self._watch_target = watch_target

    def resolve(self, job: Job) -> CanonicalResolution | None:
        """Return the first resolution meeting the documented confidence threshold."""
        direct_ats = parse_supported_ats_url(job.url)
        if direct_ats is not None:
            return CanonicalResolution(
                url=job.url,
                ats=direct_ats,
                confidence=1.0,
                method="direct",
            )

        if not job.url:
            return None

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
            # Safe to skip the redirect-based ATS check below: response_url
            # is job.url, and job.url already failed parse_supported_ats_url
            # at the direct_ats check above, so the check re-run on
            # response_url here is guaranteed to also return None.
        else:
            try:
                response = self._http.get(job.url)
                response.raise_for_status()
                response_url = response.url
                response_text = response.text
            except Exception:
                job.availability = UNVERIFIED
            else:
                job.availability = CLOSED if detect_closure(response_text) else VERIFIED

        redirected_ats = parse_supported_ats_url(response_url)
        if redirected_ats is not None:
            return CanonicalResolution(
                url=response_url,
                ats=redirected_ats,
                confidence=0.98,
                method="redirect",
            )

        embedded = resolve_embedded_ats_link(response_text, response_url or job.url)
        if embedded is not None:
            url, ats = embedded
            return CanonicalResolution(
                url=url,
                ats=ats,
                confidence=0.95,
                method="embedded",
            )

        try:
            candidates = self._search_candidates(job)
        except Exception:
            return None

        try:
            watch_ats = self._watch_target(job.company)
        except Exception:
            watch_ats = None

        if watch_ats is not None:
            for candidate in candidates:
                ats = parse_supported_ats_url(candidate.url)
                if _same_ats_board(ats, watch_ats) and _titles_match(job, candidate):
                    return CanonicalResolution(
                        url=candidate.url,
                        ats=ats,
                        confidence=0.92,
                        method="watch_target",
                    )

        generic_candidate: Job | None = None
        for candidate in candidates:
            if _same_company(job, candidate) and _titles_match(job, candidate) and locations_compatible(
                job.location, candidate.location
            ):
                ats = parse_supported_ats_url(candidate.url)
                if ats is not None:
                    return CanonicalResolution(
                        url=candidate.url,
                        ats=ats,
                        confidence=0.90,
                        method="targeted_search",
                    )
                if generic_candidate is None:
                    generic_candidate = candidate

        if generic_candidate is not None:
            return CanonicalResolution(
                url=generic_candidate.url,
                ats=None,
                confidence=0.90,
                method="targeted_search",
            )
        return None


def _same_company(left: Job, right: Job) -> bool:
    return bool(normalize_company_name(left.company)) and (
        normalize_company_name(left.company) == normalize_company_name(right.company)
    )


def _titles_match(left: Job, right: Job) -> bool:
    return bool(normalize_job_title(left.title)) and (
        normalize_job_title(left.title) == normalize_job_title(right.title)
    )


def _same_ats_board(left: AtsReference | None, right: AtsReference) -> bool:
    return left is not None and (left.provider, left.board) == (right.provider, right.board)


def resolve_embedded_ats_link(
    response_text: str, base_url: str
) -> tuple[str, AtsReference] | None:
    """The one distinct ATS posting embedded on a fetched page, if any.

    Pure -- no I/O -- so both `CanonicalResolver.resolve` (which has already
    fetched the page under its own fail-open rules) and
    `recover_posting_stage.RecoverPostingStage` (which fetches under its own
    rate-limit-aware rules) can share this matching logic without sharing
    fetch semantics that must differ between them (#259 review).

    An embedded link has no company or title check behind it: it is
    trustworthy only when the page names exactly one distinct ATS posting. A
    page listing more than one -- a careers page with several open roles, a
    "similar jobs" widget -- makes "the first anchor" an arbitrary pick among
    them, which could misattribute a job's identity to a different
    advertisement. Hardening only: #254's actual corruption was traced to
    job_hunter_upsert_job's legacy per-job merge (weak company/title/location
    identity match overwriting ats_* while leaving source_job_id alone), not
    this branch -- see #254 for the confirmed root cause. Two links to the
    same posting (a duplicated anchor) still count as one candidate.
    """
    try:
        links = extract_job_page_links(response_text, base_url)
    except Exception:
        links = []
    candidates: dict[tuple[str, str, str], tuple[str, AtsReference]] = {}
    for url in links:
        ats = parse_supported_ats_url(url)
        if ats is not None:
            candidates.setdefault((ats.provider, ats.board, ats.job_id), (url, ats))
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None


def description_adapter_for(provider: str) -> Any | None:
    """The module that fetches a supported ATS provider's full descriptions.

    The import below is deliberately local rather than module-level:
    engine.sources imports engine.ats_registry, which imports
    parse_supported_ats_url from this module, so importing engine.sources
    at module scope here creates a circular import.
    """
    from engine.sources import ashby as ashby_source
    from engine.sources import greenhouse as greenhouse_source
    from engine.sources import lever as lever_source

    return {
        "ashby": ashby_source,
        "lever": lever_source,
        "greenhouse": greenhouse_source,
    }.get(provider)


def fetch_authoritative_description(
    ats: AtsReference, target_url: str, http: "HttpClient"
) -> str | None:
    """Fetch the full official description for a resolved ATS posting.

    Non-fatal by design, matching the rest of this module: a fetch failure
    here should never take down canonical resolution. This is the right
    contract for the legacy pipeline this was written for, and the wrong one
    for a queue stage that needs to actually back off on a rate limit or
    server error rather than silently recording "no description found" --
    recover_posting_stage.py calls description_adapter_for directly instead
    of this wrapper for exactly that reason (#259 review).
    """
    adapter = description_adapter_for(ats.provider)
    if adapter is None:
        return None
    try:
        return adapter.fetch_description(ats.board, target_url, http)
    except Exception:
        return None

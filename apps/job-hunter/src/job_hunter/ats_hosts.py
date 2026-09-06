"""The public hostnames whose URLs identify a supported employer ATS posting.

One definition, read by every consumer that has to answer "is this URL an
employer's own ATS posting?" -- the canonical URL parser, richness-based dedup,
source-quality ranking, embedded-link extraction, stored-row identity backfill,
and the site: filters in canonical search queries. Adding a host is one edit
here rather than one edit per consumer, which is how
``job-boards.greenhouse.io`` stayed unrecognised for months after Greenhouse
started serving modern boards from it.

This module deliberately imports nothing from ``job_hunter``: ``canonical``
imports ``fetching`` and ``store`` imports ``canonical``, so a shared table
living in any of them would close an import cycle.

Only the host-to-provider mapping is shared. Path shapes are not, because they
differ per provider (``/<board>/<id>`` for Lever and Ashby, ``/<board>/jobs/
<id>`` for Greenhouse) and encoding that here would buy nothing at this size --
see ``canonical.parse_supported_ats_url``, which owns the path rules.

Consumers match against this table in two different ways on purpose. Callers
that hold a real URL (``fetching.extract_job_page_links``) compare the parsed
hostname exactly. Callers that score a possibly-wrapped URL
(``discovery._is_ats_url``, ``ranking.source_quality``) test for the host as a
substring, so an aggregator's redirect wrapper still reads as an ATS posting.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# Insertion order is meaningful: it fixes the order of the generated site:
# filters in pipeline._CANONICAL_SEARCH_SITES, which keeps a run's search
# queries stable across releases.
SUPPORTED_ATS_HOSTS: Mapping[str, str] = MappingProxyType(
    {
        "jobs.lever.co": "lever",
        "jobs.ashbyhq.com": "ashby",
        # Greenhouse serves postings from both hosts, depending on the board's
        # vintage; a board's API `absolute_url` returns whichever applies.
        "boards.greenhouse.io": "greenhouse",
        "job-boards.greenhouse.io": "greenhouse",
    }
)

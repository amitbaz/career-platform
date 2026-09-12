"""The public hostnames whose URLs identify a supported employer ATS posting.

One definition, read by every consumer that has to answer "is this URL an
employer's own ATS posting?" -- the canonical URL parser, richness-based dedup,
source-quality ranking, embedded-link extraction, stored-row identity backfill,
and the site: filters in canonical search queries. Adding a host is one edit
here rather than one edit per consumer, which is how
``job-boards.greenhouse.io`` stayed unrecognised for months after Greenhouse
started serving modern boards from it.

This module deliberately imports nothing from ``engine``: ``canonical``
imports ``fetching`` and ``store`` imports ``canonical``, so a shared table
living in any of them would close an import cycle.

Only the host-to-provider mapping is shared. Path shapes are not, because they
differ per provider (``/<board>/<id>`` for Lever and Ashby, ``/<board>/jobs/
<id>`` for Greenhouse) and encoding that here would buy nothing at this size --
see ``canonical.parse_supported_ats_url``, which owns the path rules.

Consumers match against this table in two different ways on purpose. Callers
that hold a real URL (``fetching.extract_job_page_links``) compare the parsed
hostname exactly. Callers that score a possibly-wrapped URL
(``discovery._is_ats_url``, ``ranking.source_quality``, and the ``LIKE`` filter
in ``store.backfill_ats_identity``) test for the host as a substring, so an
aggregator's redirect wrapper still reads as an ATS posting.

One consequence of substring matching is that a host which merely contains an
existing entry is already matched by the substring consumers before it is added
here -- ``boards.greenhouse.io`` matches ``job-boards.greenhouse.io``. The
regional hosts below are not covered that way, and neither is any consumer that
matches exactly, so nothing about that accident makes an entry redundant.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

# Insertion order is meaningful: it fixes the order of the generated site:
# filters in pipeline._CANONICAL_SEARCH_SITES, and reordering OR terms can move
# a search engine's result ranking. The first three entries are therefore kept
# in the order the hand-written filter used before this table existed, and new
# hosts are appended rather than interleaved.
SUPPORTED_ATS_HOSTS: Mapping[str, str] = MappingProxyType(
    {
        "jobs.ashbyhq.com": "ashby",
        "jobs.lever.co": "lever",
        # Greenhouse serves postings from several hosts and a board's API
        # `absolute_url` reports whichever applies: `boards` is the legacy name
        # and redirects to `job-boards`, and organizations on the EU data
        # region get the same pair under `.eu.`. All four share the
        # `/<board>/jobs/<id>` path shape. The board API itself is not
        # regionalised -- `boards-api.greenhouse.io` serves EU boards too, and
        # `boards-api.eu.greenhouse.io` does not exist -- so the adapter in
        # sources/greenhouse.py needs no regional handling.
        "boards.greenhouse.io": "greenhouse",
        "job-boards.greenhouse.io": "greenhouse",
        "boards.eu.greenhouse.io": "greenhouse",
        "job-boards.eu.greenhouse.io": "greenhouse",
    }
)

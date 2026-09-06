"""Deterministic posting-availability signal, generic across job boards.

No AI calls and no extra network fetches: callers run detect_closure on HTML
they already fetched for another reason (enrichment, canonical resolution).
"""

from __future__ import annotations

from bs4 import BeautifulSoup

#: A candidate never had its own page checked this run. Same silent
#: treatment as VERIFIED (no card warning) since most jobs reach discovery
#: through an ATS board listing that is inherently already live.
UNCHECKED = "unchecked"
#: Page was fetched and no closure phrase was found.
VERIFIED = "verified"
#: The fetch was attempted but failed or was inconclusive (timeout, 403,
#: bot protection, parse failure). Never treated as evidence of closure.
UNVERIFIED = "unverified"
#: Page was fetched and explicitly states the posting is closed/expired.
CLOSED = "closed"

# Full phrases only, not bare fragments -- "no longer available" alone
# would false-positive on unrelated page copy (e.g. an expired discount
# banner). Every phrase here names the job/application/position explicitly.
_CLOSURE_PHRASES = [
    "this job posting has expired",
    "job posting has expired",
    "no longer accepting applications",
    "this position has been filled",
    "position has been filled",
    "this job is no longer accepting applications",
    "applications are now closed",
    "this posting is no longer available",
    "this listing is no longer active",
    "this job is no longer available",
]


def detect_closure(html: str) -> bool:
    """Return True when the page's visible text states the posting is closed."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ").lower()
    text = " ".join(text.split())
    return any(phrase in text for phrase in _CLOSURE_PHRASES)

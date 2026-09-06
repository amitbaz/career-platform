"""Detect learned ATS boards that aggregate postings on behalf of other companies.

An aggregator is defined by what it tells applicants, not by how many jobs it
posts: it says the role belongs to someone else. That self-declared
provenance is scale-free -- it identifies a 40-posting aggregator the same
way it identifies a 4,000-posting one -- so no signal here may reject a board
for its size alone.
"""

from __future__ import annotations

from dataclasses import dataclass

# Specific phrasings only. The generic "on behalf of" produces false
# positives -- e.g. recruiting-scam warnings inside otherwise legitimate
# postings -- so each phrase here must itself assert the role belongs to a
# different company, not merely mention "on behalf of" in passing.
_THIRD_PARTY_LISTING_PHRASES = (
    "listed on behalf of",
    "recruiting for our partner",
    "this role is with our client",
)

# A one- or two-posting sample can't tell an aggregator from noise; a board
# needs at least this many scanned postings before its match fraction means
# anything.
_MIN_POSTINGS_SCANNED = 5

# "Most of this board's jobs are not its own." The exact cut isn't
# load-bearing -- observed separation is 98% (jobgether) vs 0% (every
# legitimate employer board sampled).
_THIRD_PARTY_MAJORITY_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class SignalEvidence:
    """One named signal's measurement against a board's scanned postings."""

    name: str
    fired: bool
    matched: int
    scanned: int
    reason: str


@dataclass(frozen=True, slots=True)
class AggregatorVerdict:
    """Whether a board should be rejected as an aggregator, and why."""

    rejected: bool
    reason: str | None
    evidence: tuple[SignalEvidence, ...]


def third_party_listing(descriptions: list[str]) -> SignalEvidence:
    """Fire when most scanned postings declare the role belongs to another company."""
    scanned = len(descriptions)
    matched = sum(
        1
        for description in descriptions
        if any(phrase in description.lower() for phrase in _THIRD_PARTY_LISTING_PHRASES)
    )
    if scanned < _MIN_POSTINGS_SCANNED:
        return SignalEvidence(
            name="third_party_listing",
            fired=False,
            matched=matched,
            scanned=scanned,
            reason=f"only {scanned} posting(s) scanned, below minimum {_MIN_POSTINGS_SCANNED}",
        )
    fraction = matched / scanned
    fired = fraction > _THIRD_PARTY_MAJORITY_THRESHOLD
    return SignalEvidence(
        name="third_party_listing",
        fired=fired,
        matched=matched,
        scanned=scanned,
        reason=(
            f"{matched}/{scanned} postings ({fraction:.0%}) declare the role "
            "belongs to another company"
        ),
    )


_SIGNALS = (third_party_listing,)


def evaluate_board(descriptions: list[str]) -> AggregatorVerdict:
    """Run every detection signal against a board's scanned postings.

    Additional signals can be added to `_SIGNALS` without touching the
    registry or the learned-ATS source that calls this.
    """
    evidence = tuple(signal(descriptions) for signal in _SIGNALS)
    fired = [e for e in evidence if e.fired]
    if not fired:
        return AggregatorVerdict(rejected=False, reason=None, evidence=evidence)
    reason = "; ".join(f"{e.name}: {e.reason}" for e in fired)
    return AggregatorVerdict(rejected=True, reason=reason, evidence=evidence)

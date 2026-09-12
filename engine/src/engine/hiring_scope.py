"""Read a posting's explicitly stated hiring scope: which regions it will hire in.

This module answers exactly one question -- *what regions is this posting open
to?* -- from the posting text alone. It knows nothing about markets, nothing
about a candidate, and nothing about scoring. That boundary is deliberate
(issue #16): hiring eligibility is a property of the posting, so it is shared
and cacheable across users and runs, while "can *this* candidate work there" is
per-user. Only the first belongs here.

The determination is conservative in one direction and permissive in the other:

* A region only enters the scope when a clause *states* who may be hired or
  where the work may be done ("open to candidates based in the US and Europe",
  "you must be located in Israel", "must have the right to work in the UK").
  Background prose that merely names a place -- "our team spans Europe", "we
  serve customers across EMEA" -- is not eligibility language and is ignored.
* A posting that states nothing returns an *empty* scope, which callers must
  read as "no explicit scope" rather than "eligible nowhere". Ambiguous and
  global postings therefore fail open.

The region atlas below covers the places the search profile's markets name plus
the broad aliases postings actually use. A place the atlas does not know maps to
no region, which again fails open: an unknown place can neither widen nor narrow
anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from engine.models import Job

NORTH_AMERICA = "north_america"
EUROPE = "europe"
MIDDLE_EAST = "middle_east"
ASIA_PACIFIC = "asia_pacific"

# Region aliases matched case-insensitively. Country and city names are here
# because market locations are written that way ("Berlin", "Bay Area") and a
# posting's eligibility clause often is too ("open to candidates in Germany").
_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    NORTH_AMERICA: (
        "north america", "united states", "united states of america", "canada",
        "new york", "new york city", "brooklyn", "san francisco", "bay area",
        "silicon valley", "seattle", "austin", "boston", "chicago", "denver",
        "los angeles", "toronto", "vancouver", "montreal",
    ),
    EUROPE: (
        "europe", "european union", "european economic area", "germany",
        "berlin", "munich", "hamburg", "frankfurt", "cologne",
        "united kingdom", "great britain", "england", "scotland", "wales",
        "london", "manchester", "ireland", "dublin",
        "netherlands", "amsterdam", "rotterdam", "utrecht",
        "france", "paris", "lyon", "spain", "madrid", "barcelona", "valencia",
        "portugal", "lisbon", "porto", "italy", "milan", "rome",
        "poland", "warsaw", "krakow", "czech republic", "czechia", "prague",
        "austria", "vienna", "switzerland", "zurich", "geneva",
        "belgium", "brussels", "denmark", "copenhagen", "sweden", "stockholm",
        "norway", "oslo", "finland", "helsinki", "estonia", "tallinn",
        "romania", "bucharest", "bulgaria", "sofia", "greece", "athens",
        "hungary", "budapest",
    ),
    MIDDLE_EAST: (
        "middle east", "israel", "tel aviv", "jerusalem", "haifa",
    ),
    ASIA_PACIFIC: (
        "asia pacific", "southeast asia", "south east asia", "singapore",
        "japan", "tokyo", "australia", "sydney", "melbourne", "new zealand",
        "india", "bangalore", "bengaluru", "hyderabad", "china", "hong kong",
        "south korea", "seoul", "indonesia", "jakarta", "philippines",
        "manila", "vietnam", "thailand", "malaysia", "kuala lumpur",
    ),
}

# Acronyms are matched case-sensitively, in upper case only. Lower-cased, "US"
# is the English pronoun -- "come join us" is not a statement about hiring in
# the United States -- and the same trap waits for the other short forms.
_REGION_ACRONYMS: dict[str, tuple[str, ...]] = {
    NORTH_AMERICA: ("U.S.", "USA", "U.S.A.", "NYC", "SF"),
    EUROPE: ("EU", "E.U.", "UK", "U.K.", "EMEA", "EEA"),
    ASIA_PACIFIC: ("APAC",),
}

# A bare "US" needs more care than the other acronyms: HTML-stripped postings
# are full of upper-case boilerplate ("JOIN US", "WORK WITH US", "ABOUT US"),
# where it is the pronoun in caps rather than the country. Reading the word in
# front of it separates the two.
_BARE_US_RE = re.compile(r"\bUS\b")
_PRONOUN_VERBS = frozenset({
    "join", "about", "with", "contact", "email", "tell", "help", "helps",
    "let", "lets", "reach", "meet", "follow", "find", "trust", "like",
})

# Clauses that state who may be hired, or from where the work may be done.
# Each alternative deliberately keeps its subject close: "you must be based in
# Berlin" is eligibility language, "you will pair with teams based in Berlin"
# is not, and only a tight window between the subject and the verb tells them
# apart.
_SCOPE_CUE_RE = re.compile(
    # Every trailing preposition demands a following space rather than a word
    # boundary, so "open to in-office collaboration in Berlin" is not read as
    # "open to ... in <region>".
    r"open to (?:candidates?|applicants?|people|anyone|those|hires?|employees?)?\s*"
    r"(?:who (?:are|live)|that are)?\s*"
    r"(?:based|located|living|residing)?\s*(?:in|within|from)(?=\s)"
    r"|(?:this|the) (?:role|position|job) (?:is|remains) open to\b"
    r"|\b(?:candidates?|applicants?|you|new hires?)\s+"
    r"(?:\w+\s+){0,2}?(?:based|located|living|residing|situated)\s+(?:in|within)(?=\s)"
    r"|\b(?:must|should|need to|needs to|have to|has to|are required to)\s+(?:be\s+)?"
    r"(?:based|located|living|residing)\s+(?:in|within)(?=\s)"
    r"|\b(?:eligible|authorized|authorised|permitted|able)\s+to work in(?=\s)"
    r"|\b(?:right|authorization|authorisation|permission)\s+to work in(?=\s)"
    # First person only: "we are hiring in Europe" states this employer's
    # scope, "our customers are hiring across Europe" states nothing about it.
    r"|\bwe(?:'re| are| have been)?\s+(?:currently\s+|actively\s+|now\s+|only\s+)?"
    r"hiring\s+(?:candidates\s+)?(?:in|within|across|throughout)(?=\s)"
    r"|\b(?:work|working|performed|done)\s+from anywhere\s+(?:in|within|across)(?=\s)"
    r"|\b(?:employment|hiring|eligibility)\s+is\s+(?:limited|restricted)\s+to\b"
    r"|\b(?:limited|restricted)\s+to\s+(?:candidates?|applicants?|residents?)\b",
    re.IGNORECASE,
)

# A clause that *denies* hiring somewhere ("we are not hiring in Europe") uses
# the same vocabulary as one that offers it. Rather than trying to invert the
# meaning, negation ends the reading: the posting falls back to having stated
# less, which fails open the way an unreadable posting already does.
#
# Two sets, because the two positions carry different risk. Before the cue, any
# negation drops the whole clause, so the list can be broad. After the cue it
# *truncates* the region list, which can leave a shortened scope that wrongly
# excludes a market -- so only words that actually negate a region belong here.
# A bare "no" does not: "open to candidates in the US, no agencies, and Europe"
# must keep Europe.
_NEGATION_BEFORE_CUE_RE = re.compile(
    r"\b(?:not|never|cannot|can't|don't|doesn't|won't|unable|no)\b",
    re.IGNORECASE,
)
_DENIAL_AFTER_CUE_RE = re.compile(
    r"\b(?:not|never|cannot|can't|won't|unable|excluding|except|outside)\b",
    re.IGNORECASE,
)

# Sentence boundaries, with the dotted-acronym problem handled explicitly: a
# period inside "U.S." must not end the clause ("open to candidates in the U.S.
# and Europe"), but a period *after* one usually does ("...in the U.S. Our team
# spans Europe."). Capitalisation is what separates them, so the split is
# suppressed only when a dotted acronym is followed by lower-case continuation.
_SENTENCE_SPLIT_RE = re.compile(
    r"\n+"
    r"|(?<=[.!?;:])\s+(?=[A-Z0-9\"'(])"
    r"|(?<![A-Z]\.)(?<=[.!?;:])\s+"
)


@dataclass(frozen=True, slots=True)
class HiringScope:
    """The regions a posting explicitly states it hires in.

    An empty `regions` means the posting said nothing determinate, not that it
    hires nowhere. Callers must treat that as "no constraint".
    """

    regions: frozenset[str]
    #: The clauses the regions were read from, for logging and debugging.
    evidence: tuple[str, ...] = ()

    @property
    def is_explicit(self) -> bool:
        return bool(self.regions)

    def permits(self, regions: frozenset[str]) -> bool:
        """Is a place in `regions` within this scope?

        True whenever the scope is not explicit, or the caller's regions are
        unknown: both are absences of evidence, and absence of evidence must
        never narrow anything here.
        """
        if not self.is_explicit or not regions:
            return True
        return bool(self.regions & regions)


_MATCHES_NOTHING = re.compile(r"(?!)")


def _compile(aliases: tuple[str, ...], flags: int = 0) -> re.Pattern[str]:
    # An empty alternation compiles to the empty pattern, which matches at
    # every position -- a region with no aliases would then claim every
    # posting. Return a pattern that matches nothing instead.
    if not aliases:
        return _MATCHES_NOTHING
    parts = []
    for alias in aliases:
        escaped = re.escape(alias)
        prefix = r"\b" if alias[0].isalnum() else ""
        suffix = r"\b" if alias[-1].isalnum() else ""
        parts.append(f"{prefix}{escaped}{suffix}")
    return re.compile("|".join(parts), flags)


_ALIAS_PATTERNS = {
    region: _compile(aliases, re.IGNORECASE)
    for region, aliases in _REGION_ALIASES.items()
}
_ACRONYM_PATTERNS = {
    region: _compile(acronyms) for region, acronyms in _REGION_ACRONYMS.items()
}
_ACRONYM_PATTERNS_ANY_CASE = {
    region: _compile(acronyms, re.IGNORECASE)
    for region, acronyms in _REGION_ACRONYMS.items()
}
_BARE_US_ANY_CASE_RE = re.compile(r"\bus\b", re.IGNORECASE)


def regions_in_text(text: str) -> frozenset[str]:
    """Return every region named anywhere in `text`.

    Naming a region is not the same as hiring there -- this is the raw alias
    lookup. Use it for prose already known to be about places: the remainder
    of a clause `determine_hiring_scope` has established is eligibility
    language. For configured place names use `regions_for_locations`, which
    reads acronyms less warily.
    """
    return _regions(text, _ACRONYM_PATTERNS, _names_bare_us_in_prose)


@lru_cache(maxsize=512)
def regions_for_locations(locations: tuple[str, ...]) -> frozenset[str]:
    """Return the regions a set of configured place names belongs to.

    A market's locations are configuration written by a person naming places,
    not prose: "emea" and "us" there are always the place and never the
    pronoun, so acronyms match whatever case they were typed in. Getting this
    wrong is not a near miss -- a market whose regions come back empty cannot
    earn the hiring-scope bonus and can lose its own location match to a rival
    that merely shares a region with the posting's stated scope.
    """
    return _regions(
        " ".join(locations),
        _ACRONYM_PATTERNS_ANY_CASE,
        lambda text: _BARE_US_ANY_CASE_RE.search(text) is not None,
    )


def _regions(text, acronym_patterns, names_bare_us) -> frozenset[str]:
    if not text:
        return frozenset()
    found = {
        region
        for region, pattern in _ALIAS_PATTERNS.items()
        if pattern.search(text) is not None
    }
    found.update(
        region
        for region, pattern in acronym_patterns.items()
        if pattern.search(text) is not None
    )
    if names_bare_us(text):
        found.add(NORTH_AMERICA)
    return frozenset(found)


def _names_bare_us_in_prose(text: str) -> bool:
    """Is an upper-case "US" here the country rather than the pronoun in caps?"""
    for match in _BARE_US_RE.finditer(text):
        preceding = text[: match.start()].split()
        if preceding and preceding[-1].strip("(,.;:\"'").lower() in _PRONOUN_VERBS:
            continue
        return True
    return False


def determine_hiring_scope(job: Job) -> HiringScope:
    """Return the regions `job`'s posting explicitly states it hires in.

    Only the title and description are read. The listing's location label is
    deliberately excluded: a label is a single, often-syndicated field that a
    posting's own text can legitimately widen, and letting it seed the scope
    would give the weaker evidence the stronger tier.
    """
    return _scope_from_text(f"{job.title or ''}\n{job.description or ''}")


def _scope_from_text(text: str) -> HiringScope:
    regions: set[str] = set()
    evidence: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        # Matched against the original text, never a lower-cased copy:
        # str.lower() is not length-preserving in Unicode, and an offset shift
        # would slice the region out of the remainder below.
        match = _SCOPE_CUE_RE.search(sentence)
        if match is None:
            continue
        if _NEGATION_BEFORE_CUE_RE.search(sentence[: match.start()]) is not None:
            continue
        remainder = sentence[match.end() :]
        denial = _DENIAL_AFTER_CUE_RE.search(remainder)
        if denial is not None:
            remainder = remainder[: denial.start()]
        named = regions_in_text(remainder)
        if named:
            regions.update(named)
            evidence.append(sentence)
    return HiringScope(regions=frozenset(regions), evidence=tuple(evidence))

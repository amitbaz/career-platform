"""Objective extraction: read a posting's own facts, once, for everyone.

A **facet** is one structured objective fact about a posting -- what it
requires and how deeply, what it discloses about pay, where it will hire,
whether it is remote, what seniority it is pitched at, what it is built on.
Facets are properties of the posting, so the same posting yields the same
answer whoever asks, and one extraction serves every later run (issue #125).

That sharing is the entire point, and it is load-bearing: the moment this
module can tell *who* is asking, its output stops being reusable and the cost
of extracting it is wasted. The boundary is enforced by the interface rather
than by care:

* `extract_facets` takes a `PostingFacts` and a provider client. There is no
  parameter anything per-user could arrive through.
* `PostingFacts` is frozen and carries only the posting's own fields.
* This module imports nothing per-user, and a test in `tests/test_facets.py`
  fails if that ever changes.

`evaluation.py` keeps the older combined operation, which does see the person
being matched. The two live apart deliberately; see
`docs/superpowers/specs/2026-09-08-job-posting-facets-design.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from job_hunter import content_confidence
from job_hunter.hiring_scope import (
    ASIA_PACIFIC,
    EUROPE,
    MIDDLE_EAST,
    NORTH_AMERICA,
    determine_hiring_scope,
)
from job_hunter.models import Compensation, Job, JobFacets

if TYPE_CHECKING:
    from job_hunter.gemini import GeminiClient

UNKNOWN = "unknown"

VALID_SENIORITY = frozenset(
    {"intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", UNKNOWN}
)
VALID_REMOTE_POLICY = frozenset({"remote", "hybrid", "onsite", UNKNOWN})
VALID_RELOCATION_POLICY = frozenset({"offered", "required", "not_offered", UNKNOWN})
VALID_DEPTHS = frozenset({"familiarity", "experience", "deep_expert"})
VALID_REQUIREMENT_KINDS = frozenset({"must_have", "preferred"})
VALID_PERIODS = frozenset({"", "hour", "day", "month", "year"})
#: The region vocabulary is `hiring_scope`'s, not a second one: the
#: deterministic reader and the model must answer the same question in the
#: same terms or the facet means two different things depending on who filled
#: it in.
VALID_REGIONS = frozenset({NORTH_AMERICA, EUROPE, MIDDLE_EAST, ASIA_PACIFIC})

#: The facets, in the order they are asked for and rendered.
FACET_FIELDS = (
    "seniority",
    "remote_policy",
    "relocation_policy",
    "hiring_regions",
    "stack",
    "compensation",
    "requirements",
)

#: Sources whose payload carries a genuine structured remote flag. Ashby's
#: `isRemote` is one. Greenhouse deliberately is *not*, although it does
#: return a structured location: its adapter derives `remote` by looking for
#: the substring "remote" in that location label
#: (`sources/greenhouse.py`), so "Hybrid Remote — Berlin" and
#: "Remote-friendly (2 days onsite)" both come through as True. Recording
#: those as `remote_policy="remote"` would pin a hybrid role as fully remote
#: and, because a supplied facet is never asked of the model, nothing would
#: ever correct it. Greenhouse's location label still reaches the prompt, so
#: the model reads it in context instead.
STRUCTURED_REMOTE_SOURCES = frozenset({"ashby"})

# One retry for transient provider 5xx/timeout failures, matching evaluation.
_FACET_MAX_ATTEMPTS = 2


class FacetExtractionError(ValueError):
    """The provider returned something that is not a usable set of facets."""


@dataclass(frozen=True, slots=True)
class PostingFacts:
    """The posting, and only the posting.

    Frozen and built from a `Job` by `from_job`. `stated_hiring_regions` is
    computed there by `hiring_scope.determine_hiring_scope`, so the
    deterministic read of the posting's own eligibility clauses happens once,
    at the boundary, rather than being re-derived by anything downstream.
    """

    title: str
    company: str
    location: str
    remote: bool | None
    description: str
    content_confidence: str
    source: str
    stated_hiring_regions: tuple[str, ...]

    @classmethod
    def from_job(cls, job: Job) -> "PostingFacts":
        scope = determine_hiring_scope(job)
        return cls(
            title=job.title or "",
            company=job.company or "",
            location=job.location or "",
            remote=job.remote,
            description=job.description or "",
            content_confidence=job.content_confidence or content_confidence.PARTIAL_UNKNOWN,
            source=job.source or "",
            # Sorted, not a frozenset's arbitrary order: the same posting has
            # to produce byte-identical prompts and stored values on every run.
            stated_hiring_regions=tuple(sorted(scope.regions)),
        )


def source_supplied_facets(posting: PostingFacts) -> dict[str, Any]:
    """Return the facets already known before any provider call is made.

    Two are, and the model is asked only for the residue:

    * `remote_policy`, when a source with a structured remote flag said the
      posting is remote. A *false* flag is deliberately not a facet: it
      separates neither hybrid from onsite, and recording it as onsite would
      invent something the source never said.
    * `hiring_regions`, when the posting states an explicit hiring scope.
      `hiring_scope` already answers exactly this question deterministically
      and is already the engine's answer to it; asking the model to re-derive
      it would buy a second, divergent answer at the price of a token budget.
      It fails open, so a posting that states nothing falls through.
    """
    supplied: dict[str, Any] = {}
    if posting.source in STRUCTURED_REMOTE_SOURCES and posting.remote is True:
        supplied["remote_policy"] = "remote"
    if posting.stated_hiring_regions:
        supplied["hiring_regions"] = list(posting.stated_hiring_regions)
    return supplied


_FIELD_INSTRUCTIONS = {
    "seniority": (
        '- "seniority": one of intern|junior|mid|senior|staff|principal|lead|manager|unknown. '
        "The level the posting is pitched at. Use unknown when it does not say."
    ),
    "remote_policy": (
        '- "remote_policy": one of remote|hybrid|onsite|unknown. What the posting states '
        "about where the work is done. Use unknown when it does not say."
    ),
    "relocation_policy": (
        '- "relocation_policy": one of offered|required|not_offered|unknown. '
        "offered means the employer states it supports relocation; required means the role "
        "demands moving; not_offered means it states it does not. Use unknown when it is silent."
    ),
    "hiring_regions": (
        '- "hiring_regions": an array of north_america|europe|middle_east|asia_pacific. '
        "Only regions the posting explicitly states it will hire in or where the work may be "
        "done. Background prose naming a place -- offices, customers, where the team spans -- "
        "is not an eligibility statement. Return [] when the posting states no scope."
    ),
    "stack": (
        '- "stack": an array of lowercase technology names the posting names -- languages, '
        "frameworks, databases, platforms. Return [] when it names none."
    ),
    "compensation": (
        '- "compensation": {"disclosed": bool, "currency": ISO-4217 code or "", '
        '"minimum": integer or null, "maximum": integer or null, '
        '"period": ""|hour|day|month|year}. Only what the posting itself discloses, in whole '
        "units of the stated currency. A posting that states one end of a range carries that "
        'end and null for the other. When it discloses nothing, set disclosed to false and '
        "leave every other field empty or null."
    ),
    "requirements": (
        '- "requirements": an array of {"requirement": string, '
        '"depth": familiarity|experience|deep_expert, "kind": must_have|preferred}. '
        "must_have is what the posting states or clearly implies is required; preferred is "
        "what it frames as a plus, a bonus or nice to have. depth is how deeply the posting "
        "demands it. Do not invent requirements the posting does not state or clearly imply."
    ),
}

_TIER_PROMPT_HINTS = {
    content_confidence.OFFICIAL_ATS: "This is the official employer/ATS posting text.",
    content_confidence.CANONICAL_EMPLOYER_PAGE: "This was extracted from the employer's own careers page.",
    content_confidence.SOURCE_DETAIL_PAGE: "This is a full detail-page scrape; likely complete but not confirmed authoritative.",
    content_confidence.AGGREGATOR_TEXT: "This is third-party aggregator or community text and may be incomplete or stale.",
    content_confidence.PARTIAL_UNKNOWN: "This content is thin or unverified. Prefer the 'unknown'/empty value over guessing when the posting does not clearly state something.",
}


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _build_facet_prompt(posting: PostingFacts, requested: list[str]) -> str:
    """Render the prompt for exactly the facets still to be established.

    A facet already supplied is not in `requested` and so is not mentioned at
    all: asking for it would spend tokens re-deriving a fact already held, and
    invite an answer that contradicts it.
    """
    fields = "\n".join(_FIELD_INSTRUCTIONS[name] for name in requested)
    shape = "{" + ", ".join(f'"{name}": ...' for name in requested) + "}"
    tier_hint = _TIER_PROMPT_HINTS.get(
        posting.content_confidence, _TIER_PROMPT_HINTS[content_confidence.PARTIAL_UNKNOWN]
    )
    return f"""You are recording objective facts about a single job posting. Every fact must be one the posting itself states or clearly implies. Never speculate about who might apply, and never infer a fact from an adjacent mention -- a posting that names a technology in passing does not thereby require it.

Where the posting is silent, say so with the field's stated "unknown" or empty value. Silence is not a "no".

Return ONLY JSON with exactly these keys and no markdown fences:
{shape}

{fields}

Posting title: {posting.title}
Company: {posting.company}
Location label: {posting.location}
Remote flag: {posting.remote}
Posting content confidence: {posting.content_confidence} — {tier_hint}
Posting text:
{posting.description}
"""


def _require_str(data: dict, key: str, allowed: frozenset[str]) -> str:
    """Read one enumerated facet, tolerating casing and surrounding space.

    Normalising before validating matters more here than it looks: a rejected
    value fails the *whole* response, so "Senior" instead of "senior" would
    throw away six correctly-read facets over a capitalisation. Genuine
    out-of-vocabulary values still fail -- a value nobody can interpret must
    not be silently recorded as "the posting did not say".
    """
    value = data.get(key)
    if isinstance(value, str):
        value = value.strip().lower()
    if value not in allowed:
        raise FacetExtractionError(f"{key} {value!r} must be one of {sorted(allowed)}")
    return value


def _parse_regions(value: object) -> list[str]:
    if not isinstance(value, list):
        raise FacetExtractionError("hiring_regions must be a list")
    regions: list[str] = []
    for region in value:
        if isinstance(region, str):
            region = region.strip().lower()
        if region not in VALID_REGIONS:
            raise FacetExtractionError(
                f"hiring_regions entry {region!r} must be one of {sorted(VALID_REGIONS)}"
            )
        if region not in regions:
            regions.append(region)
    return regions


def _parse_stack(value: object) -> list[str]:
    if not isinstance(value, list):
        raise FacetExtractionError("stack must be a list")
    stack: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise FacetExtractionError("each stack entry must be a non-empty string")
        # Lower-cased, not merely stripped: the point of this facet is that
        # two postings naming the same technology compare equal, and "React"
        # alongside "react" would defeat every later filter on it. The prompt
        # asks for lower case; this is what makes it true.
        normalized = entry.strip().lower()
        if normalized not in stack:
            stack.append(normalized)
    return stack


def _parse_amount(value: object, label: str) -> int | None:
    """Read one compensation bound, accepting the shapes JSON actually carries.

    A model asked for an integer returns `90000`, `90000.0` or `"90000"`
    depending on the day. All three name the same amount, and rejecting the
    last two would discard the whole response -- every other facet with it --
    over a serialisation detail. A fractional amount is not coerced: rounding
    someone's pay silently is worse than failing to read it.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise FacetExtractionError(f"compensation.{label} must be a number or null")
    if isinstance(value, str):
        try:
            value = float(value.strip().replace(",", ""))
        except ValueError:
            raise FacetExtractionError(
                f"compensation.{label} must be a number or null"
            ) from None
    if isinstance(value, float):
        if not value.is_integer():
            raise FacetExtractionError(
                f"compensation.{label} must be a whole number of currency units"
            )
        value = int(value)
    if not isinstance(value, int):
        raise FacetExtractionError(f"compensation.{label} must be a number or null")
    if value < 0:
        raise FacetExtractionError(f"compensation.{label} must not be negative")
    return value


def _parse_compensation(value: object) -> Compensation:
    if not isinstance(value, dict):
        raise FacetExtractionError("compensation must be an object")
    disclosed = value.get("disclosed")
    if not isinstance(disclosed, bool):
        raise FacetExtractionError("compensation.disclosed must be a boolean")
    if not disclosed:
        # Whatever else came back is discarded rather than rejected: a figure
        # alongside "the posting discloses nothing" is a contradiction the
        # posting cannot settle, and the conservative half is the safe one.
        return Compensation()

    minimum = _parse_amount(value.get("minimum"), "minimum")
    maximum = _parse_amount(value.get("maximum"), "maximum")
    if minimum is None and maximum is None:
        raise FacetExtractionError("disclosed compensation must carry a minimum or a maximum")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise FacetExtractionError("compensation.minimum must not exceed compensation.maximum")

    currency = value.get("currency")
    if not isinstance(currency, str) or not currency.strip():
        raise FacetExtractionError("disclosed compensation must name a currency")

    period = value.get("period")
    if isinstance(period, str):
        period = period.strip().lower()
    if period not in VALID_PERIODS:
        raise FacetExtractionError(
            f"compensation.period {period!r} must be one of {sorted(VALID_PERIODS)}"
        )

    return Compensation(
        disclosed=True,
        currency=currency.strip().upper(),
        minimum=minimum,
        maximum=maximum,
        period=period,
    )


def _parse_requirements(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise FacetExtractionError("requirements must be a list")
    parsed: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            raise FacetExtractionError("each requirements entry must be an object")
        requirement = item.get("requirement")
        if not isinstance(requirement, str) or not requirement.strip():
            raise FacetExtractionError("requirements.requirement must be a non-empty string")
        depth = item.get("depth")
        if isinstance(depth, str):
            depth = depth.strip().lower()
        if depth not in VALID_DEPTHS:
            raise FacetExtractionError(
                f"requirements.depth {depth!r} must be one of {sorted(VALID_DEPTHS)}"
            )
        kind = item.get("kind")
        if isinstance(kind, str):
            kind = kind.strip().lower()
        if kind not in VALID_REQUIREMENT_KINDS:
            raise FacetExtractionError(
                f"requirements.kind {kind!r} must be one of {sorted(VALID_REQUIREMENT_KINDS)}"
            )
        parsed.append(
            {"requirement": requirement.strip(), "depth": depth, "kind": kind}
        )
    return parsed


_PARSERS = {
    "seniority": lambda data: _require_str(data, "seniority", VALID_SENIORITY),
    "remote_policy": lambda data: _require_str(data, "remote_policy", VALID_REMOTE_POLICY),
    "relocation_policy": lambda data: _require_str(
        data, "relocation_policy", VALID_RELOCATION_POLICY
    ),
    "hiring_regions": lambda data: _parse_regions(data.get("hiring_regions")),
    "stack": lambda data: _parse_stack(data.get("stack")),
    "compensation": lambda data: _parse_compensation(data.get("compensation")),
    "requirements": lambda data: _parse_requirements(data.get("requirements")),
}


def _parse_facets(raw: str, requested: list[str]) -> dict[str, Any]:
    try:
        data = json.loads(_strip_code_fences(raw))
    except json.JSONDecodeError as exc:
        raise FacetExtractionError(f"provider returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FacetExtractionError("provider response must be a JSON object")
    # Only the requested keys are read. A key that was not asked for is a
    # facet already established from structured data, and that answer wins.
    return {name: _PARSERS[name](data) for name in requested}


def extract_facets(posting: PostingFacts, gemini: "GeminiClient") -> JobFacets:
    """Read `posting`'s objective facets, asking the model only for the residue.

    Raises `FacetExtractionError` when the response cannot be read as facets.
    Callers must leave the job unenriched on that error rather than recording
    a partial or placeholder answer: an unreadable response says nothing about
    the posting, and the next run has to be free to try again.
    """
    supplied = source_supplied_facets(posting)
    requested = [name for name in FACET_FIELDS if name not in supplied]

    raw = gemini.generate_text(
        _build_facet_prompt(posting, requested),
        purpose="job_facets",
        thinking_level="low",
        max_output_tokens=4000,
        json_mode=True,
        max_attempts=_FACET_MAX_ATTEMPTS,
    )
    values = _parse_facets(raw, requested)
    values.update(supplied)

    return JobFacets(
        seniority=values["seniority"],
        remote_policy=values["remote_policy"],
        relocation_policy=values["relocation_policy"],
        hiring_regions=values["hiring_regions"],
        stack=values["stack"],
        compensation=values["compensation"],
        requirements=values["requirements"],
        source_supplied=sorted(supplied),
        model=gemini.model,
    )

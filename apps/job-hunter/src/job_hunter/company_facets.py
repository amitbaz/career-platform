"""Objective extraction: read what kind of company is hiring, once, for everyone.

A **company facet** is one structured objective fact about an employer --
what industry it operates in, what its business model is, how mature it is,
roughly how large it is, and where it is headquartered. None of that depends
on who is looking, so one extraction serves every user and every later run
(issue #198).

This is the same split `facets.py` makes for a posting, applied one level up,
and it is the cheapest cache the engine has: a posting's facets are amortised
over one posting, a company's over every role that employer posts for as long
as the facts hold. The reference run's eligible set was roughly 1,263
postings across a far smaller number of employers.

Three things make that sharing real, and all three are enforced by the
interface rather than by care:

* `extract_company_facets` takes a `CompanyEvidence` and a provider client.
  There is no parameter anything per-user could arrive through.
* `CompanyEvidence` is frozen and carries only what the engine already knows
  about the employer -- its name, the hosts its postings live on, and the
  advertisements themselves, which are shared data.
* This module imports nothing per-user, and a test in
  `tests/test_company_facets.py` fails if that ever changes.

A company is identified by `job_identity.normalize_company_name` -- the
suffix-stripping normalization `job_hunter_company_watch` and `canonical.py`
already key an *employer* on -- and not by `normalize.normalize_text`, under
which "Acme Ltd" and "Acme" are two different employers.

Unknown is a first-class value throughout. A company nobody has read yet, or
one whose facts could not be established, scores neutrally: a missing fact
must never be read as a negative one, and must never block a posting from
being scored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from job_hunter.ai import (
    AIBudgetExceeded,
    AIQuotaPaused,
    AITemporaryCapacity,
    CallClass,
    CredentialUnavailable,
    PlatformAllowanceExhausted,
)
from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.hiring_scope import (
    ASIA_PACIFIC,
    EUROPE,
    MIDDLE_EAST,
    NORTH_AMERICA,
)
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import CompanyFacets

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider

UNKNOWN = "unknown"

#: What the employer sells into. Small enough to be meaningful, open enough
#: to admit an unknown: the point of a controlled vocabulary is that a query
#: can filter on it, which a paragraph of prose cannot.
VALID_INDUSTRIES = frozenset(
    {
        "software",
        "fintech",
        "healthtech",
        "biotech",
        "retail",
        "media",
        "gaming",
        "education",
        "energy",
        "logistics",
        "travel",
        "mobility",
        "real_estate",
        "security",
        "telecom",
        "manufacturing",
        "agriculture",
        "government",
        "nonprofit",
        "professional_services",
        UNKNOWN,
    }
)

#: How it makes money. This is the dimension the nearest competitor markets
#: against title matching, and the one a candidate feels most sharply: a
#: consultancy and a seed-stage product company can advertise the identical
#: role.
VALID_BUSINESS_MODELS = frozenset(
    {
        "b2b_saas",
        "b2c_product",
        "marketplace",
        "ecommerce",
        "consultancy",
        "agency",
        "hardware",
        "deep_tech",
        "open_source",
        "nonprofit",
        UNKNOWN,
    }
)

#: Maturity. Funding *history* is out of scope (see the issue); this is the
#: single coarse stage the company is at now.
VALID_STAGES = frozenset(
    {
        "pre_seed",
        "seed",
        "series_a",
        "series_b",
        "series_c_plus",
        "growth",
        "public",
        "bootstrapped",
        "established",
        UNKNOWN,
    }
)

#: Approximate headcount, as a band rather than a number. A number would
#: imply a precision nothing here can supply, and would be re-derived every
#: time it drifted by one.
VALID_SIZE_BANDS = frozenset(
    {
        "1_10",
        "11_50",
        "51_200",
        "201_500",
        "501_1000",
        "1001_5000",
        "5001_plus",
        UNKNOWN,
    }
)

#: The region vocabulary is `hiring_scope`'s, exactly as `facets.py` uses it
#: for a posting's hiring regions: one notion of a region, or the facet means
#: two different things depending on who filled it in.
VALID_HQ_REGIONS = frozenset({NORTH_AMERICA, EUROPE, MIDDLE_EAST, ASIA_PACIFIC, UNKNOWN})

#: The facets, in the order they are asked for and rendered.
COMPANY_FACET_FIELDS = (
    "industry",
    "business_model",
    "stage",
    "size_band",
    "headquarters_region",
)

#: Country-code top-level domains, mapped into `hiring_scope`'s regions. Read
#: only off a host the *employer* owns -- see `_employer_hosts` -- because an
#: ATS vendor's domain says where the vendor is, not where its customer is.
_CCTLD_REGIONS = {
    # Europe
    "de": EUROPE, "at": EUROPE, "ch": EUROPE, "nl": EUROPE, "fr": EUROPE,
    "es": EUROPE, "pt": EUROPE, "it": EUROPE, "pl": EUROPE, "se": EUROPE,
    "no": EUROPE, "dk": EUROPE, "fi": EUROPE, "ie": EUROPE, "uk": EUROPE,
    "be": EUROPE, "cz": EUROPE, "gr": EUROPE, "ro": EUROPE, "hu": EUROPE,
    "ee": EUROPE, "lt": EUROPE, "lv": EUROPE, "sk": EUROPE, "si": EUROPE,
    "bg": EUROPE, "hr": EUROPE, "is": EUROPE, "lu": EUROPE,
    # North America
    "us": NORTH_AMERICA, "ca": NORTH_AMERICA, "mx": NORTH_AMERICA,
    # Middle East
    "il": MIDDLE_EAST, "ae": MIDDLE_EAST, "sa": MIDDLE_EAST,
    "tr": MIDDLE_EAST, "qa": MIDDLE_EAST,
    # Asia-Pacific
    "au": ASIA_PACIFIC, "nz": ASIA_PACIFIC, "jp": ASIA_PACIFIC,
    "sg": ASIA_PACIFIC, "in": ASIA_PACIFIC, "hk": ASIA_PACIFIC,
    "kr": ASIA_PACIFIC, "cn": ASIA_PACIFIC, "id": ASIA_PACIFIC,
    "my": ASIA_PACIFIC, "th": ASIA_PACIFIC, "ph": ASIA_PACIFIC,
    "vn": ASIA_PACIFIC, "tw": ASIA_PACIFIC,
}

#: How much of the employer's advertisements the prompt carries. The model is
#: being asked five coarse questions, not to read a corpus: a handful of
#: titles and locations plus one advertisement's opening establishes what the
#: company does far more cheaply than every posting it has ever run, and
#: keeps the prompt -- and so the answer -- stable as postings come and go.
_MAX_TITLES = 12
_MAX_LOCATIONS = 8
_MAX_EXCERPT_CHARS = 4000

# One retry for transient provider 5xx/timeout failures, matching `facets`.
_COMPANY_MAX_ATTEMPTS = 2
# One fresh sample when a completed response still fails the parser.
_COMPANY_PARSE_MAX_ATTEMPTS = 2


class CompanyFacetExtractionError(ValueError):
    """The provider returned something that is not a usable set of facets."""


def _host_labels(host: str) -> set[str]:
    """The host's dot-separated labels, reduced to letters and digits.

    "acme-payments.de" gives {"acmepayments", "de"}; "careers.acme.co.uk"
    gives {"careers", "acme", "co", "uk"}. Punctuation goes because a company
    writes its name in a domain with a hyphen, without one, or not at all.
    """
    return {re.sub(r"[^a-z0-9]", "", label) for label in host.split(".")} - {""}


def _employer_hosts(urls: Sequence[str], identity: str) -> tuple[str, ...]:
    """The hosts among `urls` that are demonstrably the employer's own.

    A host qualifies only when one of its labels *is* the company's name --
    "acme-payments.de" or "careers.acmepayments.com" for `acme payments`.
    Nothing else does, and that test is what makes the ccTLD rule below safe.

    Excluding ATS vendors is not enough on its own, and an earlier version of
    this function that did only that was wrong. `SUPPORTED_ATS_HOSTS` holds
    six hosts; the corpus is mostly *neither* the employer nor an ATS. A US
    company whose only posting the engine holds came from an Israeli job
    board would have had `devjobs.co.il` read as its own domain and been
    recorded as headquartered in the Middle East -- and because a supplied
    facet is never asked of the model, nothing would ever correct it, for
    every user, until the refresh interval expired and re-derived the same
    wrong answer. Requiring the domain to carry the company's name inverts
    the default: an unrecognised host supplies nothing rather than supplying
    a guess.

    It fails open in every direction. A company whose domain does not spell
    its name ("acmepay.de" for `acme payments`), a shortened brand, or a
    posting held only under an aggregator all yield no employer host at all,
    and the region is asked of the model like any other unestablished fact.
    """
    if not identity:
        return ()
    name = re.sub(r"[^a-z0-9]", "", identity)
    if not name:
        return ()
    hosts: list[str] = []
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if not host or host in hosts:
            continue
        if any(ats in host for ats in SUPPORTED_ATS_HOSTS):
            continue
        if name in _host_labels(host):
            hosts.append(host)
    return tuple(sorted(hosts))


@dataclass(frozen=True, slots=True)
class CompanyEvidence:
    """The employer, and only the employer.

    Frozen and built from the postings the engine already holds by
    `from_postings`. Everything in here is shared data -- the advertisement
    text, its host, its title -- so the prompt built from it is the same
    question whoever asks it, which is what lets one answer serve everyone.

    Every collection is a sorted, bounded tuple rather than whatever order
    the postings arrived in: the same employer has to produce a byte-identical
    prompt on every run, or the extraction is not reproducible and two runs
    can disagree about a company that has not changed.
    """

    identity: str
    display_name: str
    employer_hosts: tuple[str, ...]
    ats_providers: tuple[str, ...]
    posting_titles: tuple[str, ...]
    posting_locations: tuple[str, ...]
    posting_excerpt: str

    @classmethod
    def from_postings(cls, company: str, jobs: Sequence[Any]) -> "CompanyEvidence":
        """Assemble the evidence for one employer from its advertisements.

        `jobs` are `Job`s, typed loosely on purpose: this module must not be
        able to reach anything per-user, and the narrower the surface it names
        the less there is to go wrong. Only the posting's own fields are read.
        """
        titles = sorted({(job.title or "").strip() for job in jobs} - {""})
        locations = sorted({(job.location or "").strip() for job in jobs} - {""})
        providers = sorted(
            {(job.ats_provider or "").strip().lower() for job in jobs} - {""}
        )
        # The longest description available, not the first: the employer is
        # described in whichever advertisement bothered to say who they are,
        # and picking by length gets there without ranking by content
        # confidence a second time.
        descriptions = sorted(
            ((job.description or "").strip() for job in jobs),
            key=lambda text: (-len(text), text),
        )
        excerpt = descriptions[0][:_MAX_EXCERPT_CHARS] if descriptions else ""
        display = next(
            (
                (job.company or "").strip()
                for job in sorted(jobs, key=lambda job: (job.company or ""))
                if (job.company or "").strip()
            ),
            company.strip(),
        )
        return cls(
            identity=normalize_company_name(company),
            display_name=display,
            employer_hosts=_employer_hosts(
                [job.canonical_url or job.url or "" for job in jobs],
                normalize_company_name(company),
            ),
            ats_providers=tuple(providers),
            posting_titles=tuple(titles[:_MAX_TITLES]),
            posting_locations=tuple(locations[:_MAX_LOCATIONS]),
            posting_excerpt=excerpt,
        )


def source_supplied_company_facets(evidence: CompanyEvidence) -> dict[str, Any]:
    """Return the facets already known before any provider call is made.

    One is, today: `headquarters_region`, when every employer-owned host the
    engine has seen for this company carries a country-code top-level domain
    and they all name the same region. A company that runs its careers site on
    `.de` is headquartered in Europe, and asking the model to re-derive that
    would spend a call on a fact already in hand (#198, user story 10).

    Deliberately narrow, and it fails open in three separate ways: a gTLD
    supplies nothing, an ATS vendor's host is not an employer host at all, and
    two employer hosts disagreeing about the region supply nothing rather than
    picking one. A wrong fact here is never corrected, because a supplied
    facet is never asked of the model.
    """
    supplied: dict[str, Any] = {}
    regions = {
        _CCTLD_REGIONS[host.rsplit(".", 1)[-1]]
        for host in evidence.employer_hosts
        if host.rsplit(".", 1)[-1] in _CCTLD_REGIONS
    }
    if len(regions) == 1:
        supplied["headquarters_region"] = regions.pop()
    return supplied


_FIELD_INSTRUCTIONS = {
    "industry": (
        '- "industry": one of ' + "|".join(sorted(VALID_INDUSTRIES)) + ". "
        "The sector the company itself operates in -- not the sector of a "
        "customer it names. Use unknown when the evidence does not establish it."
    ),
    "business_model": (
        '- "business_model": one of ' + "|".join(sorted(VALID_BUSINESS_MODELS)) + ". "
        "How the company makes money. consultancy and agency mean it sells its "
        "people's time to client companies; b2b_saas, b2c_product, marketplace "
        "and ecommerce mean it sells its own product. Use unknown when the "
        "evidence does not establish it."
    ),
    "stage": (
        '- "stage": one of ' + "|".join(sorted(VALID_STAGES)) + ". "
        "How mature the company is now. bootstrapped means privately funded "
        "with no venture rounds; established means a mature private company "
        "past any venture stage; public means listed. Use unknown when the "
        "evidence does not establish it."
    ),
    "size_band": (
        '- "size_band": one of ' + "|".join(sorted(VALID_SIZE_BANDS)) + ", "
        "an approximate employee headcount band. Use unknown when the evidence "
        "does not establish it."
    ),
    "headquarters_region": (
        '- "headquarters_region": one of '
        + "|".join(sorted(VALID_HQ_REGIONS))
        + ". Where the company is headquartered or primarily based -- not "
        "where it happens to be hiring. Use unknown when the evidence does "
        "not establish it."
    ),
}

_COMPANY_SCHEMAS = {
    "industry": {"type": "STRING", "enum": sorted(VALID_INDUSTRIES)},
    "business_model": {"type": "STRING", "enum": sorted(VALID_BUSINESS_MODELS)},
    "stage": {"type": "STRING", "enum": sorted(VALID_STAGES)},
    "size_band": {"type": "STRING", "enum": sorted(VALID_SIZE_BANDS)},
    "headquarters_region": {"type": "STRING", "enum": sorted(VALID_HQ_REGIONS)},
}


def _company_response_schema(requested: list[str]) -> dict[str, Any]:
    """Describe exactly the residue `_parse_company_facets` will read."""
    return {
        "type": "OBJECT",
        "properties": {name: _COMPANY_SCHEMAS[name] for name in requested},
        "required": list(requested),
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


def _build_company_prompt(evidence: CompanyEvidence, requested: list[str]) -> str:
    """Render the prompt for exactly the facts still to be established.

    A fact already supplied is not in `requested` and so is not mentioned at
    all, for the same reason `facets.py` omits one: asking would spend tokens
    re-deriving something already held, and invite an answer that contradicts
    it.
    """
    fields = "\n".join(_FIELD_INSTRUCTIONS[name] for name in requested)
    shape = "{" + ", ".join(f'"{name}": ...' for name in requested) + "}"
    return f"""You are recording objective facts about an employer, from the job advertisements it has published. Every fact must be one the evidence states or clearly implies about the company itself. Never speculate about who might apply, and never describe the role instead of the company.

Where the evidence does not establish a fact, answer "unknown". A guess is worse than an unknown here: an unknown is treated as "not yet established" and costs nothing, while a wrong value is reused for months.

Return ONLY JSON with exactly these keys and no markdown fences:
{shape}

{fields}

Company: {evidence.display_name}
Company websites: {", ".join(evidence.employer_hosts) or "none seen"}
Applicant tracking systems in use: {", ".join(evidence.ats_providers) or "none seen"}
Roles it is advertising: {"; ".join(evidence.posting_titles) or "none seen"}
Locations it advertises in: {"; ".join(evidence.posting_locations) or "none seen"}
One of its advertisements:
{evidence.posting_excerpt}
"""


def _require_str(data: dict, key: str, allowed: frozenset[str]) -> str:
    """Read one enumerated facet, tolerating casing and surrounding space.

    Normalising before validating matters for the same reason it does in
    `facets.py`: a rejected value fails the *whole* response, so "B2B SaaS"
    instead of "b2b_saas" would throw away four correctly-read facts over a
    capitalisation. A genuinely out-of-vocabulary value still fails -- a value
    nobody can filter on must not be stored, and must not be silently
    recorded as "we do not know", which is a different claim.
    """
    value = data.get(key)
    if isinstance(value, str):
        value = value.strip().lower().replace(" ", "_").replace("-", "_")
    if value not in allowed:
        raise CompanyFacetExtractionError(
            f"{key} {value!r} must be one of {sorted(allowed)}"
        )
    return value


_PARSERS = {
    name: (lambda data, name=name, allowed=allowed: _require_str(data, name, allowed))
    for name, allowed in (
        ("industry", VALID_INDUSTRIES),
        ("business_model", VALID_BUSINESS_MODELS),
        ("stage", VALID_STAGES),
        ("size_band", VALID_SIZE_BANDS),
        ("headquarters_region", VALID_HQ_REGIONS),
    )
}


def _parse_company_facets(raw: str, requested: list[str]) -> dict[str, Any]:
    try:
        data = json.loads(_strip_code_fences(raw))
    except json.JSONDecodeError as exc:
        raise CompanyFacetExtractionError(
            f"provider returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise CompanyFacetExtractionError("provider response must be a JSON object")
    # Only the requested keys are read. A key that was not asked for is a
    # fact already established from structured data, and that answer wins.
    return {name: _PARSERS[name](data) for name in requested}


def extract_company_facets(
    evidence: CompanyEvidence, ai: "AIProvider"
) -> CompanyFacets:
    """Read `evidence`'s company facets, asking the model only for the residue.

    Raises `CompanyFacetExtractionError` when the response cannot be read.
    Callers must leave the company unenriched on that error rather than
    recording a partial or placeholder answer: an unreadable response says
    nothing about the company, and the next run has to be free to try again.
    An unenriched company is not a problem for anyone -- its postings still
    score, with the company dimensions neutral.

    Raises `PlatformAllowanceExhausted` when the platform key cannot fund the
    call. That is not a failure and must not be handled as one: nothing was
    read, so there is nothing to record, and a later run reads it.
    """
    supplied = source_supplied_company_facets(evidence)
    requested = [name for name in COMPANY_FACET_FIELDS if name not in supplied]

    # Shared, objective work, so it is funded by the platform key and metered
    # in the platform ledger (#128) -- the same call class posting extraction
    # declares. The class is the whole of that decision: it selects the
    # credential and the quota inside the port, and there is no argument here
    # through which a user's key could be reached instead.
    prompt = _build_company_prompt(evidence, requested)
    schema = _company_response_schema(requested)
    for parse_attempt in range(1, _COMPANY_PARSE_MAX_ATTEMPTS + 1):
        try:
            raw = ai.generate_text(
                prompt,
                call_class=CallClass.SHARED_EXTRACTION,
                purpose="company_facets",
                thinking_level="low",
                max_output_tokens=1000,
                json_mode=True,
                json_schema=schema,
                max_attempts=_COMPANY_MAX_ATTEMPTS,
            )
        except AITemporaryCapacity:
            # Rolling capacity, not the allowance: the platform key has budget
            # left and this call may go through in a moment. The caller decides
            # whether to wait, so this passes through untranslated.
            raise
        except (AIBudgetExceeded, AIQuotaPaused, CredentialUnavailable) as exc:
            # Our own daily ceiling, the provider's persisted pause, and no
            # platform key at all, with one meaning for every caller: the
            # platform cannot pay to read this company today, and nobody else
            # may be asked to. Translated here for the same reason
            # `facets.extract_facets` translates it -- so a caller cannot
            # mistake the platform's exhaustion for the user's own.
            raise PlatformAllowanceExhausted(
                f"the platform key cannot fund company extraction: {exc}"
            ) from exc

        try:
            values = _parse_company_facets(raw, requested)
        except CompanyFacetExtractionError:
            if parse_attempt == _COMPANY_PARSE_MAX_ATTEMPTS:
                raise
        else:
            break

    values.update(supplied)

    return CompanyFacets(
        identity=evidence.identity,
        display_name=evidence.display_name,
        industry=values["industry"],
        business_model=values["business_model"],
        stage=values["stage"],
        size_band=values["size_band"],
        headquarters_region=values["headquarters_region"],
        source_supplied=sorted(supplied),
        model=ai.model,
    )

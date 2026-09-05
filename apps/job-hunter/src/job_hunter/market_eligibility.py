"""Conservative pre-Gemini market eligibility checks.

This module only rejects a job when a market's policy is *explicitly*
contradicted by the posting (disallowed required language, salary clearly
below the market floor, sponsorship explicitly unavailable where required,
a non-permanent employment type, or an Israel role that is explicitly
onsite/hybrid/location-bound). Anything the posting simply omits is kept and
marked "unknown" rather than rejected -- this stage exists to save Gemini
requests on unambiguous incompatibilities, not to make nuanced calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from job_hunter.market_policy import salary_floor_for_job
from job_hunter.models import Job, MarketPolicy
from job_hunter.normalize import normalize_text


@dataclass(frozen=True, slots=True)
class MarketEligibilityResult:
    allowed: bool
    reason_code: str = "passed"
    reason: str = "passed market eligibility"
    sponsorship_status: str = "not_applicable"
    international_remote_status: str = "not_applicable"
    warnings: tuple[str, ...] = ()
    disclosed_salary_max: int | None = None


_EMPLOYMENT_TYPE_BLOCKERS = (
    "freelance",
    "contractor",
    "part-time",
    "internship",
    "fixed-term",
    "fixed term",
    "temporary role",
)

_NO_SPONSORSHIP = (
    "no visa sponsorship",
    "cannot sponsor",
    "unable to sponsor",
    "without sponsorship",
    "must already be authorized to work",
    "must have the right to work",
)

_SPONSORSHIP_AVAILABLE = (
    "visa sponsorship available",
    "visa sponsorship provided",
    "we sponsor",
    "skilled worker sponsorship",
    "employment pass sponsorship",
)

_ONSITE_OR_HYBRID_MARKERS = ("onsite", "on-site", "in-office", "hybrid")
_INTERNATIONAL_REMOTE_MARKERS = ("worldwide", "global", "anywhere", "international remote")

# Cues that flip an onsite/hybrid mention into a *denial* of that work mode
# ("not a hybrid company", "no onsite requirement"). Matched only inside the
# same comma-delimited clause as the marker, so "we are not remote, this is a
# hybrid role" still blocks.
_WORK_MODE_NEGATION_CUES = ("not", "no", "never", "without", "nor", "zero")

_TIMEZONE_MARKERS = ("time zone", "time-zone", "timezone", "core hours", "overlap")

_STRONG_LANGUAGE_MARKERS = (
    "required",
    "must speak",
    "fluent",
    "professional proficiency",
    "c1",
    "c2",
)
_OPTIONAL_LANGUAGE_MARKERS = (
    "nice to have",
    "preferred",
    "plus",
    "bonus",
    "advantage",
    "optional",
    "not required",
    "not necessary",
    "not essential",
    "not mandatory",
    "no need",
    "not a requirement",
)

# Known non-English languages the profile is not assumed to speak. A market's
# own `allowed_languages` (e.g. Israel's English/Hebrew) removes an entry
# from consideration for that market.
_KNOWN_LANGUAGE_KEYWORDS = {
    "german": "German",
    "french": "French",
    "dutch": "Dutch",
    "spanish": "Spanish",
    "italian": "Italian",
    "portuguese": "Portuguese",
    "polish": "Polish",
    "swedish": "Swedish",
    "danish": "Danish",
    "norwegian": "Norwegian",
    "finnish": "Finnish",
    "mandarin": "Mandarin",
    "chinese": "Mandarin",
    "japanese": "Japanese",
    "korean": "Korean",
    "arabic": "Arabic",
    "russian": "Russian",
    "turkish": "Turkish",
    "hebrew": "Hebrew",
}

_SALARY_BASE_MARKERS = ("base salary", "base pay", "gross base")
_SALARY_RANGE_RE = re.compile(r"\d[\d,]*\s*k?\s*(?:-|–|—|to)\s*\d[\d,]*\s*k?")
_MONTHLY_MARKERS = ("/month", "per month", "monthly", "a month")
_NUMBER_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(k)?\b", re.IGNORECASE)

_CURRENCY_MARKERS = {
    "EUR": ("€", "eur"),
    "GBP": ("£", "gbp"),
    "ILS": ("₪", "ils", "nis"),
    "SGD": ("s$", "sgd"),
    "USD": ("$", "usd", "us$"),
}

_SENTENCE_SPLIT_RE = re.compile(r"[.!?;\n]+")


def evaluate_market_eligibility(job: Job, market: MarketPolicy) -> MarketEligibilityResult:
    """Conservatively evaluate whether a job is explicitly incompatible with a market.

    Only explicit contradictions block; anything omitted from the posting
    keeps the job and is reported as "unknown" via the status fields.
    """
    warnings = _timezone_warnings(job)
    disclosed_salary_max = _disclosed_salary_max(job, market)

    if market.remote_policy == "required":
        work_mode_blocked, international_remote_status = _remote_required_work_mode(job, market)
    else:
        work_mode_blocked = False
        international_remote_status = "not_applicable"

    if market.sponsorship_policy == "required":
        sponsorship_status, sponsorship_blocked, sponsorship_reason = _sponsorship_status(job)
    else:
        sponsorship_status, sponsorship_blocked, sponsorship_reason = "not_applicable", False, ""

    employment_blocker = _blocked_employment_type(job)
    disallowed_language = _required_disallowed_language(job, market)

    salary_floor = salary_floor_for_job(job, market)
    salary_blocked = disclosed_salary_max is not None and disclosed_salary_max < salary_floor

    def _blocked(reason_code: str, reason: str) -> MarketEligibilityResult:
        return MarketEligibilityResult(
            allowed=False,
            reason_code=reason_code,
            reason=reason,
            sponsorship_status=sponsorship_status,
            international_remote_status=international_remote_status,
            warnings=warnings,
            disclosed_salary_max=disclosed_salary_max,
        )

    if employment_blocker is not None:
        return _blocked(
            "employment_type_blocked",
            f"explicit {employment_blocker} employment type is out of scope",
        )

    if disallowed_language is not None:
        return _blocked(
            "required_language",
            f"{disallowed_language} is required but is not an allowed language for this market",
        )

    if work_mode_blocked:
        return _blocked(
            "work_mode_incompatible",
            "explicit onsite/hybrid or physical-presence requirement is incompatible "
            "with this market's remote-only policy",
        )

    if sponsorship_blocked:
        return _blocked("sponsorship_unavailable", sponsorship_reason)

    if salary_blocked:
        return _blocked(
            "salary_below_floor",
            f"disclosed base salary max {disclosed_salary_max} is below the "
            f"{market.salary.currency} {salary_floor} floor",
        )

    return MarketEligibilityResult(
        allowed=True,
        sponsorship_status=sponsorship_status,
        international_remote_status=international_remote_status,
        warnings=warnings,
        disclosed_salary_max=disclosed_salary_max,
    )


def _sentences(text: str) -> list[str]:
    """Split raw text into clauses on `.!?;` and newlines, each normalized.

    Splitting happens on the raw text *before* normalization, because
    `normalize_text` collapses newlines into plain spaces -- doing the split
    afterward would silently merge separate bullet-list lines (or
    period-less lines) into one "sentence", letting a strong marker on one
    line falsely attach to an unrelated language mention on another.
    """
    if not text:
        return []
    fragments = (normalize_text(part) for part in _SENTENCE_SPLIT_RE.split(text))
    return [fragment for fragment in fragments if fragment]


def _phrase_in(phrase: str, text: str) -> bool:
    if not text:
        return False
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def _clauses(sentence: str) -> list[str]:
    """Split an already-normalized sentence into comma-delimited clauses.

    A requirement marker only governs the clause it sits in: in "Fluent
    English required, German not necessary" the ``required`` belongs to
    English, and German is explicitly waived. Scoping to the clause keeps the
    marker attached to the language it actually qualifies.
    """
    return [clause.strip() for clause in sentence.split(",") if clause.strip()]


def _required_disallowed_language(job: Job, market: MarketPolicy) -> str | None:
    allowed = {normalize_text(lang) for lang in market.allowed_languages}
    for sentence in _sentences(f"{job.title} {job.description}"):
        for clause in _clauses(sentence):
            if any(_phrase_in(marker, clause) for marker in _OPTIONAL_LANGUAGE_MARKERS):
                continue
            if not any(_phrase_in(marker, clause) for marker in _STRONG_LANGUAGE_MARKERS):
                continue
            for keyword, canonical in _KNOWN_LANGUAGE_KEYWORDS.items():
                if normalize_text(canonical) in allowed:
                    continue
                if _phrase_in(keyword, clause):
                    return canonical
    return None


def _blocked_employment_type(job: Job) -> str | None:
    text = normalize_text(f"{job.title} {job.description}")
    for phrase in _EMPLOYMENT_TYPE_BLOCKERS:
        if _phrase_in(phrase, text):
            return phrase
    return None


def _sponsorship_status(job: Job) -> tuple[str, bool, str]:
    text = normalize_text(f"{job.title} {job.description}")
    if any(_phrase_in(phrase, text) for phrase in _NO_SPONSORSHIP):
        return "unavailable", True, "sponsorship is explicitly unavailable in a market that requires it"
    if any(_phrase_in(phrase, text) for phrase in _SPONSORSHIP_AVAILABLE):
        return "available", False, ""
    return "unknown", False, ""


def _asserts_onsite_or_hybrid(text: str) -> bool:
    """Return True only when a clause *asserts* an onsite/hybrid work mode.

    The check is sentence- and clause-scoped (never a whole-description
    substring scan) and negation-aware, so "we are fully remote, not a hybrid
    company" or "no onsite requirement" is not read as an onsite requirement.
    Ambiguity fails open: a negated-looking clause keeps the job.
    """
    for sentence in _sentences(text):
        for clause in _clauses(sentence):
            for marker in _ONSITE_OR_HYBRID_MARKERS:
                index = clause.find(marker)
                if index == -1:
                    continue
                before = clause[:index]
                if any(_phrase_in(cue, before) for cue in _WORK_MODE_NEGATION_CUES):
                    continue
                return True
    return False


def _remote_required_work_mode(job: Job, market: MarketPolicy) -> tuple[bool, str]:
    location_text = normalize_text(job.location or "")
    description_text = normalize_text(job.description or "")
    combined = f"{location_text} {description_text}"

    must_be_based_phrases = [f"must be based in {normalize_text(loc)}" for loc in market.locations]
    explicit_location_bound = any(_phrase_in(phrase, combined) for phrase in must_be_based_phrases)

    explicit_onsite_or_hybrid = job.remote is False or _asserts_onsite_or_hybrid(
        f"{job.location or ''}\n{job.description or ''}"
    )

    if explicit_location_bound or explicit_onsite_or_hybrid:
        return True, "not_applicable"

    if any(marker in combined for marker in _INTERNATIONAL_REMOTE_MARKERS):
        return False, "available"

    return False, "unknown"


def _timezone_warnings(job: Job) -> tuple[str, ...]:
    warnings = []
    for sentence in _sentences(f"{job.title} {job.description}"):
        if any(marker in sentence for marker in _TIMEZONE_MARKERS):
            warnings.append(f"time-zone/overlap expectation noted: {sentence}")
    return tuple(warnings)


def _has_currency_adjacent_range(sentence: str, currency_markers: tuple[str, ...]) -> bool:
    """Return True when a `number-number` range is directly money-marked.

    A bare numeric range is *not* salary evidence: perks prose such as "a
    $1,000 learning budget and 25-30 days of vacation" carries both a currency
    symbol and a numeric range without disclosing any pay. Only a range whose
    own edges touch a currency marker ("$150,000-180,000", "100k-120k EUR")
    counts, so an undisclosed salary can never become a false blocker.
    """
    for match in _SALARY_RANGE_RE.finditer(sentence):
        before = sentence[max(0, match.start() - 8) : match.start()].rstrip()
        after = sentence[match.end() : match.end() + 8].lstrip()
        if any(before.endswith(marker) for marker in currency_markers):
            return True
        if any(after.startswith(marker) for marker in currency_markers):
            return True
    return False


def _disclosed_salary_max(job: Job, market: MarketPolicy) -> int | None:
    currency_markers = _CURRENCY_MARKERS.get(market.salary.currency, ())
    if not currency_markers:
        return None

    best: float | None = None
    for sentence in _sentences(f"{job.title} {job.description}"):
        if not any(marker in sentence for marker in currency_markers):
            continue
        has_base_marker = any(_phrase_in(marker, sentence) for marker in _SALARY_BASE_MARKERS)
        if not (has_base_marker or _has_currency_adjacent_range(sentence, currency_markers)):
            continue

        amounts = []
        for match in _NUMBER_RE.finditer(sentence):
            raw, k = match.groups()
            value = float(raw.replace(",", ""))
            if k:
                value *= 1000
            amounts.append(value)
        if not amounts:
            continue

        amount = max(amounts)
        if any(marker in sentence for marker in _MONTHLY_MARKERS):
            amount *= 12

        if best is None or amount > best:
            best = amount

    return int(best) if best is not None else None

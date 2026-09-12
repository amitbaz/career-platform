"""Source-independent normalization and identity helpers for job records."""

from __future__ import annotations

import re


_SAFE_LEGAL_SUFFIXES = {
    "gmbh",
    "ag",
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "llc",
    "corp",
    "corporation",
}
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> list[str]:
    return _WORD_RE.findall(value.lower())


def normalize_company_name(value: str) -> str:
    """Normalize a company name while removing only trailing safe legal suffixes."""
    tokens = _tokens(value)
    while tokens and tokens[-1] in _SAFE_LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_job_title(value: str) -> str:
    """Normalize title whitespace, punctuation, and case without fuzzy matching."""
    return " ".join(_tokens(value))


def normalize_location(value: str) -> str:
    """Normalize a location to lowercase alphanumeric words."""
    return " ".join(_tokens(value))


def company_identity_key(company: str) -> str:
    """Return the stable identity key for a company name."""
    return normalize_company_name(company)


def job_fallback_identity(company: str, title: str, location: str) -> str | None:
    """Build a fallback identity when company and title are both present."""
    company_key = normalize_company_name(company)
    title_key = normalize_job_title(title)
    if not company_key or not title_key:
        return None
    return f"{company_key}|{title_key}|{normalize_location(location)}"


def locations_compatible(left: str, right: str) -> bool:
    """Check whether locations are equal or one contains the other as whole words."""
    left_tokens = normalize_location(left).split()
    right_tokens = normalize_location(right).split()
    if not left_tokens or not right_tokens:
        return True
    if left_tokens == right_tokens:
        return True
    return _contains_phrase(left_tokens, right_tokens) or _contains_phrase(right_tokens, left_tokens)


def _contains_phrase(tokens: list[str], phrase: list[str]) -> bool:
    width = len(phrase)
    return any(tokens[index : index + width] == phrase for index in range(len(tokens) - width + 1))

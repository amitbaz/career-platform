from __future__ import annotations
import hashlib
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
import re

_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gh_src", "lever-source", "source", "ref", "fbclid", "gclid",
})


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    clean_params = sorted(
        (k, v) for k, v in parse_qsl(parsed.query)
        if k not in _TRACKING_PARAMS
    )
    clean = parsed._replace(query=urlencode(clean_params), fragment="")
    return urlunparse(clean)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def ats_board_key(provider: str, board_identifier: str) -> str:
    """Return the canonical `"<provider>:<board>"` ATS denylist key.

    Case- and whitespace-insensitive on both halves, so a key built from a
    parsed job URL, a registry row, or an operator's config entry always
    compares equal. Matching only -- the registry still stores the board
    identifier in its original case, since ATS slugs are case-sensitive in
    URLs.
    """
    return f"{provider.strip().lower()}:{board_identifier.strip().lower()}"


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def job_fingerprint(job) -> str:
    # The ATS triple wins over (source, source_job_id) when all three are
    # present (#249). A board crawled directly and the same board rediscovered
    # through a company watch relabel every job it scans (`source =
    # f"watch:{provider}"`, sources/company_watch.py), so the old key --
    # scoped to `source` -- hashed one ATS job twice. The triple identifies
    # the advertisement itself, independent of which source label reached it,
    # which is exactly the property `ats_board_key` already relies on for the
    # denylist; reuse its provider/board normalization here so the two never
    # silently diverge on casing or stray whitespace. The job id is left
    # exactly as read, same as `parse_supported_ats_url` -- ATS job ids are
    # case-sensitive in URLs.
    if job.ats_provider and job.ats_board and job.ats_job_id:
        raw = f"id:{ats_board_key(job.ats_provider, job.ats_board)}:{job.ats_job_id}"
    elif job.source_job_id:
        raw = f"id:{job.source.lower()}:{job.source_job_id}"
    elif job.url:
        raw = f"url:{canonicalize_url(job.url)}"
    else:
        raw = "fallback:" + "|".join(
            normalize_text(v) for v in (job.company, job.title, job.location)
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

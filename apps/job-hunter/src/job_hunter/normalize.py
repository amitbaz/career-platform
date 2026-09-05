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


def description_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def job_fingerprint(job) -> str:
    if job.source_job_id:
        raw = f"id:{job.source.lower()}:{job.source_job_id}"
    elif job.url:
        raw = f"url:{canonicalize_url(job.url)}"
    else:
        raw = "fallback:" + "|".join(
            normalize_text(v) for v in (job.company, job.title, job.location)
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

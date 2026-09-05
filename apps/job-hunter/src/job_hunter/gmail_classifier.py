from __future__ import annotations

import json
import re
from dataclasses import replace
from email.utils import parseaddr
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from job_hunter.gemini_usage import GeminiBudgetExceeded, GeminiQuotaPaused
from job_hunter.gmail_models import (
    AUTO_CONFIDENCE_THRESHOLD,
    SUPPORTED_KINDS,
    ExtractedJob,
    GmailClassification,
    GmailMessage,
)
from job_hunter.normalize import canonicalize_url, normalize_text

if TYPE_CHECKING:
    from job_hunter.gemini import GeminiClient

_VISIBLE_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_LINKEDIN_JOB_ID_PATTERN = re.compile(r"/(?:comm/)?jobs/view/(\d+)(?:/|$)", re.IGNORECASE)
_KNOWN_PLATFORM_SENDERS = (
    "linkedin.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
)
_REQUIRED_CLASSIFICATION_FIELDS = frozenset({"kind", "confidence", "rationale"})
_JOB_FIELDS = frozenset(
    {
        "source_platform",
        "source_job_id",
        "url",
        "company",
        "title",
        "location",
        "remote",
        "description",
    }
)
_GMAIL_CLASSIFICATION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {"type": "STRING", "enum": sorted(SUPPORTED_KINDS)},
        "confidence": {"type": "NUMBER", "minimum": 0, "maximum": 1},
        "company": {"type": "STRING", "nullable": True},
        "role_title": {"type": "STRING", "nullable": True},
        "source_job_id": {"type": "STRING", "nullable": True},
        "job_urls": {
            "type": "ARRAY",
            "items": {"type": "STRING"},
        },
        "jobs": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "source_platform": {"type": "STRING"},
                    "source_job_id": {"type": "STRING", "nullable": True},
                    "url": {"type": "STRING"},
                    "company": {"type": "STRING", "nullable": True},
                    "title": {"type": "STRING", "nullable": True},
                    "location": {"type": "STRING", "nullable": True},
                    "remote": {"type": "BOOLEAN", "nullable": True},
                    "description": {"type": "STRING", "nullable": True},
                },
                "required": [
                    "source_platform",
                    "source_job_id",
                    "url",
                    "company",
                    "title",
                    "location",
                    "remote",
                    "description",
                ],
            },
        },
        "rationale": {"type": "STRING"},
    },
    "required": ["kind", "confidence", "rationale"],
}
_SCHEMA_INSTRUCTION = """Return one JSON object only with keys:
kind, confidence, company, role_title, source_job_id, job_urls, jobs, rationale.
kind must be one of JOB_ALERT, RECRUITER_CONTACT, APPLIED, INTERVIEW, TECHNICAL, OFFER, REJECTED, REVIEW_NEEDED, IRRELEVANT.
confidence must be a number from 0 to 1.
jobs must be an array of objects with source_platform, source_job_id, url, company, title, location, remote, description.
Do not infer facts not present in the email. Keep rationale under 160 characters.
"""
_JOB_ALERT_EXTRACTION_INSTRUCTION = """Deterministic rules identified this message as a JOB_ALERT.
Keep kind as JOB_ALERT and extract only job candidates evidenced by the email.
Use a URL only when it appears in the supplied email links or body.
"""
# Free-tier guardrail: keep the semantic prompt small. Classification only
# needs a representative slice of the body, not the whole email.
_SEMANTIC_BODY_CHAR_LIMIT = 6_000
_SEMANTIC_LINK_LIMIT = 20


class SemanticClassificationError(RuntimeError):
    """Raised when Gmail semantic classification fails for technical reasons."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _is_absolute_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _host_matches_domain(host: str, domain: str) -> bool:
    host = host.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _linkedin_job_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not _host_matches_domain(host, "linkedin.com"):
        return None
    match = _LINKEDIN_JOB_ID_PATTERN.search(parsed.path)
    return match.group(1) if match else None


def _normalize_url(url: str) -> str:
    linkedin_job_id = _linkedin_job_id(url)
    if linkedin_job_id:
        return f"https://www.linkedin.com/jobs/view/{linkedin_job_id}/"
    return canonicalize_url(url)


def _is_known_platform_host(host: str) -> bool:
    return any(_host_matches_domain(host, domain) for domain in _KNOWN_PLATFORM_SENDERS)


def _sender_address(message: GmailMessage) -> str:
    return parseaddr(message.sender)[1].lower()


def _is_known_platform_sender(message: GmailMessage) -> bool:
    address = _sender_address(message)
    _, separator, host = address.rpartition("@")
    return bool(separator) and _is_known_platform_host(host)


def _is_job_alert_sender(message: GmailMessage) -> bool:
    return _sender_address(message) == "jobalerts-noreply@linkedin.com"


def _message_urls(message: GmailMessage) -> list[str]:
    values = [*message.links, *_VISIBLE_URL_PATTERN.findall(message.body)]
    urls: list[str] = []
    for value in values:
        value = value.rstrip(".,;:!?)]}")
        if not _is_absolute_http_url(value):
            continue
        url = _normalize_url(value)
        if url not in urls:
            urls.append(url)
    return urls


def _job_url_platform(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    if _host_matches_domain(host, "linkedin.com") and "/jobs/view" in path:
        return "linkedin"
    if _host_matches_domain(host, "greenhouse.io"):
        return "greenhouse"
    if _host_matches_domain(host, "lever.co"):
        return "lever"
    if _host_matches_domain(host, "ashbyhq.com"):
        return "ashby"
    if _host_matches_domain(host, "workable.com"):
        return "workable"
    return None


def _known_jobs(message: GmailMessage) -> list[ExtractedJob]:
    jobs: list[ExtractedJob] = []
    for index, url in enumerate(_message_urls(message)):
        platform = _job_url_platform(url)
        if platform:
            jobs.append(
                ExtractedJob(
                    source_platform=platform,
                    source_job_id=_linkedin_job_id(url) if platform == "linkedin" else None,
                    url=url,
                    index=index,
                )
            )
    return jobs


def _classification(kind: str, message: GmailMessage, rationale: str) -> GmailClassification:
    jobs = _known_jobs(message)
    return GmailClassification(
        kind=kind,
        confidence=1.0,
        job_urls=[job.url for job in jobs],
        jobs=jobs,
        rationale=rationale,
    )


def is_probably_job_related(message: GmailMessage) -> bool:
    text = normalize_text(" ".join([message.sender, message.subject, message.snippet, message.body]))
    strong_terms = (
        "application",
        "interview",
        "recruiter",
        "hiring",
        "job alert",
        "job offer",
        "offer letter",
        "offer of employment",
        "offer you the position",
        "pleased to offer you the position",
        "technical assessment",
        "coding challenge",
        "thanks for applying",
        "received your application",
    )
    return any(term in text for term in strong_terms) or _is_known_platform_sender(message)


def classify_deterministically(message: GmailMessage) -> GmailClassification | None:
    text = normalize_text(" ".join([message.subject, message.snippet, message.body]))
    lifecycle_matches: list[tuple[str, str]] = []
    offer_signals = (
        "offer you the position",
        "pleased to offer you the position",
        "offer of employment",
        "offer letter",
    )
    if any(signal in text for signal in offer_signals):
        lifecycle_matches.append(("OFFER", "deterministic offer template"))
    if "not be moving forward" in text or "we regret to inform" in text:
        lifecycle_matches.append(("REJECTED", "deterministic rejection template"))
    if "technical assessment" in text or "coding challenge" in text:
        lifecycle_matches.append(
            ("TECHNICAL", "deterministic technical assessment template")
        )
    if "interview invitation" in text or (
        "choose a time" in text and "interview" in text
    ):
        lifecycle_matches.append(("INTERVIEW", "deterministic interview template"))
    if "thanks for applying" in text or "received your application" in text:
        lifecycle_matches.append(
            ("APPLIED", "deterministic application receipt template")
        )

    if lifecycle_matches:
        kind, rationale = lifecycle_matches[0]
        return _classification(kind, message, rationale)
    if "job alert" in text or _is_job_alert_sender(message):
        return _classification("JOB_ALERT", message, "deterministic job alert sender or template")
    if "recruiter" in text:
        return _classification("RECRUITER_CONTACT", message, "deterministic recruiter template")
    return None


def source_candidate_key(job: ExtractedJob) -> str:
    if job.source_platform.lower() == "linkedin":
        if job.source_job_id:
            return f"id:linkedin:{job.source_job_id}"
        parsed = urlparse(job.url)
        if "/comm/jobs/view/" in parsed.path.lower():
            linkedin_job_id = _linkedin_job_id(job.url)
            if linkedin_job_id:
                return f"id:linkedin:{linkedin_job_id}"
    if job.url:
        return "url:" + _normalize_url(job.url)
    if job.source_job_id:
        return f"id:{job.source_platform.lower()}:{job.source_job_id}"
    return "fallback:" + "|".join(
        [normalize_text(job.company), normalize_text(job.title), str(job.index)]
    )


def _review_needed(rationale: str) -> GmailClassification:
    return GmailClassification(kind="REVIEW_NEEDED", confidence=1.0, rationale=rationale)


def _validate_string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value.strip()


def _optional_text(data: dict, key: str) -> str:
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value.strip()


def _optional_nullable_text(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string or null")
    return value.strip() or None


def _parse_urls(values: object) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("job_urls must be an array of strings")

    urls: list[str] = []
    for value in values:
        if not _is_absolute_http_url(value):
            raise ValueError("job URLs must be absolute HTTP(S) URLs")
        url = _normalize_url(value)
        if url not in urls:
            urls.append(url)
    return urls


def _parse_jobs(values: object) -> list[ExtractedJob]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("jobs must be an array")

    jobs: list[ExtractedJob] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict) or set(value) != _JOB_FIELDS:
            raise ValueError("job must contain exactly the supported fields")
        source_platform = _validate_string(value, "source_platform")
        if not source_platform:
            raise ValueError("source_platform must be non-empty")
        url = _validate_string(value, "url")
        if url and not _is_absolute_http_url(url):
            raise ValueError("job URL must be an absolute HTTP(S) URL")
        remote = value.get("remote")
        if remote is not None and not isinstance(remote, bool):
            raise ValueError("remote must be a boolean or null")
        normalized_url = _normalize_url(url) if url else ""
        source_job_id = _optional_nullable_text(value, "source_job_id")
        if source_platform.lower() == "linkedin" and normalized_url:
            source_job_id = source_job_id or _linkedin_job_id(normalized_url)
        jobs.append(
            ExtractedJob(
                source_platform=source_platform,
                source_job_id=source_job_id,
                url=normalized_url,
                company=_optional_text(value, "company"),
                title=_optional_text(value, "title"),
                location=_optional_text(value, "location"),
                remote=remote,
                description=_optional_text(value, "description"),
                index=index,
            )
        )
    return jobs


def _parse_semantic_classification(raw: str) -> GmailClassification:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    if not _REQUIRED_CLASSIFICATION_FIELDS.issubset(data):
        raise ValueError("response missing required classification fields")

    kind = _validate_string(data, "kind")
    if kind not in SUPPORTED_KINDS:
        raise ValueError("unsupported kind")
    confidence = data.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number from 0 to 1")
    rationale = _validate_string(data, "rationale")
    if len(rationale) >= 160:
        raise ValueError("rationale must be under 160 characters")

    if kind == "IRRELEVANT":
        return GmailClassification(
            kind="IRRELEVANT",
            confidence=float(confidence),
            rationale=rationale,
        )

    return GmailClassification(
        kind=kind,
        confidence=float(confidence),
        company=_optional_text(data, "company"),
        role_title=_optional_text(data, "role_title"),
        source_job_id=_optional_nullable_text(data, "source_job_id"),
        job_urls=_parse_urls(data.get("job_urls")),
        jobs=_parse_jobs(data.get("jobs")),
        rationale=rationale,
    )


def _reconcile_semantic_urls(
    message: GmailMessage, classification: GmailClassification
) -> GmailClassification:
    message_urls = set(_message_urls(message))
    job_urls = [url for url in classification.job_urls if url in message_urls]
    jobs = [
        replace(job, url="")
        if job.url and job.url not in message_urls
        else job
        for job in classification.jobs
    ]

    known_jobs = _known_jobs(message)
    known_urls = {job.url for job in jobs if job.url}
    for job in known_jobs:
        if job.url not in known_urls:
            jobs.append(job)
            known_urls.add(job.url)
        if job.url not in job_urls:
            job_urls.append(job.url)
    return replace(classification, job_urls=job_urls, jobs=jobs)


def _build_semantic_prompt(
    message: GmailMessage, *, extract_job_alert: bool = False
) -> str:
    email_data = {
        "sender": message.sender,
        "subject": message.subject,
        "snippet": message.snippet,
        "body": normalize_text(message.body)[:_SEMANTIC_BODY_CHAR_LIMIT],
        "links": _message_urls(message)[:_SEMANTIC_LINK_LIMIT],
    }
    extraction_instruction = (
        f"\n{_JOB_ALERT_EXTRACTION_INSTRUCTION}" if extract_job_alert else ""
    )
    return (
        f"{_SCHEMA_INSTRUCTION}{extraction_instruction}\nEmail data:\n"
        f"{json.dumps(email_data, ensure_ascii=False)}"
    )


def _generate_semantic_text(
    message: GmailMessage,
    gemini: GeminiClient,
    *,
    extract_job_alert: bool,
) -> str:
    prompt = _build_semantic_prompt(message, extract_job_alert=extract_job_alert)
    return gemini.generate_text(
        prompt,
        purpose="gmail_semantic",
        thinking_level="minimal",
        max_output_tokens=5000,
        json_mode=True,
        json_schema=_GMAIL_CLASSIFICATION_SCHEMA,
    )


def classify_email(
    message: GmailMessage, gemini: GeminiClient, *, is_fresh: bool = True
) -> GmailClassification:
    if not is_probably_job_related(message):
        return GmailClassification(
            kind="IRRELEVANT",
            confidence=1.0,
            rationale="no deterministic job signal",
        )

    deterministic = classify_deterministically(message)
    extract_job_alert = bool(
        deterministic is not None
        and deterministic.kind == "JOB_ALERT"
        and (
            not deterministic.jobs
            or any(
                job.source_platform.lower() == "linkedin" and job.source_job_id
                for job in deterministic.jobs
            )
        )
    )
    # A stale job alert is never worth a Gemini call. This gate runs before
    # extraction is requested, not after, so a 15+ day backfill alert costs
    # zero generate_text calls. Lifecycle messages don't reach here at all
    # (extract_job_alert is only set for JOB_ALERT), so their deterministic-
    # first behavior is unaffected by freshness.
    if deterministic is not None and (not extract_job_alert or not is_fresh):
        return deterministic

    try:
        raw = _generate_semantic_text(
            message,
            gemini,
            extract_job_alert=extract_job_alert,
        )
    except (GeminiBudgetExceeded, GeminiQuotaPaused):
        raise
    except Exception as exc:
        raise SemanticClassificationError("gemini_error") from exc

    try:
        classification = _parse_semantic_classification(raw)
        if classification.kind != "IRRELEVANT":
            classification = _reconcile_semantic_urls(message, classification)
    except ValueError as exc:
        raise SemanticClassificationError(
            "invalid_semantic_response",
            detail=str(exc),
        ) from exc

    if classification.kind == "REVIEW_NEEDED" or classification.confidence < AUTO_CONFIDENCE_THRESHOLD:
        return replace(classification, kind="REVIEW_NEEDED")
    return classification
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Iterable

from job_hunter.gmail_models import (
    AUTO_CONFIDENCE_THRESHOLD,
    MATCH_RECENCY_DAYS,
    GmailClassification,
    GmailMessage,
)
from job_hunter.normalize import canonicalize_url, normalize_text

if TYPE_CHECKING:
    import sqlite3

    from job_hunter.store import JobStore


@dataclass(frozen=True, slots=True)
class JobMatch:
    job_id: int | None
    reason: str
    ambiguous: bool


_STATE_TIE_PRECEDENCE = {
    "APPLIED": 1,
    "RECRUITER_CONTACT": 2,
    "INTERVIEW": 3,
    "TECHNICAL": 4,
    "REJECTED": 5,
    "OFFER": 6,
}


def _single_match(rows: Iterable[sqlite3.Row], reason: str) -> JobMatch | None:
    matches = list(rows)
    if len(matches) == 1:
        return JobMatch(job_id=matches[0]["id"], reason=reason, ambiguous=False)
    if matches:
        return JobMatch(job_id=None, reason=f"ambiguous_{reason}", ambiguous=True)
    return None


def _message_urls(
    classification: GmailClassification, message: GmailMessage
) -> set[str]:
    return {
        canonicalize_url(url)
        for url in [*classification.job_urls, *(job.url for job in classification.jobs), *message.links]
        if url
    }


def _is_recent_company_match(row: sqlite3.Row, sent_at: datetime) -> bool:
    timestamps = (row["first_seen_at"], row["last_seen_at"])
    return any(
        abs(sent_at - datetime.fromisoformat(value))
        <= timedelta(days=MATCH_RECENCY_DAYS)
        for value in timestamps
    )


def match_job(
    store: JobStore, classification: GmailClassification, message: GmailMessage
) -> JobMatch:
    jobs = store.list_jobs_for_matching()
    job_urls = _message_urls(classification, message)

    if job_urls:
        result = _single_match(
            (job for job in jobs if job["url"] and canonicalize_url(job["url"]) in job_urls),
            "canonical_url",
        )
        if result is not None:
            return result

    if classification.source_job_id:
        result = _single_match(
            (job for job in jobs if job["source_job_id"] == classification.source_job_id),
            "source_job_id",
        )
        if result is not None:
            return result

    company = normalize_text(classification.company)
    title = normalize_text(classification.role_title)
    if company and title:
        result = _single_match(
            (
                job
                for job in jobs
                if normalize_text(job["company"]) == company
                and normalize_text(job["title"]) == title
            ),
            "company_and_title",
        )
        if result is not None:
            return result

    if company:
        result = _single_match(
            (
                job
                for job in jobs
                if normalize_text(job["company"]) == company
                and _is_recent_company_match(job, message.sent_at)
            ),
            "company",
        )
        if result is not None:
            if result.ambiguous:
                return JobMatch(job_id=None, reason="ambiguous_company", ambiguous=True)
            return JobMatch(job_id=result.job_id, reason="recent_company", ambiguous=False)

    return JobMatch(job_id=None, reason="unresolved", ambiguous=False)


def derive_application_state(events) -> str | None:
    eligible_events = [
        event
        for event in events
        if event["job_id"] is not None
        and event["confidence"] >= AUTO_CONFIDENCE_THRESHOLD
        and event["event_type"] in _STATE_TIE_PRECEDENCE
    ]
    if not eligible_events:
        return None
    return max(
        eligible_events,
        key=lambda event: (
            event["occurred_at"],
            _STATE_TIE_PRECEDENCE[event["event_type"]],
            event["id"],
        ),
    )["event_type"]

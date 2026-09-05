from dataclasses import dataclass, field
from datetime import datetime

from .models import GeminiQuotaSettings


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SUPPORTED_KINDS = frozenset(
    {
        "JOB_ALERT",
        "RECRUITER_CONTACT",
        "APPLIED",
        "INTERVIEW",
        "TECHNICAL",
        "OFFER",
        "REJECTED",
        "REVIEW_NEEDED",
        "IRRELEVANT",
    }
)
AUTO_CONFIDENCE_THRESHOLD = 0.90
MATCH_RECENCY_DAYS = 120
DISCOVERY_FRESHNESS_DAYS = 14
LEGACY_SEMANTIC_FAILURE_RATIONALE = "semantic classification unavailable or invalid"


@dataclass(frozen=True, slots=True)
class GmailSettings:
    client_id: str
    client_secret: str
    refresh_token: str
    gemini_api_key: str
    gemini_quota: GeminiQuotaSettings
    gemini_model: str = "gemini-3.6-flash"
    db_path: str = "var/job_hunter.sqlite3"


@dataclass(frozen=True, slots=True)
class GmailMessage:
    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    sent_at: datetime
    snippet: str
    body: str
    links: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ExtractedJob:
    source_platform: str
    source_job_id: str | None = None
    url: str = ""
    company: str = ""
    title: str = ""
    location: str = ""
    remote: bool | None = None
    description: str = ""
    index: int = 0


@dataclass(frozen=True, slots=True)
class GmailClassification:
    kind: str
    confidence: float
    company: str = ""
    role_title: str = ""
    source_job_id: str | None = None
    job_urls: list[str] = field(default_factory=list)
    jobs: list[ExtractedJob] = field(default_factory=list)
    rationale: str = ""


@dataclass(slots=True)
class GmailSyncSummary:
    fetched: int = 0
    processed: int = 0
    job_alerts: int = 0
    application_events: int = 0
    review_needed: int = 0
    irrelevant: int = 0
    errors: int = 0

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_hunter import content_confidence
from job_hunter.gmail_models import (
    AUTO_CONFIDENCE_THRESHOLD,
    LEGACY_SEMANTIC_FAILURE_RATIONALE,
    ExtractedJob,
)
from job_hunter.job_identity import (
    locations_compatible,
    normalize_company_name,
    normalize_job_title,
)
from job_hunter.models import (
    AtsRegistryEntry,
    CandidateContextCacheEntry,
    Evaluation,
    Job,
    Material,
)
from job_hunter.normalize import (
    canonicalize_url,
    description_hash,
    job_fingerprint,
    normalize_text,
)

_CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint      TEXT    NOT NULL UNIQUE,
    source           TEXT    NOT NULL DEFAULT '',
    source_job_id    TEXT,
    url              TEXT    NOT NULL DEFAULT '',
    company          TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL DEFAULT '',
    location         TEXT    NOT NULL DEFAULT '',
    remote           INTEGER,          -- NULL | 0 | 1
    description      TEXT    NOT NULL DEFAULT '',
    description_hash TEXT    NOT NULL DEFAULT '',
    first_seen_at    TEXT    NOT NULL,
    last_seen_at     TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'new'
)
"""

_R2_JOB_COLUMNS = {
    "canonical_url": "TEXT NOT NULL DEFAULT ''",
    "ats_provider": "TEXT",
    "ats_board": "TEXT",
    "ats_job_id": "TEXT",
    "market_id": "TEXT NOT NULL DEFAULT ''",
}

_R3_JOB_COLUMNS = {
    "content_confidence": "TEXT NOT NULL DEFAULT ''",
}

_CREATE_JOB_SOURCES = """
CREATE TABLE IF NOT EXISTS job_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_job_id TEXT,
    source_url TEXT NOT NULL DEFAULT '',
    identity_key TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(job_id, identity_key)
)
"""

_CREATE_COMPANY_WATCH = """
CREATE TABLE IF NOT EXISTS company_watch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL,
    normalized_company_name TEXT NOT NULL UNIQUE,
    careers_url TEXT NOT NULL DEFAULT '',
    ats_provider TEXT,
    ats_identifier TEXT,
    discovered_from_job_id INTEGER REFERENCES jobs(id),
    promotion_source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    first_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    last_successful_check_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_EVALUATIONS = """
CREATE TABLE IF NOT EXISTS evaluations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id                 INTEGER NOT NULL REFERENCES jobs(id),
    total_score            INTEGER NOT NULL DEFAULT 0,
    scores_json            TEXT    NOT NULL DEFAULT '{}',
    decision               TEXT    NOT NULL DEFAULT '',
    hard_blockers_json     TEXT    NOT NULL DEFAULT '[]',
    strengths_json         TEXT    NOT NULL DEFAULT '[]',
    gaps_json              TEXT    NOT NULL DEFAULT '[]',
    salary_note            TEXT    NOT NULL DEFAULT '',
    location_note          TEXT    NOT NULL DEFAULT '',
    rationale              TEXT    NOT NULL DEFAULT '',
    model                  TEXT    NOT NULL DEFAULT '',
    status                 TEXT    NOT NULL DEFAULT 'ok',
    description_hash_at_eval TEXT NOT NULL DEFAULT '',
    evaluated_at           TEXT    NOT NULL
)
"""

_MARKET_EVALUATION_COLUMNS = {"market_id": "TEXT NOT NULL DEFAULT ''"}

_CONTENT_TRUST_EVALUATION_COLUMNS = {
    "content_confidence_at_eval": "TEXT NOT NULL DEFAULT ''",
    "requirements_json": "TEXT NOT NULL DEFAULT '{}'",
}

_RAW_SCORE_EVALUATION_COLUMNS = {"raw_model_score": "INTEGER NOT NULL DEFAULT 0"}

_CREATE_MATERIALS = """
CREATE TABLE IF NOT EXISTS materials (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL REFERENCES jobs(id),
    cover_letter_text TEXT    NOT NULL DEFAULT '',
    generated_at      TEXT    NOT NULL
)
"""

_CREATE_DELIVERIES = """
CREATE TABLE IF NOT EXISTS deliveries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id              INTEGER NOT NULL REFERENCES jobs(id),
    delivery_type       TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'sent',
    delivered_at        TEXT    NOT NULL,
    telegram_message_id TEXT
)
"""

_CREATE_GEMINI_USAGE = """
CREATE TABLE IF NOT EXISTS gemini_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    run_id TEXT,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL,
    estimated_input_tokens INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    thinking_tokens INTEGER,
    cached_tokens INTEGER,
    total_tokens INTEGER,
    http_status INTEGER,
    error_code TEXT
)
"""

_CREATE_GEMINI_QUOTA_STATE = """
CREATE TABLE IF NOT EXISTS gemini_quota_state (
    model TEXT PRIMARY KEY,
    paused_until TEXT,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
)
"""

_CREATE_CANDIDATE_CONTEXT_CACHE = """
CREATE TABLE IF NOT EXISTS candidate_context_cache (
    cache_key TEXT PRIMARY KEY,
    profile_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    context_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_PENDING_AI_WORK = """
CREATE TABLE IF NOT EXISTS pending_ai_work (
    work_type TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(work_type, job_id)
)
"""

_CREATE_GMAIL_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS gmail_sync_state (
    account_id TEXT PRIMARY KEY,
    history_id TEXT,
    last_successful_sync_at TEXT,
    last_processed_message_at TEXT,
    backfill_completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_CREATE_GMAIL_MESSAGES = """
CREATE TABLE IF NOT EXISTS gmail_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    rationale TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL
)
"""

_CREATE_INBOUND_JOB_CANDIDATES = """
CREATE TABLE IF NOT EXISTS inbound_job_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin TEXT NOT NULL DEFAULT 'gmail',
    source_message_id TEXT NOT NULL,
    source_candidate_key TEXT NOT NULL,
    source_platform TEXT NOT NULL DEFAULT '',
    source_job_id TEXT,
    url TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(origin, source_message_id, source_candidate_key)
)
"""

_CREATE_APPLICATION_EVENTS = """
CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER REFERENCES jobs(id),
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'gmail',
    source_message_id TEXT NOT NULL UNIQUE,
    source_thread_id TEXT,
    confidence REAL NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    role_title TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
)
"""

_CREATE_REVIEW_DELIVERIES = """
CREATE TABLE IF NOT EXISTS review_deliveries (
    event_id INTEGER PRIMARY KEY REFERENCES application_events(id),
    delivered_at TEXT NOT NULL,
    telegram_message_id TEXT
)
"""

_CREATE_ATS_REGISTRY = """
CREATE TABLE IF NOT EXISTS ats_registry (
    provider TEXT NOT NULL,
    board_identifier TEXT NOT NULL,
    company_name TEXT NOT NULL DEFAULT '',
    market_hint TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_checked_at TEXT,
    last_success_at TEXT,
    last_eligible_at TEXT,
    last_job_count INTEGER NOT NULL DEFAULT 0,
    eligible_jobs_seen INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    paused_until TEXT,
    PRIMARY KEY(provider, board_identifier)
)
"""

_DELIVERABLE_SCORE_FLOOR = 60
_STALE_BOARD_DEACTIVATION_THRESHOLD = 3
_SUPPORTED_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_utc(now: datetime) -> datetime:
    """Return an aware datetime as UTC or reject an ambiguous naive input."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


class JobStore:
    """SQLite-backed persistence layer for the job hunter bot."""

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self._path = str(path)
        if read_only:
            database_uri = Path(path).resolve().as_uri() + "?mode=ro"
            self._conn = sqlite3.connect(
                database_uri,
                uri=True,
                check_same_thread=False,
            )
        else:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if read_only:
            self._conn.execute("PRAGMA foreign_keys = ON")
        else:
            self._init_db()

    # ------------------------------------------------------------------
    # Schema setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(_CREATE_JOBS)
            self._migrate_jobs_to_r2_schema()
            self._migrate_jobs_to_r3_schema()
            self._conn.execute(_CREATE_JOB_SOURCES)
            self._conn.execute(_CREATE_COMPANY_WATCH)
            self._conn.execute(_CREATE_EVALUATIONS)
            self._add_missing_columns("evaluations", _MARKET_EVALUATION_COLUMNS)
            self._add_missing_columns("evaluations", _CONTENT_TRUST_EVALUATION_COLUMNS)
            added_raw_score_columns = self._add_missing_columns(
                "evaluations", _RAW_SCORE_EVALUATION_COLUMNS
            )
            if "raw_model_score" in added_raw_score_columns:
                self._backfill_raw_model_score()
            self._conn.execute(_CREATE_MATERIALS)
            self._conn.execute(_CREATE_DELIVERIES)
            self._conn.execute(_CREATE_GEMINI_USAGE)
            self._conn.execute(_CREATE_GEMINI_QUOTA_STATE)
            self._conn.execute(_CREATE_CANDIDATE_CONTEXT_CACHE)
            self._conn.execute(_CREATE_PENDING_AI_WORK)
            self._conn.execute(_CREATE_GMAIL_SYNC_STATE)
            self._conn.execute(_CREATE_GMAIL_MESSAGES)
            self._conn.execute(_CREATE_INBOUND_JOB_CANDIDATES)
            self._conn.execute(_CREATE_APPLICATION_EVENTS)
            self._conn.execute(_CREATE_REVIEW_DELIVERIES)
            self._conn.execute(_CREATE_ATS_REGISTRY)

    def _migrate_jobs_to_r2_schema(self) -> None:
        self._add_missing_columns("jobs", _R2_JOB_COLUMNS)

    def _migrate_jobs_to_r3_schema(self) -> None:
        self._add_missing_columns("jobs", _R3_JOB_COLUMNS)

    def _backfill_raw_model_score(self) -> None:
        """Give pre-existing evaluation rows a meaningful raw score.

        `_add_missing_columns` can only ALTER TABLE ... ADD COLUMN with a fixed
        default, so rows written before `raw_model_score` existed would read
        back as 0. Before the cap was introduced no score was ever adjusted, so
        their raw score equalled their total. Callers only run this once, right
        after `raw_model_score` is actually added to the schema (see
        `_init_db`), so it is a one-time migration rather than a repeated
        full-table scan. A genuine all-zero evaluation has total_score = 0,
        making the update a no-op for that row.
        """
        self._conn.execute(
            "UPDATE evaluations SET raw_model_score = total_score WHERE raw_model_score = 0"
        )

    def _add_missing_columns(self, table: str, columns: dict[str, str]) -> list[str]:
        existing = {
            row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
        }
        added = []
        for name, definition in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                added.append(name)
        return added

    # ------------------------------------------------------------------
    # Gemini persistence
    # ------------------------------------------------------------------

    def record_gemini_usage(
        self,
        *,
        occurred_at: str,
        run_id: str | None,
        model: str,
        purpose: str,
        status: str,
        estimated_input_tokens: int,
        prompt_tokens: int | None = None,
        output_tokens: int | None = None,
        thinking_tokens: int | None = None,
        cached_tokens: int | None = None,
        total_tokens: int | None = None,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> None:
        """Record one Gemini attempt without persisting request or response content."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO gemini_usage (
                    occurred_at, run_id, model, purpose, status,
                    estimated_input_tokens, prompt_tokens, output_tokens,
                    thinking_tokens, cached_tokens, total_tokens, http_status,
                    error_code
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    occurred_at,
                    run_id,
                    model,
                    purpose,
                    status,
                    estimated_input_tokens,
                    prompt_tokens,
                    output_tokens,
                    thinking_tokens,
                    cached_tokens,
                    total_tokens,
                    http_status,
                    error_code,
                ),
            )

    def gemini_usage_rows(
        self,
        start_at: str,
        end_at: str,
        *,
        model: str | None = None,
        run_id: str | None = None,
    ) -> list[sqlite3.Row]:
        """Return Gemini ledger rows in the half-open time range [start_at, end_at)."""
        clauses = ["occurred_at >= ?", "occurred_at < ?"]
        parameters: list[str] = [start_at, end_at]
        if model is not None:
            clauses.append("model = ?")
            parameters.append(model)
        if run_id is not None:
            clauses.append("run_id = ?")
            parameters.append(run_id)
        return self._conn.execute(
            f"SELECT * FROM gemini_usage WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at, id",
            parameters,
        ).fetchall()

    def set_gemini_pause(
        self, model: str, paused_until: str | None, reason: str
    ) -> None:
        """Persist the active quota pause for a Gemini model."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO gemini_quota_state (model, paused_until, reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(model) DO UPDATE SET
                    paused_until = excluded.paused_until,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (model, paused_until, reason, _now_iso()),
            )

    def get_gemini_pause(self, model: str) -> sqlite3.Row | None:
        """Return the persisted quota pause for a model, if present."""
        return self._conn.execute(
            "SELECT * FROM gemini_quota_state WHERE model = ?", (model,)
        ).fetchone()

    def clear_gemini_pause(self, model: str) -> None:
        """Remove a model's persisted quota pause."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM gemini_quota_state WHERE model = ?", (model,)
            )

    def get_candidate_context(self, cache_key: str) -> CandidateContextCacheEntry | None:
        """Return a cached candidate context, decoding its stored JSON payload."""
        row = self._conn.execute(
            "SELECT * FROM candidate_context_cache WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            return None
        return CandidateContextCacheEntry(
            cache_key=row["cache_key"],
            profile_hash=row["profile_hash"],
            model=row["model"],
            schema_version=row["schema_version"],
            context=json.loads(row["context_json"]),
            created_at=row["created_at"],
        )

    def save_candidate_context(
        self,
        *,
        cache_key: str,
        profile_hash: str,
        model: str,
        schema_version: str,
        context: dict,
    ) -> None:
        """Persist a structured candidate context under its cache identity."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO candidate_context_cache
                    (cache_key, profile_hash, model, schema_version, context_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    profile_hash = excluded.profile_hash,
                    model = excluded.model,
                    schema_version = excluded.schema_version,
                    context_json = excluded.context_json,
                    created_at = excluded.created_at
                """,
                (
                    cache_key,
                    profile_hash,
                    model,
                    schema_version,
                    json.dumps(context),
                    _now_iso(),
                ),
            )

    def enqueue_ai_work(self, work_type: str, job_id: int) -> None:
        """Idempotently enqueue deferred AI work and refresh its retry timestamp."""
        now = _now_iso()
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                """
                INSERT INTO pending_ai_work (work_type, job_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(work_type, job_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (work_type, job_id, now, now),
            )

    def list_pending_ai_work(self, work_type: str) -> list[sqlite3.Row]:
        """Return pending rows for one AI-work category in stable retry order."""
        return self._conn.execute(
            """
            SELECT * FROM pending_ai_work
            WHERE work_type = ?
            ORDER BY created_at, job_id
            """,
            (work_type,),
        ).fetchall()

    def complete_ai_work(self, work_type: str, job_id: int) -> None:
        """Remove a completed deferred AI-work item."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM pending_ai_work WHERE work_type = ? AND job_id = ?",
                (work_type, job_id),
            )

    # ------------------------------------------------------------------
    # Job operations
    # ------------------------------------------------------------------

    def upsert_job(self, job: Job) -> tuple[int, bool, bool]:
        """
        Insert or update a job record.

        Returns
        -------
        (job_id, is_new, description_changed)
        """
        fingerprint = job_fingerprint(job)
        desc_hash = description_hash(job.description or "")
        now = _now_iso()
        remote_int: int | None = None if job.remote is None else int(job.remote)
        canonical_url = job.canonical_url or canonicalize_url(job.url or "")

        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (fingerprint, source, source_job_id, url, company, title,
                     location, remote, description, description_hash,
                     canonical_url, ats_provider, ats_board, ats_job_id,
                     content_confidence, first_seen_at, last_seen_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
                """,
                (
                    fingerprint,
                    job.source or "",
                    job.source_job_id,
                    job.url or "",
                    job.company or "",
                    job.title or "",
                    job.location or "",
                    remote_int,
                    job.description or "",
                    desc_hash,
                    canonical_url,
                    job.ats_provider,
                    job.ats_board,
                    job.ats_job_id,
                    job.content_confidence or "",
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT id, description_hash, first_seen_at FROM jobs WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()

            job_id: int = row["id"]
            old_hash: str = row["description_hash"]
            is_new: bool = row["first_seen_at"] == now
            description_changed: bool = False

            if not is_new:
                description_changed = old_hash != desc_hash
                self._conn.execute(
                    """
                    UPDATE jobs SET
                        url              = ?,
                        company          = ?,
                        title            = ?,
                        location         = ?,
                        remote           = ?,
                        description      = ?,
                        description_hash = ?,
                        canonical_url    = COALESCE(NULLIF(?, ''), canonical_url),
                        ats_provider     = COALESCE(?, ats_provider),
                        ats_board        = COALESCE(?, ats_board),
                        ats_job_id       = COALESCE(?, ats_job_id),
                        content_confidence = ?,
                        last_seen_at     = ?
                    WHERE id = ?
                    """,
                    (
                        job.url or "",
                        job.company or "",
                        job.title or "",
                        job.location or "",
                        remote_int,
                        job.description or "",
                        desc_hash,
                        canonical_url,
                        job.ats_provider,
                        job.ats_board,
                        job.ats_job_id,
                        job.content_confidence or "",
                        now,
                        job_id,
                    ),
                )

        return job_id, is_new, description_changed

    def upsert_logical_job(self, job: Job) -> tuple[int, bool, bool]:
        """Persist a source-independent logical job and its discovery provenance.

        Identity is resolved from strongest to weakest exact evidence. Existing
        rows retain richer fields, while a resolved canonical URL becomes the
        usable job URL. The return shape matches :meth:`upsert_job`.
        """
        canonical_url = canonicalize_url(job.canonical_url) if job.canonical_url else ""
        ats_provider = (job.ats_provider or "").lower()
        fingerprint = job_fingerprint(job)

        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            candidate_ids: list[int] = []

            if canonical_url:
                for candidate_id in self._find_job_ids_by_canonical_url(
                    canonical_url
                ):
                    self._append_unique_id(candidate_ids, candidate_id)
            if (
                ats_provider in _SUPPORTED_ATS_PROVIDERS
                and job.ats_board
                and job.ats_job_id
            ):
                for candidate_id in self._find_job_ids_by_ats(
                    ats_provider, job.ats_board, job.ats_job_id
                ):
                    self._append_unique_id(candidate_ids, candidate_id)
            for candidate_id in self._find_job_ids_by_identity(
                job.company, job.title, job.location
            ):
                self._append_unique_id(candidate_ids, candidate_id)
            self._append_unique_id(
                candidate_ids,
                self._find_single_job_id(
                    "SELECT id FROM jobs WHERE fingerprint = ?", (fingerprint,)
                ),
            )

            if candidate_ids:
                job_id = min(candidate_ids, key=self._job_survivor_sort_key)
                previous_hash = self._conn.execute(
                    "SELECT description_hash FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()["description_hash"]
                for duplicate_id in candidate_ids:
                    if duplicate_id != job_id:
                        job_id = self._merge_jobs(job_id, duplicate_id)
                self._update_logical_job(job_id, job)
                current_hash = self._conn.execute(
                    "SELECT description_hash FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()["description_hash"]
                description_changed = previous_hash != current_hash
                is_new = False
            else:
                job_id = self._insert_logical_job(job, fingerprint)
                description_changed = False
                is_new = True

            self._record_job_source(
                job_id,
                source=job.source or "",
                source_job_id=job.source_job_id,
                source_url=job.original_url or job.url or "",
            )

        return job_id, is_new, description_changed

    @staticmethod
    def _append_unique_id(ids: list[int], job_id: int | None) -> None:
        if job_id is not None and job_id not in ids:
            ids.append(job_id)

    def _insert_logical_job(self, job: Job, fingerprint: str) -> int:
        now = _now_iso()
        persisted_url = job.canonical_url or job.url or ""
        canonical_url = canonicalize_url(job.canonical_url or job.url or "")
        remote = None if job.remote is None else int(job.remote)
        cursor = self._conn.execute(
            """
            INSERT INTO jobs
                (fingerprint, source, source_job_id, url, company, title,
                 location, remote, description, description_hash,
                 canonical_url, ats_provider, ats_board, ats_job_id,
                 content_confidence, first_seen_at, last_seen_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new')
            """,
            (
                fingerprint,
                job.source or "",
                job.source_job_id,
                persisted_url,
                job.company or "",
                job.title or "",
                job.location or "",
                remote,
                job.description or "",
                description_hash(job.description or ""),
                canonical_url,
                job.ats_provider,
                job.ats_board,
                job.ats_job_id,
                job.content_confidence or "",
                now,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _update_logical_job(self, job_id: int, job: Job) -> bool:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"job does not exist: {job_id}")

        description, description_confidence = self._better_description(
            row["description"], row["content_confidence"], job.description, job.content_confidence
        )
        description_changed = description_hash(description) != row["description_hash"]
        canonical_url = canonicalize_url(job.canonical_url) if job.canonical_url else ""
        persisted_url = job.canonical_url or row["url"] or job.url or ""

        remote = row["remote"]
        if remote is None and job.remote is not None:
            remote = int(job.remote)

        self._conn.execute(
            """
            UPDATE jobs SET
                url = ?,
                company = ?,
                title = ?,
                location = ?,
                remote = ?,
                description = ?,
                description_hash = ?,
                canonical_url = ?,
                ats_provider = ?,
                ats_board = ?,
                ats_job_id = ?,
                content_confidence = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                persisted_url,
                row["company"] or job.company or "",
                row["title"] or job.title or "",
                row["location"] or job.location or "",
                remote,
                description,
                description_hash(description),
                canonical_url or row["canonical_url"],
                job.ats_provider or row["ats_provider"],
                job.ats_board or row["ats_board"],
                job.ats_job_id or row["ats_job_id"],
                description_confidence,
                _now_iso(),
                job_id,
            ),
        )
        return description_changed

    def merge_jobs(self, survivor_id: int, duplicate_id: int) -> int:
        """Transactionally merge a duplicate job and all attached records."""
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            return self._merge_jobs(survivor_id, duplicate_id)

    def _merge_jobs(self, survivor_id: int, duplicate_id: int) -> int:
        if survivor_id == duplicate_id:
            return survivor_id

        if self._job_survivor_sort_key(duplicate_id) < self._job_survivor_sort_key(
            survivor_id
        ):
            survivor_id, duplicate_id = duplicate_id, survivor_id

        survivor = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (survivor_id,)
        ).fetchone()
        duplicate = self._conn.execute(
            "SELECT * FROM jobs WHERE id = ?", (duplicate_id,)
        ).fetchone()
        if survivor is None or duplicate is None:
            raise ValueError("survivor and duplicate jobs must both exist")

        survivor_has_ats = self._has_complete_ats_identity(survivor)
        duplicate_has_ats = self._has_complete_ats_identity(duplicate)
        prefer_duplicate_identity = duplicate_has_ats and not survivor_has_ats
        canonical_url = (
            duplicate["canonical_url"]
            if prefer_duplicate_identity
            else survivor["canonical_url"] or duplicate["canonical_url"]
        )
        url = (
            duplicate["url"]
            if prefer_duplicate_identity
            else survivor["url"] or duplicate["url"]
        )
        if canonical_url and (survivor_has_ats or duplicate_has_ats):
            url = canonical_url
        description, description_confidence = self._better_description(
            survivor["description"], survivor["content_confidence"],
            duplicate["description"], duplicate["content_confidence"],
        )

        self._conn.execute(
            """
            UPDATE jobs SET
                source = ?,
                source_job_id = ?,
                url = ?,
                company = ?,
                title = ?,
                location = ?,
                remote = ?,
                description = ?,
                description_hash = ?,
                canonical_url = ?,
                ats_provider = ?,
                ats_board = ?,
                ats_job_id = ?,
                content_confidence = ?,
                first_seen_at = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                survivor["source"] or duplicate["source"],
                survivor["source_job_id"] or duplicate["source_job_id"],
                url,
                survivor["company"] or duplicate["company"],
                survivor["title"] or duplicate["title"],
                survivor["location"] or duplicate["location"],
                survivor["remote"]
                if survivor["remote"] is not None
                else duplicate["remote"],
                description,
                description_hash(description),
                canonical_url,
                duplicate["ats_provider"]
                if prefer_duplicate_identity
                else survivor["ats_provider"] or duplicate["ats_provider"],
                duplicate["ats_board"]
                if prefer_duplicate_identity
                else survivor["ats_board"] or duplicate["ats_board"],
                duplicate["ats_job_id"]
                if prefer_duplicate_identity
                else survivor["ats_job_id"] or duplicate["ats_job_id"],
                description_confidence,
                min(survivor["first_seen_at"], duplicate["first_seen_at"]),
                max(survivor["last_seen_at"], duplicate["last_seen_at"]),
                survivor_id,
            ),
        )

        for source in self._conn.execute(
            "SELECT * FROM job_sources WHERE job_id = ? ORDER BY id", (duplicate_id,)
        ).fetchall():
            self._conn.execute(
                """
                INSERT INTO job_sources
                    (job_id, source, source_job_id, source_url, identity_key,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, identity_key) DO UPDATE SET
                    first_seen_at = MIN(first_seen_at, excluded.first_seen_at),
                    last_seen_at = MAX(last_seen_at, excluded.last_seen_at)
                """,
                (
                    survivor_id,
                    source["source"],
                    source["source_job_id"],
                    source["source_url"],
                    source["identity_key"],
                    source["first_seen_at"],
                    source["last_seen_at"],
                ),
            )
        self._conn.execute("DELETE FROM job_sources WHERE job_id = ?", (duplicate_id,))

        for table in ("application_events", "materials", "deliveries", "evaluations"):
            self._conn.execute(
                f"UPDATE {table} SET job_id = ? WHERE job_id = ?",
                (survivor_id, duplicate_id),
            )
        self._conn.execute(
            """
            UPDATE company_watch SET discovered_from_job_id = ?
            WHERE discovered_from_job_id = ?
            """,
            (survivor_id, duplicate_id),
        )
        self._conn.execute("DELETE FROM jobs WHERE id = ?", (duplicate_id,))
        return survivor_id

    def _job_survivor_sort_key(self, job_id: int) -> tuple[int, int, str, int]:
        """Rank a stored job for deterministic, history-preserving merges."""
        row = self._conn.execute(
            """
            SELECT
                j.first_seen_at,
                EXISTS(
                    SELECT 1 FROM application_events a WHERE a.job_id = j.id
                ) AS has_application_events,
                (
                    EXISTS(SELECT 1 FROM evaluations e WHERE e.job_id = j.id)
                    OR EXISTS(SELECT 1 FROM materials m WHERE m.job_id = j.id)
                    OR EXISTS(SELECT 1 FROM deliveries d WHERE d.job_id = j.id)
                ) AS has_other_history
            FROM jobs j
            WHERE j.id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"job does not exist: {job_id}")
        return (
            -int(row["has_application_events"]),
            -int(row["has_other_history"]),
            row["first_seen_at"],
            job_id,
        )

    @staticmethod
    def _has_complete_ats_identity(row: sqlite3.Row) -> bool:
        return bool(row["ats_provider"] and row["ats_board"] and row["ats_job_id"])

    @staticmethod
    def _better_description(
        current: str, current_confidence: str, candidate: str, candidate_confidence: str
    ) -> tuple[str, str]:
        if not candidate:
            return current, current_confidence
        if not current:
            return candidate, candidate_confidence
        current_rank = content_confidence.tier_rank(current_confidence)
        candidate_rank = content_confidence.tier_rank(candidate_confidence)
        if candidate_rank < current_rank:
            return candidate, candidate_confidence
        if candidate_rank > current_rank:
            return current, current_confidence
        if len(candidate.strip()) > len(current.strip()):
            return candidate, candidate_confidence
        return current, current_confidence

    def record_job_source(
        self,
        job_id: int,
        *,
        source: str,
        source_job_id: str | None,
        source_url: str,
    ) -> None:
        """Record a discovery source once while refreshing its last-seen time."""
        with self._conn:
            self._record_job_source(
                job_id,
                source=source,
                source_job_id=source_job_id,
                source_url=source_url,
            )

    def _record_job_source(
        self,
        job_id: int,
        *,
        source: str,
        source_job_id: str | None,
        source_url: str,
    ) -> None:
        canonical_source_url = canonicalize_url(source_url)
        identity_key = (
            f"id:{source}:{source_job_id}"
            if source_job_id
            else f"url:{canonical_source_url}"
        )
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO job_sources
                (job_id, source, source_job_id, source_url, identity_key,
                 first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, identity_key) DO UPDATE SET
                last_seen_at = excluded.last_seen_at
            """,
            (
                job_id,
                source,
                source_job_id,
                source_url,
                identity_key,
                now,
                now,
            ),
        )

    def list_job_sources(self, job_id: int) -> list[sqlite3.Row]:
        """Return source provenance for a job in insertion order."""
        return self._conn.execute(
            "SELECT * FROM job_sources WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()

    def find_job_by_canonical_url(self, url: str) -> int | None:
        """Return a job ID only when a canonical URL identifies one job."""
        matching_ids = self._find_job_ids_by_canonical_url(url)
        return matching_ids[0] if len(matching_ids) == 1 else None

    def _find_job_ids_by_canonical_url(self, url: str) -> list[int]:
        return self._find_job_ids(
            "SELECT id FROM jobs WHERE canonical_url = ? ORDER BY id",
            (canonicalize_url(url),),
        )

    def find_job_by_ats(
        self, provider: str, board: str, job_id: str | None
    ) -> int | None:
        """Return a job ID only when an ATS tuple identifies one job."""
        if not job_id:
            return None
        matching_ids = self._find_job_ids_by_ats(provider, board, job_id)
        return matching_ids[0] if len(matching_ids) == 1 else None

    def _find_job_ids_by_ats(
        self, provider: str, board: str, job_id: str
    ) -> list[int]:
        return self._find_job_ids(
            """
            SELECT id FROM jobs
            WHERE ats_provider = ? AND ats_board = ? AND ats_job_id = ?
            ORDER BY id
            """,
            (provider, board, job_id),
        )

    def find_job_by_identity(
        self, company: str, title: str, location: str
    ) -> int | None:
        """Return a job ID only for one normalized company/title/location match."""
        matching_ids = self._find_job_ids_by_identity(company, title, location)
        return matching_ids[0] if len(matching_ids) == 1 else None

    def _find_job_ids_by_identity(
        self, company: str, title: str, location: str
    ) -> list[int]:
        """Return all normalized identity matches only when locations agree."""
        company_key = normalize_company_name(company)
        title_key = normalize_job_title(title)
        if not company_key or not title_key:
            return []
        matching_rows = [
            row
            for row in self._conn.execute("SELECT id, company, title, location FROM jobs")
            if normalize_company_name(row["company"]) == company_key
            and normalize_job_title(row["title"]) == title_key
            and locations_compatible(location, row["location"])
        ]
        if any(
            not locations_compatible(left["location"], right["location"])
            for index, left in enumerate(matching_rows)
            for right in matching_rows[index + 1 :]
        ):
            return []
        return [row["id"] for row in matching_rows]

    def _find_job_ids(
        self, query: str, parameters: tuple[str, ...]
    ) -> list[int]:
        return [
            row["id"] for row in self._conn.execute(query, parameters).fetchall()
        ]

    def _find_single_job_id(
        self, query: str, parameters: tuple[str, ...]
    ) -> int | None:
        matching_ids = self._find_job_ids(query, parameters)
        return matching_ids[0] if len(matching_ids) == 1 else None

    def set_job_market(self, job_id: int, market_id: str | None) -> None:
        """Persist the primary market a job has been attributed to."""
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET market_id = ? WHERE id = ?",
                (market_id or "", job_id),
            )

    # ------------------------------------------------------------------
    # Company watch operations
    # ------------------------------------------------------------------

    def upsert_company_watch(
        self,
        *,
        company_name: str,
        careers_url: str,
        ats_provider: str | None,
        ats_identifier: str | None,
        discovered_from_job_id: int | None,
        promotion_source: str,
        confidence: float,
    ) -> int:
        """Insert or safely upgrade one normalized company watch target.

        Supported ATS targets outrank generic URLs, which outrank company-only
        entries. Equal-strength replacements require greater confidence, and a
        manual promotion source is never downgraded to automatic.
        """
        normalized_name = normalize_company_name(company_name)
        if not normalized_name:
            raise ValueError("company_name must normalize to a non-empty value")

        provider = (ats_provider or "").strip().lower() or None
        identifier = (ats_identifier or "").strip() or None
        careers_url = (careers_url or "").strip()
        now = _now_iso()

        with self._conn:
            insert = self._conn.execute(
                """
                INSERT INTO company_watch
                    (company_name, normalized_company_name, careers_url,
                     ats_provider, ats_identifier, discovered_from_job_id,
                     promotion_source, confidence, first_seen_at,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(normalized_company_name) DO NOTHING
                """,
                (
                    company_name,
                    normalized_name,
                    careers_url,
                    provider,
                    identifier,
                    discovered_from_job_id,
                    promotion_source,
                    confidence,
                    now,
                    now,
                    now,
                ),
            )
            if insert.rowcount == 1:
                return int(insert.lastrowid)

            row = self._conn.execute(
                "SELECT * FROM company_watch WHERE normalized_company_name = ?",
                (normalized_name,),
            ).fetchone()

            existing_strength = self._watch_endpoint_strength(
                row["careers_url"], row["ats_provider"], row["ats_identifier"]
            )
            candidate_strength = self._watch_endpoint_strength(
                careers_url, provider, identifier
            )
            replace_target = candidate_strength > existing_strength or (
                candidate_strength == existing_strength
                and confidence > row["confidence"]
            )
            retained_source = (
                "manual"
                if row["promotion_source"] == "manual" or promotion_source == "manual"
                else "automatic"
            )
            self._conn.execute(
                """
                UPDATE company_watch SET
                    careers_url = ?,
                    ats_provider = ?,
                    ats_identifier = ?,
                    discovered_from_job_id = COALESCE(?, discovered_from_job_id),
                    promotion_source = ?,
                    confidence = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    careers_url if replace_target else row["careers_url"],
                    provider if replace_target else row["ats_provider"],
                    identifier if replace_target else row["ats_identifier"],
                    discovered_from_job_id,
                    retained_source,
                    confidence if replace_target else row["confidence"],
                    now,
                    row["id"],
                ),
            )
            return int(row["id"])

    def get_company_watch(self, company_name: str) -> sqlite3.Row | None:
        """Return the normalized company watch row, if one exists."""
        normalized_name = normalize_company_name(company_name)
        if not normalized_name:
            return None
        return self._conn.execute(
            "SELECT * FROM company_watch WHERE normalized_company_name = ?",
            (normalized_name,),
        ).fetchone()

    def list_due_company_watches(self, now: datetime) -> list[sqlite3.Row]:
        """Return active watch targets whose health pause has expired."""
        timestamp = _normalize_utc(now).isoformat()
        return self._conn.execute(
            """
            SELECT * FROM company_watch
            WHERE active = 1
              AND (
                  paused_until IS NULL
                  OR julianday(paused_until) <= julianday(?)
              )
            ORDER BY id
            """,
            (timestamp,),
        ).fetchall()

    def record_watch_success(self, watch_id: int, now: datetime) -> None:
        """Record a verified endpoint check and clear its failure backoff."""
        timestamp = _normalize_utc(now).isoformat()
        with self._conn:
            self._conn.execute(
                """
                UPDATE company_watch SET
                    last_successful_check_at = ?,
                    last_verified_at = ?,
                    consecutive_failures = 0,
                    paused_until = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, timestamp, watch_id),
            )

    def record_watch_failure(self, watch_id: int, now: datetime) -> None:
        """Increment endpoint failures and apply the deterministic 24h pause."""
        normalized_now = _normalize_utc(now)
        timestamp = normalized_now.isoformat()
        paused_until = (normalized_now + timedelta(hours=24)).isoformat()
        with self._conn:
            self._conn.execute(
                """
                UPDATE company_watch SET
                    consecutive_failures = consecutive_failures + 1,
                    paused_until = CASE
                        WHEN consecutive_failures + 1 >= 3 THEN ?
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE id = ?
                """,
                (paused_until, timestamp, watch_id),
            )

    @staticmethod
    def _watch_endpoint_strength(
        careers_url: str, ats_provider: str | None, ats_identifier: str | None
    ) -> int:
        if ats_provider in _SUPPORTED_ATS_PROVIDERS and ats_identifier:
            return 3
        if careers_url:
            return 2
        return 1

    def upsert_ats_board(
        self,
        provider: str,
        board_identifier: str,
        company_name: str = "",
        market_hint: str = "",
    ) -> bool:
        """Insert or refresh one learned ATS board's discovery metadata.

        On conflict, updates display metadata and `last_seen_at` and
        reactivates the board, but leaves `paused_until` and
        `consecutive_failures` untouched — ordinary rediscovery must not
        bypass an unexpired pause; the board becomes due naturally once
        `paused_until` elapses.
        """
        provider = provider.strip().lower()
        if provider not in _SUPPORTED_ATS_PROVIDERS:
            raise ValueError(f"unsupported ATS provider: {provider!r}")
        board_identifier = board_identifier.strip()
        now = _now_iso()

        with self._conn:
            insert = self._conn.execute(
                """
                INSERT INTO ats_registry
                    (provider, board_identifier, company_name, market_hint,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, board_identifier) DO NOTHING
                """,
                (provider, board_identifier, company_name, market_hint, now, now),
            )
            if insert.rowcount == 1:
                return True

            self._conn.execute(
                """
                UPDATE ats_registry SET
                    company_name = COALESCE(NULLIF(?, ''), company_name),
                    market_hint = COALESCE(NULLIF(?, ''), market_hint),
                    last_seen_at = ?,
                    active = 1
                WHERE provider = ? AND board_identifier = ?
                """,
                (company_name, market_hint, now, provider, board_identifier),
            )
            return False

    def list_due_ats_boards(self, now: datetime) -> list[AtsRegistryEntry]:
        """Return active ATS boards whose health pause has expired."""
        timestamp = _normalize_utc(now).isoformat()
        rows = self._conn.execute(
            """
            SELECT * FROM ats_registry
            WHERE active = 1
              AND (
                  paused_until IS NULL
                  OR julianday(paused_until) <= julianday(?)
              )
            ORDER BY provider, board_identifier
            """,
            (timestamp,),
        ).fetchall()
        return [self._ats_entry_from_row(row) for row in rows]

    def record_ats_scan_success(
        self, provider: str, board_identifier: str, now: datetime, job_count: int
    ) -> None:
        """Record a successful scan and clear the board's failure backoff."""
        timestamp = _normalize_utc(now).isoformat()
        with self._conn:
            self._conn.execute(
                """
                UPDATE ats_registry SET
                    last_checked_at = ?,
                    last_success_at = ?,
                    last_job_count = ?,
                    consecutive_failures = 0,
                    paused_until = NULL
                WHERE provider = ? AND board_identifier = ?
                """,
                (timestamp, timestamp, job_count, provider, board_identifier),
            )

    def record_ats_scan_failure(
        self,
        provider: str,
        board_identifier: str,
        now: datetime,
        *,
        permanent: bool = False,
    ) -> None:
        """Increment scan failures and back off the board.

        Every failure is isolated per board and gets the same 24h pause so
        it retries soon. `consecutive_failures` is a single shared counter
        incremented by every failure, transient or permanent alike — it does
        not track which kind of failure contributed to it. `permanent=True`
        (a stale-looking 404) additionally escalates: once that shared
        counter reaches `_STALE_BOARD_DEACTIVATION_THRESHOLD`, a permanent
        failure deactivates the board (`active = 0`) instead of just pausing
        it. Transient failures accrue toward the same counter but never
        deactivate a board on their own — only a `permanent=True` call checks
        the threshold and can flip `active`. So a mixed sequence such as
        [transient, transient, permanent] deactivates the board on that
        single 404, not only after three permanent failures in a row.

        Deactivation preserves registry history — it is not a delete — and
        `upsert_ats_board` already reactivates any inactive board the next
        time it is rediscovered. That reactivation only sets `active = 1`;
        it does not reset `consecutive_failures`, so a rediscovered board
        that immediately fails again resumes from its prior strike count and
        can re-deactivate right away, not after three fresh strikes.
        """
        normalized_now = _normalize_utc(now)
        timestamp = normalized_now.isoformat()
        paused_until = (normalized_now + timedelta(hours=24)).isoformat()
        with self._conn:
            if permanent:
                self._conn.execute(
                    """
                    UPDATE ats_registry SET
                        last_checked_at = ?,
                        consecutive_failures = consecutive_failures + 1,
                        paused_until = ?,
                        active = CASE
                            WHEN consecutive_failures + 1 >= ? THEN 0
                            ELSE active
                        END
                    WHERE provider = ? AND board_identifier = ?
                    """,
                    (
                        timestamp,
                        paused_until,
                        _STALE_BOARD_DEACTIVATION_THRESHOLD,
                        provider,
                        board_identifier,
                    ),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE ats_registry SET
                        last_checked_at = ?,
                        consecutive_failures = consecutive_failures + 1,
                        paused_until = ?
                    WHERE provider = ? AND board_identifier = ?
                    """,
                    (timestamp, paused_until, provider, board_identifier),
                )

    def record_ats_eligible_job(
        self, provider: str, board_identifier: str, now: datetime
    ) -> None:
        """Record that a scan of this board surfaced a candidate-eligible job."""
        timestamp = _normalize_utc(now).isoformat()
        with self._conn:
            self._conn.execute(
                """
                UPDATE ats_registry SET
                    last_eligible_at = ?,
                    eligible_jobs_seen = eligible_jobs_seen + 1
                WHERE provider = ? AND board_identifier = ?
                """,
                (timestamp, provider, board_identifier),
            )

    def count_ats_boards(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM ats_registry").fetchone()
        return row[0]

    @staticmethod
    def _ats_entry_from_row(row: sqlite3.Row) -> AtsRegistryEntry:
        return AtsRegistryEntry(
            provider=row["provider"],
            board_identifier=row["board_identifier"],
            company_name=row["company_name"],
            market_hint=row["market_hint"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
            last_checked_at=row["last_checked_at"],
            last_success_at=row["last_success_at"],
            last_eligible_at=row["last_eligible_at"],
            last_job_count=row["last_job_count"],
            eligible_jobs_seen=row["eligible_jobs_seen"],
            consecutive_failures=row["consecutive_failures"],
            active=bool(row["active"]),
            paused_until=row["paused_until"],
        )

    def count_jobs(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()
        return row[0]

    def list_jobs_for_matching(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT id, source_job_id, url, company, title, first_seen_at, last_seen_at
            FROM jobs
            ORDER BY id
            """
        ).fetchall()

    # ------------------------------------------------------------------
    # Gmail sync and staging operations
    # ------------------------------------------------------------------

    def has_processed_gmail_message(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM gmail_messages WHERE message_id = ? LIMIT 1",
            (message_id,),
        ).fetchone()
        return row is not None

    def record_gmail_message(
        self,
        *,
        message_id: str,
        thread_id: str | None,
        sender: str,
        subject: str,
        occurred_at: str,
        classification: str,
        confidence: float,
        rationale: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO gmail_messages
                    (message_id, thread_id, sender, subject, occurred_at,
                     classification, confidence, rationale, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    thread_id,
                    sender,
                    subject,
                    occurred_at,
                    classification,
                    confidence,
                    rationale,
                    _now_iso(),
                ),
            )

    def get_gmail_sync_state(self, account_id: str) -> sqlite3.Row | None:
        table = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'gmail_sync_state'"
        ).fetchone()
        if table is None:
            return None
        return self._conn.execute(
            "SELECT * FROM gmail_sync_state WHERE account_id = ?",
            (account_id,),
        ).fetchone()

    def save_gmail_sync_state(
        self,
        account_id: str,
        history_id: str | None,
        last_successful_sync_at: str | None,
        backfill_completed_at: str | None,
    ) -> None:
        now = _now_iso()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO gmail_sync_state
                    (account_id, history_id, last_successful_sync_at,
                     backfill_completed_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    history_id = excluded.history_id,
                    last_successful_sync_at = excluded.last_successful_sync_at,
                    backfill_completed_at = excluded.backfill_completed_at,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    history_id,
                    last_successful_sync_at,
                    backfill_completed_at,
                    now,
                    now,
                ),
            )

    def stage_inbound_job(
        self,
        source_message_id: str,
        source_candidate_key: str,
        job: ExtractedJob,
    ) -> int:
        now = _now_iso()
        remote = None if job.remote is None else int(job.remote)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO inbound_job_candidates
                    (origin, source_message_id, source_candidate_key,
                     source_platform, source_job_id, url, company, title,
                     location, remote, description, created_at, last_seen_at)
                VALUES ('gmail', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin, source_message_id, source_candidate_key) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    source_message_id,
                    source_candidate_key,
                    job.source_platform,
                    job.source_job_id,
                    job.url,
                    job.company,
                    job.title,
                    job.location,
                    remote,
                    "",
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                """
                SELECT id FROM inbound_job_candidates
                WHERE origin = 'gmail' AND source_message_id = ?
                  AND source_candidate_key = ?
                """,
                (source_message_id, source_candidate_key),
            ).fetchone()
        return row["id"]

    def list_unmaterialized_inbound_jobs(self) -> list[sqlite3.Row]:
        candidates = self._conn.execute(
            "SELECT * FROM inbound_job_candidates ORDER BY id"
        ).fetchall()
        jobs = self._conn.execute(
            """
            SELECT source, source_job_id, url, company, title, location
            FROM jobs
            """
        ).fetchall()

        return [
            candidate
            for candidate in candidates
            if not any(
                self._matches_materialized_job(candidate, job) for job in jobs
            )
        ]

    @staticmethod
    def _matches_materialized_job(
        candidate: sqlite3.Row, job: sqlite3.Row
    ) -> bool:
        if (
            job["source"] == f"gmail:{candidate['source_platform']}"
            and job["source_job_id"] == candidate["source_candidate_key"]
        ):
            return True

        if candidate["url"] and job["url"]:
            if canonicalize_url(candidate["url"]) == canonicalize_url(job["url"]):
                return True

        candidate_identity = "|".join(
            normalize_text(value)
            for value in (candidate["company"], candidate["title"], candidate["location"])
        )
        job_identity = "|".join(
            normalize_text(value)
            for value in (job["company"], job["title"], job["location"])
        )
        return candidate_identity != "||" and candidate_identity == job_identity

    def save_application_event(
        self,
        *,
        job_id: int | None,
        event_type: str,
        occurred_at: str,
        source_message_id: str,
        source_thread_id: str | None,
        confidence: float,
        company: str,
        role_title: str,
        rationale: str,
        source: str = "gmail",
    ) -> int:
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                """
                INSERT OR IGNORE INTO application_events
                    (job_id, event_type, occurred_at, source, source_message_id,
                     source_thread_id, confidence, company, role_title,
                     rationale, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    event_type,
                    occurred_at,
                    source,
                    source_message_id,
                    source_thread_id,
                    confidence,
                    company,
                    role_title,
                    rationale,
                    _now_iso(),
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM application_events WHERE source_message_id = ?",
                (source_message_id,),
            ).fetchone()
        return row["id"]

    def list_application_events(self, job_id: int) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT * FROM application_events
            WHERE job_id = ?
            ORDER BY occurred_at, id
            """,
            (job_id,),
        ).fetchall()

    def current_application_state(self, job_id: int) -> str | None:
        from job_hunter.gmail_matching import derive_application_state

        return derive_application_state(self.list_application_events(job_id))

    def pending_review_events(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT e.*, m.subject
            FROM application_events e
            JOIN gmail_messages m ON m.message_id = e.source_message_id
            LEFT JOIN review_deliveries d ON d.event_id = e.id
            WHERE d.event_id IS NULL
              AND (
                  e.event_type = 'REVIEW_NEEDED'
                  OR (
                      e.event_type IN (
                          'RECRUITER_CONTACT', 'APPLIED', 'INTERVIEW',
                          'TECHNICAL', 'OFFER', 'REJECTED'
                      )
                      AND (
                          e.job_id IS NULL
                          OR e.confidence < ?
                      )
                  )
              )
            ORDER BY e.occurred_at, e.id
            """,
            (AUTO_CONFIDENCE_THRESHOLD,),
        ).fetchall()

    def mark_review_delivered(
        self, event_ids: list[int], telegram_message_id: str
    ) -> None:
        if not event_ids:
            return
        with self._conn:
            self._conn.executemany(
                """
                INSERT OR IGNORE INTO review_deliveries
                    (event_id, delivered_at, telegram_message_id)
                VALUES (?, ?, ?)
                """,
                [(event_id, _now_iso(), telegram_message_id) for event_id in event_ids],
            )

    def release_legacy_gmail_semantic_failures(self) -> int:
        """Remove only legacy synthetic technical reviews so Gmail can retry them."""
        rationale = LEGACY_SEMANTIC_FAILURE_RATIONALE
        with self._conn:
            rows = self._conn.execute(
                """
                SELECT message_id
                FROM gmail_messages
                WHERE classification = 'REVIEW_NEEDED'
                  AND rationale = ?
                """,
                (rationale,),
            ).fetchall()
            message_ids = [row["message_id"] for row in rows]
            if not message_ids:
                return 0

            placeholders = ",".join("?" for _ in message_ids)
            event_rows = self._conn.execute(
                f"""
                SELECT id
                FROM application_events
                WHERE source = 'gmail'
                  AND event_type = 'REVIEW_NEEDED'
                  AND rationale = ?
                  AND source_message_id IN ({placeholders})
                """,
                (rationale, *message_ids),
            ).fetchall()
            event_ids = [row["id"] for row in event_rows]
            if event_ids:
                event_placeholders = ",".join("?" for _ in event_ids)
                self._conn.execute(
                    f"DELETE FROM review_deliveries WHERE event_id IN ({event_placeholders})",
                    event_ids,
                )
                self._conn.execute(
                    f"DELETE FROM application_events WHERE id IN ({event_placeholders})",
                    event_ids,
                )

            self._conn.execute(
                f"DELETE FROM gmail_messages WHERE message_id IN ({placeholders})",
                message_ids,
            )
        return len(message_ids)

    # ------------------------------------------------------------------
    # Evaluation operations
    # ------------------------------------------------------------------

    def needs_evaluation(self, job_id: int) -> bool:
        """
        Return True when the job should be evaluated (or re-evaluated).

        Conditions:
        - No evaluation row exists, OR
        - Most recent evaluation has status == 'failed', OR
        - The job description changed since the most recent evaluation, OR
        - The job's content_confidence changed since the most recent evaluation.
        """
        row = self._conn.execute(
            """
            SELECT e.status, e.description_hash_at_eval, j.description_hash,
                   e.content_confidence_at_eval, j.content_confidence
            FROM   evaluations e
            JOIN   jobs j ON j.id = e.job_id
            WHERE  e.job_id = ?
            ORDER  BY e.id DESC
            LIMIT  1
            """,
            (job_id,),
        ).fetchone()

        if row is None:
            return True

        if row["status"] == "failed":
            return True

        if row["description_hash_at_eval"] != row["description_hash"]:
            return True

        if row["content_confidence_at_eval"] != row["content_confidence"]:
            return True

        return False

    def save_evaluation(self, job_id: int, evaluation: Evaluation) -> None:
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            row = self._conn.execute(
                "SELECT description_hash, content_confidence FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            description_hash = row["description_hash"] if row else ""
            row_content_confidence = row["content_confidence"] if row else ""
            content_confidence_value = evaluation.content_confidence or row_content_confidence or ""
            self._conn.execute(
                """
                INSERT INTO evaluations
                    (job_id, total_score, scores_json, decision,
                     hard_blockers_json, strengths_json, gaps_json,
                     salary_note, location_note, rationale, model, status,
                     market_id, description_hash_at_eval, content_confidence_at_eval,
                     requirements_json, raw_model_score, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    evaluation.total_score,
                    json.dumps(evaluation.scores),
                    evaluation.decision,
                    json.dumps(evaluation.hard_blockers),
                    json.dumps(evaluation.strengths),
                    json.dumps(evaluation.gaps),
                    evaluation.salary_note,
                    evaluation.location_note,
                    evaluation.rationale,
                    evaluation.model,
                    evaluation.status,
                    evaluation.market_id,
                    description_hash,
                    content_confidence_value,
                    json.dumps(evaluation.requirements),
                    evaluation.raw_model_score,
                    _now_iso(),
                ),
            )

    # ------------------------------------------------------------------
    # Material operations
    # ------------------------------------------------------------------

    def save_material(self, job_id: int, material: Material) -> None:
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                """
                INSERT INTO materials (job_id, cover_letter_text, generated_at)
                VALUES (?, ?, ?)
                """,
                (job_id, material.cover_letter_text, _now_iso()),
            )

    # ------------------------------------------------------------------
    # Delivery operations
    # ------------------------------------------------------------------

    def mark_delivered(
        self,
        job_id: int,
        delivery_type: str,
        telegram_id: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                """
                INSERT INTO deliveries
                    (job_id, delivery_type, status, delivered_at, telegram_message_id)
                VALUES (?, ?, 'sent', ?, ?)
                """,
                (job_id, delivery_type, _now_iso(), telegram_id),
            )

    def has_delivery(self, job_id: int, delivery_type: str | None = None) -> bool:
        if delivery_type is None:
            row = self._conn.execute(
                "SELECT 1 FROM deliveries WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM deliveries WHERE job_id = ? AND delivery_type = ? LIMIT 1",
                (job_id, delivery_type),
            ).fetchone()
        return row is not None

    def pending_delivery_job_ids(self) -> list[int]:
        rows = self._conn.execute("""
            SELECT j.id, e.decision, e.total_score,
                   EXISTS (SELECT 1 FROM deliveries d WHERE d.job_id=j.id AND d.delivery_type='telegram_message') AS msg,
                   EXISTS (SELECT 1 FROM deliveries d WHERE d.job_id=j.id AND d.delivery_type='telegram_document') AS doc
            FROM jobs j JOIN evaluations e ON e.id=(SELECT MAX(id) FROM evaluations WHERE job_id=j.id)
        """).fetchall()
        return [
            r["id"]
            for r in rows
            if r["total_score"] > _DELIVERABLE_SCORE_FLOOR
            and (
                (r["decision"] == "possible_match" and not r["msg"])
                or (r["decision"] in ("high_priority", "package_match") and not r["msg"])
            )
        ]

    # ------------------------------------------------------------------
    # Retrieval for delivery-retry (avoid re-calling Gemini)
    # ------------------------------------------------------------------

    def get_job(self, job_id: int) -> Job | None:
        row = self._conn.execute(
            """
            SELECT source, title, company, location, url, description,
                   source_job_id, remote, market_id, content_confidence
            FROM jobs WHERE id = ?
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return Job(
            source=row["source"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            url=row["url"],
            description=row["description"],
            source_job_id=row["source_job_id"],
            remote=None if row["remote"] is None else bool(row["remote"]),
            market_id=row["market_id"] or None,
            content_confidence=row["content_confidence"] or "",
        )

    def get_evaluation(self, job_id: int) -> Evaluation | None:
        row = self._conn.execute(
            """
            SELECT total_score, scores_json, decision, hard_blockers_json,
                   strengths_json, gaps_json, salary_note, location_note,
                   rationale, model, status, market_id, content_confidence_at_eval,
                   requirements_json, raw_model_score
            FROM evaluations
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return Evaluation(
            job_id=job_id,
            total_score=row["total_score"],
            scores=json.loads(row["scores_json"]),
            decision=row["decision"],
            hard_blockers=json.loads(row["hard_blockers_json"]),
            strengths=json.loads(row["strengths_json"]),
            gaps=json.loads(row["gaps_json"]),
            salary_note=row["salary_note"],
            location_note=row["location_note"],
            rationale=row["rationale"],
            model=row["model"],
            status=row["status"],
            market_id=row["market_id"],
            content_confidence=row["content_confidence_at_eval"],
            requirements=json.loads(row["requirements_json"]) if row["requirements_json"] else {},
            raw_model_score=row["raw_model_score"],
        )

    def get_material(self, job_id: int) -> Material | None:
        row = self._conn.execute(
            """
            SELECT cover_letter_text FROM materials
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return Material(job_id=job_id, cover_letter_text=row["cover_letter_text"])

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *_) -> None:
        self.close()

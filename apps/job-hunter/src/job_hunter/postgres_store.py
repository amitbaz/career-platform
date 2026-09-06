"""Postgres-backed persistence layer for the job hunter bot.

Replaces the SQLite-backed `JobStore` in `store.py`, porting it table by
table over the course of issue #70's task series onto `SupabaseClient`
and `Supabase`'s row-level security. This module currently holds only the
class shell: the constructor and the context-manager/`close()` surface
existing call sites already depend on. Every read/write method arrives in
a later task, built on the row-mapping helpers in `store_mapping.py`.

There is no `read_only` mode. The SQLite original had one so the webhook
could open a snapshot without creating tables; there is no snapshot
concept against Postgres, and `__init__` does no schema work at all --
migrations own the schema now (see `supabase/migrations/`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.canonical import parse_supported_ats_url
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import AtsRegistryEntry, Evaluation, Job, Material
from job_hunter.normalize import job_fingerprint
from job_hunter.store_mapping import (
    ats_entry_from_row,
    evaluation_from_row,
    job_from_row,
    material_from_row,
    to_iso,
    touch,
)
from job_hunter.supabase_client import SupabaseClient

# Translates store.py's `_DELIVERABLE_SCORE_FLOOR`. A job must score strictly
# above this to ever be a delivery candidate.
_DELIVERABLE_SCORE_FLOOR = 60

# The tie-break `pending_delivery_job_ids`'s SQL function and the two
# "latest row" reads below share: newest `evaluated_at`/`generated_at` wins,
# with `created_at` then `id` as a deterministic (if practically unreachable
# -- see the two methods' docstrings) fallback.
_LATEST_EVALUATION_ORDER = "evaluated_at.desc,created_at.desc,id.desc"
_LATEST_MATERIAL_ORDER = "generated_at.desc,created_at.desc,id.desc"


# Translates store.py's `_SUPPORTED_ATS_PROVIDERS` and
# `_STALE_BOARD_DEACTIVATION_THRESHOLD`. Redefined here rather than imported
# because `store.py` is deleted at the end of this port.
_SUPPORTED_ATS_PROVIDERS = frozenset({"ashby", "greenhouse", "lever"})
_STALE_BOARD_DEACTIVATION_THRESHOLD = 3

# A company watch pauses on its third consecutive failure; an ATS board
# pauses on every failure. Both pause for the same fixed span.
_WATCH_PAUSE_THRESHOLD = 3
_HEALTH_PAUSE = timedelta(hours=24)


def _require_aware(now: datetime) -> datetime:
    """Return an aware datetime as UTC, or reject an ambiguous naive one.

    Translates store.py's `_normalize_utc`. `store_mapping.to_iso` treats a
    naive datetime as already being UTC, which is right for an instant the
    store stamped itself but wrong for one a caller supplied -- a caller's
    naive local time would be silently recorded as UTC. Every method that
    takes `now` from its caller passes it through here first.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _watch_endpoint_strength(
    careers_url: str, ats_provider: str | None, ats_identifier: str | None
) -> int:
    """Rank a watch endpoint: supported ATS > generic URL > company only."""
    if ats_provider in _SUPPORTED_ATS_PROVIDERS and ats_identifier:
        return 3
    if careers_url:
        return 2
    return 1


class PostgresJobStore:
    """Persistence layer backed by a user-scoped `SupabaseClient`.

    `__init__` stores the client and nothing else: no schema creation, no
    migration machinery, no lazy table setup. `close()`, `__enter__`, and
    `__exit__` are no-ops beyond `close()` itself -- kept only so call
    sites written against the SQLite store's context-manager usage don't
    need to change.
    """

    def __init__(self, client: SupabaseClient) -> None:
        self._client = client

    def close(self) -> None:
        pass

    def __enter__(self) -> "PostgresJobStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def _canonicalize_url(self, url: str) -> str:
        """Canonicalize a URL through the SQL authority, not Python's own.

        `job_hunter_canonicalize_url` is the sole authority for canonical
        URLs and identity keys (migration 202609060004): it disagrees with
        `normalize.canonicalize_url` both on percent-encoding (Python's
        `parse_qsl`/`urlencode` round trip turns `%20` into `+`; SQL leaves
        the raw text alone) and on tracking-param ordering (SQL sorts
        `"k=v"` strings under `collate "C"`; Python sorts `(k, v)` tuples).
        Every RPC-backed write (`job_hunter_upsert_job`) computes
        `canonical_url` and `job_sources.identity_key` with the SQL
        function, so a Python-side computation here would silently diverge
        from what's already stored -- a URL differing only in encoding
        would miss a job that exists, or `record_job_source` would write a
        second provenance row for an identity `job_hunter_upsert_job`
        already recorded under a differently-encoded key.
        """
        return self._client.rpc("job_hunter_canonicalize_url", {"p_url": url})[0]

    @staticmethod
    def _job_payload(job: Job) -> dict[str, Any]:
        """Build the jsonb payload `job_hunter_upsert_job` expects.

        Every recognised key is included, `match_mode` excepted -- callers
        add that themselves so the shared helper stays agnostic to which of
        the two upsert behaviours is being invoked. `fingerprint` is
        computed here, in Python, and never recomputed in SQL: see the
        migration's note on `job_hunter_upsert_job` for why.
        """
        return {
            "fingerprint": job_fingerprint(job),
            "source": job.source or "",
            "source_job_id": job.source_job_id,
            "url": job.url or "",
            "canonical_url": job.canonical_url or "",
            "company": job.company or "",
            "title": job.title or "",
            "location": job.location or "",
            "remote": job.remote,
            "description": job.description or "",
            "content_confidence": job.content_confidence or "",
            "ats_provider": job.ats_provider,
            "ats_board": job.ats_board,
            "ats_job_id": job.ats_job_id,
            "original_url": job.original_url or "",
        }

    def upsert_job(self, job: Job) -> tuple[str, bool, bool]:
        """Insert or update a job record, matched by fingerprint alone.

        Translates store.py:649-743. The narrow upsert never merges
        duplicates -- see `upsert_logical_job` for the identity-resolving
        path. `match_mode: "fingerprint"` selects that branch inside
        `job_hunter_upsert_job`; omitting it (as `upsert_logical_job` does)
        defaults to the merging 'logical' branch.
        """
        payload = self._job_payload(job)
        payload["match_mode"] = "fingerprint"
        row = self._client.rpc("job_hunter_upsert_job", {"p_job": payload})[0]
        return row["id"], row["is_new"], row["description_changed"]

    def upsert_logical_job(self, job: Job) -> tuple[str, bool, bool]:
        """Persist a source-independent logical job and its provenance.

        Translates store.py:744-905. Identity is resolved from strongest to
        weakest exact evidence (canonical URL, ATS triple, normalized
        company/title/location, fingerprint) and every duplicate found is
        merged into one survivor. The return shape matches `upsert_job`.
        """
        payload = self._job_payload(job)
        row = self._client.rpc("job_hunter_upsert_job", {"p_job": payload})[0]
        return row["id"], row["is_new"], row["description_changed"]

    def merge_jobs(self, survivor_id: str, duplicate_id: str) -> str:
        """Transactionally merge a duplicate job and all attached records.

        Translates store.py:906-1038 (`merge_jobs`/`_merge_jobs`) into a
        single call to `job_hunter_merge_jobs`, which returns a bare scalar
        uuid -- a one-element list, not a row dict. `retry=False` is
        required: the merge is not idempotent, and `HttpClient` retries
        POST on 5xx, so a retried merge on a transient error would merge
        the same duplicate twice.
        """
        result = self._client.rpc(
            "job_hunter_merge_jobs",
            {"p_survivor": survivor_id, "p_duplicate": duplicate_id},
            retry=False,
        )
        return result[0]

    def record_job_source(
        self,
        job_id: str,
        *,
        source: str,
        source_job_id: str | None,
        source_url: str,
    ) -> None:
        """Record a discovery source once while refreshing its last-seen time.

        Translates store.py:1090-1140. On a repeat call for the same
        identity key, `source`, `source_job_id`, `source_url`, and
        `first_seen_at` must not move -- the SQLite original's
        `ON CONFLICT ... DO UPDATE SET last_seen_at = excluded.last_seen_at`
        leaves all four untouched, but `SupabaseClient.upsert` issues a
        merge-duplicates PATCH-via-POST that would overwrite every column.
        Reading back any existing row's four columns first and carrying
        them forward reproduces the original's selective-column behaviour;
        only a genuinely new identity uses this call's arguments and `now`.
        """
        identity_key = (
            f"id:{source}:{source_job_id}"
            if source_job_id
            else f"url:{self._canonicalize_url(source_url)}"
        )
        now = to_iso(datetime.now(timezone.utc))
        existing = self._client.select(
            "job_hunter_job_sources",
            params={
                "job_id": f"eq.{job_id}",
                "identity_key": f"eq.{identity_key}",
                "limit": "1",
                "select": "source,source_job_id,source_url,first_seen_at",
            },
        )
        if existing:
            row = existing[0]
            source = row["source"]
            source_job_id = row["source_job_id"]
            source_url = row["source_url"]
            first_seen_at = row["first_seen_at"]
        else:
            first_seen_at = now
        self._client.upsert(
            "job_hunter_job_sources",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "source": source,
                    "source_job_id": source_job_id,
                    "source_url": source_url,
                    "identity_key": identity_key,
                    "first_seen_at": first_seen_at,
                    "last_seen_at": now,
                }
            ],
            on_conflict="job_id,identity_key",
        )

    def list_job_sources(self, job_id: str) -> list[dict[str, Any]]:
        """Return source provenance for a job in insertion order.

        Translates store.py:1142-1146. Ids are random uuids now, so
        `created_at` (with `select`'s `id.asc` tie-breaker) replaces
        `ORDER BY id` as the insertion-order proxy.
        """
        return self._client.select(
            "job_hunter_job_sources",
            params={"job_id": f"eq.{job_id}", "order": "created_at.asc"},
        )

    def find_job_by_canonical_url(self, url: str) -> str | None:
        """Return a job ID only when a canonical URL identifies one job.

        Translates store.py:1148-1157.
        """
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "canonical_url": f"eq.{self._canonicalize_url(url)}",
                "select": "id",
            },
        )
        return rows[0]["id"] if len(rows) == 1 else None

    def find_job_by_ats(
        self, provider: str, board: str, job_id: str | None
    ) -> str | None:
        """Return a job ID only when an ATS tuple identifies one job.

        Translates store.py:1159-1178.
        """
        if not job_id:
            return None
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "ats_provider": f"eq.{provider}",
                "ats_board": f"eq.{board}",
                "ats_job_id": f"eq.{job_id}",
                "select": "id",
            },
        )
        return rows[0]["id"] if len(rows) == 1 else None

    def find_job_by_identity(
        self, company: str, title: str, location: str
    ) -> str | None:
        """Return a job ID only for one normalized company/title/location match.

        Translates store.py:1180-1222. `job_hunter_find_job_by_identity`
        returns `setof uuid` -- a plain list of id strings, not row dicts --
        and the row is taken only when exactly one comes back. `limit 1` (or
        indexing `result[0]` unconditionally) would silently accept an
        ambiguous match as a confident one.
        """
        result = self._client.rpc(
            "job_hunter_find_job_by_identity",
            {"p_company": company, "p_title": title, "p_location": location},
        )
        return result[0] if len(result) == 1 else None

    def set_job_market(self, job_id: str, market_id: str | None) -> None:
        """Persist the primary market a job has been attributed to.

        Translates store.py:1223-1229.
        """
        self._client.update(
            "job_hunter_jobs",
            {"market_id": market_id or ""},
            params={"id": f"eq.{job_id}"},
        )

    def count_jobs(self) -> int:
        """Translates store.py:1662-1664."""
        rows = self._client.select("job_hunter_jobs", params={"select": "id"})
        return len(rows)

    def list_jobs_for_matching(self) -> list[dict[str, Any]]:
        """Translates store.py:1666-1673.

        Ids are random uuids now, so `created_at` (with `select`'s
        `id.asc` tie-breaker) replaces `ORDER BY id` as the insertion-order
        proxy.
        """
        return self._client.select(
            "job_hunter_jobs",
            params={
                "select": "id,source_job_id,url,company,title,first_seen_at,last_seen_at",
                "order": "created_at.asc",
            },
        )

    def get_job(self, job_id: str) -> Job | None:
        """Translates store.py:2147-2169."""
        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "id": f"eq.{job_id}",
                "select": (
                    "source,title,company,location,url,description,"
                    "source_job_id,remote,market_id,content_confidence"
                ),
            },
        )
        if not rows:
            return None
        return job_from_row(rows[0])

    def backfill_ats_identity(self) -> int:
        """Attribute stored jobs that have a supported ATS URL but no identity.

        Translates store.py:399-457. SQLite's `LIKE` is case-insensitive;
        Postgres's is not, so the host-match filter below uses `ilike`
        (PostgREST's `*` wildcard alias) instead. Never overwrites a field
        that is already set -- only an empty/missing `ats_provider`,
        `ats_board`, or `ats_job_id` is backfilled from the parsed
        reference. Returns how many rows were updated.
        """
        missing_identity = "or(" + ",".join(
            [
                "ats_provider.is.null",
                "ats_provider.eq.",
                "ats_board.is.null",
                "ats_board.eq.",
                "ats_job_id.is.null",
                "ats_job_id.eq.",
            ]
        ) + ")"
        host_terms = [
            f"{column}.ilike.*{host}/*"
            for column in ("url", "canonical_url")
            for host in SUPPORTED_ATS_HOSTS
        ]
        host_match = "or(" + ",".join(host_terms) + ")"

        rows = self._client.select(
            "job_hunter_jobs",
            params={
                "and": f"({missing_identity},{host_match})",
                "select": "id,url,canonical_url,ats_provider,ats_board,ats_job_id",
            },
        )

        updated = 0
        for row in rows:
            reference = None
            for url in (row.get("canonical_url"), row.get("url")):
                if not url:
                    continue
                reference = parse_supported_ats_url(url)
                if reference is not None:
                    break
            if reference is None:
                continue
            self._client.update(
                "job_hunter_jobs",
                {
                    "ats_provider": row.get("ats_provider") or reference.provider,
                    "ats_board": row.get("ats_board") or reference.board,
                    "ats_job_id": row.get("ats_job_id") or reference.job_id,
                },
                params={"id": f"eq.{row['id']}"},
            )
            updated += 1
        return updated

    # ------------------------------------------------------------------
    # Evaluations
    # ------------------------------------------------------------------

    def needs_evaluation(self, job_id: str) -> bool:
        """Translates store.py:1999-2034.

        The SQLite original joined `evaluations` to `jobs` in one query and
        ordered by `e.id DESC` to find the most recent evaluation. Ids are
        random uuids now, so `id DESC` is meaningless; this orders by
        `evaluated_at` (with the usual `created_at`/`id` fallback -- see
        `get_evaluation`) instead, in two requests rather than one PostgREST
        embed, matching this module's existing two-step pattern (e.g.
        `record_job_source`).
        """
        evaluations = self._client.select(
            "job_hunter_evaluations",
            params={
                "job_id": f"eq.{job_id}",
                "select": "status,description_hash_at_eval,content_confidence_at_eval",
                "order": _LATEST_EVALUATION_ORDER,
                "limit": "1",
            },
        )
        if not evaluations:
            return True
        evaluation = evaluations[0]
        if evaluation["status"] == "failed":
            return True

        jobs = self._client.select(
            "job_hunter_jobs",
            params={"id": f"eq.{job_id}", "select": "description_hash,content_confidence"},
        )
        job_row = jobs[0] if jobs else {}
        if evaluation["description_hash_at_eval"] != (job_row.get("description_hash") or ""):
            return True
        if evaluation["content_confidence_at_eval"] != (job_row.get("content_confidence") or ""):
            return True
        return False

    def save_evaluation(self, job_id: str, evaluation: Evaluation) -> None:
        """Translates store.py:2036-2075.

        Upserts against `job_hunter_evaluations`'s
        `(user_id, job_id, evaluated_at)` constraint rather than inserting --
        `HttpClient` retries POST on 5xx, so a plain insert here would
        double-write on a transient error. `evaluated_at` is stamped now,
        same as the original's `_now_iso()`; nothing about the evaluation
        itself carries a caller-supplied timestamp to preserve.
        """
        jobs = self._client.select(
            "job_hunter_jobs",
            params={"id": f"eq.{job_id}", "select": "description_hash,content_confidence"},
        )
        job_row = jobs[0] if jobs else {}
        description_hash = job_row.get("description_hash") or ""
        content_confidence_value = (
            evaluation.content_confidence or job_row.get("content_confidence") or ""
        )
        self._client.upsert(
            "job_hunter_evaluations",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "total_score": evaluation.total_score,
                    "scores_json": evaluation.scores,
                    "decision": evaluation.decision,
                    "hard_blockers_json": evaluation.hard_blockers,
                    "strengths_json": evaluation.strengths,
                    "gaps_json": evaluation.gaps,
                    "salary_note": evaluation.salary_note,
                    "location_note": evaluation.location_note,
                    "rationale": evaluation.rationale,
                    "model": evaluation.model,
                    "status": evaluation.status,
                    "market_id": evaluation.market_id,
                    "description_hash_at_eval": description_hash,
                    "content_confidence_at_eval": content_confidence_value,
                    "requirements_json": evaluation.requirements,
                    "raw_model_score": evaluation.raw_model_score,
                    "evaluated_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,job_id,evaluated_at",
        )

    def get_evaluation(self, job_id: str) -> Evaluation | None:
        """Translates store.py:2171-2204.

        "Latest evaluation" means newest `evaluated_at` now, not highest
        `id` -- ids are random uuids, so max-id is meaningless. Ties are
        broken by `created_at` then `id`, matching
        `job_hunter_pending_delivery_jobs`'s per-job ordering. In practice a
        tie on `evaluated_at` for one job can't arise: `save_evaluation`
        always upserts against `(user_id, job_id, evaluated_at)`, so two
        saves that land on the same instant converge into one row instead
        of leaving two to choose between (see the dedicated test for this).
        The tie-break stays here anyway, matching the SQL function's
        pattern, as a defensive-in-depth safeguard.
        """
        rows = self._client.select(
            "job_hunter_evaluations",
            params={
                "job_id": f"eq.{job_id}",
                "select": (
                    "job_id,total_score,scores_json,decision,hard_blockers_json,"
                    "strengths_json,gaps_json,salary_note,location_note,rationale,"
                    "model,status,market_id,content_confidence_at_eval,"
                    "requirements_json,raw_model_score"
                ),
                "order": _LATEST_EVALUATION_ORDER,
                "limit": "1",
            },
        )
        if not rows:
            return None
        return evaluation_from_row(rows[0])

    # ------------------------------------------------------------------
    # Materials
    # ------------------------------------------------------------------

    def save_material(self, job_id: str, material: Material) -> None:
        """Translates store.py:2081-2090.

        Upserts against `job_hunter_materials`'s `(user_id, job_id,
        generated_at)` constraint, never inserts -- same retry-safety
        reasoning as `save_evaluation`.
        """
        self._client.upsert(
            "job_hunter_materials",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "cover_letter_text": material.cover_letter_text,
                    "generated_at": to_iso(datetime.now(timezone.utc)),
                }
            ],
            on_conflict="user_id,job_id,generated_at",
        )

    def get_material(self, job_id: str) -> Material | None:
        """Translates store.py:2206-2218.

        Same "latest" reasoning as `get_evaluation`: newest `generated_at`
        replaces highest `id`, with the same `created_at`/`id` fallback.
        """
        rows = self._client.select(
            "job_hunter_materials",
            params={
                "job_id": f"eq.{job_id}",
                "select": "job_id,cover_letter_text",
                "order": _LATEST_MATERIAL_ORDER,
                "limit": "1",
            },
        )
        if not rows:
            return None
        return material_from_row(rows[0])

    # ------------------------------------------------------------------
    # Deliveries
    # ------------------------------------------------------------------

    def mark_delivered(
        self,
        job_id: str,
        delivery_type: str,
        telegram_id: str | None = None,
    ) -> None:
        """Translates store.py:2096-2111.

        Upserts against `job_hunter_deliveries`'s `(user_id, job_id,
        delivery_type, delivered_at)` constraint, never inserts -- same
        retry-safety reasoning as `save_evaluation`.
        """
        self._client.upsert(
            "job_hunter_deliveries",
            [
                {
                    "user_id": self._client.user_id,
                    "job_id": job_id,
                    "delivery_type": delivery_type,
                    "status": "sent",
                    "delivered_at": to_iso(datetime.now(timezone.utc)),
                    "telegram_message_id": telegram_id,
                }
            ],
            on_conflict="user_id,job_id,delivery_type,delivered_at",
        )

    def has_delivery(self, job_id: str, delivery_type: str | None = None) -> bool:
        """Translates store.py:2113-2124."""
        params: dict[str, str] = {"job_id": f"eq.{job_id}", "select": "id", "limit": "1"}
        if delivery_type is not None:
            params["delivery_type"] = f"eq.{delivery_type}"
        rows = self._client.select("job_hunter_deliveries", params=params)
        return len(rows) > 0

    def pending_delivery_job_ids(self) -> list[str]:
        """Translates store.py:2126-2141.

        `job_hunter_pending_delivery_jobs` (migration
        202609060004) reimplements the whole query -- the per-job "latest
        evaluation" join, the score floor, the decision filter, and the
        anti-join against a sent `telegram_message` delivery -- as one SQL
        function, rather than fetching every job/evaluation pair into
        Python and filtering there. It `returns table (job_id uuid)`, so
        `rpc` hands back `[{'job_id': '...'}, ...]`; unwrap the single key.
        """
        rows = self._client.rpc(
            "job_hunter_pending_delivery_jobs",
            {"p_score_floor": _DELIVERABLE_SCORE_FLOOR},
        )
        return [row["job_id"] for row in rows]

    # ------------------------------------------------------------------
    # Company watch
    # ------------------------------------------------------------------

    def upsert_company_watch(
        self,
        *,
        company_name: str,
        careers_url: str,
        ats_provider: str | None,
        ats_identifier: str | None,
        discovered_from_job_id: str | None,
        promotion_source: str,
        confidence: float,
    ) -> str:
        """Insert or safely upgrade one normalized company watch target.

        Translates store.py:1235-1329. Supported ATS targets outrank generic
        URLs, which outrank company-only entries. Equal-strength
        replacements require greater confidence, and a manual promotion
        source is never downgraded to automatic.

        The SQLite original did INSERT-OR-NOTHING, then re-read and UPDATEd
        the losing row. That is two statements in one transaction; over
        PostgREST it would be two requests with no transaction around them,
        so this reads first and then writes the fully-merged row through a
        single merge-duplicates upsert. A row written by anyone between the
        read and the write is not lost: the upsert collides on
        `unique (user_id, normalized_company_name)` and updates that row
        instead of adding a second one (see the dedicated test). What such a
        race can cost is the strength comparison, which was computed against
        the row we read -- acceptable because both workflows that write here
        share `concurrency: group: job-hunter-state`, so there is one writer.
        """
        normalized_name = normalize_company_name(company_name)
        if not normalized_name:
            raise ValueError("company_name must normalize to a non-empty value")

        provider = (ats_provider or "").strip().lower() or None
        identifier = (ats_identifier or "").strip() or None
        careers_url = (careers_url or "").strip()
        now = to_iso(datetime.now(timezone.utc))

        existing = self._client.select(
            "job_hunter_company_watch",
            params={
                "normalized_company_name": f"eq.{normalized_name}",
                "select": (
                    "company_name,careers_url,ats_provider,ats_identifier,"
                    "discovered_from_job_id,promotion_source,confidence,first_seen_at"
                ),
                "limit": "1",
            },
        )

        if existing:
            row = existing[0]
            replace_target = self._replaces_watch_target(
                row, careers_url, provider, identifier, confidence
            )
            values = {
                # The original's UPDATE never touched company_name, so the
                # display name stays as first discovered even when a later
                # call spells it differently.
                "company_name": row["company_name"],
                "careers_url": careers_url if replace_target else row["careers_url"],
                "ats_provider": provider if replace_target else row["ats_provider"],
                "ats_identifier": (
                    identifier if replace_target else row["ats_identifier"]
                ),
                "discovered_from_job_id": (
                    discovered_from_job_id or row["discovered_from_job_id"]
                ),
                "promotion_source": (
                    "manual"
                    if "manual" in (row["promotion_source"], promotion_source)
                    else "automatic"
                ),
                "confidence": confidence if replace_target else row["confidence"],
                "first_seen_at": row["first_seen_at"],
            }
        else:
            values = {
                "company_name": company_name,
                "careers_url": careers_url,
                "ats_provider": provider,
                "ats_identifier": identifier,
                "discovered_from_job_id": discovered_from_job_id,
                "promotion_source": promotion_source,
                "confidence": confidence,
                "first_seen_at": now,
            }

        values["user_id"] = self._client.user_id
        values["normalized_company_name"] = normalized_name
        written = self._client.upsert(
            "job_hunter_company_watch",
            [touch(values)],
            on_conflict="user_id,normalized_company_name",
        )
        return written[0]["id"]

    @staticmethod
    def _replaces_watch_target(
        row: dict[str, Any],
        careers_url: str,
        provider: str | None,
        identifier: str | None,
        confidence: float,
    ) -> bool:
        """Whether the candidate endpoint outranks the stored one."""
        existing_strength = _watch_endpoint_strength(
            row["careers_url"], row["ats_provider"], row["ats_identifier"]
        )
        candidate_strength = _watch_endpoint_strength(careers_url, provider, identifier)
        return candidate_strength > existing_strength or (
            candidate_strength == existing_strength and confidence > row["confidence"]
        )

    def get_company_watch(self, company_name: str) -> dict[str, Any] | None:
        """Return the normalized company watch row, if one exists.

        Translates store.py:1334-1342. Callers index the result by column
        name (`watch["id"]` in `sources/company_watch.py`), which a dict
        supports exactly as the original `sqlite3.Row` did.
        """
        normalized_name = normalize_company_name(company_name)
        if not normalized_name:
            return None
        rows = self._client.select(
            "job_hunter_company_watch",
            params={
                "normalized_company_name": f"eq.{normalized_name}",
                "limit": "1",
            },
        )
        return rows[0] if rows else None

    def list_due_company_watches(self, now: datetime) -> list[dict[str, Any]]:
        """Return active watch targets whose health pause has expired.

        Translates store.py:1344-1358. SQLite compared
        `julianday(paused_until) <= julianday(?)`, which normalised both
        sides to an instant; `paused_until` is `timestamptz` here, so
        PostgREST's `lte` comparison already is one -- a pause stored at
        `+02:00` and a `now` given at `-04:00` compare as the instants they
        name, not as the strings they were written as. Ids are random uuids,
        so `created_at` (with `select`'s `id.asc` tie-breaker) replaces
        `ORDER BY id` as the insertion-order proxy.
        """
        timestamp = to_iso(_require_aware(now))
        return self._client.select(
            "job_hunter_company_watch",
            params={
                "active": "eq.true",
                "or": f"(paused_until.is.null,paused_until.lte.{timestamp})",
                "order": "created_at.asc",
            },
        )

    def record_watch_success(self, watch_id: str, now: datetime) -> None:
        """Record a verified endpoint check and clear its failure backoff.

        Translates store.py:1360-1375.
        """
        timestamp = to_iso(_require_aware(now))
        self._client.update(
            "job_hunter_company_watch",
            touch(
                {
                    "last_successful_check_at": timestamp,
                    "last_verified_at": timestamp,
                    "consecutive_failures": 0,
                    "paused_until": None,
                }
            ),
            params={"id": f"eq.{watch_id}"},
        )

    def record_watch_failure(self, watch_id: str, now: datetime) -> None:
        """Increment endpoint failures and apply the deterministic 24h pause.

        Translates store.py:1377-1397. The original incremented and tested
        the counter inside one `UPDATE ... CASE WHEN`; PostgREST cannot
        express a self-referential update, so the counter is read first and
        the new value written back. Same single-writer reasoning as
        `upsert_company_watch`. A watch id that matches nothing is a no-op,
        as the original's UPDATE was.
        """
        normalized_now = _require_aware(now)
        rows = self._client.select(
            "job_hunter_company_watch",
            params={
                "id": f"eq.{watch_id}",
                "select": "consecutive_failures",
                "limit": "1",
            },
        )
        if not rows:
            return
        failures = rows[0]["consecutive_failures"] + 1
        paused_until = (
            to_iso(normalized_now + _HEALTH_PAUSE)
            if failures >= _WATCH_PAUSE_THRESHOLD
            else None
        )
        self._client.update(
            "job_hunter_company_watch",
            touch({"consecutive_failures": failures, "paused_until": paused_until}),
            params={"id": f"eq.{watch_id}"},
        )

    # ------------------------------------------------------------------
    # ATS registry
    # ------------------------------------------------------------------

    def upsert_ats_board(
        self,
        provider: str,
        board_identifier: str,
        company_name: str = "",
        market_hint: str = "",
    ) -> bool:
        """Insert or refresh one learned ATS board's discovery metadata.

        Translates store.py:1407-1455. On an existing board, updates display
        metadata and `last_seen_at` and reactivates it, but leaves
        `paused_until` and `consecutive_failures` untouched -- ordinary
        rediscovery must not bypass an unexpired pause; the board becomes
        due naturally once `paused_until` elapses. A board with a
        `rejected_reason` is never reactivated by rediscovery, so a board
        rejected once stays rejected (see `reject_ats_board`).

        Returns True only when the board was newly registered.
        `job_hunter_ats_registry` has no `updated_at` column, so no `touch`
        here.
        """
        provider = provider.strip().lower()
        if provider not in _SUPPORTED_ATS_PROVIDERS:
            raise ValueError(f"unsupported ATS provider: {provider!r}")
        board_identifier = board_identifier.strip()
        now = to_iso(datetime.now(timezone.utc))

        existing = self._client.select(
            "job_hunter_ats_registry",
            params={
                "provider": f"eq.{provider}",
                "board_identifier": f"eq.{board_identifier}",
                "select": "id,company_name,market_hint,active,rejected_reason",
                "limit": "1",
            },
        )
        if not existing:
            self._client.upsert(
                "job_hunter_ats_registry",
                [
                    {
                        "user_id": self._client.user_id,
                        "provider": provider,
                        "board_identifier": board_identifier,
                        "company_name": company_name,
                        "market_hint": market_hint,
                        "first_seen_at": now,
                        "last_seen_at": now,
                    }
                ],
                on_conflict="user_id,provider,board_identifier",
            )
            return True

        row = existing[0]
        self._client.update(
            "job_hunter_ats_registry",
            {
                # COALESCE(NULLIF(?, ''), col): a blank argument means "no
                # new information", not "clear what is stored".
                "company_name": company_name or row["company_name"],
                "market_hint": market_hint or row["market_hint"],
                "last_seen_at": now,
                "active": True if row["rejected_reason"] is None else row["active"],
            },
            params={"id": f"eq.{row['id']}"},
        )
        return False

    def reject_ats_board(
        self, provider: str, board_identifier: str, reason: str, now: datetime
    ) -> None:
        """Deactivate a board and persist why, so rediscovery can't resurrect it.

        Translates store.py:1457-1476. Used for both aggregator-detection
        rejections and the config denylist's "instant kill" of an
        already-registered board.
        """
        timestamp = to_iso(_require_aware(now))
        self._client.update(
            "job_hunter_ats_registry",
            {"active": False, "rejected_reason": reason, "last_checked_at": timestamp},
            params={
                "provider": f"eq.{provider}",
                "board_identifier": f"eq.{board_identifier}",
            },
        )

    def clear_ats_board_rejection(self, provider: str, board_identifier: str) -> None:
        """Reverse a rejection, putting the board back in the due rotation.

        Translates store.py:1478-1501. The inverse of `reject_ats_board`,
        and the only code path that clears `rejected_reason`. Used when an
        operator names a board in `learned_ats_allowlist`, having judged its
        rejection wrong.

        Scoped to rejected rows on purpose: a board deactivated by repeated
        404s carries no `rejected_reason`, and reviving it here would
        confuse "wrongly judged" with "broken", which health backoff owns.
        Clearing a board that was never rejected is a no-op.

        The original matched `lower(provider) = lower(?)`. PostgREST's
        case-insensitive operator is `ilike`, which would also read `_` and
        `%` in a board identifier as wildcards and could clear a
        neighbouring board's rejection, so the rejected rows are fetched and
        compared in Python instead. There are only ever a handful of them.
        """
        wanted = (provider.strip().casefold(), board_identifier.strip().casefold())
        for row in self._client.select(
            "job_hunter_ats_registry",
            params={
                "rejected_reason": "not.is.null",
                "select": "id,provider,board_identifier",
            },
        ):
            if (
                row["provider"].casefold(),
                row["board_identifier"].casefold(),
            ) != wanted:
                continue
            self._client.update(
                "job_hunter_ats_registry",
                {"active": True, "rejected_reason": None},
                params={"id": f"eq.{row['id']}"},
            )

    def list_due_ats_boards(self, now: datetime) -> list[AtsRegistryEntry]:
        """Return active ATS boards whose health pause has expired.

        Translates store.py:1503-1518. Same `timestamptz` comparison as
        `list_due_company_watches`.
        """
        timestamp = to_iso(_require_aware(now))
        rows = self._client.select(
            "job_hunter_ats_registry",
            params={
                "active": "eq.true",
                "or": f"(paused_until.is.null,paused_until.lte.{timestamp})",
                "order": "provider.asc,board_identifier.asc",
            },
        )
        return [ats_entry_from_row(row) for row in rows]

    def list_rejected_ats_boards(self) -> list[AtsRegistryEntry]:
        """Return boards rejected as aggregators or by the config denylist.

        Translates store.py:1520-1534. `list_due_ats_boards` only returns
        active boards, so this is the only way to read a rejection (and its
        reason) back after the run that made it.
        """
        rows = self._client.select(
            "job_hunter_ats_registry",
            params={
                "rejected_reason": "not.is.null",
                "order": "provider.asc,board_identifier.asc",
            },
        )
        return [ats_entry_from_row(row) for row in rows]

    def record_ats_scan_success(
        self, provider: str, board_identifier: str, now: datetime, job_count: int
    ) -> None:
        """Record a successful scan and clear the board's failure backoff.

        Translates store.py:1536-1553.
        """
        timestamp = to_iso(_require_aware(now))
        self._client.update(
            "job_hunter_ats_registry",
            {
                "last_checked_at": timestamp,
                "last_success_at": timestamp,
                "last_job_count": job_count,
                "consecutive_failures": 0,
                "paused_until": None,
            },
            params={
                "provider": f"eq.{provider}",
                "board_identifier": f"eq.{board_identifier}",
            },
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

        Translates store.py:1555-1620. Every failure is isolated per board
        and gets the same 24h pause so it retries soon. `consecutive_failures`
        is a single shared counter incremented by every failure, transient or
        permanent alike -- it does not track which kind of failure contributed
        to it. `permanent=True` (a stale-looking 404) additionally escalates:
        once that shared counter reaches `_STALE_BOARD_DEACTIVATION_THRESHOLD`,
        a permanent failure deactivates the board instead of just pausing it.
        Transient failures accrue toward the same counter but never deactivate
        a board on their own -- only a `permanent=True` call checks the
        threshold. So a mixed sequence such as [transient, transient,
        permanent] deactivates the board on that single 404, not only after
        three permanent failures in a row.

        Deactivation preserves registry history -- it is not a delete -- and
        `upsert_ats_board` already reactivates any inactive board the next
        time it is rediscovered. That reactivation only sets `active`; it
        does not reset `consecutive_failures`, so a rediscovered board that
        immediately fails again resumes from its prior strike count and can
        re-deactivate right away, not after three fresh strikes.

        Like `record_watch_failure`, the self-referential counter update
        becomes a read followed by a write.
        """
        normalized_now = _require_aware(now)
        rows = self._client.select(
            "job_hunter_ats_registry",
            params={
                "provider": f"eq.{provider}",
                "board_identifier": f"eq.{board_identifier}",
                "select": "id,consecutive_failures",
                "limit": "1",
            },
        )
        if not rows:
            return
        failures = rows[0]["consecutive_failures"] + 1
        values: dict[str, Any] = {
            "last_checked_at": to_iso(normalized_now),
            "consecutive_failures": failures,
            "paused_until": to_iso(normalized_now + _HEALTH_PAUSE),
        }
        if permanent and failures >= _STALE_BOARD_DEACTIVATION_THRESHOLD:
            values["active"] = False
        self._client.update(
            "job_hunter_ats_registry", values, params={"id": f"eq.{rows[0]['id']}"}
        )

    def record_ats_eligible_job(
        self, provider: str, board_identifier: str, now: datetime
    ) -> None:
        """Record that a scan of this board surfaced a candidate-eligible job.

        Translates store.py:1622-1636, with the same read-then-write
        treatment of the counter as `record_ats_scan_failure`.
        """
        timestamp = to_iso(_require_aware(now))
        rows = self._client.select(
            "job_hunter_ats_registry",
            params={
                "provider": f"eq.{provider}",
                "board_identifier": f"eq.{board_identifier}",
                "select": "id,eligible_jobs_seen",
                "limit": "1",
            },
        )
        if not rows:
            return
        self._client.update(
            "job_hunter_ats_registry",
            {
                "last_eligible_at": timestamp,
                "eligible_jobs_seen": rows[0]["eligible_jobs_seen"] + 1,
            },
            params={"id": f"eq.{rows[0]['id']}"},
        )

    def count_ats_boards(self) -> int:
        """Translates store.py:1638-1640."""
        return len(
            self._client.select("job_hunter_ats_registry", params={"select": "id"})
        )

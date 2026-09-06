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

from datetime import datetime, timezone
from typing import Any

from job_hunter.ats_hosts import SUPPORTED_ATS_HOSTS
from job_hunter.canonical import parse_supported_ats_url
from job_hunter.models import Evaluation, Job, Material
from job_hunter.normalize import job_fingerprint
from job_hunter.store_mapping import (
    evaluation_from_row,
    job_from_row,
    material_from_row,
    to_iso,
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

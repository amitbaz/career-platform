"""Postgres/pgmq adapter for the shared stage queue contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .stage_queue import QueueDepth, QueueMessage, Stage

_QUEUE_NAMES = {
    Stage.CRAWL_SOURCE: "job_hunter_crawl_source",
    Stage.RESOLVE_PERSIST: "job_hunter_resolve_persist",
    Stage.EXTRACT_FACETS: "job_hunter_extract_facets",
    Stage.RECHECK_FRESHNESS: "job_hunter_recheck_freshness",
    Stage.RECOVER_POSTING: "job_hunter_recover_posting",
}


class PostgresStageQueue:
    """Run short queue-state transactions over ingestion's connection pool."""

    def __init__(
        self,
        database,
        queue_names: Mapping[Stage, str] | None = None,
    ) -> None:
        self._database = database
        self._queue_names = dict(queue_names or _QUEUE_NAMES)

    def enqueue(
        self,
        stage: Stage,
        payload: dict[str, Any],
        *,
        delay_seconds: int = 0,
        connection=None,
    ) -> int:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        if connection is not None:
            return self._enqueue(connection, stage, payload, delay_seconds)
        with self._database.connection() as leased:
            return self._enqueue(leased, stage, payload, delay_seconds)

    def _enqueue(self, connection, stage, payload, delay_seconds) -> int:
        with connection.cursor() as cursor:
            cursor.execute(
                "select * from pgmq.send(%s, %s::jsonb, %s)",
                (self._queue_names[stage], json.dumps(payload), delay_seconds),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("pgmq did not return an enqueued message id")
        return int(row[0])

    def claim(
        self,
        stage: Stage,
        *,
        visibility_timeout_seconds: int,
        batch_size: int,
    ) -> list[QueueMessage]:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select q.msg_id, q.message, coalesce(a.attempt_count, 0), "
                    "q.enqueued_at, now() "
                    "from pgmq.read(%s, %s, %s) q "
                    "left join public.job_hunter_stage_attempts a "
                    "on a.stage = %s and a.message_id = q.msg_id "
                    "order by q.msg_id",
                    (
                        self._queue_names[stage],
                        visibility_timeout_seconds,
                        batch_size,
                        stage.value,
                    ),
                )
                rows = cursor.fetchall()
        return [
            QueueMessage(
                stage=stage,
                message_id=int(message_id),
                payload=(
                    payload
                    if isinstance(payload, dict)
                    else {"invalid_payload": payload}
                ),
                attempt_count=int(attempt_count),
                enqueued_at=enqueued_at,
                claimed_at=claimed_at,
            )
            for message_id, payload, attempt_count, enqueued_at, claimed_at in rows
        ]

    def complete(self, message: QueueMessage) -> None:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select pgmq.delete(%s, %s)",
                    (self._queue_names[message.stage], message.message_id),
                )
                cursor.execute(
                    "delete from public.job_hunter_stage_attempts "
                    "where stage = %s and message_id = %s",
                    (message.stage.value, message.message_id),
                )

    def retry(
        self,
        message: QueueMessage,
        *,
        delay_seconds: int,
        attempt_count: int,
    ) -> None:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "insert into public.job_hunter_stage_attempts "
                    "(stage, message_id, attempt_count, updated_at) "
                    "values (%s, %s, %s, now()) "
                    "on conflict (stage, message_id) do update set "
                    "attempt_count = excluded.attempt_count, updated_at = now()",
                    (message.stage.value, message.message_id, attempt_count),
                )
                cursor.execute(
                    "select * from pgmq.set_vt(%s, %s, %s)",
                    (
                        self._queue_names[message.stage],
                        message.message_id,
                        delay_seconds,
                    ),
                )

    def release(self, message: QueueMessage, *, delay_seconds: int) -> None:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select * from pgmq.set_vt(%s, %s, %s)",
                    (
                        self._queue_names[message.stage],
                        message.message_id,
                        delay_seconds,
                    ),
                )

    def dead_letter(
        self,
        message: QueueMessage,
        *,
        failure_class: str,
        error: str,
        attempt_count: int,
    ) -> None:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "insert into public.job_hunter_stage_dead_letters "
                    "(stage, message_id, payload, failure_class, error, "
                    "attempt_count) values (%s, %s, %s::jsonb, %s, %s, %s) "
                    "on conflict (stage, message_id) do nothing",
                    (
                        message.stage.value,
                        message.message_id,
                        json.dumps(message.payload),
                        failure_class,
                        error[:2000],
                        attempt_count,
                    ),
                )
                cursor.execute(
                    "select pgmq.delete(%s, %s)",
                    (self._queue_names[message.stage], message.message_id),
                )
                cursor.execute(
                    "delete from public.job_hunter_stage_attempts "
                    "where stage = %s and message_id = %s",
                    (message.stage.value, message.message_id),
                )

    def metrics(self) -> list[QueueDepth]:
        with self._database.connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "select stage, queue_depth, visible_depth, dead_letter_depth "
                    "from public.job_hunter_stage_queue_metrics() order by stage"
                )
                rows = cursor.fetchall()
        return [
            QueueDepth(
                stage=Stage(stage),
                queue_depth=int(queue_depth),
                visible_depth=int(visible_depth),
                dead_letter_depth=int(dead_letter_depth),
            )
            for stage, queue_depth, visible_depth, dead_letter_depth in rows
        ]

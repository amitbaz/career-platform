"""The privileged, user-free ``resolve_persist`` stage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .stage_queue import (
    PermanentStageFailure,
    QueueMessage,
    Stage,
    TransientStageFailure,
)


class _ConnectionLease(Protocol):
    def connection(self): ...


@dataclass(frozen=True)
class PostingBatch:
    """The postings a staged batch resolved to, keyed by fingerprint."""

    posting_ids: dict[str, str] = field(default_factory=dict)
    newly_discovered: int = 0
    # Which fingerprints were new, not just how many. The count answers
    # "how much did this run add"; the set answers "which source added it",
    # which is what per-source yield -- and so the crawl cadence banded on it
    # -- needs (issue #184).
    new_fingerprints: frozenset[str] = frozenset()


class ResolvePersistStage:
    """Turn one staged crawl batch into shared posting rows."""

    def __init__(self, database: _ConnectionLease) -> None:
        self._database = database

    def __call__(self, message: QueueMessage) -> PostingBatch:
        batch_id = self._batch_id(message)
        try:
            with self._database.connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "select fingerprint, posting_id, is_new "
                        "from public.job_hunter_merge_posting_batch(%s)",
                        (batch_id,),
                    )
                    rows = cursor.fetchall()
        except Exception as error:
            raise TransientStageFailure("resolve_persist failed") from error

        return PostingBatch(
            posting_ids={
                fingerprint: str(posting_id)
                for fingerprint, posting_id, _is_new in rows
                if posting_id is not None
            },
            newly_discovered=sum(1 for _fingerprint, _posting_id, is_new in rows if is_new),
            new_fingerprints=frozenset(
                fingerprint for fingerprint, _posting_id, is_new in rows if is_new
            ),
        )

    @staticmethod
    def _batch_id(message: QueueMessage) -> str:
        if message.stage is not Stage.RESOLVE_PERSIST:
            raise PermanentStageFailure("resolve_persist received the wrong stage")
        if set(message.payload) != {"batch_id"}:
            raise PermanentStageFailure(
                "resolve_persist payload must contain only batch_id"
            )
        batch_id = message.payload.get("batch_id")
        if not isinstance(batch_id, str):
            raise PermanentStageFailure("resolve_persist batch_id must be a UUID")
        try:
            return str(uuid.UUID(batch_id))
        except ValueError as error:
            raise PermanentStageFailure(
                "resolve_persist batch_id must be a UUID"
            ) from error

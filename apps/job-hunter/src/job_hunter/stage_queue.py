"""Durable queue contract shared by ingestion and enrichment stages.

This module deliberately knows nothing about users, matching, delivery, or AI
credentials.  A worker holding only the privileged ingestion connection can
import and run it (issue #183, constraint C1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, TypeVar

logger = logging.getLogger(__name__)

_Result = TypeVar("_Result")


class Stage(str, Enum):
    """The four queue-coupled engine stages fixed by epic #181."""

    CRAWL_SOURCE = "crawl_source"
    RESOLVE_PERSIST = "resolve_persist"
    EXTRACT_FACETS = "extract_facets"
    RECHECK_FRESHNESS = "recheck_freshness"


@dataclass(frozen=True)
class QueueMessage:
    stage: Stage
    message_id: int
    payload: dict[str, Any]
    attempt_count: int = 0


@dataclass(frozen=True)
class QueueDepth:
    stage: Stage
    queue_depth: int
    visible_depth: int
    dead_letter_depth: int


@dataclass(frozen=True)
class StageOutcome:
    message: QueueMessage
    result: Any


class TransientStageFailure(RuntimeError):
    """Work may succeed later and should consume one retry attempt."""


class PermanentStageFailure(RuntimeError):
    """Work cannot succeed unchanged and should dead-letter immediately."""


class QuotaExhausted(RuntimeError):
    """Capacity is unavailable; delay the message without blaming it."""

    def __init__(self, retry_after_seconds: int) -> None:
        if retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must not be negative")
        super().__init__(f"quota exhausted; retry after {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds


class StageQueue(Protocol):
    """Storage operations the runner needs, independent of Postgres."""

    def claim(
        self,
        stage: Stage,
        *,
        visibility_timeout_seconds: int,
        batch_size: int,
    ) -> list[QueueMessage]: ...

    def complete(self, message: QueueMessage) -> None: ...

    def retry(
        self,
        message: QueueMessage,
        *,
        delay_seconds: int,
        attempt_count: int,
    ) -> None: ...

    def release(self, message: QueueMessage, *, delay_seconds: int) -> None: ...

    def dead_letter(
        self,
        message: QueueMessage,
        *,
        failure_class: str,
        error: str,
        attempt_count: int,
    ) -> None: ...

    def metrics(self) -> list[QueueDepth]: ...


class StageRunner:
    """Claim one bounded batch and apply the common completion policy."""

    def __init__(
        self,
        queue: StageQueue,
        *,
        visibility_timeout_seconds: int,
        max_attempts: int = 3,
        base_backoff_seconds: int = 30,
        max_backoff_seconds: int = 15 * 60,
    ) -> None:
        if visibility_timeout_seconds <= 0:
            raise ValueError("visibility_timeout_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if base_backoff_seconds <= 0:
            raise ValueError("base_backoff_seconds must be positive")
        if max_backoff_seconds < base_backoff_seconds:
            raise ValueError("max_backoff_seconds must not be below the base")
        self._queue = queue
        self._visibility_timeout_seconds = visibility_timeout_seconds
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds

    def run_once(
        self,
        stage: Stage,
        handler: Callable[[QueueMessage], _Result],
        *,
        batch_size: int = 10,
    ) -> list[StageOutcome]:
        """Process at most ``batch_size`` visible messages for one stage.

        ``BaseException`` is intentionally not caught.  A killed or interrupted
        process does not acknowledge its claim; Postgres' visibility timeout
        makes the message available to a later worker.
        """
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        messages = self._queue.claim(
            stage,
            visibility_timeout_seconds=self._visibility_timeout_seconds,
            batch_size=batch_size,
        )
        outcomes: list[StageOutcome] = []
        for message in messages:
            try:
                result = handler(message)
            except QuotaExhausted as error:
                self._queue.release(
                    message, delay_seconds=error.retry_after_seconds
                )
                logger.warning(
                    "stage_queue_result: stage=%s message_id=%s "
                    "failure_class=quota action=release delay_seconds=%s",
                    message.stage.value,
                    message.message_id,
                    error.retry_after_seconds,
                )
            except PermanentStageFailure as error:
                attempt_count = message.attempt_count + 1
                self._queue.dead_letter(
                    message,
                    failure_class="permanent",
                    error=str(error),
                    attempt_count=attempt_count,
                )
                logger.warning(
                    "stage_queue_result: stage=%s message_id=%s "
                    "failure_class=permanent action=dead_letter attempt_count=%s",
                    message.stage.value,
                    message.message_id,
                    attempt_count,
                )
            except Exception as error:
                self._handle_transient(message, error)
            else:
                self._queue.complete(message)
                outcomes.append(StageOutcome(message=message, result=result))

        self._log_metrics()
        return outcomes

    def _handle_transient(self, message: QueueMessage, error: Exception) -> None:
        attempt_count = message.attempt_count + 1
        if not isinstance(error, TransientStageFailure):
            logger.exception(
                "unclassified stage failure; treating it as transient: stage=%s "
                "message_id=%s",
                message.stage.value,
                message.message_id,
            )
        if attempt_count >= self._max_attempts:
            self._queue.dead_letter(
                message,
                failure_class="transient",
                error=str(error),
                attempt_count=attempt_count,
            )
            logger.warning(
                "stage_queue_result: stage=%s message_id=%s "
                "failure_class=transient action=dead_letter attempt_count=%s",
                message.stage.value,
                message.message_id,
                attempt_count,
            )
            return
        delay_seconds = min(
            self._base_backoff_seconds * (2 ** (attempt_count - 1)),
            self._max_backoff_seconds,
        )
        self._queue.retry(
            message,
            delay_seconds=delay_seconds,
            attempt_count=attempt_count,
        )
        logger.warning(
            "stage_queue_result: stage=%s message_id=%s "
            "failure_class=transient action=retry attempt_count=%s "
            "delay_seconds=%s",
            message.stage.value,
            message.message_id,
            attempt_count,
            delay_seconds,
        )

    def _log_metrics(self) -> None:
        try:
            metrics = self._queue.metrics()
        except Exception:
            logger.exception("stage queue metrics could not be read")
            return
        for depth in metrics:
            logger.info(
                "stage_queue: stage=%s queue_depth=%s visible_depth=%s "
                "dead_letter_depth=%s",
                depth.stage.value,
                depth.queue_depth,
                depth.visible_depth,
                depth.dead_letter_depth,
            )

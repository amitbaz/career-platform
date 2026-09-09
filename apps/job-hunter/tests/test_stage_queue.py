"""The shared stage-runner contract for the ingestion engine (issue #183)."""

from __future__ import annotations

import logging
from contextlib import contextmanager

import pytest

from job_hunter.resolve_persist import PostingBatch, ResolvePersistStage
from job_hunter.stage_queue import (
    PermanentStageFailure,
    QueueDepth,
    QueueMessage,
    QuotaExhausted,
    Stage,
    StageRunner,
    TransientStageFailure,
)


class RecordingQueue:
    def __init__(self, messages: list[QueueMessage] | None = None) -> None:
        self.messages = messages or []
        self.completed: list[QueueMessage] = []
        self.retried: list[tuple[QueueMessage, int, int]] = []
        self.released: list[tuple[QueueMessage, int]] = []
        self.dead_letters: list[tuple[QueueMessage, str, int]] = []

    def claim(self, stage, *, visibility_timeout_seconds, batch_size):
        assert stage is Stage.RESOLVE_PERSIST
        assert visibility_timeout_seconds == 60
        return self.messages[:batch_size]

    def complete(self, message):
        self.completed.append(message)

    def retry(self, message, *, delay_seconds, attempt_count):
        self.retried.append((message, delay_seconds, attempt_count))

    def release(self, message, *, delay_seconds):
        self.released.append((message, delay_seconds))

    def dead_letter(self, message, *, failure_class, error, attempt_count):
        assert error
        self.dead_letters.append((message, failure_class, attempt_count))

    def metrics(self):
        return [
            QueueDepth(
                stage=stage,
                queue_depth=4 if stage is Stage.RESOLVE_PERSIST else 0,
                visible_depth=3 if stage is Stage.RESOLVE_PERSIST else 0,
                dead_letter_depth=2 if stage is Stage.RESOLVE_PERSIST else 0,
            )
            for stage in Stage
        ]


def _message(*, attempt_count: int = 0) -> QueueMessage:
    return QueueMessage(
        stage=Stage.RESOLVE_PERSIST,
        message_id=7,
        payload={"batch_id": "batch-7"},
        attempt_count=attempt_count,
    )


def test_a_successful_stage_message_is_deleted_and_returns_its_result():
    message = _message()
    queue = RecordingQueue([message])
    runner = StageRunner(queue, visibility_timeout_seconds=60)

    outcomes = runner.run_once(
        Stage.RESOLVE_PERSIST,
        lambda claimed: f"resolved:{claimed.payload['batch_id']}",
    )

    assert outcomes[0].message == message
    assert outcomes[0].result == "resolved:batch-7"
    assert queue.completed == [message]


def test_a_killed_process_leaves_the_claimed_message_unacknowledged():
    message = _message()
    queue = RecordingQueue([message])
    runner = StageRunner(queue, visibility_timeout_seconds=60)

    with pytest.raises(KeyboardInterrupt):
        runner.run_once(
            Stage.RESOLVE_PERSIST,
            lambda _claimed: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    assert queue.completed == []
    assert queue.retried == []
    assert queue.released == []
    assert queue.dead_letters == []


def test_a_transient_failure_retries_with_exponential_backoff(caplog):
    message = _message(attempt_count=1)
    queue = RecordingQueue([message])
    runner = StageRunner(
        queue,
        visibility_timeout_seconds=60,
        max_attempts=4,
        base_backoff_seconds=10,
    )

    with caplog.at_level(logging.WARNING):
        runner.run_once(
            Stage.RESOLVE_PERSIST,
            lambda _claimed: (_ for _ in ()).throw(
                TransientStageFailure("timeout")
            ),
        )

    assert queue.retried == [(message, 20, 2)]
    assert queue.dead_letters == []
    assert (
        "failure_class=transient action=retry attempt_count=2 delay_seconds=20"
        in caplog.text
    )


def test_a_transient_failure_dead_letters_at_the_attempt_limit():
    message = _message(attempt_count=2)
    queue = RecordingQueue([message])
    runner = StageRunner(queue, visibility_timeout_seconds=60, max_attempts=3)

    runner.run_once(
        Stage.RESOLVE_PERSIST,
        lambda _claimed: (_ for _ in ()).throw(TransientStageFailure("timeout")),
    )

    assert queue.retried == []
    assert queue.dead_letters == [(message, "transient", 3)]


def test_an_unclassified_exception_is_safe_to_retry():
    message = _message()
    queue = RecordingQueue([message])
    runner = StageRunner(queue, visibility_timeout_seconds=60)

    runner.run_once(
        Stage.RESOLVE_PERSIST,
        lambda _claimed: (_ for _ in ()).throw(RuntimeError("connection reset")),
    )

    assert queue.retried == [(message, 30, 1)]


def test_a_permanent_failure_dead_letters_on_its_first_attempt():
    message = _message()
    queue = RecordingQueue([message])
    runner = StageRunner(queue, visibility_timeout_seconds=60)

    runner.run_once(
        Stage.RESOLVE_PERSIST,
        lambda _claimed: (_ for _ in ()).throw(PermanentStageFailure("bad payload")),
    )

    assert queue.dead_letters == [(message, "permanent", 1)]
    assert queue.retried == []


def test_quota_exhaustion_releases_without_incrementing_the_attempt_count():
    message = _message(attempt_count=2)
    queue = RecordingQueue([message])
    runner = StageRunner(queue, visibility_timeout_seconds=60)

    runner.run_once(
        Stage.RESOLVE_PERSIST,
        lambda _claimed: (_ for _ in ()).throw(QuotaExhausted(90)),
    )

    assert queue.released == [(message, 90)]
    assert queue.retried == []
    assert queue.dead_letters == []


def test_queue_and_dead_letter_depth_are_logged_per_stage(caplog):
    queue = RecordingQueue()
    runner = StageRunner(queue, visibility_timeout_seconds=60)

    with caplog.at_level(logging.INFO):
        runner.run_once(Stage.RESOLVE_PERSIST, lambda _message: None)

    for stage in Stage:
        assert f"stage={stage.value}" in caplog.text
    assert (
        "stage=resolve_persist queue_depth=4 visible_depth=3 dead_letter_depth=2"
        in caplog.text
    )


class MergeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, params):
        self.calls.append((statement, params))

    def fetchall(self):
        return self.rows


class MergeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


class MergeDatabase:
    def __init__(self, rows):
        self.cursor = MergeCursor(rows)
        self.opened = 0

    @contextmanager
    def connection(self):
        self.opened += 1
        yield MergeConnection(self.cursor)


def test_resolve_persist_consumes_a_batch_id_and_returns_the_merge_result():
    database = MergeDatabase(
        [
            ("fingerprint-a", "00000000-0000-0000-0000-000000000001", True),
            ("fingerprint-b", "00000000-0000-0000-0000-000000000002", False),
        ]
    )
    stage = ResolvePersistStage(database)
    message = QueueMessage(
        stage=Stage.RESOLVE_PERSIST,
        message_id=1,
        payload={"batch_id": "18300000-0000-0000-0000-000000000001"},
    )

    result = stage(message)

    assert result == PostingBatch(
        posting_ids={
            "fingerprint-a": "00000000-0000-0000-0000-000000000001",
            "fingerprint-b": "00000000-0000-0000-0000-000000000002",
        },
        newly_discovered=1,
    )
    assert "job_hunter_merge_posting_batch" in database.cursor.calls[0][0]
    assert database.cursor.calls[0][1] == (
        "18300000-0000-0000-0000-000000000001",
    )


@pytest.mark.parametrize(
    "payload",
    [{}, {"batch_id": "not-a-uuid"}, {"batch_id": 7}, {"user_id": "forbidden"}],
)
def test_resolve_persist_dead_letters_a_payload_without_one_valid_batch_id(payload):
    database = MergeDatabase([])
    stage = ResolvePersistStage(database)
    message = QueueMessage(
        stage=Stage.RESOLVE_PERSIST,
        message_id=1,
        payload=payload,
    )

    with pytest.raises(PermanentStageFailure, match="batch_id"):
        stage(message)

    assert database.opened == 0


def test_resolve_persist_classifies_a_database_failure_as_transient():
    class BrokenDatabase:
        @contextmanager
        def connection(self):
            raise ConnectionError("database unavailable")
            yield

    stage = ResolvePersistStage(BrokenDatabase())
    message = QueueMessage(
        stage=Stage.RESOLVE_PERSIST,
        message_id=1,
        payload={"batch_id": "18300000-0000-0000-0000-000000000001"},
    )

    with pytest.raises(TransientStageFailure, match="resolve_persist failed"):
        stage(message)

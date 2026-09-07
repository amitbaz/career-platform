"""Batch discovery writes on PostgresJobStore.

These run against the local Supabase stack through the `store` fixture, so
they exercise the real functions, real RLS, and a real token.
"""

from __future__ import annotations

import pytest

from job_hunter.models import Job


def _make_job(fingerprint: str, title: str, url: str) -> Job:
    return Job(
        source="test",
        source_job_id=fingerprint,
        url=url,
        company="Acme",
        title=title,
        location="Remote",
        remote=True,
        description=f"description for {title}",
    )


def test_upsert_logical_jobs_returns_one_result_per_input_in_order(store):
    jobs = [
        _make_job("batch-1", "Frontend Engineer", "https://example.test/1"),
        _make_job("batch-2", "Backend Engineer", "https://example.test/2"),
        _make_job("batch-3", "Platform Engineer", "https://example.test/3"),
    ]

    results = store.upsert_logical_jobs(jobs)

    assert len(results) == 3
    assert all(result is not None for result in results)
    assert all(is_new for _job_id, is_new, _changed in results)
    stored_titles = {
        store.get_job(result[0]).title for result in results
    }
    assert stored_titles == {"Frontend Engineer", "Backend Engineer", "Platform Engineer"}


def test_upsert_logical_jobs_matches_the_single_job_method(store):
    single = store.upsert_logical_job(_make_job("same-1", "Frontend Engineer", "https://example.test/s1"))
    batched = store.upsert_logical_jobs(
        [_make_job("same-1", "Frontend Engineer", "https://example.test/s1")]
    )

    assert batched[0][0] == single[0]
    assert batched[0][1] is False  # already stored by the single-job call


def test_upsert_logical_jobs_chunks_by_the_configured_size(store, monkeypatch):
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_JOB_UPSERT_CHUNK_SIZE", 2)
    calls: list[int] = []
    original = store._client.rpc

    def counting_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs":
            calls.append(len(payload["p_jobs"]))
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", counting_rpc)

    jobs = [_make_job(f"chunk-{i}", f"Engineer {i}", f"https://example.test/c{i}") for i in range(5)]
    results = store.upsert_logical_jobs(jobs)

    assert calls == [2, 2, 1]
    assert len(results) == 5


def test_upsert_logical_jobs_replays_a_failed_chunk_one_job_at_a_time(store, monkeypatch, caplog):
    """A single bad job must cost its own row, not the chunk and not the run."""
    jobs = [
        _make_job("fallback-1", "Frontend Engineer", "https://example.test/f1"),
        _make_job("fallback-2", "Backend Engineer", "https://example.test/f2"),
    ]
    original = store._client.rpc
    failed_once = {"done": False}

    def flaky_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs" and not failed_once["done"]:
            failed_once["done"] = True
            raise RuntimeError("chunk exploded")
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", flaky_rpc)

    results = store.upsert_logical_jobs(jobs)

    assert len(results) == 2
    assert all(result is not None for result in results)
    assert "retrying" in caplog.text.lower()


def test_upsert_logical_jobs_skips_a_job_that_fails_on_replay(store, monkeypatch):
    jobs = [
        _make_job("skip-good", "Frontend Engineer", "https://example.test/g1"),
        _make_job("skip-bad", "Backend Engineer", "https://example.test/b1"),
    ]
    original_rpc = store._client.rpc
    original_single = store.upsert_logical_job

    def failing_batch(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs":
            raise RuntimeError("chunk exploded")
        return original_rpc(function, payload, **kwargs)

    def failing_for_bad(job):
        if job.source_job_id == "skip-bad":
            raise RuntimeError("this one job is malformed")
        return original_single(job)

    monkeypatch.setattr(store._client, "rpc", failing_batch)
    monkeypatch.setattr(store, "upsert_logical_job", failing_for_bad)

    results = store.upsert_logical_jobs(jobs)

    assert results[0] is not None
    assert results[1] is None


def test_upsert_logical_jobs_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made for an empty batch")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    assert store.upsert_logical_jobs([]) == []

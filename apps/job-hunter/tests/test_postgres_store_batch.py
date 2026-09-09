"""Batch discovery writes on PostgresJobStore.

These run against the local Supabase stack through the `store` fixture, so
they exercise the real functions, real RLS, and a real token.

Every fingerprint below is made unique per run. A posting is global, has no
owner and cannot be deleted by anyone (20260909100000), so a fixed
fingerprint would resolve to the row an earlier run -- or a suite running
right now under a different seed user -- left behind, and the readers that
take their facts from the posting (#177) would answer from someone else's
description.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from job_hunter.models import Evaluation, Job

_RUN = uuid.uuid4().hex


def _make_job(
    fingerprint: str, title: str, url: str, content_confidence: str = ""
) -> Job:
    return Job(
        source="test",
        source_job_id=f"{fingerprint}-{_RUN}",
        url=url,
        company="Acme",
        title=title,
        location="Remote",
        remote=True,
        description=f"description for {title}",
        content_confidence=content_confidence,
    )


def _make_evaluation(job_id: str, status: str = "ok") -> Evaluation:
    return Evaluation(
        job_id=job_id,
        total_score=0,
        scores={},
        decision="pass",
        hard_blockers=[],
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale="",
        model="test",
        status=status,
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

    def failing_for_bad(job, **kwargs):
        # `_make_job` makes the fingerprint unique per run, so this matches
        # the prefix rather than the whole value.
        if job.source_job_id.startswith("skip-bad"):
            raise RuntimeError("this one job is malformed")
        return original_single(job)

    monkeypatch.setattr(store._client, "rpc", failing_batch)
    monkeypatch.setattr(store, "upsert_logical_job", failing_for_bad)

    results = store.upsert_logical_jobs(jobs)

    assert results[0] is not None
    assert results[1] is None


def test_upsert_logical_jobs_raises_when_every_chunk_fails(store, monkeypatch):
    """A systemically broken batch path must stop the run, not replay it per job.

    Falling back to one request per job is the right answer for a single
    poison posting. It is the wrong answer when the batch path itself is
    down (statement_timeout, missing function, dead PostgREST): replaying
    19,000 jobs individually produces a run slower than the unbatched code
    this replaced, and it looks in the log like a few unlucky postings
    rather than an outage. After `_CONSECUTIVE_CHUNK_FAILURE_LIMIT` chunks
    fail back to back, the store raises.
    """
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_JOB_UPSERT_CHUNK_SIZE", 1)
    original_rpc = store._client.rpc
    replayed: list[str] = []

    def always_failing_batch(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs":
            raise RuntimeError("statement timeout")
        return original_rpc(function, payload, **kwargs)

    def recording_single(job, **kwargs):
        replayed.append(job.source_job_id)
        return ("00000000-0000-0000-0000-000000000001", False, False)

    monkeypatch.setattr(store._client, "rpc", always_failing_batch)
    monkeypatch.setattr(store, "upsert_logical_job", recording_single)

    jobs = [
        _make_job(f"systemic-{i}", f"Engineer {i}", f"https://example.test/sy{i}")
        for i in range(10)
    ]

    with pytest.raises(Exception, match="consecutive batch job upserts failed"):
        store.upsert_logical_jobs(jobs)

    # It gave the isolated-poison-posting theory exactly
    # _CONSECUTIVE_CHUNK_FAILURE_LIMIT - 1 chances (one job per chunk here)
    # before giving up, rather than replaying all ten.
    assert len(replayed) == module._CONSECUTIVE_CHUNK_FAILURE_LIMIT - 1


def test_upsert_logical_jobs_tolerates_isolated_chunk_failures(store, monkeypatch):
    """The consecutive-failure guard must not fire on scattered bad chunks.

    Failures separated by a success are the poison-posting case the per-job
    replay exists for, however many of them a run hits. Only an unbroken
    streak means the batch path itself is down.
    """
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_JOB_UPSERT_CHUNK_SIZE", 1)
    original_rpc = store._client.rpc
    chunk_number = {"n": 0}

    def failing_on_even_chunks(function, payload=None, **kwargs):
        if function == "job_hunter_upsert_jobs":
            chunk_number["n"] += 1
            if chunk_number["n"] % 2 == 1:
                raise RuntimeError("one bad posting")
        return original_rpc(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", failing_on_even_chunks)

    jobs = [
        _make_job(f"scattered-{i}", f"Engineer {i}", f"https://example.test/sc{i}")
        for i in range(8)
    ]

    results = store.upsert_logical_jobs(jobs)

    assert len(results) == 8
    assert all(result is not None for result in results)


def test_upsert_logical_jobs_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made for an empty batch")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    assert store.upsert_logical_jobs([]) == []


def test_needs_evaluation_bulk_agrees_with_the_single_job_method(store):
    job_ids = [
        result[0]
        for result in store.upsert_logical_jobs(
            [
                _make_job("bulk-eval-1", "Frontend Engineer", "https://example.test/e1"),
                _make_job("bulk-eval-2", "Backend Engineer", "https://example.test/e2"),
            ]
        )
    ]

    bulk = store.needs_evaluation_bulk(job_ids)

    assert bulk == {job_id: store.needs_evaluation(job_id) for job_id in job_ids}
    assert all(bulk.values())  # nothing evaluated yet


def test_needs_evaluation_bulk_defaults_an_unreadable_id_to_true(store):
    unknown = "99999999-0000-0000-0000-000000000009"

    assert store.needs_evaluation_bulk([unknown]) == {unknown: True}


def test_needs_evaluation_bulk_makes_one_request_per_chunk(store, monkeypatch):
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_ID_ARRAY_CHUNK_SIZE", 2)
    calls: list[int] = []
    original = store._client.rpc

    def counting_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_needs_evaluation":
            calls.append(len(payload["p_job_ids"]))
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", counting_rpc)

    job_ids = [
        result[0]
        for result in store.upsert_logical_jobs(
            [
                _make_job(f"bulk-chunk-{i}", f"Engineer {i}", f"https://example.test/bc{i}")
                for i in range(3)
            ]
        )
    ]
    store.needs_evaluation_bulk(job_ids)

    assert calls == [2, 1]


def test_needs_evaluation_bulk_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made for an empty id list")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    assert store.needs_evaluation_bulk([]) == {}


# The tests above only ever exercise a job with no evaluation row, or an id
# that does not exist -- both always answer `true`, so they would not catch
# the SQL's evaluation comparison being weakened or its ordering flipped.
# Everything below is differential: it drives both `needs_evaluation` and
# `needs_evaluation_bulk` for the same job through the real per-job path
# (`store.upsert_job`/`store.save_evaluation`) and asserts they agree, so
# the bulk SQL can never silently drift from the single-job Python method
# it is meant to reproduce.


def test_needs_evaluation_bulk_agrees_when_the_evaluation_is_current(store):
    job_id, _, _ = store.upsert_job(_make_job("diff-current", "Engineer", "https://example.test/dc"))
    store.save_evaluation(job_id, _make_evaluation(job_id))

    assert store.needs_evaluation_bulk([job_id])[job_id] is store.needs_evaluation(job_id) is False


def test_needs_evaluation_bulk_agrees_when_the_description_changed_since_evaluation(store):
    job_id, _, _ = store.upsert_job(_make_job("diff-desc", "Engineer V1", "https://example.test/dd"))
    store.save_evaluation(job_id, _make_evaluation(job_id))

    # Same fingerprint (source_job_id), different title -> different
    # description text -> a new description_hash on the posting, which is
    # what decides re-evaluation (#177). The second title is longer on
    # purpose: the posting keeps the better description, so an equally
    # trustworthy but shorter re-fetch would leave it -- and the evaluation
    # that was made against it -- unchanged.
    store.upsert_job(
        _make_job("diff-desc", "Engineer V2 with more detail", "https://example.test/dd")
    )

    assert store.needs_evaluation_bulk([job_id])[job_id] is store.needs_evaluation(job_id) is True


def test_needs_evaluation_bulk_agrees_when_content_confidence_changed_since_evaluation(store):
    job_id, _, _ = store.upsert_job(
        _make_job("diff-conf", "Engineer", "https://example.test/dcf", content_confidence="partial_unknown")
    )
    store.save_evaluation(job_id, _make_evaluation(job_id))

    # An upgrade, not a downgrade. The posting keeps the more trustworthy
    # tier, so a weaker re-fetch leaves nothing for the evaluation to be
    # stale against (#177).
    store.upsert_job(
        _make_job("diff-conf", "Engineer", "https://example.test/dcf", content_confidence="official_ats")
    )

    assert store.needs_evaluation_bulk([job_id])[job_id] is store.needs_evaluation(job_id) is True


def test_needs_evaluation_bulk_agrees_when_the_latest_evaluation_failed(store):
    job_id, _, _ = store.upsert_job(_make_job("diff-failed", "Engineer", "https://example.test/df"))
    store.save_evaluation(job_id, _make_evaluation(job_id, status="failed"))

    assert store.needs_evaluation_bulk([job_id])[job_id] is store.needs_evaluation(job_id) is True


def test_needs_evaluation_bulk_agrees_on_which_evaluation_is_latest(store):
    """An older evaluation that matches must not win over a newer one that doesn't -- and vice versa."""
    job_id, _, _ = store.upsert_job(_make_job("diff-order", "Engineer V1", "https://example.test/do"))
    # eval_old: matches the job's state at the time it was saved.
    store.save_evaluation(job_id, _make_evaluation(job_id))

    # The job changes after eval_old, so eval_old is now stale relative to
    # the job's current state.
    store.upsert_job(
        _make_job("diff-order", "Engineer V2 with more detail", "https://example.test/do")
    )

    # eval_new: saved after the change, so it matches the job's *current*
    # state. If the SQL picked the oldest evaluation instead of the
    # newest, it would see eval_old (stale) and wrongly report `true`.
    store.save_evaluation(job_id, _make_evaluation(job_id))

    assert store.needs_evaluation_bulk([job_id])[job_id] is store.needs_evaluation(job_id) is False


def test_needs_evaluation_bulk_maps_each_id_to_its_own_verdict(store):
    """One call covering several of the cases above proves the per-row mapping, not just the single-id case."""
    matching_id, _, _ = store.upsert_job(_make_job("diff-mix-match", "Engineer", "https://example.test/dmm"))
    store.save_evaluation(matching_id, _make_evaluation(matching_id))

    stale_id, _, _ = store.upsert_job(_make_job("diff-mix-stale", "Engineer V1", "https://example.test/dms"))
    store.save_evaluation(stale_id, _make_evaluation(stale_id))
    store.upsert_job(
        _make_job("diff-mix-stale", "Engineer V2 with more detail", "https://example.test/dms")
    )

    failed_id, _, _ = store.upsert_job(_make_job("diff-mix-failed", "Engineer", "https://example.test/dmf"))
    store.save_evaluation(failed_id, _make_evaluation(failed_id, status="failed"))

    unevaluated_id, _, _ = store.upsert_job(_make_job("diff-mix-none", "Engineer", "https://example.test/dmn"))

    job_ids = [matching_id, stale_id, failed_id, unevaluated_id]

    assert store.needs_evaluation_bulk(job_ids) == {
        job_id: store.needs_evaluation(job_id) for job_id in job_ids
    }
    assert store.needs_evaluation_bulk(job_ids) == {
        matching_id: False,
        stale_id: True,
        failed_id: True,
        unevaluated_id: True,
    }


def test_set_job_markets_writes_each_job_its_own_market(store):
    results = store.upsert_logical_jobs(
        [
            _make_job("market-1", "Frontend Engineer", "https://example.test/mk1"),
            _make_job("market-2", "Backend Engineer", "https://example.test/mk2"),
        ]
    )
    first, second = results[0][0], results[1][0]

    store.set_job_markets([(first, "israel"), (second, "eu_remote")])

    assert store.get_job(first).market_id == "israel"
    assert store.get_job(second).market_id == "eu_remote"


def test_set_job_markets_stores_an_unattributed_job_as_empty(store):
    job_id = store.upsert_logical_jobs(
        [_make_job("market-3", "Platform Engineer", "https://example.test/mk3")]
    )[0][0]

    store.set_job_markets([(job_id, None)])

    assert not store.get_job(job_id).market_id


def test_set_job_markets_on_empty_input_makes_no_request(store, monkeypatch):
    def exploding_rpc(*args, **kwargs):
        raise AssertionError("no request should be made with nothing to set")

    monkeypatch.setattr(store._client, "rpc", exploding_rpc)

    store.set_job_markets([])


def test_set_job_markets_chunks_by_the_configured_size(store, monkeypatch):
    """More pairs than one chunk holds must still land correctly, over more than one RPC call.

    A test that only checked the final values would pass whether or not
    chunking exists, so this asserts both: every job gets its own correct
    market, and the store made more than one request to do it.
    """
    from job_hunter import postgres_store as module

    monkeypatch.setattr(module, "_ID_ARRAY_CHUNK_SIZE", 2)
    calls: list[int] = []
    original = store._client.rpc

    def counting_rpc(function, payload=None, **kwargs):
        if function == "job_hunter_set_job_markets":
            calls.append(len(payload["p_rows"]))
        return original(function, payload, **kwargs)

    monkeypatch.setattr(store._client, "rpc", counting_rpc)

    jobs = [
        _make_job(f"market-chunk-{i}", f"Engineer {i}", f"https://example.test/mc{i}")
        for i in range(5)
    ]
    job_ids = [result[0] for result in store.upsert_logical_jobs(jobs)]
    markets = [f"market-{i}" for i in range(5)]

    store.set_job_markets(list(zip(job_ids, markets)))

    assert calls == [2, 2, 1]
    for job_id, market in zip(job_ids, markets):
        assert store.get_job(job_id).market_id == market


def test_upsert_ats_boards_asks_once_per_distinct_board(store, monkeypatch):
    """Many sightings of the same (provider, board) must collapse to one call.

    Asserting only the returned count would pass even if every sighting hit
    the single-board method separately -- the count would still land on 2
    because repeats of an already-newly-registered board return False on the
    second call. The call log is what actually proves deduplication.
    """
    calls: list[tuple[str, str]] = []
    original = store.upsert_ats_board

    def counting(provider, board_identifier, company_name="", market_hint=""):
        calls.append((provider, board_identifier))
        return original(provider, board_identifier, company_name, market_hint)

    monkeypatch.setattr(store, "upsert_ats_board", counting)

    newly_registered = store.upsert_ats_boards(
        [
            ("greenhouse", "acme", "Acme", "israel"),
            ("greenhouse", "acme", "Acme", "israel"),
            ("lever", "globex", "Globex", ""),
        ]
    )

    assert calls == [("greenhouse", "acme"), ("lever", "globex")]
    assert newly_registered == 2


def test_upsert_ats_boards_dedupe_key_is_provider_and_board_only(store, monkeypatch):
    """Two sightings sharing (provider, board) collapse even when company_name differs.

    The dedupe key is (provider, board_identifier) -- not the full tuple.
    A sighting with a different company_name for the same board must still
    collapse into a single call (first sighting wins), while a sighting with
    a different board_identifier must never be collapsed with it. This
    guards against an implementation that dedupes on the whole 4-tuple,
    which would under-collapse and leave the request count tied to the
    number of distinct company-name spellings rather than distinct boards.
    """
    calls: list[tuple[str, str, str, str]] = []
    original = store.upsert_ats_board

    def counting(provider, board_identifier, company_name="", market_hint=""):
        calls.append((provider, board_identifier, company_name, market_hint))
        return original(provider, board_identifier, company_name, market_hint)

    monkeypatch.setattr(store, "upsert_ats_board", counting)

    newly_registered = store.upsert_ats_boards(
        [
            ("greenhouse", "acme", "Acme Inc.", "israel"),
            ("greenhouse", "acme", "ACME Corp", "remote"),  # same key, differing fields
            ("greenhouse", "acme-eu", "Acme Inc.", "israel"),  # different key, same company
        ]
    )

    assert calls == [
        ("greenhouse", "acme", "Acme Inc.", "israel"),
        ("greenhouse", "acme-eu", "Acme Inc.", "israel"),
    ]
    assert newly_registered == 2


def test_upsert_ats_boards_backfills_blank_fields_from_a_later_sighting(
    store, monkeypatch
):
    """A board first seen with a blank field must still learn it from a later sighting.

    The per-job loop this replaced called `upsert_ats_board` once per
    sighting, and that method's `company_name or row["company_name"]` meant
    the second sighting backfilled whatever the first left blank. Freezing
    the first sighting would keep the blank for the whole run -- and a blank
    `market_hint` costs the board its place in `select_ats_boards`' market
    ranking, so this is not cosmetic.
    """
    calls: list[tuple[str, str, str, str]] = []
    original = store.upsert_ats_board

    def counting(provider, board_identifier, company_name="", market_hint=""):
        calls.append((provider, board_identifier, company_name, market_hint))
        return original(provider, board_identifier, company_name, market_hint)

    monkeypatch.setattr(store, "upsert_ats_board", counting)

    store.upsert_ats_boards(
        [
            ("greenhouse", "backfill", "", ""),  # first sighting knows nothing
            ("greenhouse", "backfill", "Backfill Inc.", ""),  # learns the company
            ("greenhouse", "backfill", "Other Name", "israel"),  # learns the market
        ]
    )

    # Still one request for the board -- merging must not cost the collapse.
    assert calls == [("greenhouse", "backfill", "Backfill Inc.", "israel")]

    boards = store.list_due_ats_boards(datetime.now(timezone.utc))
    matching = [b for b in boards if b.board_identifier == "backfill"]
    assert len(matching) == 1
    assert matching[0].company_name == "Backfill Inc."
    assert matching[0].market_hint == "israel"


def test_upsert_ats_boards_returns_only_the_newly_registered_count(store):
    """The count reflects boards newly admitted, not every board passed in.

    Register a board first, then call again with a superset that repeats it
    alongside a genuinely new board. If the return value merely counted
    distinct boards seen (rather than distinct boards *newly written*), this
    would wrongly report 2 on the second call instead of 1.
    """
    first_pass = store.upsert_ats_boards(
        [("greenhouse", "acme", "Acme", "israel")]
    )
    assert first_pass == 1

    second_pass = store.upsert_ats_boards(
        [
            ("greenhouse", "acme", "Acme", "israel"),  # already registered
            ("lever", "globex", "Globex", ""),  # new
        ]
    )

    assert second_pass == 1


def test_upsert_ats_boards_survives_one_bad_board(store, monkeypatch):
    original = store.upsert_ats_board

    def failing_for_globex(provider, board_identifier, company_name="", market_hint=""):
        if board_identifier == "globex":
            raise RuntimeError("registry write failed")
        return original(provider, board_identifier, company_name, market_hint)

    monkeypatch.setattr(store, "upsert_ats_board", failing_for_globex)

    newly_registered = store.upsert_ats_boards(
        [("lever", "globex", "Globex", ""), ("greenhouse", "initech", "Initech", "")]
    )

    assert newly_registered == 1


def test_upsert_ats_boards_on_empty_input_does_nothing(store, monkeypatch):
    def exploding(*args, **kwargs):
        raise AssertionError("no board write should happen for an empty list")

    monkeypatch.setattr(store, "upsert_ats_board", exploding)

    assert store.upsert_ats_boards([]) == 0

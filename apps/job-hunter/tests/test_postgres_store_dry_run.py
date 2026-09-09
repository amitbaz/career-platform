"""Tests for `DryRunStore`, the safe stand-in for a dry `cli.py` run.

These are pure unit tests against the class and its registries -- none of
them need the local Supabase stack, because the whole point of
`DryRunStore` is that it never reaches the client on a write path. Where a
test needs *some* client to prove that, it uses a spy that raises on any
attribute access, so a wrapper that accidentally forwarded to the real
store would fail loudly instead of quietly making a network call.
"""

from __future__ import annotations

import uuid

import pytest

from job_hunter.models import Job
from job_hunter.postgres_store import (
    DryRunStore,
    PostgresJobStore,
    PostingBatch,
    _POSTGRES_JOB_STORE_READ_METHODS,
    _POSTGRES_JOB_STORE_WRITE_METHODS,
)


class _ExplodingClient:
    """A `SupabaseClient` stand-in that fails any attempt to use it.

    Wired into a real `PostgresJobStore`, then wrapped in `DryRunStore`, so
    that calling a write method can only pass this test if `DryRunStore`
    truly never reaches into the wrapped store's client.
    """

    def __getattr__(self, name):  # pragma: no cover - exercised via AssertionError
        raise AssertionError(
            f"DryRunStore must never touch the client, but {name!r} was accessed"
        )


def _classified_public_method_names() -> set[str]:
    """Every public callable/property `PostgresJobStore` defines directly."""
    names = set()
    for name, value in vars(PostgresJobStore).items():
        if name.startswith("_"):
            continue
        names.add(name)
    return names


def test_every_public_method_is_classified():
    """The write/read registries partition every public PostgresJobStore member.

    This is the test that matters: if `PostgresJobStore` grows a new public
    method (write or read) and nobody adds it to one of the two registries
    in `postgres_store.py`, this fails immediately. Left unclassified, a
    write method would fall through `DryRunStore.__getattr__` and delegate
    straight to the real store -- exactly the mutation a dry run must never
    perform.

    Non-vacuousness: temporarily adding an unregistered attribute to
    `PostgresJobStore` (simulating a forgotten new method) makes this
    assertion fail with the added name reported as unclassified; removing
    it again restores the pass. That was verified by hand while writing
    this test, and is re-verified below without leaving the mutation in
    place for other tests to trip over.
    """
    all_public = _classified_public_method_names()
    write_names = set(_POSTGRES_JOB_STORE_WRITE_METHODS)
    read_names = set(_POSTGRES_JOB_STORE_READ_METHODS)

    overlap = write_names & read_names
    assert overlap == set(), f"methods classified as both read and write: {overlap}"

    classified = write_names | read_names
    unclassified = all_public - classified
    assert unclassified == set(), (
        f"PostgresJobStore has public methods not classified as a read or "
        f"write for DryRunStore: {unclassified}. Add each to "
        f"_POSTGRES_JOB_STORE_WRITE_METHODS (with its synthetic-return "
        f"shape) or _POSTGRES_JOB_STORE_READ_METHODS in postgres_store.py."
    )

    stale = classified - all_public
    assert stale == set(), (
        f"DryRunStore registries name methods PostgresJobStore no longer "
        f"has: {stale}"
    )


def test_completeness_check_fails_when_a_write_method_is_forgotten():
    """Prove the completeness test actually bites, not just that it passes today.

    Simulates the exact failure this whole design exists to catch: a brand
    new method lands on `PostgresJobStore` and nobody updates either
    registry. Attaching it directly to the class (via `setattr`, undone in
    a `finally`) reproduces that without mutating the source file.
    """

    def _new_write_method(self, *args, **kwargs):  # pragma: no cover - never called
        raise AssertionError("should never run")

    setattr(PostgresJobStore, "totally_new_write_method", _new_write_method)
    try:
        with pytest.raises(AssertionError, match="not classified"):
            test_every_public_method_is_classified()
    finally:
        delattr(PostgresJobStore, "totally_new_write_method")

    # And the suite is healthy again once the simulated method is gone.
    test_every_public_method_is_classified()


@pytest.mark.parametrize("name,shape", sorted(_POSTGRES_JOB_STORE_WRITE_METHODS.items()))
def test_write_methods_never_touch_the_client_and_return_synthetic_values(name, shape):
    """Every registered write method must be safe to call with no client at all.

    `DryRunStore`'s wrappers ignore their arguments entirely (see
    `_make_dry_run_write`), so calling with zero arguments exercises the
    real code path regardless of the wrapped method's true signature. If a
    wrapper ever forwarded to `self._store`, `_ExplodingClient` would raise
    on the first attribute access the real method made.
    """
    real_store = PostgresJobStore(_ExplodingClient())
    dry_run = DryRunStore(real_store)

    result = getattr(dry_run, name)()

    if shape is None:
        assert result is None
    elif isinstance(shape, tuple):
        assert isinstance(result, tuple) and len(result) == len(shape)
        for part, expected_shape in zip(result, shape):
            _assert_synthetic(part, expected_shape)
    else:
        _assert_synthetic(result, shape)


def _assert_synthetic(value, shape: str) -> None:
    if shape == "id":
        assert isinstance(value, str)
        uuid.UUID(value)  # raises ValueError if not a real uuid string
    elif shape == "bool":
        assert value is False
    elif shape == "count":
        assert value == 0
    elif shape == "job_upsert_results":
        assert value == []
    elif shape == "posting_batch":
        # An empty batch, which is exactly what a store with no direct
        # Postgres connection returns -- and what every caller already
        # handles by resolving each posting inside its own job upsert.
        assert value == PostingBatch()
    elif shape == "echo_job_id":
        # The wrapper hands back its first argument; the caller above passes
        # none, so None is the honest expectation here. The value it echoes
        # for a real call is covered by its own test below.
        assert value is None
    else:  # pragma: no cover - defensive
        raise AssertionError(f"unknown shape {shape!r}")


def test_write_methods_prove_the_exploding_client_is_actually_wired():
    """Non-vacuousness proof for the test above.

    If `_ExplodingClient` were inert (never actually raising), the previous
    test would pass even for a write method that WAS forwarded to the real
    store, because nothing would object. This proves the spy bites: calling
    the *real* `PostgresJobStore` method (bypassing `DryRunStore` entirely)
    raises exactly the AssertionError `_ExplodingClient` is built to raise.
    """
    real_store = PostgresJobStore(_ExplodingClient())
    with pytest.raises(AssertionError, match="DryRunStore must never touch the client"):
        real_store.set_job_market("some-job-id", None)


def test_two_id_and_bool_write_methods_return_independent_synthetic_ids():
    """uuid4() ids are freshly generated per call, never reused or derived."""
    real_store = PostgresJobStore(_ExplodingClient())
    dry_run = DryRunStore(real_store)

    first = dry_run.upsert_company_watch(
        company_name="a",
        careers_url="",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="automatic",
        confidence=0.5,
    )
    second = dry_run.upsert_company_watch(
        company_name="a",
        careers_url="",
        ats_provider=None,
        ats_identifier=None,
        discovered_from_job_id=None,
        promotion_source="automatic",
        confidence=0.5,
    )
    assert first != second


@pytest.mark.parametrize("job_count", [0, 1, 3])
def test_batch_job_upsert_returns_one_non_insert_result_per_job(job_count):
    real_store = PostgresJobStore(_ExplodingClient())
    dry_run = DryRunStore(real_store)
    jobs = [
        Job(
            source="test",
            source_job_id=str(index),
            title="Senior Product Engineer",
        )
        for index in range(job_count)
    ]

    results = dry_run.upsert_logical_jobs(jobs)

    assert len(results) == job_count
    synthetic_ids = []
    for synthetic_id, is_new, description_changed in results:
        uuid.UUID(synthetic_id)
        synthetic_ids.append(synthetic_id)
        assert is_new is False
        assert description_changed is False
    assert len(set(synthetic_ids)) == job_count


class _FakeReadOnlyStore:
    """A minimal double standing in for a real `PostgresJobStore` on reads."""

    def __init__(self):
        self.client = "sentinel-client"
        self.calls = []

    def get_job(self, job_id):
        self.calls.append(("get_job", job_id))
        return f"job:{job_id}"

    def count_jobs(self):
        self.calls.append(("count_jobs",))
        return 42


def test_read_methods_delegate_to_the_wrapped_store():
    """Reads pass straight through untouched -- `DryRunStore` adds no logic."""
    fake = _FakeReadOnlyStore()
    dry_run = DryRunStore(fake)

    assert dry_run.get_job("abc") == "job:abc"
    assert dry_run.count_jobs() == 42
    assert fake.calls == [("get_job", "abc"), ("count_jobs",)]


def test_client_raises_instead_of_reaching_the_wrapped_store():
    """`.client` is the one member DryRunStore must never delegate for real.

    Handing back the wrapped store's `.client` would return the live,
    fully write-capable `SupabaseClient`, bypassing every write wrapper
    above. `DryRunStore` defines `client` itself (not via `__getattr__`)
    so it raises before ever touching `self._store.client`.
    """
    fake = _FakeReadOnlyStore()
    dry_run = DryRunStore(fake)

    with pytest.raises(AssertionError, match="a dry run must not reach the client"):
        dry_run.client


def test_dry_run_store_is_a_context_manager_that_closes_nothing():
    fake = _FakeReadOnlyStore()
    with DryRunStore(fake) as dry_run:
        assert dry_run is not None


def test_save_evaluation_echoes_the_job_id_it_was_given():
    """A dry run writes nothing, so the evaluation cannot have moved.

    The real `save_evaluation` returns the id it wrote against, which is the
    survivor when the job was merged away mid-run (#145). Under a dry run the
    caller's own id is the truthful answer, and it has to be a usable id --
    the pipeline puts it straight into the digest item.
    """
    dry_run = DryRunStore(PostgresJobStore(_ExplodingClient()))

    assert dry_run.save_evaluation("job-1", object()) == "job-1"

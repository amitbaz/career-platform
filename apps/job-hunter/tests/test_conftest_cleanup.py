"""The store fixtures' cleanup explains the one failure it cannot prevent.

The local Supabase stack is one instance per machine rather than one per
worktree, and every session's fixtures clean the same two seed users. When
two runs overlap, the symptom is a scatter of unrelated-looking assertion
failures in whichever tests happened to be running -- nothing that points at
the cleanup. These tests pin the translation that turns the one legible
signal (a foreign-key violation during teardown) into that explanation.

Pure unit tests: they drive `_truncate` with a fake client and never touch
the stack, so they run in any environment.
"""

from __future__ import annotations

import pytest

from job_hunter.supabase_client import SupabaseAuthError, SupabaseRequestError
from tests.conftest import _TABLES_CHILD_FIRST, _truncate


class _FakeClient:
    """Records deletes, and fails on one named table."""

    def __init__(self, fail_on: str | None = None, error: Exception | None = None):
        self.fail_on = fail_on
        self.error = error
        self.deleted: list[str] = []

    def delete(self, table: str, params=None):
        self.deleted.append(table)
        if table == self.fail_on and self.error is not None:
            raise self.error
        return []


def _foreign_key_error(referencing_table: str) -> SupabaseRequestError:
    """The 409 PostgREST returns when a child row still points at a parent."""
    return SupabaseRequestError(
        'Supabase request failed with 409: {"code":"23503","details":"Key is '
        f'still referenced from table \\"{referencing_table}\\".","hint":null,'
        '"message":"update or delete on table \\"job_hunter_jobs\\" violates '
        "foreign key constraint on table "
        f'\\"{referencing_table}\\""}}'
    )


def test_cleanup_deletes_every_table_with_jobs_last():
    client = _FakeClient()

    _truncate(client)

    assert client.deleted == list(_TABLES_CHILD_FIRST)
    assert client.deleted[-1] == "job_hunter_jobs", (
        "job_hunter_jobs is what everything else references; deleting it "
        "before its children would fail on a correctly ordered run too"
    )


def test_a_foreign_key_violation_is_explained_as_a_second_writer():
    client = _FakeClient(
        fail_on="job_hunter_jobs",
        error=_foreign_key_error("job_hunter_evaluations"),
    )

    with pytest.raises(RuntimeError) as caught:
        _truncate(client)

    message = str(caught.value)
    assert "job_hunter_jobs" in message
    assert "another session" in message
    # The reader needs to know why their unrelated-looking failures happened,
    # and what to run to confirm it.
    assert "one instance per machine" in message
    assert "git worktree list" in message
    assert "pytest" in message


def test_the_original_supabase_error_is_kept_as_the_cause():
    """The explanation must not hide the 409 it was derived from."""
    original = _foreign_key_error("job_hunter_company_watch")
    client = _FakeClient(fail_on="job_hunter_jobs", error=original)

    with pytest.raises(RuntimeError) as caught:
        _truncate(client)

    assert caught.value.__cause__ is original
    assert "23503" in str(caught.value.__cause__)


def test_any_other_failure_is_left_alone():
    """Only the foreign-key case has this explanation; nothing else does."""
    original = SupabaseAuthError("Supabase rejected the access token (401)")
    client = _FakeClient(fail_on="job_hunter_evaluations", error=original)

    with pytest.raises(SupabaseAuthError) as caught:
        _truncate(client)

    assert caught.value is original


def test_cleanup_stops_at_the_failing_table():
    """A dirty database must not be mistaken for a clean one."""
    client = _FakeClient(
        fail_on="job_hunter_evaluations",
        error=_foreign_key_error("job_hunter_jobs"),
    )

    with pytest.raises(RuntimeError):
        _truncate(client)

    assert client.deleted[-1] == "job_hunter_evaluations"
    assert "job_hunter_jobs" not in client.deleted

from __future__ import annotations


def release_legacy_blank_linkedin_jobs(store) -> int:
    """Release only safe blank/poisoned LinkedIn Gmail artifacts for reprocessing.

    The query logic that used to live here (and reached around the store via
    `store._conn`) now lives on the store itself --
    `PostgresJobStore.release_legacy_blank_linkedin_jobs` for the Postgres
    backend, `JobStore.release_legacy_blank_linkedin_jobs` for the SQLite
    backend still in use until issue #70 task 14 replaces it -- so both
    backends expose the same method name and this module is left as a thin
    forwarding shim for `gmail_sync.py`'s existing import. See either
    method's docstring for the translated dependency/poison-title checks.
    """
    return store.release_legacy_blank_linkedin_jobs()

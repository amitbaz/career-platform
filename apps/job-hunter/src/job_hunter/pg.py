"""Ingestion's direct, pooled Postgres connection (issue #182).

Every other database call this application makes goes through PostgREST over
HTTP, as a user, under row-level security. That is the right transport for
anything a user can see, and it stays exactly where it is.

It is the wrong transport for a crawl. Run 34289288702 spent 1170.8s resolving
1,221 jobs against only 81 network attempts to other people's servers: the
engine was waiting on its own database, one row at a time, over HTTP. PostgREST
also cannot express the two things that make a crawl cheap -- ``COPY`` a batch
in, and one set-based statement over it.

So ingestion gets a second transport: a pooled connection as the privileged
role, used only for tables with no user dimension (``job_hunter_postings`` and
its staging area). Matching, delivery and every per-user read keep PostgREST and
row-level security. The asymmetry is the point, and it is why this module
deliberately offers a connection and nothing that knows what a posting is --
the domain logic lives in `postgres_store`, where the rest of it already is.

The connection is optional. A deployment with no ``SUPABASE_DB_URL`` runs the
whole pipeline exactly as it did before, one posting upsert per listing, so
this is a saving that can be turned off rather than a dependency that can break
a run.
"""

from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# A crawl uses one connection at a time -- it stages a batch and merges it --
# so the pool exists to hold that connection open across batches, not to run
# them in parallel. The ceiling is small on purpose: the session pooler in
# front of Supabase has a bounded client budget shared with everything else
# pointed at the project.
_DEFAULT_MIN_SIZE = 1
_DEFAULT_MAX_SIZE = 2

# How long a caller waits for a free connection before giving up. Well above
# any healthy wait at this pool size, so hitting it means the pool is wedged
# rather than busy, and a run that reports it names a real problem.
_POOL_TIMEOUT_SECONDS = 30.0

_MISSING_DRIVER = (
    "psycopg is not installed, so ingestion cannot open a direct Postgres "
    "connection. Install the app's dependencies (pip install -e '.[test,webhook]')."
)


class IngestionDatabase:
    """A pooled Postgres connection held by ingestion, as a privileged role.

    Opened lazily: constructing one does not connect, so a misconfigured
    ``SUPABASE_DB_URL`` fails at the first batch rather than at import time,
    alongside the work it was meant to serve.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = _DEFAULT_MIN_SIZE,
        max_size: int = _DEFAULT_MAX_SIZE,
    ) -> None:
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as error:  # pragma: no cover - dependency is declared
            raise RuntimeError(_MISSING_DRIVER) from error
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=_POOL_TIMEOUT_SECONDS,
            open=False,
            # Named so a connection is identifiable in pg_stat_activity: a
            # privileged connection nobody can attribute is the kind of thing
            # that gets left open for a week.
            kwargs={"application_name": "job-hunter-ingestion"},
        )
        self._opened = False
        self._unavailable = False

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Lease a connection for one unit of work.

        The transaction commits on a clean exit and rolls back on an
        exception, which is psycopg's own connection-context behaviour and
        exactly what a staged batch wants: a batch is either merged or still
        in staging, never half of each.

        A run that cannot get a connection at all stops asking. Waiting for
        one costs `_POOL_TIMEOUT_SECONDS`, and a crawl leases at least three
        -- raw persist, unique persist, and the deferred canonical writes --
        so an unreachable pooler would otherwise add a minute and a half of
        dead waiting to a run whose only remedy is the fallback it already
        took the first time. The caller's fallback is the same either way, and
        a run is short enough that giving up for its length is the right unit:
        the next run builds a new pool and tries again.
        """
        if self._unavailable:
            raise RuntimeError(
                "ingestion's direct Postgres connection was already found "
                "unreachable this run"
            )
        with ExitStack() as stack:
            try:
                if not self._opened:
                    self._pool.open()
                    self._opened = True
                # The wait for a free connection is inside __enter__, not in
                # the call above it, so the lease has to be entered here to be
                # caught at all. An error raised by the caller's own work is
                # deliberately outside this guard: that connection worked.
                connection = stack.enter_context(self._pool.connection())
            except Exception:
                self._unavailable = True
                raise
            yield connection

    def close(self) -> None:
        """Return every connection to the server.

        Safe to call on a pool that was never opened, so a run that ended
        before it staged anything closes the same way as one that did.
        """
        if self._opened:
            self._pool.close()
            self._opened = False

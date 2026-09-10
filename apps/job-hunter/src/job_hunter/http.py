from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field

import requests
from requests.adapters import HTTPAdapter

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_BACKOFF_BASE = 2  # seconds: 2s, 4s


class _NotModified:
    """The response to a conditional request whose resource did not change.

    A distinct object rather than `None` or an empty list, because the whole
    point of a conditional request is that "unchanged" and "empty" are
    different answers and must not be able to collapse into one. A source
    that returns this reports `not_modified`; a source that genuinely
    returned no jobs reports `fetched` with zero.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NOT_MODIFIED"


NOT_MODIFIED = _NotModified()


@dataclass(frozen=True)
class Validators:
    """The HTTP cache validators a source carries between crawls."""

    etag: str = ""
    last_modified: str = ""

    def as_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


class NotModifiedSignal(BaseException):
    """Raised inside an adapter when its board answered 304.

    Deliberately a `BaseException` rather than an `Exception`, which is the
    opposite of the rule `crawl_source` follows and needs its own defence.

    Adapters catch `Exception` freely -- per-board `try` blocks in
    `learned_ats`, stale-board 404 handling in several others. A signal that
    those swallow is a signal that silently becomes "this source returned
    nothing", which is precisely the collapse `NOT_MODIFIED` exists to
    prevent: "unchanged" and "empty" would stop being different answers.
    Sitting outside `Exception` is what makes the signal survive the
    seventeen adapters unchanged, none of which knows conditional requests
    exist.

    It is caught in exactly two places -- `discovery._iter_source_jobs` and
    `crawl_source.CrawlSourceStage.__call__` -- both of which sit outside the
    adapter and convert it into a recorded `not_modified` outcome. It must
    never be allowed to escape past those, which is why both catch it
    explicitly rather than relying on a bare `except`.
    """

    __slots__ = ()


@dataclass
class ConditionalScope:
    """The validators for one crawl of one source, and what came back.

    `url` is the resource the stored validators were captured from. It is
    empty the first time a source is crawled, and the first GET the source
    issues then adopts the scope: that is what lets a source bootstrap its
    own cursor without anyone configuring a URL for it.

    Exactly one request per scope is made conditional: the first one matching
    `url`, or simply the first when bootstrapping. Everything else in the same
    crawl -- later pages, other boards -- is fetched unconditionally. A
    paginated source reuses one URL with different `params`, so anything
    looser would send page 0's validator to page 1 and then store page 1's
    ETag under the identity of the whole board.

    Only sources that are a single resource open a scope at all; see
    `JobSource.crawl_is_one_resource`.
    """

    url: str = ""
    validators: Validators = field(default_factory=Validators)
    observed_url: str = ""
    observed: Validators = field(default_factory=Validators)
    not_modified: bool = False


class HttpClient:
    """Thin wrapper around requests.Session with retry logic and sensible defaults.

    Counts the requests it makes in `request_count`. Discovery reads that
    counter either side of a source's `discover()` and attributes the delta
    to that source (see `job_hunter.discovery.collect_candidates`), which is
    why the count lives on the shared client rather than in each source: the
    sources differ in how they issue requests, and every one of them goes
    through here. Every attempt counts, retries included, because a source
    that is slow through being throttled has to read as chatty rather than
    as cheap.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "job-hunter-bot/1.0"})
        self._timeout = (5, 25)
        self.request_count = 0
        self._last_validators = Validators()
        self._scope: ConditionalScope | None = None

    def timeout_for_read(self, read_seconds: float) -> tuple[float, float]:
        """Return this client's timeout with a longer read budget.

        The default read budget suits request/response traffic -- PostgREST
        calls, Telegram sends, job-board fetches -- where a slow reply means
        something is wrong and failing fast is right. Long-form model
        generation is the exception: the server is legitimately still
        working. Callers in that position widen the read budget through this
        helper so they keep the shared connect budget rather than inventing
        their own pair.
        """
        return (self._timeout[0], read_seconds)

    def get(
        self,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._request(
            "GET",
            url,
            retry_status_codes=retry_status_codes,
            retry=retry,
            **kwargs,
        )

    def post(
        self,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._request(
            "POST",
            url,
            retry_status_codes=retry_status_codes,
            retry=retry,
            **kwargs,
        )

    def patch(
        self,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._request(
            "PATCH",
            url,
            retry_status_codes=retry_status_codes,
            retry=retry,
            **kwargs,
        )

    def delete(
        self,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return self._request(
            "DELETE",
            url,
            retry_status_codes=retry_status_codes,
            retry=retry,
            **kwargs,
        )

    @contextmanager
    def conditional(self, validators: Validators, *, url: str = ""):
        """Make this client issue one source's crawl conditionally.

        Wraps a single source's whole drain. Scopes nest and restore, so a
        source that somehow opened its own scope cannot strand the caller's.

        The adapters are not involved and do not change: they call
        `get_json(url)` exactly as before, and this decides whether that call
        carries `If-None-Match`/`If-Modified-Since`. A 304 raises
        `NotModifiedSignal` out through the adapter to whoever opened the
        scope, because there is no return value an adapter expecting a list
        or a dict could be handed that does not either crash it or get
        mistaken for an empty board.
        """
        scope = ConditionalScope(url=url, validators=validators)
        previous, self._scope = self._scope, scope
        try:
            yield scope
        finally:
            self._scope = previous

    def _scope_for(self, url: str) -> ConditionalScope | None:
        """The active scope, if it governs `url`.

        An empty `scope.url` means the source has no stored cursor yet and
        the first GET claims the scope. `observed_url` is what makes that
        happen once: without it every later URL in the same crawl would
        overwrite the first, and a paginated source would store the validator
        of its last page under the identity of its whole board.
        """
        scope = self._scope
        if scope is None:
            return None
        # One request per scope, always. Paginated sources walk many pages
        # from a single URL varying only `params` -- himalayas and remotive
        # both do -- so matching on the URL alone would send page 0's
        # validator to page 1, then store page 1's ETag under the identity of
        # the whole board. The next crawl would then 304 mid-drain and skip
        # every remaining page while reporting the board unchanged.
        if scope.observed_url:
            return None
        if scope.url:
            return scope if scope.url == url else None
        return scope

    def get_json(self, url: str, *, validators: Validators | None = None, **kwargs):
        """GET and decode JSON, honouring a conditional request.

        Returns `NOT_MODIFIED` when the server answers 304 to validators
        passed explicitly here. When the 304 answers an active
        `conditional()` scope instead, raises `NotModifiedSignal`: the
        explicit caller asked for the sentinel and can read it, whereas a
        scope is invisible to the adapter that made the call and needs a
        signal that unwinds rather than a value it would misread.

        Every other status keeps the previous behaviour exactly,
        `raise_for_status` included, so a real failure is still a failure.
        """
        scope = self._scope_for(url) if validators is None else None
        effective = validators if validators is not None else (
            scope.validators if scope is not None else None
        )
        if effective is not None:
            conditional = effective.as_headers()
            if conditional:
                headers = dict(kwargs.pop("headers", None) or {})
                headers.update(conditional)
                kwargs["headers"] = headers
        response = self.get(url, **kwargs)
        observed = Validators(
            etag=response.headers.get("ETag", "") or "",
            last_modified=response.headers.get("Last-Modified", "") or "",
        )
        self._last_validators = observed
        if scope is not None:
            scope.observed_url = url
            scope.not_modified = response.status_code == 304
            if response.status_code == 304:
                # A 304 may legally carry neither validator -- RFC 7232 only
                # requires ETag when none was sent -- and many boards send an
                # empty-headed one. Storing what it omitted would erase the
                # validator it just confirmed is still good, and the source
                # would alternate conditional and full fetches forever. Keep
                # what we sent, and fill in only what this response restated.
                scope.observed = Validators(
                    etag=observed.etag or scope.validators.etag,
                    last_modified=(
                        observed.last_modified or scope.validators.last_modified
                    ),
                )
                raise NotModifiedSignal()
            scope.observed = observed
        if response.status_code == 304:
            return NOT_MODIFIED
        response.raise_for_status()
        return response.json()

    def last_validators(self) -> Validators:
        """The validators from the most recent response, for persisting."""
        return self._last_validators

    def _request(
        self,
        method: str,
        url: str,
        *,
        retry_status_codes: set[int] | None = None,
        retry: bool = True,
        **kwargs,
    ) -> requests.Response:
        retry_codes = _RETRY_STATUS_CODES if retry_status_codes is None else retry_status_codes
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                self.request_count += 1
                response = self._session.request(method, url, **kwargs)
                if retry and response.status_code in retry_codes and attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))
                    continue
                return response
            except requests.RequestException as exc:
                last_exc = exc
                if not retry:
                    raise
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE * (2 ** attempt))
        if last_exc is not None:
            raise last_exc
        # Should not reach here, but satisfy type checker
        raise RuntimeError("Unexpected retry loop exit")

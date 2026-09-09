from __future__ import annotations

import time
from dataclasses import dataclass

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

    def get_json(self, url: str, *, validators: Validators | None = None, **kwargs):
        """GET and decode JSON, honouring a conditional request.

        Returns `NOT_MODIFIED` when the server answers 304. Every other
        status keeps the previous behaviour exactly, `raise_for_status`
        included, so a real failure is still a failure.
        """
        if validators is not None:
            conditional = validators.as_headers()
            if conditional:
                headers = dict(kwargs.pop("headers", None) or {})
                headers.update(conditional)
                kwargs["headers"] = headers
        response = self.get(url, **kwargs)
        self._last_validators = Validators(
            etag=response.headers.get("ETag", "") or "",
            last_modified=response.headers.get("Last-Modified", "") or "",
        )
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

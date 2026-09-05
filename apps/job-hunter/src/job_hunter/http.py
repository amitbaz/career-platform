from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter

_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_BACKOFF_BASE = 2  # seconds: 2s, 4s


class HttpClient:
    """Thin wrapper around requests.Session with retry logic and sensible defaults."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "job-hunter-bot/1.0"})
        self._timeout = (5, 25)

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

    def get_json(self, url: str, **kwargs) -> dict:
        response = self.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

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

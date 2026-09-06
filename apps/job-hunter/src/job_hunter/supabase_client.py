"""A minimal PostgREST client that acts as one Supabase user.

Deliberately narrow: it covers what issue #69 needs to prove data isolation,
and no more. Issue #70 grows it with the query surface the ported store
actually needs.

Two headers go on every request. ``Authorization`` carries the short-lived
token that decides which rows row-level security will return; ``apikey``
carries the project's publishable key, which Supabase requires separately and
which is public by design. A minted token is not valid in the ``apikey``
header.

Caveat for callers: the shared HttpClient retries 5xx responses, including on
POST. Every job_hunter_* table has a user-scoped unique key, so a retried
insert conflicts rather than duplicating, but a caller relying on
non-idempotent writes should pass ``retry=False`` itself.
"""

from __future__ import annotations

from typing import Any

from .config import SupabaseSettings
from .http import HttpClient
from .supabase_auth import AccessTokenMinter

_PAGE_SIZE = 1000


class SupabaseError(RuntimeError):
    """Base class for every failed Supabase request."""


class SupabaseAuthError(SupabaseError):
    """HTTP 401 — the token was missing, malformed, or expired."""


class SupabasePermissionError(SupabaseError):
    """HTTP 403 — a row-level security policy refused the write."""


class SupabaseRequestError(SupabaseError):
    """Any other failure status."""


class SupabaseClient:
    def __init__(
        self,
        http: HttpClient,
        settings: SupabaseSettings,
        minter: AccessTokenMinter,
    ) -> None:
        if minter.user_id != settings.user_id:
            raise ValueError(
                "SupabaseClient settings.user_id and minter.user_id disagree "
                f"({settings.user_id!r} vs {minter.user_id!r}); a mismatch here "
                "would make reads silently return the minter's user's rows "
                "while writes fail closed on the RLS policy's WITH CHECK"
            )
        self._http = http
        self._settings = settings
        self._minter = minter

    def select(
        self, table: str, *, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Read rows, following PostgREST's row cap to completion.

        PostgREST caps a response at max-rows (1000 locally) and gives no
        signal that it truncated, so a caller reading a whole table would
        silently see a prefix. Page with Range headers until a short page
        arrives.

        A caller that passes its own ``limit`` means it, and gets one request.
        """
        query = dict(params or {})
        if "limit" in query:
            response = self._http.get(self._url(table), headers=self._headers(), params=query)
            return self._parse(response)

        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            headers = self._headers()
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{offset}-{offset + _PAGE_SIZE - 1}"
            page = self._parse(
                self._http.get(self._url(table), headers=headers, params=query)
            )
            collected.extend(page)
            if len(page) < _PAGE_SIZE:
                return collected
            offset += _PAGE_SIZE

    def insert(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        response = self._http.post(
            self._url(table), headers=self._headers(write=True), json=rows
        )
        return self._parse(response)

    def upsert(
        self, table: str, rows: list[dict[str, Any]], *, on_conflict: str
    ) -> list[dict[str, Any]]:
        """Insert rows, updating any that collide on ``on_conflict``.

        ``on_conflict`` is a comma-separated column list naming a unique
        constraint. Every write on the hot path goes through here rather than
        ``insert``: HttpClient retries POST on 5xx, and an upsert makes that
        retry converge instead of duplicating the row.
        """
        if not on_conflict:
            raise ValueError("upsert requires on_conflict naming a unique constraint")
        response = self._http.post(
            self._url(table),
            headers=self._headers(
                write=True, prefer="resolution=merge-duplicates,return=representation"
            ),
            params={"on_conflict": on_conflict},
            json=rows,
        )
        return self._parse(response)

    def update(
        self, table: str, values: dict[str, Any], *, params: dict[str, str]
    ) -> list[dict[str, Any]]:
        self._require_filter(params)
        response = self._http.patch(
            self._url(table),
            headers=self._headers(write=True),
            params=params,
            json=values,
        )
        return self._parse(response)

    def delete(self, table: str, *, params: dict[str, str]) -> list[dict[str, Any]]:
        self._require_filter(params)
        response = self._http.delete(
            self._url(table), headers=self._headers(write=True), params=params
        )
        return self._parse(response)

    def _url(self, table: str) -> str:
        return f"{self._settings.url}/rest/v1/{table}"

    def _headers(self, *, write: bool = False, prefer: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._minter.token()}",
            "apikey": self._settings.publishable_key,
            "Accept": "application/json",
        }
        if write:
            headers["Content-Type"] = "application/json"
            headers["Prefer"] = prefer or "return=representation"
        return headers

    @staticmethod
    def _require_filter(params: dict[str, str]) -> None:
        """Refuse an unfiltered update or delete.

        PostgREST applies a filterless write to every row the caller can see.
        Row-level security limits that to the caller's own rows, which is still
        their entire dataset.
        """
        if not params:
            raise ValueError("update and delete require a filter in params")

    @staticmethod
    def _parse(response) -> list[dict[str, Any]]:
        if response.status_code == 401:
            raise SupabaseAuthError("Supabase rejected the access token (401)")
        if response.status_code == 403:
            raise SupabasePermissionError(
                "a row-level security policy refused the request (403)"
            )
        if response.status_code >= 400:
            raise SupabaseRequestError(
                f"Supabase request failed with {response.status_code}: {response.text}"
            )
        if response.status_code == 204 or not response.text:
            return []
        payload = response.json()
        return payload if isinstance(payload, list) else [payload]

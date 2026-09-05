from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .gmail_auth import AccessTokenProvider
from .gmail_models import GmailMessage


_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_PAGE_SIZE = 100


class _Response(Protocol):
    status_code: int

    def json(self) -> dict: ...

    def raise_for_status(self) -> None: ...


class _HttpClient(Protocol):
    def get(self, url: str, **kwargs) -> _Response: ...


@dataclass(frozen=True, slots=True)
class GmailPage:
    message_ids: list[str]
    next_page_token: str | None


@dataclass(frozen=True, slots=True)
class GmailHistoryPage:
    message_ids: list[str]
    history_id: str
    next_page_token: str | None


class GmailHistoryExpired(Exception):
    """Raised when Gmail no longer retains the requested history ID."""


class GmailClient:
    def __init__(self, http: _HttpClient, token_provider: AccessTokenProvider) -> None:
        self._http = http
        self._token_provider = token_provider

    def get_profile(self) -> tuple[str, str]:
        payload = self._get_json(f"{_GMAIL_API_BASE}/profile")
        return payload["emailAddress"], payload["historyId"]

    def list_message_ids(self, query: str, page_token: str | None = None) -> GmailPage:
        params = {"q": query, "maxResults": _PAGE_SIZE}
        if page_token is not None:
            params["pageToken"] = page_token
        payload = self._get_json(f"{_GMAIL_API_BASE}/messages", params=params)
        return GmailPage(
            message_ids=[message["id"] for message in payload.get("messages", [])],
            next_page_token=payload.get("nextPageToken"),
        )

    def list_history(
        self, start_history_id: str, page_token: str | None = None
    ) -> GmailHistoryPage:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": _PAGE_SIZE,
        }
        if page_token is not None:
            params["pageToken"] = page_token
        response = self._get(f"{_GMAIL_API_BASE}/history", params=params)
        if response.status_code == 404:
            raise GmailHistoryExpired()
        response.raise_for_status()
        payload = response.json()
        message_ids = list(
            dict.fromkeys(
                added["message"]["id"]
                for history in payload.get("history", [])
                for added in history.get("messagesAdded", [])
            )
        )
        return GmailHistoryPage(
            message_ids=message_ids,
            history_id=payload["historyId"],
            next_page_token=payload.get("nextPageToken"),
        )

    def get_message(self, message_id: str) -> GmailMessage:
        payload = self._get_json(
            f"{_GMAIL_API_BASE}/messages/{message_id}", params={"format": "full"}
        )
        message_payload = payload["payload"]
        headers = {
            header["name"].lower(): header["value"]
            for header in message_payload.get("headers", [])
        }
        plain_parts: list[str] = []
        html_parts: list[str] = []
        self._collect_inline_text(message_payload, plain_parts, html_parts)

        links: list[str] = []
        for html in html_parts:
            for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
                href = anchor["href"]
                parsed_href = urlparse(href)
                if (
                    parsed_href.scheme in {"http", "https"}
                    and parsed_href.netloc
                    and href not in links
                ):
                    links.append(href)

        body = "\n".join(plain_parts)
        if not body and html_parts:
            body = "\n".join(
                BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
                for html in html_parts
            )
        if not body:
            body = payload.get("snippet", "")

        return GmailMessage(
            message_id=payload["id"],
            thread_id=payload.get("threadId"),
            sender=headers.get("from", ""),
            subject=headers.get("subject", ""),
            sent_at=datetime.fromtimestamp(int(payload["internalDate"]) / 1000, tz=UTC),
            snippet=payload.get("snippet", ""),
            body=body,
            links=links,
        )

    def _get_json(self, url: str, *, params: dict | None = None) -> dict:
        response = self._get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _get(self, url: str, *, params: dict | None = None) -> _Response:
        return self._http.get(
            url,
            headers={"Authorization": f"Bearer {self._token_provider.get_access_token()}"},
            params=params,
        )

    def _collect_inline_text(
        self, part: dict[str, Any], plain_parts: list[str], html_parts: list[str]
    ) -> None:
        mime_type = part.get("mimeType")
        data = part.get("body", {}).get("data")
        if mime_type in {"text/plain", "text/html"} and data:
            text = _decode_body_data(data)
            if mime_type == "text/plain":
                plain_parts.append(text)
            else:
                html_parts.append(text)
        for child in part.get("parts", []):
            self._collect_inline_text(child, plain_parts, html_parts)


def _decode_body_data(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")

import base64
from datetime import UTC, datetime

import pytest

from job_hunter.gmail_client import GmailClient, GmailHistoryExpired


class FakeTokenProvider:
    def __init__(self, token: str) -> None:
        self.token = token

    def get_access_token(self) -> str:
        return self.token


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.raise_for_status_called = False

    def json(self) -> dict:
        return self.payload

    def raise_for_status(self) -> None:
        self.raise_for_status_called = True
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, payload: dict) -> None:
        self.response = FakeResponse(payload)
        self.last_url = ""
        self.last_params = None
        self.last_headers = None

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.last_url = url
        self.last_params = kwargs.get("params")
        self.last_headers = kwargs.get("headers")
        return self.response


class Fake404Http:
    def get(self, url: str, **kwargs) -> FakeResponse:
        return FakeResponse({}, status_code=404)


class MessageHttp(FakeHttp):
    pass


def b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message_payload(payload: dict, *, snippet: str = "preview") -> dict:
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1756656000000",
        "snippet": snippet,
        "payload": {
            "headers": [
                {"name": "from", "value": "Alerts <alerts@example.com>"},
                {"name": "SUBJECT", "value": "New role"},
            ],
            **payload,
        },
    }


def test_get_profile_returns_email_address_and_history_id():
    http = FakeHttp({"emailAddress": "candidate@example.com", "historyId": "99"})
    client = GmailClient(http=http, token_provider=FakeTokenProvider("token"))

    assert client.get_profile() == ("candidate@example.com", "99")
    assert http.last_url == "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    assert http.last_headers == {"Authorization": "Bearer token"}


def test_list_message_ids_returns_ids_and_page_token():
    http = FakeHttp({"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"})
    client = GmailClient(http=http, token_provider=FakeTokenProvider("token"))

    page = client.list_message_ids("after:2025/08/31")

    assert page.message_ids == ["m1", "m2"]
    assert page.next_page_token == "p2"
    assert http.last_params == {"q": "after:2025/08/31", "maxResults": 100}
    assert http.last_headers["Authorization"] == "Bearer token"


def test_list_message_ids_sends_page_token():
    http = FakeHttp({"messages": []})
    client = GmailClient(http=http, token_provider=FakeTokenProvider("token"))

    client.list_message_ids("label:inbox", page_token="p2")

    assert http.last_params == {"q": "label:inbox", "maxResults": 100, "pageToken": "p2"}


def test_list_history_returns_deduplicated_message_ids_and_page_token():
    http = FakeHttp(
        {
            "history": [
                {"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]},
                {"messagesAdded": [{"message": {"id": "m1"}}]},
            ],
            "historyId": "102",
            "nextPageToken": "p2",
        }
    )
    client = GmailClient(http=http, token_provider=FakeTokenProvider("token"))

    page = client.list_history("100", page_token="p1")

    assert page.message_ids == ["m1", "m2"]
    assert page.history_id == "102"
    assert page.next_page_token == "p2"
    assert http.last_params == {
        "startHistoryId": "100",
        "historyTypes": "messageAdded",
        "maxResults": 100,
        "pageToken": "p1",
    }


def test_history_404_raises_history_expired():
    client = GmailClient(http=Fake404Http(), token_provider=FakeTokenProvider("token"))

    with pytest.raises(GmailHistoryExpired):
        client.list_history("123")


def test_decode_prefers_plain_text_over_html():
    http = MessageHttp(
        message_payload(
            {
                "parts": [
                    {"mimeType": "text/html", "body": {"data": b64url("<p>HTML role</p>")}},
                    {"mimeType": "text/plain", "body": {"data": b64url("Plain role")}},
                ]
            }
        )
    )

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.body == "Plain role"
    assert message.sender == "Alerts <alerts@example.com>"
    assert message.subject == "New role"
    assert message.sent_at == datetime(2025, 8, 31, 16, tzinfo=UTC)


def test_decode_falls_back_to_stripped_html():
    http = MessageHttp(
        message_payload({"mimeType": "text/html", "body": {"data": b64url("<p>HTML <b>role</b></p>")}})
    )

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.body == "HTML role"


def test_decode_recurses_nested_multipart():
    http = MessageHttp(
        message_payload(
            {
                "mimeType": "multipart/mixed",
                "parts": [
                    {
                        "mimeType": "multipart/alternative",
                        "parts": [{"mimeType": "text/plain", "body": {"data": b64url("Nested role")}}],
                    }
                ],
            }
        )
    )

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.body == "Nested role"


def test_decode_uses_snippet_when_body_empty():
    http = MessageHttp(message_payload({"mimeType": "multipart/mixed", "parts": []}, snippet="Job alert preview"))

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.body == "Job alert preview"


def test_html_anchor_hrefs_are_preserved_in_message_links():
    html = '<a href="https://jobs.example.com/a">Apply</a><a href="/relative">Ignore</a><a href="https://jobs.example.com/a">Again</a>'
    http = MessageHttp(message_payload({"mimeType": "text/html", "body": {"data": b64url(html)}}))

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.links == ["https://jobs.example.com/a"]


def test_html_anchor_scheme_only_href_is_not_preserved_in_message_links():
    http = MessageHttp(
        message_payload({"mimeType": "text/html", "body": {"data": b64url('<a href="https:foo">Ignore</a>')}})
    )

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.links == []


def test_binary_attachment_is_not_loaded():
    http = MessageHttp(
        message_payload(
            {
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": b64url("Inline text")}},
                    {"mimeType": "application/pdf", "filename": "role.pdf", "body": {"attachmentId": "a1"}},
                ]
            }
        )
    )

    message = GmailClient(http, FakeTokenProvider("token")).get_message("m1")

    assert message.body == "Inline text"
    assert http.last_url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/m1"
    assert http.last_params == {"format": "full"}

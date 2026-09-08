import logging

import pytest

from job_hunter.models import (
    GeminiQuotaSettings,
    ProviderCredentials,
    SearchPolicy,
    Settings,
)
from job_hunter.postgres_store import PostgresJobStore


class FakeRuntimeConfigClient:
    """Return controlled runtime rows and record the exact store boundary calls."""

    def __init__(self, *, credential_rows=None, document_rows=None):
        self.credential_rows = [] if credential_rows is None else credential_rows
        self.document_rows = [] if document_rows is None else document_rows
        self.calls = []

    def rpc(self, function):
        self.calls.append(("rpc", function))
        return self.credential_rows

    def select(self, table, *, params):
        self.calls.append(("select", table, params))
        return self.document_rows


def test_runtime_models_do_not_reveal_secrets_in_representations():
    credentials = ProviderCredentials(
        gemini_api_key="gemini-secret",
        brave_search_api_key="brave-secret",
    )
    settings = Settings(
        gemini_api_key="gemini-secret",
        candidate_profile="private-cv",
        cover_letter_template="private-cover-letter",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=SearchPolicy([], [], [], 90_000, {}),
        gemini_quota=GeminiQuotaSettings(rpm=10, tpm=250_000, rpd=500),
        brave_search_api_key="brave-secret",
        telegram_bot_token="telegram-secret",
        telegram_chat_id="private-chat-id",
    )

    combined_repr = repr(credentials) + repr(settings)

    for secret in (
        "gemini-secret",
        "brave-secret",
        "private-cv",
        "private-cover-letter",
        "telegram-secret",
        "private-chat-id",
    ):
        assert secret not in combined_repr


def test_get_provider_credentials_maps_runner_rpc_rows():
    client = FakeRuntimeConfigClient(
        credential_rows=[
            {"provider": "gemini", "secret": "gemini-value"},
            {"provider": "brave", "secret": "brave-value"},
        ]
    )

    credentials = PostgresJobStore(client).get_provider_credentials()

    assert credentials == ProviderCredentials(
        gemini_api_key="gemini-value",
        brave_search_api_key="brave-value",
    )
    assert client.calls == [("rpc", "job_hunter_get_provider_credentials")]


def test_get_provider_credentials_allows_missing_optional_brave():
    client = FakeRuntimeConfigClient(
        credential_rows=[{"provider": "gemini", "secret": "gemini-value"}]
    )

    credentials = PostgresJobStore(client).get_provider_credentials()

    assert credentials == ProviderCredentials(gemini_api_key="gemini-value")


@pytest.mark.parametrize(
    "rows",
    [
        [{"provider": "unknown", "secret": "private-value"}],
        [
            {"provider": "gemini", "secret": "private-value"},
            {"provider": "gemini", "secret": "second-private-value"},
        ],
        [{"provider": None, "secret": "private-value"}],
        [{"provider": "gemini", "secret": None}],
        [{"provider": "gemini", "secret": "   "}],
        ["not-a-row"],
    ],
)
def test_get_provider_credentials_rejects_unknown_or_duplicate_provider_rows(
    rows, caplog
):
    client = FakeRuntimeConfigClient(credential_rows=rows)

    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as excinfo:
        PostgresJobStore(client).get_provider_credentials()

    assert "private-value" not in str(excinfo.value)
    assert "private-value" not in caplog.text


def test_get_source_documents_returns_latest_cv_and_cover_letter():
    client = FakeRuntimeConfigClient(
        document_rows=[
            {
                "kind": "cv",
                "content": "latest cv",
                "updated_at": "2026-09-08T12:00:00Z",
            },
            {
                "kind": "cover_letter",
                "content": "latest cover letter",
                "updated_at": "2026-09-08T11:00:00Z",
            },
            {
                "kind": "cv",
                "content": "older cv",
                "updated_at": "2026-09-07T12:00:00Z",
            },
        ]
    )

    documents = PostgresJobStore(client).get_source_documents()

    assert documents == {"cv": "latest cv", "cover_letter": "latest cover letter"}
    assert client.calls == [
        (
            "select",
            "source_documents",
            {"select": "kind,content,updated_at", "order": "updated_at.desc"},
        )
    ]


def test_get_source_documents_ignores_null_content_but_not_other_users_in_code():
    client = FakeRuntimeConfigClient(
        document_rows=[
            {
                "kind": "cv",
                "content": None,
                "updated_at": "2026-09-08T12:00:00Z",
            },
            {
                "kind": "other",
                "content": "ignored",
                "updated_at": "2026-09-08T11:00:00Z",
            },
            {
                "kind": "cv",
                "content": "usable cv",
                "updated_at": "2026-09-07T12:00:00Z",
            },
        ]
    )

    documents = PostgresJobStore(client).get_source_documents()

    assert documents == {"cv": "usable cv"}
    assert client.calls == [
        (
            "select",
            "source_documents",
            {"select": "kind,content,updated_at", "order": "updated_at.desc"},
        )
    ]

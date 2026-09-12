import logging

import pytest

from job_hunter.config import SupabaseSettings
from job_hunter.models import (
    AIQuotaSettings,
    ProviderCredentials,
    SearchPolicy,
    Settings,
)
from job_hunter.postgres_store import PostgresJobStore
from job_hunter.supabase_client import SupabaseClient, SupabaseRequestError


_UNSET = object()


class FakeRuntimeConfigClient:
    """Return controlled runtime rows and record the exact store boundary calls."""

    def __init__(self, *, credential_rows=_UNSET, document_rows=_UNSET):
        self.credential_rows = [] if credential_rows is _UNSET else credential_rows
        self.document_rows = [] if document_rows is _UNSET else document_rows
        self.calls = []

    def rpc(self, function):
        self.calls.append(("rpc", function))
        return self.credential_rows

    def select(self, table, *, params):
        self.calls.append(("select", table, params))
        return self.document_rows


class ErrorResponse:
    def __init__(self, body):
        self.status_code = 500
        self.text = body


class ErrorHttp:
    def __init__(self, body):
        self.response = ErrorResponse(body)

    def get(self, *args, **kwargs):
        return self.response

    def post(self, *args, **kwargs):
        return self.response


class FakeMinter:
    user_id = "aaaaaaaa-0000-0000-0000-000000000001"

    def token(self):
        return "test-token"


def _store_with_failed_http_response(body):
    settings = SupabaseSettings(
        user_id=FakeMinter.user_id,
        url="https://example.supabase.co",
        publishable_key="publishable-key",
        signing_key_jwk={},
    )
    return PostgresJobStore(SupabaseClient(ErrorHttp(body), settings, FakeMinter()))


def test_runtime_models_do_not_reveal_secrets_in_representations():
    credentials = ProviderCredentials(
        gemini_api_key="gemini-secret",
        brave_search_api_key="brave-secret",
    )
    settings = Settings(
        ai_api_key="gemini-secret",
        candidate_profile="private-cv",
        cover_letter_template="private-cover-letter",
        timezone="Europe/Berlin",
        scheduled_hour=9,
        policy=SearchPolicy([], [], [], 90_000, {}),
        ai_quota=AIQuotaSettings(rpm=10, tpm=250_000, rpd=500),
        brave_search_api_key="brave-secret",
    )

    combined_repr = repr(credentials) + repr(settings)

    for secret in (
        "gemini-secret",
        "brave-secret",
        "private-cv",
        "private-cover-letter",
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


@pytest.mark.parametrize(
    "rows",
    [
        None,
        {"kind": "cv", "content": "private-document-value"},
        [None],
        [
            {
                "kind": [],
                "content": "private-document-value",
                "updated_at": "2026-09-08T12:00:00Z",
            }
        ],
        [
            {
                "kind": "other",
                "content": "private-document-value",
                "updated_at": "2026-09-08T12:00:00Z",
            }
        ],
        [
            {
                "kind": "cv",
                "content": 123,
                "updated_at": "2026-09-08T12:00:00Z",
            }
        ],
        [
            {
                "kind": "cv",
                "content": "private-document-value",
                "updated_at": "2026-09-08T12:00:00Z",
            },
            {
                "kind": "cv",
                "content": "second-private-document-value",
                "updated_at": "2026-09-07T12:00:00Z",
            },
        ],
    ],
)
def test_get_source_documents_rejects_malformed_material_without_values(
    rows, caplog
):
    client = FakeRuntimeConfigClient(document_rows=rows)

    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError) as excinfo:
        PostgresJobStore(client).get_source_documents()

    assert "private-document-value" not in str(excinfo.value)
    assert "private-document-value" not in repr(excinfo.value)
    assert "private-document-value" not in caplog.text


def test_get_source_documents_ignores_null_content_but_not_other_users_in_code():
    client = FakeRuntimeConfigClient(
        document_rows=[
            {
                "kind": "cv",
                "content": None,
                "updated_at": "2026-09-08T12:00:00Z",
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


@pytest.mark.parametrize(
    ("reader", "sensitive_body"),
    [
        ("get_provider_credentials", "response contains provider-secret-sentinel"),
        ("get_source_documents", "response contains document-text-sentinel"),
    ],
)
def test_sensitive_runtime_reads_sanitize_supabase_response_failures(
    reader, sensitive_body
):
    store = _store_with_failed_http_response(sensitive_body)

    with pytest.raises(SupabaseRequestError) as excinfo:
        getattr(store, reader)()

    assert sensitive_body not in str(excinfo.value)
    assert sensitive_body not in repr(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None

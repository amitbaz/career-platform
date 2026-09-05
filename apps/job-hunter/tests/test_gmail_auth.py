from unittest.mock import Mock, patch

from job_hunter.gmail_models import GmailSettings
from job_hunter.gmail_auth import GoogleOAuthTokenProvider
from job_hunter.models import GeminiQuotaSettings


def test_token_provider_refreshes_expired_credentials_and_reuses_token():
    credentials = Mock(expired=True, valid=False, token="fresh-token")
    request = Mock()
    credentials_class = Mock(return_value=credentials)
    settings = GmailSettings(
        "client",
        "secret",
        "refresh",
        "gemini",
        GeminiQuotaSettings(rpm=10, tpm=250000, rpd=500),
    )

    with (
        patch("job_hunter.gmail_auth.Credentials", credentials_class),
        patch("job_hunter.gmail_auth.Request", return_value=request),
    ):
        provider = GoogleOAuthTokenProvider(settings)
        assert provider.get_access_token() == "fresh-token"
        credentials.expired = False
        credentials.valid = True
        assert provider.get_access_token() == "fresh-token"

    credentials_class.assert_called_once_with(
        token=None,
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client",
        client_secret="secret",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    credentials.refresh.assert_called_once_with(request)

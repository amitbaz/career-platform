from typing import Protocol

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .gmail_models import GMAIL_READONLY_SCOPE, GmailSettings


class AccessTokenProvider(Protocol):
    def get_access_token(self) -> str: ...


class GoogleOAuthTokenProvider:
    def __init__(self, settings: GmailSettings) -> None:
        self._credentials = Credentials(
            token=None,
            refresh_token=settings.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            scopes=[GMAIL_READONLY_SCOPE],
        )

    def get_access_token(self) -> str:
        if not self._credentials.valid or self._credentials.expired:
            self._credentials.refresh(Request())
        return self._credentials.token

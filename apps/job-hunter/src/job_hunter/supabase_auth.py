"""Minting short-lived access tokens for one Supabase user.

Job Hunter is a batch process: there is no end-user session to inherit, so it
signs its own token naming the user a run acts for. Postgres row-level security
reads the ``sub`` claim through ``auth.uid()`` and only returns that user's rows,
so the token is the whole basis of data isolation — see
docs/superpowers/specs/2026-09-06-job-hunter-per-user-jwt-design.md.

Nothing here is ever logged: both the private key and the minted token grant
access to the user's data.
"""

from __future__ import annotations

import json
import time

import jwt
from jwt.algorithms import ECAlgorithm

_LIFETIME_SECONDS = 300
_REFRESH_MARGIN_SECONDS = 60


class AccessTokenMinter:
    """Issues ES256 access tokens for a single user.

    The token is cached and reused until it is within
    ``_REFRESH_MARGIN_SECONDS`` of expiry, so a long run re-mints a handful of
    times rather than holding one token valid for its whole duration.
    """

    def __init__(self, user_id: str, signing_key_jwk: dict) -> None:
        if not signing_key_jwk.get("d"):
            raise ValueError("signing key JWK has no private component ('d')")
        self._user_id = user_id
        self._kid = signing_key_jwk["kid"]
        self._key = ECAlgorithm.from_jwk(json.dumps(signing_key_jwk))
        self._token: str | None = None
        self._expires_at = 0.0

    @property
    def user_id(self) -> str:
        """The ``sub`` claim every token minted by this instance carries.

        Read-only so a caller can verify which user a minter acts for
        without being able to repoint it after construction.
        """
        return self._user_id

    def token(self) -> str:
        """Return a currently-valid access token, minting one if needed."""
        now = time.time()
        if self._token is None or self._expires_at - now <= _REFRESH_MARGIN_SECONDS:
            self._mint(now)
        assert self._token is not None
        return self._token

    def _mint(self, now: float) -> None:
        expires_at = int(now) + _LIFETIME_SECONDS
        self._token = jwt.encode(
            {
                "sub": self._user_id,
                "role": "authenticated",
                "exp": expires_at,
            },
            self._key,
            algorithm="ES256",
            headers={"kid": self._kid},
        )
        self._expires_at = float(expires_at)

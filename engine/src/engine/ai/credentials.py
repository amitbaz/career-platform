"""Where the port gets a credential from, and for which call class.

The port never reads a key from the environment, from `Settings`, or from a
module-level constant: it asks a `CredentialResolver` for the credential
belonging to a call class. That is the seam issue #72 replaces with per-user
encrypted storage without touching a single core module, and the seam #128
uses to fund shared extraction from a platform-owned key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from engine.ai.port import CallClass


@dataclass(frozen=True, slots=True)
class Credential:
    """One provider secret.

    `repr=False` is the same promise `SupabaseSettings.signing_key_jwk` makes:
    a stray `logger.info(credential)` or a `pytest --showlocals` traceback
    cannot print the key.
    """

    secret: str = field(repr=False)


class CredentialUnavailable(RuntimeError):
    """No credential exists for this call class, and none may be substituted."""


class CredentialResolver(Protocol):
    """Resolve the credential that funds a call of the given class."""

    def resolve(self, call_class: CallClass) -> Credential: ...


class EnvCredentialResolver:
    """The single implementation: one key per call class, and no crossing over.

    Both keys arrive as they do today -- the user's read at startup by
    `config.py` from the per-user credential store, the platform's from the
    deployment's own environment, because it belongs to no user -- and are held
    here rather than in the adapter so that swapping either source (issue #72)
    is a change to one class.

    The two branches never meet. A `SHARED_EXTRACTION` call is served the
    platform credential or it is served nothing: when no platform key is
    configured it fails closed, because charging shared extraction to one
    person's quota is the failure mode the call class exists to make
    impossible, and it would do so invisibly. There is deliberately no setting
    that changes this -- a configurable fallback is the same bug with a switch
    in front of it.
    """

    def __init__(self, user_api_key: str, platform_api_key: str | None = None) -> None:
        self._user_api_key = user_api_key
        self._platform_api_key = platform_api_key

    def resolve(self, call_class: CallClass) -> Credential:
        if call_class is CallClass.SHARED_EXTRACTION:
            if not self._platform_api_key:
                raise CredentialUnavailable(
                    "no platform credential is configured for shared extraction; "
                    "a user credential is never served for it (see issue #128)"
                )
            return Credential(self._platform_api_key)
        return Credential(self._user_api_key)

"""Where the port gets a credential from, and for which call class.

The port never reads a key from the environment, from `Settings`, or from a
module-level constant: it asks a `CredentialResolver` for the credential
belonging to a call class. That is the seam issue #72 replaces with per-user
encrypted storage without touching a single core module, and the seam #128
extends with the platform-owned key that funds shared extraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from job_hunter.ai.port import CallClass


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
    """The single implementation: the user key this run was configured with.

    The key arrives exactly as it does today -- read at startup by `config.py`
    from the per-user credential store -- and is held here rather than in the
    adapter so that swapping its source (issue #72) is a change to one class.

    `SHARED_EXTRACTION` is refused unconditionally. It is not "unsupported
    yet" in the sense of a missing branch: the user key is never a fallback for
    platform-funded work, because charging shared extraction to one person's
    quota is the failure mode the call class exists to make impossible. #128
    adds a platform credential here; until it does, an extraction-class call
    fails closed.
    """

    def __init__(self, user_api_key: str) -> None:
        self._user_api_key = user_api_key

    def resolve(self, call_class: CallClass) -> Credential:
        if call_class is CallClass.SHARED_EXTRACTION:
            raise CredentialUnavailable(
                "no platform credential is configured for shared extraction; "
                "a user credential is never served for it (see issue #128)"
            )
        return Credential(self._user_api_key)

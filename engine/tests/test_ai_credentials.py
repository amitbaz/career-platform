import pytest

from engine.ai import (
    CallClass,
    CredentialUnavailable,
    EnvCredentialResolver,
)


def test_user_subjective_calls_get_the_user_key():
    resolver = EnvCredentialResolver("user-key")

    assert resolver.resolve(CallClass.USER_SUBJECTIVE).secret == "user-key"


def test_shared_extraction_never_receives_the_user_key():
    """With no platform key, extraction has no credential at all (#128).

    The refusal is unconditional: a user key being present, valid and unused is
    exactly the situation in which a fallback would be tempting.
    """
    resolver = EnvCredentialResolver("user-key")

    with pytest.raises(CredentialUnavailable) as excinfo:
        resolver.resolve(CallClass.SHARED_EXTRACTION)

    assert "user-key" not in str(excinfo.value)


def test_a_credential_never_reaches_a_log_line_through_repr():
    assert "user-key" not in repr(EnvCredentialResolver("user-key").resolve(
        CallClass.USER_SUBJECTIVE
    ))


def test_the_platform_credential_funds_shared_extraction():
    resolver = EnvCredentialResolver("user-key", "platform-key")

    assert resolver.resolve(CallClass.SHARED_EXTRACTION).secret == "platform-key"
    assert resolver.resolve(CallClass.USER_SUBJECTIVE).secret == "user-key"


def test_an_empty_platform_credential_is_no_credential():
    """An unset environment variable arrives as "", not as None.

    Treating that as a configured key would send an empty `x-goog-api-key` and
    turn a missing platform key into a provider error per posting, instead of
    the deliberate stop it is.
    """
    resolver = EnvCredentialResolver("user-key", "")

    with pytest.raises(CredentialUnavailable):
        resolver.resolve(CallClass.SHARED_EXTRACTION)

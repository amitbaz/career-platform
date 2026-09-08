import pytest

from job_hunter.ai import (
    CallClass,
    CredentialUnavailable,
    EnvCredentialResolver,
)


def test_user_subjective_calls_get_the_user_key():
    resolver = EnvCredentialResolver("user-key")

    assert resolver.resolve(CallClass.USER_SUBJECTIVE).secret == "user-key"


def test_shared_extraction_never_receives_the_user_key():
    """#128 owns the platform key; until it lands, extraction has no credential.

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

import json
import logging

import jwt
import pytest

from engine.supabase_auth import AccessTokenMinter

USER_A = "aaaaaaaa-0000-0000-0000-000000000001"

PRIVATE_JWK = {
    "kty": "EC",
    "kid": "11111111-2222-3333-4444-555555555555",
    "alg": "ES256",
    "crv": "P-256",
    "d": "ROsbtI7IzXA9aF9O60sCUheqjrmenjRbZYYirWO9Kn8",
    "x": "S-EfNzQOiAhLH7jdkWWUXeMtt2GEqDI-GdTuK7RWNUA",
    "y": "IgKTms8k072_kvlmjOsDIdIUrA9WYtLuHlLyd_6OxHc",
}


def _public_key():
    public = {k: v for k, v in PRIVATE_JWK.items() if k != "d"}
    return jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(public))


def test_token_carries_the_expected_claims():
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    token = minter.token()
    claims = jwt.decode(token, _public_key(), algorithms=["ES256"])

    assert claims["sub"] == USER_A
    assert claims["role"] == "authenticated"
    assert claims["job_hunter_runner"] is True
    assert set(claims) == {"sub", "role", "job_hunter_runner", "exp"}


def test_token_header_names_the_key():
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    header = jwt.get_unverified_header(minter.token())

    assert header["alg"] == "ES256"
    assert header["kid"] == PRIVATE_JWK["kid"]
    assert header["typ"] == "JWT"


def test_token_expires_five_minutes_out(monkeypatch):
    monkeypatch.setattr("engine.supabase_auth.time.time", lambda: 1_000_000.0)
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    claims = jwt.decode(
        minter.token(), _public_key(), algorithms=["ES256"], options={"verify_exp": False}
    )

    assert claims["exp"] == 1_000_300


def test_a_fresh_token_is_reused(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("engine.supabase_auth.time.time", lambda: clock["now"])
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    first = minter.token()
    clock["now"] += 100
    assert minter.token() == first


def test_an_expiring_token_is_reminted(monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr("engine.supabase_auth.time.time", lambda: clock["now"])
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    first = minter.token()
    clock["now"] += 250  # 50 seconds left, inside the 60 second margin
    second = minter.token()

    assert second != first
    claims = jwt.decode(
        second, _public_key(), algorithms=["ES256"], options={"verify_exp": False}
    )
    assert claims["exp"] == 1_000_550


def test_nothing_secret_reaches_the_logs(caplog):
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    with caplog.at_level(logging.DEBUG):
        token = minter.token()

    assert token not in caplog.text
    assert PRIVATE_JWK["d"] not in caplog.text


def test_user_id_property_exposes_the_subject():
    minter = AccessTokenMinter(USER_A, PRIVATE_JWK)

    assert minter.user_id == USER_A


def test_a_key_without_a_private_half_is_rejected():
    public_only = {k: v for k, v in PRIVATE_JWK.items() if k != "d"}

    with pytest.raises(ValueError, match="private"):
        AccessTokenMinter(USER_A, public_only)

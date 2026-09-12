import base64
import json

import pytest

from engine.config import load_supabase_settings

VALID_JWK = {
    "kty": "EC",
    "kid": "11111111-2222-3333-4444-555555555555",
    "alg": "ES256",
    "crv": "P-256",
    "d": "ROsbtI7IzXA9aF9O60sCUheqjrmenjRbZYYirWO9Kn8",
    "x": "S-EfNzQOiAhLH7jdkWWUXeMtt2GEqDI-GdTuK7RWNUA",
    "y": "IgKTms8k072_kvlmjOsDIdIUrA9WYtLuHlLyd_6OxHc",
}


def _encode(jwk: dict) -> str:
    return base64.b64encode(json.dumps(jwk).encode("utf-8")).decode("ascii")


def _set_all(monkeypatch, **overrides) -> None:
    values = {
        "JOB_HUNTER_USER_ID": "aaaaaaaa-0000-0000-0000-000000000001",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_example",
        "SUPABASE_SIGNING_KEY_B64": _encode(VALID_JWK),
    }
    values.update(overrides)
    for name, value in values.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_loads_all_settings(monkeypatch):
    _set_all(monkeypatch)

    settings = load_supabase_settings()

    assert settings.user_id == "aaaaaaaa-0000-0000-0000-000000000001"
    assert settings.url == "https://example.supabase.co"
    assert settings.publishable_key == "sb_publishable_example"
    assert settings.signing_key_jwk == VALID_JWK


def test_strips_trailing_slash_from_url(monkeypatch):
    _set_all(monkeypatch, SUPABASE_URL="https://example.supabase.co/")

    assert load_supabase_settings().url == "https://example.supabase.co"


def test_missing_variable_raises(monkeypatch):
    _set_all(monkeypatch, SUPABASE_URL=None)

    with pytest.raises(ValueError, match="SUPABASE_URL"):
        load_supabase_settings()


def test_non_uuid_user_id_raises(monkeypatch):
    _set_all(monkeypatch, JOB_HUNTER_USER_ID="not-a-uuid")

    with pytest.raises(ValueError, match="JOB_HUNTER_USER_ID must be a UUID"):
        load_supabase_settings()


def test_malformed_signing_key_raises_without_echoing_it(monkeypatch):
    secret = base64.b64encode(b"not json at all").decode("ascii")
    _set_all(monkeypatch, SUPABASE_SIGNING_KEY_B64=secret)

    with pytest.raises(ValueError) as excinfo:
        load_supabase_settings()

    assert "SUPABASE_SIGNING_KEY_B64" in str(excinfo.value)
    assert secret not in str(excinfo.value)
    assert "not json" not in str(excinfo.value)


def test_signing_key_without_kid_raises(monkeypatch):
    incomplete = {key: value for key, value in VALID_JWK.items() if key != "kid"}
    _set_all(monkeypatch, SUPABASE_SIGNING_KEY_B64=_encode(incomplete))

    with pytest.raises(ValueError, match="kid"):
        load_supabase_settings()


def test_repr_never_prints_the_private_key(monkeypatch):
    _set_all(monkeypatch)

    settings = load_supabase_settings()

    assert VALID_JWK["d"] not in repr(settings)
    assert "signing_key_jwk" not in repr(settings)

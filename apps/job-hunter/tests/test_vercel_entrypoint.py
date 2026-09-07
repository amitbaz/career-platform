import base64
import importlib
import json
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask


def _b64url_uint(value: int, length: int) -> str:
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


def _generate_test_jwk() -> dict:
    """Build a throwaway ES256 keypair at import time.

    The webhook now reads Telegram navigation sessions straight from
    Postgres (issue #70 task 14b), so importing `main` builds a
    `SupabaseClient` and mints a token with a signing key. Generating it
    here (rather than committing a real-shaped private JWK literal) means
    no key-shaped secret lives in the repository -- this key is fresh per
    test run, never reused, and never touches a real request.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.private_numbers()
    public_numbers = numbers.public_numbers
    return {
        "kty": "EC",
        "kid": "11111111-2222-3333-4444-555555555555",
        "alg": "ES256",
        "crv": "P-256",
        "d": _b64url_uint(numbers.private_value, 32),
        "x": _b64url_uint(public_numbers.x, 32),
        "y": _b64url_uint(public_numbers.y, 32),
    }


_TEST_JWK = _generate_test_jwk()


def test_vercel_entrypoint_exposes_flask_app(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", "amitbaz/job-hunter-bot")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "test-dispatch-token")
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "aaaaaaaa-0000-0000-0000-000000000001")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_example")
    monkeypatch.setenv(
        "SUPABASE_SIGNING_KEY_B64",
        base64.b64encode(json.dumps(_TEST_JWK).encode("utf-8")).decode("ascii"),
    )

    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    assert isinstance(main.app, Flask)

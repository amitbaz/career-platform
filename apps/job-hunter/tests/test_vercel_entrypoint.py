import base64
import importlib
import json
import sys

from flask import Flask

# The webhook now reads Telegram navigation sessions straight from Postgres
# (issue #70 task 14b), so importing `main` builds a `SupabaseClient` and
# mints a token with this key. It is a throwaway ES256 keypair used only to
# let construction succeed offline -- no request is made at import time.
_TEST_JWK = {
    "kty": "EC",
    "kid": "11111111-2222-3333-4444-555555555555",
    "alg": "ES256",
    "crv": "P-256",
    "d": "ROsbtI7IzXA9aF9O60sCUheqjrmenjRbZYYirWO9Kn8",
    "x": "S-EfNzQOiAhLH7jdkWWUXeMtt2GEqDI-GdTuK7RWNUA",
    "y": "IgKTms8k072_kvlmjOsDIdIUrA9WYtLuHlLyd_6OxHc",
}


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

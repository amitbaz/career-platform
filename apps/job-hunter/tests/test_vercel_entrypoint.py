import importlib
import sys

from flask import Flask


def test_vercel_entrypoint_exposes_flask_app(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", "amitbaz/job-hunter-bot")
    monkeypatch.setenv("GITHUB_STATE_TOKEN", "test-github-token")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "test-dispatch-token")

    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    assert isinstance(main.app, Flask)

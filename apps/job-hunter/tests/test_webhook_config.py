import pytest

from job_hunter.config import load_webhook_settings


def test_load_webhook_settings_requires_only_webhook_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("GITHUB_REPOSITORY", "amitbaz/job-hunter-bot")
    monkeypatch.setenv("GITHUB_STATE_TOKEN", "github-token")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "dispatch-token")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("CANDIDATE_PROFILE_B64", raising=False)
    monkeypatch.delenv("COVER_LETTER_TEMPLATE_B64", raising=False)

    settings = load_webhook_settings()

    assert settings.telegram_bot_token == "bot-token"
    assert settings.telegram_webhook_secret == "webhook-secret"
    assert settings.github_repository == "amitbaz/job-hunter-bot"
    assert settings.github_state_token == "github-token"
    assert settings.github_dispatch_token == "dispatch-token"
    assert settings.github_state_artifact_name == "job-hunter-state"
    assert settings.github_state_cache_dir == "/tmp/job-hunter-state"


def test_load_webhook_settings_requires_webhook_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")
    monkeypatch.delenv("TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "amitbaz/job-hunter-bot")
    monkeypatch.setenv("GITHUB_STATE_TOKEN", "github-token")

    with pytest.raises(ValueError):
        load_webhook_settings()

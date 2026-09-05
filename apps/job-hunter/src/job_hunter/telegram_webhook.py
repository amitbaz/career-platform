from __future__ import annotations

import hmac
import logging

from flask import Flask, jsonify, request

from job_hunter.config import WebhookSettings, load_webhook_settings
from job_hunter.github_dispatch import trigger_repository_dispatch
from job_hunter.github_state import GitHubArtifactStateLoader
from job_hunter.http import HttpClient
from job_hunter.navigation_repository import GitHubArtifactNavigationRepository
from job_hunter.telegram import TelegramClient
from job_hunter.telegram_navigation import handle_callback_query, parse_callback

logger = logging.getLogger(__name__)


def create_app(
    *,
    settings: WebhookSettings | None = None,
    navigation_repository=None,
    telegram=None,
) -> Flask:
    settings = settings or load_webhook_settings()
    http = HttpClient()
    if navigation_repository is None:
        state_loader = GitHubArtifactStateLoader(
            settings.github_repository,
            settings.github_state_token,
            settings.github_state_artifact_name,
            settings.github_state_cache_dir,
        )
        navigation_repository = GitHubArtifactNavigationRepository(state_loader)
    telegram = telegram or TelegramClient(settings.telegram_bot_token, None, http)

    def _trigger_cover_letter_generation(job_id: int) -> None:
        try:
            trigger_repository_dispatch(
                settings.github_repository,
                settings.github_dispatch_token,
                "generate_cover_letter",
                {"job_id": job_id},
                http=http,
            )
        except Exception:
            logger.exception("failed to trigger cover letter generation for job_id=%s", job_id)

    app = Flask(__name__)

    @app.get("/health")
    def health():
        return jsonify(ok=True)

    @app.post("/telegram/webhook")
    def telegram_webhook():
        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied_secret, settings.telegram_webhook_secret):
            return jsonify(ok=False), 403

        update = request.get_json(silent=True)
        if not isinstance(update, dict):
            return jsonify(ok=False), 400

        callback_query = update.get("callback_query")
        if not isinstance(callback_query, dict):
            return jsonify(ok=True)

        parsed = parse_callback(str(callback_query.get("data") or ""))
        if parsed is None or parsed[0] in {"a", "x"}:
            handle_callback_query(
                callback_query,
                session_loader=lambda _session_id: None,
                telegram=telegram,
            )
            return jsonify(ok=True)

        session_id = parsed[1]
        callback_id = str(callback_query.get("id") or "")
        try:
            session = navigation_repository.get_session(session_id)
        except Exception:
            logger.exception("failed to load Telegram navigation session")
            if callback_id:
                telegram.answer_callback(
                    callback_id,
                    text="Could not load this job list right now.",
                )
            return jsonify(ok=True)

        if session is None:
            if callback_id:
                telegram.answer_callback(
                    callback_id,
                    text="Job list is still syncing. Try again shortly.",
                )
            return jsonify(ok=True)

        handle_callback_query(
            callback_query,
            session_loader=lambda requested_id: session if requested_id == session_id else None,
            telegram=telegram,
            on_generate=_trigger_cover_letter_generation,
        )
        return jsonify(ok=True)

    return app

from __future__ import annotations

import hmac
import logging
import os

from flask import Flask, jsonify, request

from job_hunter.config import WebhookSettings, load_supabase_settings, load_webhook_settings
from job_hunter.engine_lab_web import register_engine_lab_routes
from job_hunter.github_dispatch import trigger_repository_dispatch
from job_hunter.http import HttpClient
from job_hunter.navigation_repository import PostgresNavigationRepository
from job_hunter.postgres_store import DryRunStore, PostgresJobStore
from job_hunter.supabase_auth import AccessTokenMinter
from job_hunter.supabase_client import SupabaseClient
from job_hunter.telegram import TelegramClient
from job_hunter.telegram_navigation import handle_callback_query, parse_callback

logger = logging.getLogger(__name__)

# Env vars `load_webhook_settings()` and `load_supabase_settings()` each
# require. `/health` reports exactly which of these are absent instead of
# letting the WSGI module fail to import (see `create_app`'s docstring).
_WEBHOOK_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GITHUB_REPOSITORY",
    "GITHUB_DISPATCH_TOKEN",
)
_SUPABASE_ENV_VARS = (
    "JOB_HUNTER_USER_ID",
    "SUPABASE_URL",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_SIGNING_KEY_B64",
)


def _build_supabase_client(http: HttpClient) -> SupabaseClient:
    supabase_settings = load_supabase_settings()
    return SupabaseClient(
        http,
        supabase_settings,
        AccessTokenMinter(supabase_settings.user_id, supabase_settings.signing_key_jwk),
    )


def _default_navigation_store_factory(http: HttpClient) -> DryRunStore:
    """Builds the webhook's navigation store, lazily, on first real use.

    `PostgresNavigationRepository` calls this at most once -- the first
    time a callback actually needs a session, never at import or
    `create_app()` time. That laziness is what lets `/health` load and
    respond even when `SUPABASE_*`/`JOB_HUNTER_USER_ID` env vars are
    missing; a real callback still fails immediately once this runs.

    The webhook is an internet-facing process that would otherwise hold a
    live, per-user read/write token (RLS scopes it to the owner, so this
    is not cross-user exposure -- the risk is self-corruption from a
    future webhook edit). Wrapping in `DryRunStore` restores the old
    SQLite snapshot's read-only guarantee: reads reach Postgres live,
    writes are discarded.
    """
    return DryRunStore(PostgresJobStore(_build_supabase_client(http)))


def create_app(
    *,
    settings: WebhookSettings | None = None,
    navigation_repository=None,
    telegram=None,
) -> Flask:
    """Build the Telegram webhook Flask app.

    Importing this module (via `main.py`) must succeed even when every
    Supabase/webhook env var is missing -- `/health`'s entire purpose is
    to report that misconfiguration, so it cannot depend on module import
    having already crashed. `settings`, `telegram`, and the default
    `navigation_repository` are therefore all resolved lazily (and cached
    after first success) rather than being built here.
    """
    http = HttpClient()
    _settings_box: dict[str, WebhookSettings] = {}
    _telegram_box: dict[str, TelegramClient] = {}

    def _resolve_settings() -> WebhookSettings:
        if settings is not None:
            return settings
        if "value" not in _settings_box:
            _settings_box["value"] = load_webhook_settings()
        return _settings_box["value"]

    def _resolve_telegram() -> TelegramClient:
        if telegram is not None:
            return telegram
        if "value" not in _telegram_box:
            _telegram_box["value"] = TelegramClient(
                _resolve_settings().telegram_bot_token, None, http
            )
        return _telegram_box["value"]

    repository = navigation_repository
    if repository is None:
        repository = PostgresNavigationRepository(lambda: _default_navigation_store_factory(http))

    def _missing_env_vars() -> list[str]:
        # Only report env vars for pieces this app instance would actually
        # build from them -- injected `settings`/`navigation_repository`
        # (as tests do) are already configured and don't need their env
        # vars present.
        missing: list[str] = []
        if settings is None:
            missing.extend(name for name in _WEBHOOK_ENV_VARS if not os.environ.get(name))
        if navigation_repository is None:
            missing.extend(name for name in _SUPABASE_ENV_VARS if not os.environ.get(name))
        return missing

    def _trigger_cover_letter_generation(job_id: str) -> None:
        try:
            current_settings = _resolve_settings()
            trigger_repository_dispatch(
                current_settings.github_repository,
                current_settings.github_dispatch_token,
                "generate_cover_letter",
                {"job_id": job_id},
                http=http,
            )
        except Exception:
            logger.exception("failed to trigger cover letter generation for job_id=%s", job_id)

    app = Flask(__name__)

    @app.get("/health")
    def health():
        missing = _missing_env_vars()
        if missing:
            return jsonify(ok=False, missing=missing), 503
        return jsonify(ok=True)

    @app.post("/telegram/webhook")
    def telegram_webhook():
        # Resolving settings here (rather than at create_app time) is what
        # makes misconfiguration fail on the first real callback instead
        # of at import: an unhandled exception here is the correct,
        # fail-fast behaviour for a webhook that cannot be served.
        current_settings = _resolve_settings()
        current_telegram = _resolve_telegram()

        supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(supplied_secret, current_settings.telegram_webhook_secret):
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
                telegram=current_telegram,
            )
            return jsonify(ok=True)

        session_id = parsed[1]
        callback_id = str(callback_query.get("id") or "")
        try:
            session = repository.get_session(session_id)
        except Exception:
            logger.exception("failed to load Telegram navigation session")
            if callback_id:
                current_telegram.answer_callback(
                    callback_id,
                    text="Could not load this job list right now.",
                )
            return jsonify(ok=True)

        if session is None:
            if callback_id:
                current_telegram.answer_callback(
                    callback_id,
                    text="Job list is still syncing. Try again shortly.",
                )
            return jsonify(ok=True)

        handle_callback_query(
            callback_query,
            session_loader=lambda requested_id: session if requested_id == session_id else None,
            telegram=current_telegram,
            on_generate=_trigger_cover_letter_generation,
        )
        return jsonify(ok=True)

    register_engine_lab_routes(app, http)

    return app

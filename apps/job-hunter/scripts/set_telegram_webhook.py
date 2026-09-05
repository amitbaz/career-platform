from __future__ import annotations

import argparse
import os

import requests

_BASE_URL = "https://api.telegram.org/bot{token}"


def set_webhook(
    bot_token: str,
    webhook_url: str,
    secret_token: str,
    *,
    http=None,
) -> dict:
    if not webhook_url.startswith("https://"):
        raise ValueError("Telegram webhook URL must use https://")
    if not bot_token or not secret_token:
        raise ValueError("bot token and webhook secret are required")

    http = http or requests
    response = http.post(
        f"{_BASE_URL.format(token=bot_token)}/setWebhook",
        json={
            "url": webhook_url,
            "secret_token": secret_token,
            "allowed_updates": ["callback_query"],
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "Telegram rejected webhook registration")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="set_telegram_webhook")
    parser.add_argument(
        "--url",
        required=True,
        help="Public HTTPS endpoint, e.g. https://example.com/telegram/webhook",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    secret_token = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not bot_token or not secret_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET must be set")

    set_webhook(bot_token, args.url, secret_token)
    print("Telegram webhook registered successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

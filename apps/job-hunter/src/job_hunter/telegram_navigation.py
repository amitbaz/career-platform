from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Protocol

from job_hunter.models import DigestItem, NavigationCard, NavigationSession

_CALLBACK_ACTIONS = {"n", "a", "x", "c"}


class InteractiveTelegram(Protocol):
    def edit_job_card(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        keyboard: list[list[dict[str, str]]],
    ) -> bool: ...

    def answer_callback(
        self,
        callback_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool: ...


def encode_callback(action: str, session_id: str, index: int) -> str:
    if action not in _CALLBACK_ACTIONS or index < 0 or not session_id:
        raise ValueError("invalid callback payload")
    payload = f"{action}|{session_id}|{index}"
    if len(payload.encode("utf-8")) > 64:
        raise ValueError("callback payload exceeds Telegram limit")
    return payload


def parse_callback(data: str) -> tuple[str, str, int] | None:
    parts = data.split("|")
    if len(parts) != 3 or parts[0] not in _CALLBACK_ACTIONS or not parts[1]:
        return None
    try:
        index = int(parts[2])
    except ValueError:
        return None
    if index < 0:
        return None
    return parts[0], parts[1], index


def navigation_sort_key(item: DigestItem) -> tuple[int, str, str, int]:
    return (-item.score, (item.company or "").lower(), (item.title or "").lower(), item.job_id)


def build_navigation_card(
    card: NavigationCard,
    session_id: str,
    index: int,
    total: int,
) -> tuple[str, list[list[dict[str, str]]]]:
    if total <= 0 or index < 0 or index >= total:
        raise ValueError("invalid navigation position")

    note_line = f"\nNote: {card.market_note}" if card.market_note else ""
    text = (
        f"{card.title}\n\n"
        f"Company: {card.company or 'Not specified'}\n"
        f"Location: {card.location or 'Not specified'}\n"
        f"Match: {card.score}%"
        f"{note_line}"
    )

    actions: list[dict[str, str]] = []
    if card.url:
        actions.append({"text": "View job", "url": card.url})
    actions.append({"text": "Apply", "callback_data": encode_callback("a", session_id, index)})
    actions.append({"text": "Gen CL", "callback_data": encode_callback("c", session_id, index)})

    previous_index = index - 1 if index > 0 else index
    next_index = index + 1 if index < total - 1 else index
    previous_action = "n" if index > 0 else "x"
    next_action = "n" if index < total - 1 else "x"

    navigation = [
        {
            "text": "◀ Previous",
            "callback_data": encode_callback(previous_action, session_id, previous_index),
        },
        {
            "text": f"{index + 1} / {total}",
            "callback_data": encode_callback("x", session_id, index),
        },
        {
            "text": "Next ▶",
            "callback_data": encode_callback(next_action, session_id, next_index),
        },
    ]
    return text, [actions, navigation]


def _is_expired(session: NavigationSession) -> bool:
    try:
        expires = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= datetime.now(timezone.utc)


def handle_callback_query(
    callback_query: dict,
    *,
    session_loader: Callable[[str], NavigationSession | None],
    telegram: InteractiveTelegram,
    on_generate: Callable[[int], None] | None = None,
) -> bool:
    callback_id = str(callback_query.get("id") or "")
    parsed = parse_callback(str(callback_query.get("data") or ""))

    def answer(text: str | None = None) -> None:
        if callback_id:
            telegram.answer_callback(callback_id, text=text)

    if parsed is None:
        answer("This action is no longer available.")
        return False

    action, session_id, target_index = parsed
    if action == "a":
        answer("Apply functionality coming soon.")
        return True
    if action == "x":
        answer()
        return True

    session = session_loader(session_id)
    if session is None or _is_expired(session):
        answer("This job list has expired.")
        return False

    if target_index >= len(session.cards):
        answer("This action is no longer available.")
        return False

    message = callback_query.get("message") or {}
    chat = message.get("chat") or {}
    message_id = message.get("message_id")
    chat_id = chat.get("id")
    if message_id is None or chat_id is None:
        answer("This action is no longer available.")
        return False

    if session.telegram_message_id is not None and str(message_id) != str(session.telegram_message_id):
        answer("This action is no longer available.")
        return False

    if action == "c":
        if on_generate is not None:
            on_generate(session.cards[target_index].job_id)
        answer("Generating your cover letter - I'll send it here shortly.")
        return True

    text, keyboard = build_navigation_card(
        session.cards[target_index],
        session.session_id,
        target_index,
        len(session.cards),
    )
    edited = telegram.edit_job_card(
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        keyboard=keyboard,
    )
    if not edited:
        answer("Could not update this job right now.")
        return False

    answer()
    return True

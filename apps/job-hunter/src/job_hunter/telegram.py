from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from job_hunter.models import DigestItem, GeminiUsageSummary, ReviewItem

if TYPE_CHECKING:
    from job_hunter.http import HttpClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.telegram.org/bot{token}"
_CAPTION_LIMIT = 1000

_GROUP_HEADERS = {
    "high_priority": "Ready to apply",
    "package_match": "Ready to apply",
    "possible_match": "Possible matches",
    "blocked": "Needs review / blockers",
}
_GROUP_ORDER = ("Ready to apply", "Possible matches", "Needs review / blockers")
_DELIVERABLE_DECISIONS = frozenset(_GROUP_HEADERS)
_DELIVERABLE_SCORE_FLOOR = 60
_REVIEW_HEADER = "Gmail activity I couldn't link"
_REVIEW_EVENT_LABELS = {
    "RECRUITER_CONTACT": "Recruiter contact",
    "APPLIED": "Application update",
    "INTERVIEW": "Interview",
    "TECHNICAL": "Technical assessment",
    "OFFER": "Job offer",
    "REJECTED": "Rejection",
    "REVIEW_NEEDED": "Job-related activity",
}
_REVIEW_EXPLANATIONS = {
    "RECRUITER_CONTACT": "A recruiter contacted you, but I couldn't link this email to a tracked job.",
    "APPLIED": "This looks like an application confirmation, but I couldn't link it to a tracked job.",
    "INTERVIEW": "This looks like an interview email, but I couldn't link it to a tracked job.",
    "TECHNICAL": "This looks like a technical assessment, but I couldn't link it to a tracked job.",
    "OFFER": "This looks like a job offer, but I couldn't link it to a tracked job.",
    "REJECTED": "This looks like a rejection email, but I couldn't link it to a tracked job.",
}
_REVIEW_FALLBACK_EXPLANATION = (
    "This looks job-related, but I couldn't classify or link it confidently."
)


def select_deliverable_items(items: Sequence[DigestItem]) -> list[DigestItem]:
    selected = []
    for item in items:
        if item.score <= _DELIVERABLE_SCORE_FLOOR:
            continue
        if item.decision == "skip":
            continue
        if item.decision not in _DELIVERABLE_DECISIONS:
            logger.warning("omitting unknown Telegram decision=%s job_id=%s", item.decision, item.job_id)
            continue
        selected.append(item)
    return selected


def _item_sort_key(item: DigestItem) -> tuple[int, str, str, int]:
    return (-item.score, (item.company or "").lower(), (item.title or "").lower(), item.job_id)


def _digest_line(item: DigestItem) -> str:
    line = f"- {item.score} | {item.company} - {item.title}"
    if item.url:
        line += f" | {item.url}"
    if item.hard_blockers:
        line += f" | blockers: {', '.join(item.hard_blockers)}"
    return line


def build_digest(items: Sequence[DigestItem]) -> str:
    grouped: dict[str, list[DigestItem]] = {header: [] for header in _GROUP_ORDER}
    for item in select_deliverable_items(items):
        header = _GROUP_HEADERS[item.decision]
        grouped[header].append(item)

    sections = []
    for header in _GROUP_ORDER:
        group_items = grouped[header]
        if not group_items:
            continue
        sections.append("\n".join([header, *(_digest_line(item) for item in sorted(group_items, key=_item_sort_key))]))

    return "\n\n".join(sections) if sections else "No matching jobs today."


def _gmail_review_title(item: ReviewItem) -> str:
    if item.company and item.role_title:
        return f"{item.company} — {item.role_title}"
    if item.company:
        label = _REVIEW_EVENT_LABELS.get(item.event_type, "Job-related activity")
        return f"{item.company} — {label}"
    return item.subject or "Gmail message"


def _gmail_review_explanation(item: ReviewItem) -> str:
    return _REVIEW_EXPLANATIONS.get(item.event_type, _REVIEW_FALLBACK_EXPLANATION)


def _gmail_review_block(item: ReviewItem) -> str:
    gmail_id = item.source_thread_id or item.source_message_id
    gmail_url = f"https://mail.google.com/mail/u/0/#all/{gmail_id}"
    return "\n".join(
        [
            _gmail_review_title(item),
            _gmail_review_explanation(item),
            f"Open email: {gmail_url}",
        ]
    )


def _fit_gmail_review_block(item: ReviewItem, max_length: int) -> str:
    block = _gmail_review_block(item)
    if len(block) <= max_length:
        return block

    title, explanation, link = block.split("\n", 2)
    suffix = f"{explanation}\n{link}"
    available_title = max_length - len(suffix) - 1
    if available_title <= 1:
        return block[: max(1, max_length - 1)].rstrip() + "…"
    if len(title) > available_title:
        title = title[: available_title - 1].rstrip() + "…"
    return f"{title}\n{suffix}"


def build_gmail_review_digest(items: Sequence[ReviewItem]) -> str:
    """Format unresolved Gmail activity without exposing classifier diagnostics."""
    ordered = sorted(items, key=lambda item: (item.occurred_at, item.event_id))
    blocks = [_gmail_review_block(item) for item in ordered]
    return "\n\n".join([_REVIEW_HEADER, *blocks])


def build_gmail_review_digest_chunks(
    items: Sequence[ReviewItem], limit: int = 3900
) -> list[tuple[str, list[int]]]:
    """Chunk Gmail activity while keeping each event with its explanation and link."""
    ordered = sorted(items, key=lambda item: (item.occurred_at, item.event_id))
    chunks: list[tuple[str, list[int]]] = []
    current_blocks: list[str] = []
    current_ids: list[int] = []
    max_block_length = max(1, limit - len(_REVIEW_HEADER) - 2)

    for item in ordered:
        block = _fit_gmail_review_block(item, max_block_length)
        candidate = "\n\n".join([_REVIEW_HEADER, *current_blocks, block])
        if current_blocks and len(candidate) > limit:
            chunks.append(
                ("\n\n".join([_REVIEW_HEADER, *current_blocks]), current_ids)
            )
            current_blocks = [block]
            current_ids = [item.event_id]
        else:
            current_blocks.append(block)
            current_ids.append(item.event_id)

    if current_blocks:
        chunks.append(("\n\n".join([_REVIEW_HEADER, *current_blocks]), current_ids))
    return chunks


def chunk_message(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(line[:limit])
            line = line[limit:]

        line_len = len(line) + (1 if current else 0)
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
            line_len = len(line)

        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))

    return chunks


def build_gemini_pause_warning(summary: GeminiUsageSummary) -> str | None:
    """A single warning line when work was deferred this run, else `None`.

    A persisted provider pause and our own internal ceiling are distinct
    causes worth telling the operator apart; at most one of these two
    messages is ever returned since the caller sends it once per run.
    """
    if summary.provider_paused:
        return (
            "⚠️ Gemini hit its free-tier provider limit and is paused — "
            "remaining work has been deferred to a later run."
        )
    if summary.internal_budget_exhausted:
        return (
            "⚠️ Gemini's daily budget ceiling was reached — "
            "remaining work has been deferred to a later run."
        )
    return None


def _truncate_caption(caption: str, limit: int = _CAPTION_LIMIT) -> str:
    if len(caption) <= limit:
        return caption
    return caption[: limit - 1].rstrip() + "…"


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str | None, http: "HttpClient") -> None:
        self._chat_id = chat_id
        self._http = http
        self._base_url = _BASE_URL.format(token=bot_token)

    def _configured_chat_id(self) -> str | None:
        if not self._chat_id:
            logger.warning("telegram operation requires a configured chat id")
            return None
        return self._chat_id

    def send_message(self, text: str) -> str | None:
        chat_id = self._configured_chat_id()
        if chat_id is None:
            return None
        last_message_id = None
        for chunk in chunk_message(text):
            response = self._http.post(
                f"{self._base_url}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
            if response.status_code >= 400:
                logger.warning("telegram sendMessage failed: %s", response.text)
                return None
            last_message_id = response.json().get("result", {}).get("message_id")

        return str(last_message_id) if last_message_id is not None else None

    def send_job_card(self, text: str, keyboard: list[list[dict[str, str]]]) -> str | None:
        chat_id = self._configured_chat_id()
        if chat_id is None:
            return None
        response = self._http.post(
            f"{self._base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        if response.status_code >= 400:
            logger.warning("telegram navigator sendMessage failed: %s", response.text)
            return None
        message_id = response.json().get("result", {}).get("message_id")
        return str(message_id) if message_id is not None else None

    def edit_job_card(
        self,
        *,
        chat_id: str | int,
        message_id: str | int,
        text: str,
        keyboard: list[list[dict[str, str]]],
    ) -> bool:
        response = self._http.post(
            f"{self._base_url}/editMessageText",
            json={
                "chat_id": str(chat_id),
                "message_id": message_id,
                "text": text,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
        if response.status_code >= 400:
            logger.warning("telegram editMessageText failed: %s", response.text)
            return False
        return True

    def answer_callback(
        self,
        callback_id: str,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        payload: dict[str, object] = {
            "callback_query_id": callback_id,
            "show_alert": show_alert,
        }
        if text is not None:
            payload["text"] = text
        response = self._http.post(
            f"{self._base_url}/answerCallbackQuery",
            json=payload,
        )
        if response.status_code >= 400:
            logger.warning("telegram answerCallbackQuery failed: %s", response.text)
            return False
        return True

    def send_document(self, path: Path, caption: str) -> str | None:
        chat_id = self._configured_chat_id()
        if chat_id is None:
            return None
        path = Path(path)
        caption = _truncate_caption(caption)

        content = path.read_bytes()
        response = self._http.post(
            f"{self._base_url}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (path.name, content)},
        )

        if response.status_code >= 400:
            logger.warning("telegram sendDocument failed: %s", response.text)
            return None

        message_id = response.json().get("result", {}).get("message_id")
        return str(message_id) if message_id is not None else None

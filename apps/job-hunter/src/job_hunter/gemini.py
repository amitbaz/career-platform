from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

import requests

from job_hunter.gemini_usage import GeminiQuotaPaused, GeminiTemporaryCapacity

if TYPE_CHECKING:
    from job_hunter.gemini_usage import GeminiPauseKind, GeminiPurpose, GeminiUsageTracker
    from job_hunter.http import HttpClient

logger = logging.getLogger(__name__)

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0


class GeminiError(RuntimeError):
    pass


class GeminiIncompleteResponse(GeminiError):
    """Gemini stopped generation before completing the requested response."""

    def __init__(self, finish_reason: str) -> None:
        super().__init__(f"Gemini response incomplete: finish_reason={finish_reason}")
        self.finish_reason = finish_reason


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _classify_429(response: requests.Response) -> tuple[GeminiPauseKind, str | None]:
    """Classify a Gemini 429 body into one of the design spec's three pause kinds."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return "unknown", None

    error = body.get("error")
    if not isinstance(error, dict):
        return "unknown", None

    tokens: list[str] = [str(error.get("status", "")), str(error.get("message", ""))]
    error_code = error.get("status") or None
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        reason = detail.get("reason")
        if reason:
            tokens.append(str(reason))
            error_code = error_code or reason
        for violation in detail.get("violations") or []:
            if not isinstance(violation, dict):
                continue
            quota_id = violation.get("quotaId") or violation.get("quotaMetric")
            if quota_id:
                tokens.append(str(quota_id))

    haystack = " ".join(tokens).lower()
    if any(marker in haystack for marker in ("quota_exceeded", "perday", "per_day")):
        return "daily_quota", error_code
    if any(
        marker in haystack
        for marker in ("rate_limit_exceeded", "too_many_requests", "perminute", "per_minute")
    ):
        return "rate_limit", error_code
    return "unknown", error_code


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        http: HttpClient,
        tracker: GeminiUsageTracker | None = None,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._http = http
        self._tracker = tracker
        self._sleep_fn = sleep_fn

    def _preflight_with_pacing(
        self,
        purpose: GeminiPurpose | None,
        prompt: str,
    ) -> datetime:
        now = _now()
        if self._tracker is None:
            return now

        try:
            self._tracker.preflight(purpose, prompt, now)
        except GeminiTemporaryCapacity as exc:
            logger.info(
                "Gemini rolling capacity full; waiting %.2fs before retry: purpose=%s",
                exc.retry_after_seconds,
                purpose,
            )
            self._sleep_fn(exc.retry_after_seconds)
            now = _now()
            self._tracker.preflight(purpose, prompt, now)
        return now

    def generate_text(
        self,
        prompt: str,
        *,
        purpose: GeminiPurpose | None = None,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
        json_schema: dict | None = None,
        max_attempts: int = 1,
    ) -> str:
        """Call Gemini, optionally retrying transient failures.

        `max_attempts` bounds retries for HTTP 5xx responses and network
        timeouts only (`_RETRYABLE_STATUS_CODES` / `requests.Timeout`) —
        the failures a production run showed to be safe to retry. Every
        attempt re-runs preflight pacing and is recorded to the tracker, so
        usage accounting reflects retries exactly like fresh calls. 429s,
        other 4xx, and malformed response bodies never consume retry budget:
        they are permanent or already handled by the quota pause path.
        """
        url = f"{_BASE_URL}/{self.model}:generateContent"
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        generation_config: dict[str, Any] = {}
        if thinking_level is not None:
            generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level}
        if max_output_tokens is not None:
            generation_config["maxOutputTokens"] = max_output_tokens
        if json_mode or json_schema is not None:
            generation_config["responseMimeType"] = "application/json"
            if json_schema is not None:
                generation_config["responseSchema"] = json_schema
        if generation_config:
            payload["generationConfig"] = generation_config

        attempt = 0
        while True:
            attempt += 1
            now = self._preflight_with_pacing(purpose, prompt)

            try:
                response = self._http.post(
                    url,
                    json=payload,
                    headers=headers,
                    retry_status_codes=_RETRYABLE_STATUS_CODES,
                    retry=False,
                )
            except requests.RequestException as exc:
                if self._tracker is not None:
                    self._tracker.record_error(
                        purpose,
                        prompt,
                        now,
                        error_code=type(exc).__name__,
                    )
                if isinstance(exc, requests.Timeout) and attempt < max_attempts:
                    logger.warning(
                        "Gemini %s timed out (attempt %s/%s); retrying: purpose=%s",
                        self.model,
                        attempt,
                        max_attempts,
                        purpose,
                    )
                    self._sleep_fn(_TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                raise

            if response.status_code == 429:
                kind, error_code = _classify_429(response)
                if self._tracker is not None:
                    paused_until, reason = self._tracker.record_429(
                        purpose, prompt, now, kind=kind, error_code=error_code
                    )
                    raise GeminiQuotaPaused(
                        f"Gemini {self.model} is paused until {paused_until} ({reason})",
                        paused_until=paused_until,
                        reason=reason,
                    )
                raise GeminiError(f"Gemini API error 429: {response.text}")

            if response.status_code >= 400:
                if self._tracker is not None:
                    self._tracker.record_error(
                        purpose, prompt, now, http_status=response.status_code
                    )
                if response.status_code in _RETRYABLE_STATUS_CODES and attempt < max_attempts:
                    logger.warning(
                        "Gemini %s returned %s (attempt %s/%s); retrying: purpose=%s",
                        self.model,
                        response.status_code,
                        attempt,
                        max_attempts,
                        purpose,
                    )
                    self._sleep_fn(_TRANSIENT_RETRY_DELAY_SECONDS)
                    continue
                raise GeminiError(f"Gemini API error {response.status_code}: {response.text}")

            break

        try:
            data = response.json()
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeminiError("Gemini response missing content") from exc

        if not text:
            raise GeminiError("Gemini response missing content")

        if self._tracker is not None:
            usage = data.get("usageMetadata") if isinstance(data, dict) else None
            if usage:
                self._tracker.record_success(
                    purpose,
                    prompt,
                    now,
                    prompt_tokens=usage.get("promptTokenCount"),
                    output_tokens=usage.get("candidatesTokenCount"),
                    thinking_tokens=usage.get("thoughtsTokenCount"),
                    cached_tokens=usage.get("cachedContentTokenCount"),
                    total_tokens=usage.get("totalTokenCount"),
                )
            else:
                logger.warning(
                    "Gemini response for purpose %r missing usageMetadata; "
                    "recording estimated input tokens only",
                    purpose,
                )
                self._tracker.record_success(purpose, prompt, now)

        finish_reason = candidate.get("finishReason") if isinstance(candidate, dict) else None
        if finish_reason == "MAX_TOKENS":
            raise GeminiIncompleteResponse(finish_reason)

        return text

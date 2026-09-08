"""The Gemini adapter: the only module that knows Gemini exists.

It owns three provider-specific jobs and nothing else: building Google's
request body, choosing the credential header, and translating Gemini's error
bodies into the port's errors -- `_classify_429` in particular, which maps a
429 body onto the port's three pause kinds. Everything above it (backoff,
budget ceilings, the persisted pause, the ledger) is provider-neutral and
lives in `job_hunter.ai.usage`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping

import requests

from job_hunter.ai.credentials import CredentialResolver, EnvCredentialResolver
from job_hunter.ai.port import (
    AIError,
    AIIncompleteResponse,
    AIPurpose,
    AIQuotaPaused,
    AITemporaryCapacity,
    CallClass,
    PauseKind,
    QuotaUnavailable,
)

if TYPE_CHECKING:
    from job_hunter.ai.usage import AIUsageTracker
    from job_hunter.http import HttpClient

logger = logging.getLogger(__name__)

#: The provider name written to every ledger and pause row this adapter makes.
PROVIDER = "gemini"

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _usage_tokens(usage: dict) -> dict[str, int | None]:
    """Map Google's `usageMetadata` names onto the tracker's token arguments."""
    return {
        "prompt_tokens": usage.get("promptTokenCount"),
        "output_tokens": usage.get("candidatesTokenCount"),
        "thinking_tokens": usage.get("thoughtsTokenCount"),
        "cached_tokens": usage.get("cachedContentTokenCount"),
        "total_tokens": usage.get("totalTokenCount"),
    }


def _finish_reason(data: object) -> str | None:
    """Read the first candidate's `finishReason`, tolerating any body shape.

    This runs before the body is known to be well-formed, so every missing or
    unexpected level yields `None` instead of raising.
    """
    if not isinstance(data, dict):
        return None
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return None
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return None
    reason = candidate.get("finishReason")
    return reason if isinstance(reason, str) else None


def _classify_429(response: requests.Response) -> tuple[PauseKind, str | None]:
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


class GeminiProvider:
    """An `AIProvider` backed by Google's Generative Language API.

    `credentials` and `trackers` are both keyed by call class, and both are
    consulted per call: the class decides which credential funds a call and
    which quota governs it. A class with no tracker is unaccounted, which is
    why the credential is resolved *first* -- a class with no credential never
    reaches a quota, a request, or a ledger row.
    """

    def __init__(
        self,
        model: str,
        http: HttpClient,
        credentials: CredentialResolver,
        trackers: Mapping[CallClass, AIUsageTracker] | None = None,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self._http = http
        self._credentials = credentials
        self._trackers = dict(trackers or {})
        self._sleep_fn = sleep_fn

    def _preflight_with_pacing(
        self,
        tracker: AIUsageTracker | None,
        purpose: AIPurpose | None,
        prompt: str,
    ) -> datetime:
        now = _now()
        if tracker is None:
            return now

        try:
            tracker.preflight(purpose, prompt, now)
        except AITemporaryCapacity as exc:
            logger.info(
                "Gemini rolling capacity full; waiting %.2fs before retry: purpose=%s",
                exc.retry_after_seconds,
                purpose,
            )
            self._sleep_fn(exc.retry_after_seconds)
            now = _now()
            tracker.preflight(purpose, prompt, now)
        return now

    def generate_text(
        self,
        prompt: str,
        *,
        call_class: CallClass,
        purpose: AIPurpose | None = None,
        thinking_level: str | None = None,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
        json_schema: dict | None = None,
        max_attempts: int = 1,
        read_timeout: float | None = None,
    ) -> str:
        """Call Gemini, optionally retrying transient failures.

        `read_timeout` overrides the HTTP client's default read budget for
        this call. Pass it when the generation is long enough that a slow
        reply means the model is still working rather than that something is
        broken -- a cover letter is the one such call today.

        `call_class` selects the credential and the quota, in that order: the
        credential is resolved before any pacing, request or ledger row, so a
        class with no credential of its own can never borrow another's -- not
        on the first attempt, not on a retry, and not when a quota is
        exhausted.

        `max_attempts` bounds retries for HTTP 5xx responses and network
        timeouts only (`_RETRYABLE_STATUS_CODES` / `requests.Timeout`) —
        the failures a production run showed to be safe to retry. Every
        attempt re-runs preflight pacing and is recorded to the tracker, so
        usage accounting reflects retries exactly like fresh calls. 429s,
        other 4xx, and malformed response bodies never consume retry budget:
        they are permanent or already handled by the quota pause path.
        """
        credential = self._credentials.resolve(call_class)
        tracker = self._trackers.get(call_class)
        if tracker is None and self._trackers:
            raise QuotaUnavailable(
                f"no quota is configured for call class {call_class.value!r}"
            )

        url = f"{_BASE_URL}/{self.model}:generateContent"
        headers = {
            "x-goog-api-key": credential.secret,
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
            now = self._preflight_with_pacing(tracker, purpose, prompt)

            try:
                post_kwargs: dict[str, Any] = {}
                if read_timeout is not None:
                    post_kwargs["timeout"] = self._http.timeout_for_read(read_timeout)
                response = self._http.post(
                    url,
                    json=payload,
                    headers=headers,
                    retry_status_codes=_RETRYABLE_STATUS_CODES,
                    retry=False,
                    **post_kwargs,
                )
            except requests.RequestException as exc:
                if tracker is not None:
                    tracker.record_error(
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
                if tracker is not None:
                    paused_until, reason = tracker.record_429(
                        purpose, prompt, now, kind=kind, error_code=error_code
                    )
                    raise AIQuotaPaused(
                        f"Gemini {self.model} is paused until {paused_until} ({reason})",
                        paused_until=paused_until,
                        reason=reason,
                    )
                raise AIError(f"Gemini API error 429: {response.text}")

            if response.status_code >= 400:
                if tracker is not None:
                    tracker.record_error(
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
                raise AIError(f"Gemini API error {response.status_code}: {response.text}")

            break

        # Usage is recorded only once the response body has been judged, so a
        # response the caller never gets to use is never logged as a success.
        # It is still logged: the call reached Google and consumed real quota.
        try:
            data = response.json()
        except ValueError as exc:
            self._record_response_failure(tracker, purpose, prompt, now, None, "invalid_json")
            raise AIError("Gemini response missing content") from exc

        usage = data.get("usageMetadata") if isinstance(data, dict) else None
        finish_reason = _finish_reason(data)
        # A truncated candidate can arrive with no text at all — thinking
        # tokens can consume the whole output budget. That still reaches the
        # caller as a missing-content failure, but the ledger names the
        # truncation rather than blaming a malformed body for it.
        no_content_code = finish_reason if finish_reason == "MAX_TOKENS" else "missing_content"

        try:
            candidate = data["candidates"][0]
            parts = candidate["content"]["parts"]
            text = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            self._record_response_failure(tracker, purpose, prompt, now, usage, no_content_code)
            raise AIError("Gemini response missing content") from exc

        if not text:
            self._record_response_failure(tracker, purpose, prompt, now, usage, no_content_code)
            raise AIError("Gemini response missing content")

        if finish_reason == "MAX_TOKENS":
            self._record_response_failure(tracker, purpose, prompt, now, usage, finish_reason)
            raise AIIncompleteResponse(
                "max_output_tokens", provider_finish_reason=finish_reason
            )

        if tracker is not None:
            if usage:
                tracker.record_success(purpose, prompt, now, **_usage_tokens(usage))
            else:
                logger.warning(
                    "Gemini response for purpose %r missing usageMetadata; "
                    "recording estimated input tokens only",
                    purpose,
                )
                tracker.record_success(purpose, prompt, now)

        return text

    def _record_response_failure(
        self,
        tracker: AIUsageTracker | None,
        purpose: AIPurpose | None,
        prompt: str,
        now: datetime,
        usage: dict | None,
        error_code: str,
    ) -> None:
        """Log a 200 response the caller cannot use as a failed attempt.

        The provider still billed the request, so any `usageMetadata` it did
        report is carried onto the error row rather than dropped.
        """
        if tracker is None:
            return
        tracker.record_error(
            purpose,
            prompt,
            now,
            error_code=error_code,
            **(_usage_tokens(usage) if usage else {}),
        )


def build_gemini_provider(
    api_key: str,
    model: str,
    http: HttpClient,
    *,
    tracker: AIUsageTracker | None = None,
    platform_api_key: str | None = None,
    platform_tracker: AIUsageTracker | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> GeminiProvider:
    """Wire one adapter with a credential and a ledger for each call class.

    `USER_SUBJECTIVE` gets the user's key and the user's ledger.
    `SHARED_EXTRACTION` gets the platform key and the platform ledger (#128),
    and gets nothing at all where no platform key is configured -- a
    deployment without one does no extraction rather than quietly doing it on
    somebody's key.

    The two are passed in pairs, and the pairing is enforced here rather than
    merely documented: a platform key with no platform ledger would spend a
    credential nobody is metering. A provider wired with *no* trackers at all
    stays legal -- that is the port's own exemption for a probe or a double --
    but a metered provider that meters only one of its two keys is a wiring
    mistake, and wiring is where it is visible.
    """
    if platform_api_key and platform_tracker is None and tracker is not None:
        raise ValueError(
            "a platform credential was supplied with no platform ledger to meter "
            "it; pass both or neither (see issue #128)"
        )
    trackers: dict[CallClass, AIUsageTracker] = {}
    if tracker is not None:
        trackers[CallClass.USER_SUBJECTIVE] = tracker
    if platform_tracker is not None:
        trackers[CallClass.SHARED_EXTRACTION] = platform_tracker
    return GeminiProvider(
        model,
        http,
        EnvCredentialResolver(api_key, platform_api_key),
        trackers,
        sleep_fn=sleep_fn,
    )

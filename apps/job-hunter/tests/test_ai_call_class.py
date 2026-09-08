"""The call class decides the credential and the quota -- on every branch.

These tests are the direct assertion issue #73 asks for: there must be no path
through the port by which a `SHARED_EXTRACTION` call obtains a user credential,
including the paths a fallback would be most tempting on (an exhausted budget,
an active provider pause).
"""

import pytest

from job_hunter.ai import CallClass, Credential, CredentialUnavailable, EnvCredentialResolver
from job_hunter.ai.gemini import GeminiProvider, build_gemini_provider
from job_hunter.ai.port import AIBudgetExceeded, AIQuotaPaused, QuotaUnavailable


class FakeResponse:
    status_code = 200

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": "hi"}]}}]}


class FakeHttp:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


class RecordingTracker:
    def __init__(self, preflight_error=None):
        self.preflight_calls = []
        self.success_calls = []
        self._preflight_error = preflight_error

    def preflight(self, purpose, prompt, now):
        self.preflight_calls.append(purpose)
        if self._preflight_error is not None:
            raise self._preflight_error

    def record_success(self, purpose, prompt, now, **kwargs):
        self.success_calls.append(purpose)

    def record_error(self, purpose, prompt, now, **kwargs):
        raise AssertionError("no error expected")

    def record_429(self, purpose, prompt, now, **kwargs):
        raise AssertionError("no 429 expected")


class LenientResolver:
    """A resolver that would hand the user key to anyone who asked.

    It exists to prove the *adapter* does not ask on behalf of a class it may
    not fund: with this resolver in place, any extraction-class call that
    reached a request would carry the user's key, so the assertions below fail
    loudly if the refusal ever moves out of the resolver.
    """

    def resolve(self, call_class):
        return Credential("user-key")


def test_a_user_subjective_call_carries_the_user_key():
    http = FakeHttp()
    provider = build_gemini_provider("user-key", "gemini-test", http)

    provider.generate_text("hi", call_class=CallClass.USER_SUBJECTIVE)

    assert http.calls[0][1]["headers"]["x-goog-api-key"] == "user-key"


def test_an_extraction_call_is_refused_before_any_request_is_made():
    http = FakeHttp()
    provider = build_gemini_provider("user-key", "gemini-test", http)

    with pytest.raises(CredentialUnavailable):
        provider.generate_text("hi", call_class=CallClass.SHARED_EXTRACTION)

    assert http.calls == []


@pytest.mark.parametrize(
    "exhaustion",
    [
        AIBudgetExceeded("daily budget exceeded"),
        AIQuotaPaused("paused", paused_until="2026-09-09T00:00:00+00:00", reason="daily_quota"),
    ],
    ids=["budget_exhausted", "provider_paused"],
)
def test_an_exhausted_user_quota_is_still_not_a_route_to_the_user_key(exhaustion):
    """The tempting branch: the user's quota is gone, so borrowing looks free."""
    http = FakeHttp()
    tracker = RecordingTracker(preflight_error=exhaustion)
    provider = build_gemini_provider("user-key", "gemini-test", http, tracker=tracker)

    with pytest.raises(CredentialUnavailable):
        provider.generate_text("hi", call_class=CallClass.SHARED_EXTRACTION)

    assert http.calls == []
    # The user's quota was never even consulted for work it does not fund.
    assert tracker.preflight_calls == []


def test_an_extraction_call_never_spends_the_user_quota_even_if_a_key_is_offered():
    """With a resolver that hands out the user key, the class still has no quota.

    The call fails rather than running unmetered: a class that can reach a
    credential but no quota is exactly the accounting hole the class exists to
    prevent.
    """
    http = FakeHttp()
    user_tracker = RecordingTracker()
    provider = GeminiProvider(
        "gemini-test",
        http,
        LenientResolver(),
        {CallClass.USER_SUBJECTIVE: user_tracker},
    )

    with pytest.raises(QuotaUnavailable):
        provider.generate_text(
            "hi", call_class=CallClass.SHARED_EXTRACTION, purpose="job_facets"
        )

    assert http.calls == []
    assert user_tracker.preflight_calls == []
    assert user_tracker.success_calls == []


def test_the_class_selects_the_quota_for_a_call_it_does_fund():
    http = FakeHttp()
    user_tracker = RecordingTracker()
    provider = GeminiProvider(
        "gemini-test",
        http,
        EnvCredentialResolver("user-key"),
        {CallClass.USER_SUBJECTIVE: user_tracker},
    )

    provider.generate_text(
        "hi", call_class=CallClass.USER_SUBJECTIVE, purpose="job_evaluation"
    )

    assert user_tracker.preflight_calls == ["job_evaluation"]
    assert user_tracker.success_calls == ["job_evaluation"]

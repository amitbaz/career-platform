"""The call class decides the credential and the quota -- on every branch.

These tests are the direct assertion issue #73 asks for: there must be no path
through the port by which a `SHARED_EXTRACTION` call obtains a user credential,
including the paths a fallback would be most tempting on (an exhausted budget,
an active provider pause).
"""

import pytest

from engine.ai import CallClass, Credential, CredentialUnavailable, EnvCredentialResolver
from engine.ai.gemini import GeminiProvider, build_gemini_provider
from engine.ai.port import AIBudgetExceeded, AIQuotaPaused, QuotaUnavailable


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


def test_an_extraction_call_carries_the_platform_key(monkeypatch):
    http = FakeHttp()
    provider = build_gemini_provider(
        "user-key", "gemini-test", http, platform_api_key="platform-key"
    )

    provider.generate_text("hi", call_class=CallClass.SHARED_EXTRACTION)

    assert http.calls[0][1]["headers"]["x-goog-api-key"] == "platform-key"


@pytest.mark.parametrize(
    "exhaustion",
    [
        AIBudgetExceeded("platform daily budget exceeded"),
        AIQuotaPaused("paused", paused_until="2026-09-09T00:00:00+00:00", reason="daily_quota"),
    ],
    ids=["platform_budget_exhausted", "platform_paused"],
)
def test_an_exhausted_platform_quota_is_not_a_route_to_the_user_key(exhaustion):
    """The branch #128 names as the one that must not exist.

    The platform key is spent, the user's is not, and the work is objective --
    every ingredient of a fallback that would be invisible to the person
    billed for it. What happens instead is that the refusal propagates and the
    user's ledger is never even consulted.
    """
    http = FakeHttp()
    user_tracker = RecordingTracker()
    platform_tracker = RecordingTracker(preflight_error=exhaustion)
    provider = build_gemini_provider(
        "user-key",
        "gemini-test",
        http,
        tracker=user_tracker,
        platform_api_key="platform-key",
        platform_tracker=platform_tracker,
    )

    with pytest.raises(type(exhaustion)):
        provider.generate_text(
            "hi", call_class=CallClass.SHARED_EXTRACTION, purpose="job_facets"
        )

    assert http.calls == []
    assert platform_tracker.preflight_calls == ["job_facets"]
    assert user_tracker.preflight_calls == []
    assert user_tracker.success_calls == []


def test_an_exhausted_user_quota_is_not_a_route_to_the_platform_key():
    """The mirror image, which is a different wrong and equally forbidden.

    A platform key funding a judgement about one person would spend a shared
    allowance on work only that person benefits from, and the class that
    selects the credential makes the substitution unreachable in that
    direction too.
    """
    http = FakeHttp()
    user_tracker = RecordingTracker(preflight_error=AIBudgetExceeded("user budget gone"))
    platform_tracker = RecordingTracker()
    provider = build_gemini_provider(
        "user-key",
        "gemini-test",
        http,
        tracker=user_tracker,
        platform_api_key="platform-key",
        platform_tracker=platform_tracker,
    )

    with pytest.raises(AIBudgetExceeded):
        provider.generate_text(
            "hi", call_class=CallClass.USER_SUBJECTIVE, purpose="job_evaluation"
        )

    assert http.calls == []
    assert platform_tracker.preflight_calls == []


def test_a_platform_key_with_no_platform_ledger_is_refused_at_wiring():
    """A credential nobody is metering is the accounting hole, not a shortcut.

    Caught where the mistake is -- the wiring -- rather than one call later,
    because a provider with no trackers at all is a legitimate double and
    would let the unmetered call through.
    """
    with pytest.raises(ValueError):
        build_gemini_provider(
            "user-key",
            "gemini-test",
            FakeHttp(),
            tracker=RecordingTracker(),
            platform_api_key="platform-key",
        )


def test_an_extraction_call_with_no_platform_quota_spends_nothing():
    """The port's own guarantee, below the wiring: a class with a credential
    but no quota never reaches the provider."""
    http = FakeHttp()
    provider = GeminiProvider(
        "gemini-test",
        http,
        EnvCredentialResolver("user-key", "platform-key"),
        {CallClass.USER_SUBJECTIVE: RecordingTracker()},
    )

    with pytest.raises(QuotaUnavailable):
        provider.generate_text(
            "hi", call_class=CallClass.SHARED_EXTRACTION, purpose="job_facets"
        )

    assert http.calls == []

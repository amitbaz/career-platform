"""The AI provider port: the only AI vocabulary core modules may know.

Core modules (`evaluation`, `candidate_context`, `facets`, `preferences`)
talk to *an* AI provider through `AIProvider` and catch the errors defined
here. Nothing outside
`job_hunter.ai.gemini` names Gemini, so a second adapter is a new file rather
than an edit to every call site.

Two concepts deserve their own note.

**Call class.** AI usage divides into two kinds that differ in who pays and in
whether the result can be reused. `CallClass.SHARED_EXTRACTION` reads a posting
once and records what the posting itself says: the answer is a property of the
job, identical for every user, and is funded by a platform-owned key.
`CallClass.USER_SUBJECTIVE` judges fit against one person's profile and runs on
that person's own key. Every call declares its class, and the class -- not the
call site, not configuration -- selects the credential and the quota. The
platform key arrived with issue #128, and the forbidden thing (an
extraction-class call reaching a user's credential, on any branch, including
quota exhaustion) stays unrepresentable rather than merely avoided: the class
alone chooses the credential, so there is no argument, setting or fallback
branch through which extraction could be pointed at a user's key.

**Purpose** is orthogonal to call class: it names *which* piece of work a call
does, so the ledger and the daily budget can tell an evaluation apart from a
cover letter. Two calls with the same purpose can never have different classes
today, but the two answer different questions and are deliberately not merged.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Protocol, runtime_checkable

AIPurpose = Literal[
    "gmail_semantic",
    "candidate_context",
    "job_evaluation",
    "job_facets",
    "company_facets",
    "cover_letter",
]
AI_PURPOSES: tuple[AIPurpose, ...] = (
    "gmail_semantic",
    "candidate_context",
    "job_evaluation",
    # Objective facet extraction (issue #125). Deliberately *not* the core
    # purpose: a posting's facts are shared and reusable, and reading a
    # posting nobody has read yet can wait for tomorrow's run, while scoring a
    # job whose posting has already been read cannot. Since #126 the two are
    # in sequence -- a job cannot be scored before its posting is read -- so
    # exhausting this budget defers exactly the jobs whose postings are still
    # unread, and leaves every other job scoreable out of the core reserve.
    "job_facets",
    # Objective company extraction (issue #198). Its own purpose, not folded
    # into `job_facets`, because the two amortise over completely different
    # denominators: a posting's facts are read once per posting, a company's
    # once per employer across every role it publishes for months. A ledger
    # that could not tell them apart could not show that ratio, and the ratio
    # is the entire argument for reading companies at all.
    "company_facets",
    "cover_letter",
)

#: The purpose whose daily budget is protected by the core reserve.
CORE_PURPOSE: AIPurpose = "job_evaluation"


class CallClass(Enum):
    """Who pays for a call, and whether its answer is shared."""

    #: Objective facts about a posting, funded by the platform key (#128).
    SHARED_EXTRACTION = "shared_extraction"
    #: A judgement about one person, funded by that person's own key.
    USER_SUBJECTIVE = "user_subjective"


#: How long a provider's own throttling response asks us to stay away.
PauseKind = Literal["daily_quota", "rate_limit", "unknown"]

#: Why a provider stopped generating before the answer was complete. Providers
#: spell this differently (`MAX_TOKENS`, `max_tokens`, `length`); adapters
#: translate, and core modules branch on these three values only.
IncompleteReason = Literal["max_output_tokens", "content_filter", "other"]


class AIError(RuntimeError):
    """A provider call failed in a way the caller cannot use."""


class AIIncompleteResponse(AIError):
    """The provider stopped generating before completing the answer.

    `provider_finish_reason` carries the provider's own word for it, for logs
    and ledger rows only; decisions branch on `reason`.
    """

    def __init__(
        self, reason: IncompleteReason, *, provider_finish_reason: str | None = None
    ) -> None:
        detail = provider_finish_reason or reason
        super().__init__(f"AI response incomplete: {reason} ({detail})")
        self.reason: IncompleteReason = reason
        self.provider_finish_reason = provider_finish_reason


class AIBudgetExceeded(RuntimeError):
    """Our own internal daily ceiling or core reserve refused this call."""


class AITemporaryCapacity(AIBudgetExceeded):
    """Rolling RPM/TPM capacity is temporarily full but will free shortly."""

    def __init__(self, message: str, *, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class QuotaUnavailable(RuntimeError):
    """This provider has no quota governing the given call class.

    A provider that accounts one call class must account all of them: a class
    that could obtain a credential but no quota would spend a key nobody is
    metering. Only a provider wired with no trackers at all (a test double, a
    probe) is exempt.
    """


class PlatformAllowanceExhausted(RuntimeError):
    """The platform key cannot fund another shared-extraction call today.

    Deliberately not an `AIBudgetExceeded`: that exception is a statement
    about the *user's* budget, and a caller that treats the two alike would
    stop doing the user's work because work nobody is waiting on ran out of
    the platform's. This one means only that extraction pauses -- the run
    finishes, postings already read still score, and the postings that were
    not read are enriched by a later run.

    It is raised in place of the underlying refusal (an exhausted daily
    ceiling, an active provider pause, or no platform credential at all)
    because all three have the same consequence and the same non-consequence:
    no user is charged, on any of them.
    """


class AIQuotaPaused(RuntimeError):
    """A persisted circuit-breaker pause from a real provider 429 is active."""

    def __init__(self, message: str, *, paused_until: str, reason: str) -> None:
        super().__init__(message)
        self.paused_until = paused_until
        self.reason = reason


@runtime_checkable
class AIProvider(Protocol):
    """One text generation call, against one model, for one call class.

    The argument shapes come from a paper review of this interface against
    Anthropic's and OpenAI's request and response shapes (recorded on issue
    #73); each is deliberately the neutral form rather than Gemini's:

    - `max_output_tokens` is optional, because Gemini treats it as optional.
      An adapter for a provider that requires it (Anthropic's `max_tokens`)
      substitutes its own default rather than pushing the requirement onto
      every call site.
    - `json_schema` is a plain JSON Schema. The adapter translates it into
      whatever the provider calls structured output -- Gemini's
      `responseSchema`, OpenAI's `response_format`, an Anthropic tool.
    - `thinking_level` is a hint, not a token budget, because the three
      providers express it incompatibly (`thinkingLevel`, `reasoning_effort`,
      `thinking.budget_tokens`); an adapter maps the hint onto its own.
    - `prompt` is one string. Anthropic and OpenAI separate a system prompt
      from the user turn; every call site here builds a single prompt, so an
      adapter can send it as the sole user message and lose nothing. This is
      the one argument a future adapter may need to widen.
    """

    model: str

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
    ) -> str: ...

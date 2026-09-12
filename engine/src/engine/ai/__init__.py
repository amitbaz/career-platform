"""The AI provider port and its adapters.

Import the port's vocabulary from here (`engine.ai`); import
`engine.ai.gemini` only where a provider is actually wired up.
"""

from engine.ai.credentials import (
    Credential,
    CredentialResolver,
    CredentialUnavailable,
    EnvCredentialResolver,
)
from engine.ai.retry import wait_out_capacity
from engine.ai.port import (
    AI_PURPOSES,
    CORE_PURPOSE,
    AIBudgetExceeded,
    AIError,
    AIIncompleteResponse,
    AIProvider,
    AIPurpose,
    AIQuotaPaused,
    AITemporaryCapacity,
    CallClass,
    IncompleteReason,
    PauseKind,
    PlatformAllowanceExhausted,
    QuotaUnavailable,
)

__all__ = [
    "AI_PURPOSES",
    "AIBudgetExceeded",
    "AIError",
    "AIIncompleteResponse",
    "AIProvider",
    "AIPurpose",
    "AIQuotaPaused",
    "AITemporaryCapacity",
    "CORE_PURPOSE",
    "CallClass",
    "Credential",
    "CredentialResolver",
    "CredentialUnavailable",
    "EnvCredentialResolver",
    "IncompleteReason",
    "PauseKind",
    "PlatformAllowanceExhausted",
    "QuotaUnavailable",
    "wait_out_capacity",
]

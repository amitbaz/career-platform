"""Published free-tier limits, as defaults in code rather than setup questions.

Asking someone to transcribe three rate limits off a pricing page during
onboarding is both a barrier and a source of wrong numbers, so a run needs an
API key and nothing else. The three `GEMINI_FREE_*` environment variables
survive as an optional per-user override for the case this table is stale or a
project's limits are not the published ones.

The table is a convenience, not a safety mechanism. Published limits and
enforced limits diverge, and the provider's own 429 -- classified by the
adapter, persisted as a pause by `AIUsageTracker` -- is what actually keeps a
run inside the free tier. That is why an unknown model degrades to the most
conservative known limits and logs it, rather than refusing to start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from job_hunter.models import AIQuotaSettings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FreeTierLimits:
    """One model's published free-tier request and token limits."""

    rpm: int
    tpm: int
    rpd: int


#: Published Google AI Studio free-tier limits, keyed by model id.
#:
#: `gemini-3.5-flash-lite` is this deployment's own AI Studio Rate Limits page,
#: read on 2026-09-05; the 2.5 family are Google's published free-tier figures.
#: Refresh an entry the same way it was written: from the Rate Limits page for
#: the project the key belongs to. A model missing here is not an error -- see
#: `CONSERVATIVE_FREE_TIER`.
FREE_TIER_LIMITS: dict[str, FreeTierLimits] = {
    "gemini-3.5-flash-lite": FreeTierLimits(rpm=15, tpm=250_000, rpd=500),
    "gemini-2.5-flash-lite": FreeTierLimits(rpm=15, tpm=250_000, rpd=1_000),
    "gemini-2.5-flash": FreeTierLimits(rpm=10, tpm=250_000, rpd=250),
    "gemini-2.5-pro": FreeTierLimits(rpm=5, tpm=250_000, rpd=100),
}

#: The floor an unlisted model runs under: no dimension above any known model's.
#: A newer model is almost always more generous than this, so the cost of the
#: fallback is a slower run, never an overspend.
CONSERVATIVE_FREE_TIER = FreeTierLimits(rpm=5, tpm=250_000, rpd=100)


def free_tier_quota(
    model: str,
    *,
    rpm: int | None = None,
    tpm: int | None = None,
    rpd: int | None = None,
) -> AIQuotaSettings:
    """Return the quota to run `model` under, with any explicit override applied.

    Each override replaces exactly one published default, so setting one of the
    three environment variables does not oblige a user to supply the other two.
    """
    limits = FREE_TIER_LIMITS.get(model)
    if limits is None:
        limits = CONSERVATIVE_FREE_TIER
        logger.warning(
            "no published free-tier limits are recorded for model %s; "
            "falling back to the most conservative known limits "
            "(rpm=%s tpm=%s rpd=%s). The provider's own 429 remains the "
            "binding limit; set GEMINI_FREE_RPM/TPM/RPD to override.",
            model,
            limits.rpm,
            limits.tpm,
            limits.rpd,
        )
    return AIQuotaSettings(
        rpm=rpm if rpm is not None else limits.rpm,
        tpm=tpm if tpm is not None else limits.tpm,
        rpd=rpd if rpd is not None else limits.rpd,
    )

from __future__ import annotations
from .market_eligibility import evaluate_market_eligibility
from .models import Job, MarketPolicy, SearchPolicy, PrefilterResult
from .normalize import normalize_text


def is_software_engineering_title(title: str, policy: SearchPolicy) -> bool:
    normalized = normalize_text(title or "")
    if not normalized:
        return False

    if any(normalize_text(phrase) in normalized for phrase in policy.blocked_profession_title_phrases):
        return False

    if any(normalize_text(phrase) in normalized for phrase in policy.engineering_title_phrases):
        return True

    return any(normalize_text(keyword) in normalized for keyword in policy.engineering_title_keywords)


def prefilter_job(job: Job, policy: SearchPolicy, market: MarketPolicy | None = None) -> PrefilterResult:
    title_lower = normalize_text(job.title)
    desc_lower = normalize_text(job.description)
    location_lower = normalize_text(job.location)

    if market is None:
        # Legacy global policy (no markets configured): a single remote-only
        # hard blocker applies to every job.
        if job.remote is False:
            return PrefilterResult(
                should_evaluate=False,
                hard_blocker=True,
                reason="not remote",
                reason_code="not_remote",
            )
        if "onsite" in location_lower or "on-site" in location_lower or "in-office" in location_lower:
            if job.remote is not True:
                return PrefilterResult(
                    should_evaluate=False,
                    hard_blocker=True,
                    reason="location indicates not remote",
                    reason_code="not_remote",
                )
    else:
        # Market-driven policy: the global remote-only blocker is replaced by
        # the attributed market's own conservative eligibility rules (work
        # mode, language, salary, sponsorship, employment type).
        eligibility = evaluate_market_eligibility(job, market)
        if not eligibility.allowed:
            return PrefilterResult(
                should_evaluate=False,
                hard_blocker=True,
                reason=eligibility.reason,
                reason_code=eligibility.reason_code,
            )

    # Hard block: blocked title keyword
    for kw in policy.blocked_title_keywords:
        if kw.lower() in title_lower:
            return PrefilterResult(
                should_evaluate=False,
                hard_blocker=True,
                reason=f"blocked title keyword: {kw}",
                reason_code="blocked_title",
            )

    if not is_software_engineering_title(job.title, policy):
        return PrefilterResult(
            should_evaluate=False,
            hard_blocker=False,
            reason="title is outside target engineering professions",
            reason_code="off_target_profession",
        )

    # Check for target title or keyword evidence
    has_target_title = any(t.lower() in title_lower for t in policy.target_titles)
    has_keyword = any(kw.lower() in title_lower or kw.lower() in desc_lower for kw in policy.positive_keywords)

    if not has_target_title and not has_keyword:
        return PrefilterResult(
            should_evaluate=False,
            hard_blocker=False,
            reason="no target title or keyword match",
            reason_code="no_relevance",
        )

    return PrefilterResult(
        should_evaluate=True,
        hard_blocker=False,
        reason="passed prefilter",
        reason_code="passed",
    )

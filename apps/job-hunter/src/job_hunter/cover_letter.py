from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from job_hunter.gemini import GeminiIncompleteResponse
from job_hunter.models import CandidateContext, Evaluation, Job

if TYPE_CHECKING:
    from job_hunter.gemini import GeminiClient

logger = logging.getLogger(__name__)

# Bounded MAX_TOKENS recovery: one retry at double the output budget before
# giving up. Keeps the pipeline from discarding an otherwise-good letter that
# merely ran out of room the first time.
_OUTPUT_TOKEN_BUDGETS = (800, 1600)

_KNOWN_PLACEHOLDERS = (
    "[Company]",
    "[Position]",
    "[Role]",
    "[Date]",
    "[Hiring Manager]",
    "[Your Name]",
    "[Team]",
)


def _build_cover_letter_prompt(
    job: Job, evaluation: Evaluation, context: CandidateContext, template: str, today: date
) -> str:
    strengths = ", ".join(evaluation.strengths) or "none noted"
    gaps = ", ".join(evaluation.gaps) or "none noted"
    career_evidence = "; ".join(context.career_evidence) or "none noted"
    return f"""Write a concise, ready-to-send cover letter for this job application.

NEVER invent facts about the candidate. Only use what is stated in the candidate context below.
Use the template's voice and structure as a reference, but replace every bracket placeholder
(such as [Company], [Position], [Date]) with the real values given here. The final letter must
not contain any remaining bracket placeholder text.

Candidate career evidence:
{career_evidence}

Cover letter template (voice/structure reference only):
{template}

Job title: {job.title}
Company: {job.company}
Today's date: {today.isoformat()}

Candidate strengths for this role: {strengths}
Known gaps to acknowledge tactfully or omit: {gaps}

Return only the final cover letter text. No markdown code fences, no commentary.
"""


def generate_cover_letter(
    job: Job,
    evaluation: Evaluation,
    context: CandidateContext,
    template: str,
    gemini: "GeminiClient",
    today: date,
) -> str:
    prompt = _build_cover_letter_prompt(job, evaluation, context, template, today)

    text: str | None = None
    last_finish_reason: str | None = None
    for attempt, max_output_tokens in enumerate(_OUTPUT_TOKEN_BUDGETS, start=1):
        try:
            text = gemini.generate_text(
                prompt,
                purpose="cover_letter",
                thinking_level="low",
                max_output_tokens=max_output_tokens,
            )
        except GeminiIncompleteResponse as exc:
            last_finish_reason = exc.finish_reason
            retrying = attempt < len(_OUTPUT_TOKEN_BUDGETS)
            logger.warning(
                "cover letter hit finish_reason=%s at max_output_tokens=%s (attempt %s/%s); %s",
                exc.finish_reason,
                max_output_tokens,
                attempt,
                len(_OUTPUT_TOKEN_BUDGETS),
                "retrying with larger output budget" if retrying else "giving up",
            )
            continue
        break

    if text is None:
        raise GeminiIncompleteResponse(last_finish_reason or "MAX_TOKENS")

    text = text.strip()

    if not text:
        raise ValueError("Gemini returned an empty cover letter")

    lowered = text.lower()
    for placeholder in _KNOWN_PLACEHOLDERS:
        if placeholder.lower() in lowered:
            raise ValueError(f"Cover letter contains unreplaced placeholder {placeholder!r}")

    return text

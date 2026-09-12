from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from job_hunter.ai import AIBudgetExceeded, AIIncompleteResponse, AIQuotaPaused, CallClass
from job_hunter.candidate_context import get_candidate_context
from job_hunter.models import CandidateContext, Evaluation, Job, Material, Settings
from job_hunter.pdf import render_cover_letter_pdf

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider
    from job_hunter.postgres_store import PostgresJobStore
    from job_hunter.telegram import TelegramClient

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


# A cover letter is the longest single generation this app asks for, and it
# routinely takes longer than the HTTP client's default 25s read budget: the
# model is still writing, not stuck. Everything else -- evaluations, database
# calls, Telegram sends -- keeps the short default, where a slow reply really
# does mean something is wrong. Sized well above the observed generation time
# rather than just above it, because the cost of waiting too long here is one
# slow run, while the cost of being too tight is no cover letter at all.
_READ_TIMEOUT_SECONDS = 120


def generate_cover_letter(
    job: Job,
    evaluation: Evaluation,
    context: CandidateContext,
    template: str,
    ai: "AIProvider",
    today: date,
) -> str:
    prompt = _build_cover_letter_prompt(job, evaluation, context, template, today)

    text: str | None = None
    last_finish_reason: str | None = None
    for attempt, max_output_tokens in enumerate(_OUTPUT_TOKEN_BUDGETS, start=1):
        try:
            text = ai.generate_text(
                prompt,
                call_class=CallClass.USER_SUBJECTIVE,
                purpose="cover_letter",
                thinking_level="low",
                max_output_tokens=max_output_tokens,
                read_timeout=_READ_TIMEOUT_SECONDS,
            )
        except AIIncompleteResponse as exc:
            last_finish_reason = exc.provider_finish_reason
            retrying = attempt < len(_OUTPUT_TOKEN_BUDGETS)
            logger.warning(
                "cover letter hit finish_reason=%s at max_output_tokens=%s (attempt %s/%s); %s",
                exc.provider_finish_reason or exc.reason,
                max_output_tokens,
                attempt,
                len(_OUTPUT_TOKEN_BUDGETS),
                "retrying with larger output budget" if retrying else "giving up",
            )
            continue
        break

    if text is None:
        raise AIIncompleteResponse(
            "max_output_tokens", provider_finish_reason=last_finish_reason
        )

    text = text.strip()

    if not text:
        raise ValueError("the model returned an empty cover letter")

    lowered = text.lower()
    for placeholder in _KNOWN_PLACEHOLDERS:
        if placeholder.lower() in lowered:
            raise ValueError(f"Cover letter contains unreplaced placeholder {placeholder!r}")

    return text


def cover_letter_output_dir(settings: Settings) -> Path:
    return Path(settings.output_dir) / "cover_letters"


def generate_cover_letter_on_demand(
    settings: Settings,
    job_id: str,
    *,
    store: "PostgresJobStore",
    ai: "AIProvider",
    telegram: "TelegramClient",
) -> bool:
    """Generate (or resend) one job's cover letter on demand and deliver it.

    A repeat call for a job that already has a saved cover letter resends the
    existing PDF for free instead of calling the model again. If the requested
    job was merged away, all reads and writes follow its redirect to the
    surviving job. A missing job with no redirect returns False after telling
    the user that the job is no longer available.
    """
    job = store.get_job(job_id)
    resolved_job_id = job_id
    if job is None:
        survivor_id = store.resolve_merged_job_id(job_id)
        if survivor_id is None:
            logger.warning("no job or merge redirect found for job_id=%s", job_id)
            telegram.send_message(
                "This job is no longer available, so I can't generate a cover letter for it."
            )
            return False
        resolved_job_id = survivor_id
        job = store.get_job(resolved_job_id)

    evaluation = store.get_evaluation(resolved_job_id)
    if job is None or evaluation is None:
        logger.warning(
            "no job/evaluation found for resolved_job_id=%s; cannot generate cover letter",
            resolved_job_id,
        )
        return False

    material = store.get_material(resolved_job_id)
    if material is not None:
        text = material.cover_letter_text
    else:
        try:
            candidate_context = get_candidate_context(settings.candidate_profile, settings.policy, ai, store)
            text = generate_cover_letter(
                job, evaluation, candidate_context, settings.cover_letter_template, ai, date.today()
            )
        except (AIBudgetExceeded, AIQuotaPaused):
            logger.warning("cover letter generation deferred by AI quota for job_id=%s", job_id)
            telegram.send_message(
                f"Couldn't generate a cover letter for {job.company} - {job.title} right now "
                "(AI quota limit) - try again later."
            )
            return False
        except Exception:
            logger.exception("cover letter generation failed for job_id=%s", job_id)
            telegram.send_message(
                f"Couldn't generate a cover letter for {job.company} - {job.title} - something went wrong."
            )
            return False
        store.save_material(
            resolved_job_id,
            Material(job_id=resolved_job_id, cover_letter_text=text),
        )

    out_dir = cover_letter_output_dir(settings)
    pdf_path = render_cover_letter_pdf(text, job.company, job.title, out_dir)
    caption = f"{job.company} - {job.title} - {evaluation.total_score} - {job.url}"
    document_id = telegram.send_document(pdf_path, caption)
    if document_id is not None:
        store.mark_delivered(resolved_job_id, "telegram_document", document_id)
    return document_id is not None

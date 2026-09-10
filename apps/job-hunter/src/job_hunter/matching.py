"""Answer matching as one on-demand operation over stored facets (#187, #188).

`match_jobs` is the operation a dashboard, an on-demand search and the daily
digest all call: one ranking implementation, not one per caller. It ranks
and flags the requesting user's whole corpus in a single SQL round trip
(`PostgresJobStore.match_jobs`, a term-for-term port of
`ranking.profile_priority_score` and `hard_blockers.hard_blockers_from_facets`
-- see `supabase/migrations/20260910140000_job_hunter_match_jobs.sql` and
`docs/superpowers/specs/2026-09-10-answer-matching-as-one-operation-design.md`),
so filtering and hard blocking cost nothing before a provider call. A row
already delivered to this user is skipped entirely; a row already evaluated
but not yet delivered is reused verbatim; everything else that survives
blocking is handed to the model, on the caller's own credentials and ledger,
until `limit` fresh model-scored jobs have been produced -- see
`docs/superpowers/specs/2026-09-10-daily-digest-as-matching-call-design.md`
for why the operation itself has to know what this user has already been
told.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from job_hunter.ai import AIBudgetExceeded, AIQuotaPaused, wait_out_capacity
from job_hunter.evaluation import EvaluationError, evaluate_job
from job_hunter.hard_blockers import blocked_evaluation
from job_hunter.job_identity import normalize_company_name
from job_hunter.models import CandidateContext, Evaluation, SearchPolicy

if TYPE_CHECKING:
    from job_hunter.ai import AIProvider
    from job_hunter.postgres_store import PostgresJobStore

logger = logging.getLogger(__name__)


#: The delivery kind that marks a job as already told to the user. The one
#: kind `run_pipeline` writes today (`mark_delivered(..., "telegram_message")`),
#: named here rather than threaded in as a parameter: every caller of
#: `match_jobs` -- today's digest, a future dashboard or search -- means the
#: same "have I already shown this user this job" question, and a caller that
#: could name a different kind could silently defeat the no-reappear rule.
_DELIVERY_KIND = "telegram_message"


@dataclass(frozen=True, slots=True)
class MatchedJob:
    """One ranked row, plus what the operation decided about it.

    `scored` is a property of `evaluation` itself: False for a facet-decided
    block (`hard_blockers.blocked_evaluation`, no `model` on the row), True
    when `evaluation` is a model's own answer -- whether that answer was
    produced by this call or read back from an earlier one.

    `fresh` is a property of this *call*: True when `evaluation` was decided
    just now (scored or blocked this time, and therefore not yet persisted by
    this function -- the caller must still `save_evaluation` it and, if it is
    an offer, run company-watch promotion). False when the row already had a
    stored evaluation and was simply not yet delivered (`_DELIVERY_KIND`) --
    reused verbatim, at no provider cost, so the caller has nothing new to
    persist. A row already both evaluated and delivered is not returned at
    all (see `match_jobs`): #188's "previously-delivered jobs do not
    reappear", checked once here rather than by every caller separately.
    """

    job_id: str
    posting_id: str
    score: int
    evaluation: Evaluation
    scored: bool
    fresh: bool


@dataclass(frozen=True, slots=True)
class MatchResult:
    """`match_jobs`'s whole answer: what it decided, and what it could not.

    `failed_job_ids` is the #145 guarantee carried into this operation: a
    scoring call that raises an ordinary (non-quota) exception costs that one
    row its turn, logged and skipped, and never the rest of the batch. A
    caller that tracks per-run health (`RunSummary.errors`, in `pipeline.py`)
    reads its length; a caller that does not is free to ignore it.
    """

    matched: list[MatchedJob]
    failed_job_ids: list[str]
    #: The subset of `failed_job_ids` whose model response could not be
    #: parsed at all (`evaluation.EvaluationError`), as opposed to a
    #: transient failure (a network error, a malformed-but-parseable reply).
    #: Kept apart because it is what `RunSummary.scoring_parse_failures`
    #: reports -- a caller's health signal, not this operation's own.
    parse_failure_job_ids: list[str]
    #: Ranked rows left undecided this call because there was nothing
    #: current to block or score against -- no facets row at all, or one a
    #: changed posting made stale (see the staleness check in `match_jobs`).
    #: A caller's own facet pre-pass already knows about its own shortlist's
    #: share of this; the rest -- a row this call reached from elsewhere in
    #: the corpus -- is only visible here.
    skipped_without_facets_job_ids: list[str]


def match_jobs(
    store: "PostgresJobStore",
    ai: "AIProvider",
    policy: SearchPolicy,
    candidate_context: CandidateContext,
    limit: int,
) -> MatchResult:
    """Rank this user's whole corpus in SQL, then score the top `limit` of it.

    Iterates the SQL ranking in order, with three outcomes per row, checked
    in this sequence (#188):

    1. **Already evaluated and delivered -- not returned at all.** Costs
       nothing: `delivered_job_ids`/`get_evaluations_bulk` are bulk reads
       taken once for the whole ranked set, not one round trip per row. This
       is what stops a call from re-scoring the user's entire delivered
       history every time it runs, and is why a job sent last week does not
       reappear today.
    2. **Already evaluated, not yet delivered -- reused verbatim.** No
       provider call, `fresh=False`; does not count against `limit`. This is
       the retry path: a job withheld by an earlier run's offer cap, or one
       whose Telegram send failed, comes back exactly as it was scored.
    3. **Neither -- decided now, exactly as before.** A hard-blocked row
       becomes a `blocked_evaluation` and never reaches `evaluate_job`; a row
       nobody has read yet (`has_facets` is False) is skipped, since neither
       this function nor `evaluate_job` can score a posting with no facets.
       Everything else is scored by the model until `limit` model-scored
       jobs have been produced or the ranking is exhausted. Both outcomes
       are `fresh=True`: the caller must persist them.
    """
    preferences = candidate_context.preferences
    rows = store.match_jobs(
        preferred_roles=preferences.preferred_roles,
        preferred_seniority=preferences.preferred_seniority,
        must_have_signals=preferences.must_have_signals,
        nice_to_have_signals=preferences.nice_to_have_signals,
        preferred_locations=preferences.preferred_locations,
        avoid_signals=preferences.avoid_signals,
    )

    all_job_ids = [row["job_id"] for row in rows]
    delivered_ids = store.delivered_job_ids(all_job_ids, _DELIVERY_KIND)
    existing_evaluations = store.get_evaluations_bulk(all_job_ids)
    # `row["has_facets"]` only says a facets row exists, not that it is
    # current -- a posting edited since it was read still carries one. This
    # is the same staleness comparison the facet pre-pass makes before this
    # call ever runs, asked again here because the pre-pass only reaches
    # this run's own shortlist: a row `matching.match_jobs` surfaces from
    # elsewhere in the corpus (a retry, a reused ranking position) can carry
    # facets that went stale on a run that never touched it.
    stale_facet_ids = store.jobs_needing_facets(
        [row["job_id"] for row in rows if row["has_facets"]]
    )

    results: list[MatchedJob] = []
    failed_job_ids: list[str] = []
    parse_failure_job_ids: list[str] = []
    skipped_without_facets_job_ids: list[str] = []
    scored_count = 0
    company_facets_cache: dict[str, Any] = {}

    for row in rows:
        job_id = row["job_id"]

        if job_id in delivered_ids and job_id in existing_evaluations:
            continue

        if not row["has_facets"] or job_id in stale_facet_ids:
            # Nothing trustworthy to block or score against -- a hard
            # blocker the SQL ranking computed from a stale facets row is
            # exactly as unreliable as scoring would be. Checked before the
            # reuse branch below too: an evaluation stored against a
            # posting's *earlier* facets is exactly as stale as a hard
            # blocker would be, and reusing it verbatim would serve a score
            # that no longer reflects what the posting now says, forever --
            # nothing after this call would ever re-derive it. Left
            # unresolved: the facet pre-pass (or a later run's) is what
            # earns this row a decision, not this call.
            skipped_without_facets_job_ids.append(job_id)
            continue

        existing = existing_evaluations.get(job_id)
        if existing is not None and existing.model:
            # A genuine model answer, reused verbatim at no cost. A stored
            # row with no `model` is a facet-decided block, not a model
            # verdict -- see below for why that one is never reused this
            # way.
            results.append(
                MatchedJob(
                    job_id,
                    row["posting_id"],
                    row["score"],
                    existing,
                    scored=True,
                    fresh=False,
                )
            )
            continue

        # A facet-decided block costs nothing to redo -- `row["hard_blockers"]`
        # is already this call's own fresh SQL answer, computed against the
        # user's *current* profile -- so it is checked before the budget
        # gate below and never read from `existing_evaluations` the way a
        # model score is. Caching it would let a block outlive the floor or
        # preference that produced it: a profile edit between two runs must
        # be able to unblock a job it no longer disqualifies, at no extra
        # cost, on the very next call, rather than the job staying reused as
        # blocked forever. Checking it ahead of `scored_count` also means a
        # call whose scoring budget is already spent still recomputes every
        # block it passes on its way past -- free, so there is no reason to
        # stop.
        blockers = row["hard_blockers"] or []

        if blockers:
            job = store.get_job(job_id)
            if job is None:
                continue
            evaluation = blocked_evaluation(job, blockers)
            results.append(
                MatchedJob(
                    job_id, row["posting_id"], row["score"], evaluation,
                    scored=False, fresh=True,
                )
            )
            continue

        if scored_count >= limit:
            # Not `break`: a reused model-scored row (already evaluated, not
            # yet delivered) can rank below this point, and it must still
            # surface -- it costs nothing and #188 needs it to keep
            # appearing regardless of where this call's scoring budget ran
            # out. Only a row that would need a *fresh model call* is
            # skipped here; it is left unresolved and ranks again next call,
            # same as before this loop stopped scanning early.
            continue

        job = store.get_job(job_id)
        if job is None:
            continue
        facets = store.get_job_facets(job_id)
        if facets is None:
            continue

        company_identity = normalize_company_name(job.company or "")
        if company_identity and company_identity not in company_facets_cache:
            company_facets_cache.update(store.get_company_facets_bulk([job.company or ""]))
        company = company_facets_cache.get(company_identity)

        try:
            evaluation = wait_out_capacity(
                lambda: evaluate_job(job, facets, candidate_context, policy, ai, company),
                doing="scoring",
                job_id=job_id,
            )
        except (AIBudgetExceeded, AIQuotaPaused):
            # The user's own key is exhausted or paused -- every row still
            # to come would fail identically, since scoring paces against
            # that one key with no other claimant this call. Stop here and
            # hand back everything already decided: an unresolved row has no
            # evaluation, so it stays unresolved and is reconsidered from
            # this same rank position the next time anyone calls match_jobs.
            break
        except EvaluationError:
            # The response came back but could not be read as an evaluation
            # -- distinct from an ordinary failure below in exactly the way
            # the pre-#188 pipeline distinguished them
            # (`RunSummary.scoring_parse_failures`).
            logger.exception("evaluation response could not be parsed for job_id=%s", job_id)
            failed_job_ids.append(job_id)
            parse_failure_job_ids.append(job_id)
            continue
        except Exception:
            # #145's guarantee, carried into this operation: one job's
            # failure costs that job its turn today, never the rest of the
            # batch. Unlike quota exhaustion, an ordinary failure (a bad
            # response, a transient network error) says nothing about the
            # next row, so scoring continues rather than stopping.
            logger.exception("scoring failed for job_id=%s", job_id)
            failed_job_ids.append(job_id)
            continue
        results.append(
            MatchedJob(
                job_id, row["posting_id"], row["score"], evaluation,
                scored=True, fresh=True,
            )
        )
        scored_count += 1

    return MatchResult(
        matched=results,
        failed_job_ids=failed_job_ids,
        parse_failure_job_ids=parse_failure_job_ids,
        skipped_without_facets_job_ids=skipped_without_facets_job_ids,
    )

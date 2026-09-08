"""Decide a posting's hard blockers from its facets, with no model call (#127).

Two of the hard blockers the evaluation prompt asks for are not judgement
calls: compensation disclosed below the user's floor, and a role that is not
remote or requires relocation contrary to the user's policy. Both are
comparisons between a fact the posting states about itself -- read once for
everybody as a facet (#125), and since #126 handed to the scoring call in
place of the description -- and a number in this user's own search profile.

**The asymmetry is the design.** The facts are shared; the thresholds are not.
So the comparison happens per user, here, at the moment of scoring, and its
result is written only to that user's evaluation row. `BlockingThresholds`
exists to make that split visible: everything per-user enters through it, and
`hard_blockers_from_facets` cannot reach a profile any other way.

**It fails open.** A facet that is `unknown`, or a posting too thin to have
been read reliably, sends the job on to scoring. An absent fact is not
evidence of a disqualifying one, and discarding jobs because the reading was
incomplete would be a far worse failure than spending the call. The prompt keeps both rules for the same reason: this
module removes calls, it does not take the model's authority over what the
facets cannot settle. See
`docs/superpowers/specs/2026-09-08-facet-hard-blockers-design.md`.
"""

from __future__ import annotations

from dataclasses import dataclass

from job_hunter import content_confidence
from job_hunter.evaluation import SCORE_MAXIMA
from job_hunter.market_policy import salary_floor_for_job
from job_hunter.models import Evaluation, Job, JobFacets, MarketPolicy, SearchPolicy

#: The only period a stored floor can be compared against. A floor is an
#: annual gross base figure; anything else needs an hours-per-year or
#: 13th-month assumption the engine has no source for.
_ANNUAL = "year"

#: Work modes that contradict a remote requirement. `unknown` is deliberately
#: absent: the posting not saying is not the posting saying no.
_NOT_REMOTE = frozenset({"hybrid", "onsite"})

#: Market policies under which a role the user would have to move for is still
#: acceptable. `none` is the only one that is not.
_RELOCATION_ALLOWED_POLICIES = frozenset({"selective", "allowed"})


@dataclass(frozen=True, slots=True)
class BlockingThresholds:
    """The per-user half of the comparison, and only that half.

    Built by `for_job` from the search profile and the market the job was
    attributed to. Frozen, and carrying no posting data, so it can be read
    at a glance whether a value here belongs to a person or to a posting.
    """

    #: The currency the floor is denominated in. A posting disclosing pay in
    #: any other currency is not compared at all.
    currency: str
    salary_floor: int
    remote_required: bool
    relocation_allowed: bool

    @classmethod
    def for_job(
        cls, job: Job, policy: SearchPolicy, market: MarketPolicy | None
    ) -> "BlockingThresholds":
        """Collect the thresholds that apply to `job` for this user.

        With no market configured the legacy global policy applies: a EUR
        floor, remote required, relocation refused -- exactly the two rules
        the remote-only prompt states. With a market, its own currency, floor
        (city-specific where `salary_floor_for_job` finds one) and work-mode
        rules replace them.
        """
        if market is None:
            return cls(
                currency="EUR",
                salary_floor=policy.salary_floor_eur,
                remote_required=True,
                relocation_allowed=False,
            )
        return cls(
            currency=market.salary.currency,
            salary_floor=salary_floor_for_job(job, market),
            remote_required=market.remote_policy == "required",
            relocation_allowed=market.relocation_policy in _RELOCATION_ALLOWED_POLICIES,
        )


def hard_blockers_from_facets(
    facets: JobFacets, thresholds: BlockingThresholds
) -> list[str]:
    """Return the hard blockers `facets` establish under `thresholds`.

    An empty list means "these facets disqualify nothing", never "this job is
    a match": every fact the posting left unstated is still the model's to
    read.
    """
    blockers: list[str] = []

    compensation = facets.compensation
    if (
        compensation.disclosed
        and compensation.maximum is not None
        # Only the maximum is compared, matching the prompt's own rule: a
        # posting stating only the bottom of its range leaves the top
        # unknown, and an unknown top is not evidence of a low ceiling.
        and compensation.currency == thresholds.currency
        and compensation.period == _ANNUAL
        and compensation.maximum < thresholds.salary_floor
    ):
        blockers.append(
            f"disclosed compensation maximum {compensation.currency} "
            f"{compensation.maximum} is below the "
            f"{thresholds.currency} {thresholds.salary_floor} floor"
        )

    if thresholds.remote_required and facets.remote_policy in _NOT_REMOTE:
        blockers.append(f"posting states a {facets.remote_policy} role, and remote is required")

    if not thresholds.relocation_allowed and facets.relocation_policy == "required":
        blockers.append("posting requires relocation")

    return blockers


def blocked_evaluation(job: Job, blockers: list[str]) -> Evaluation:
    """Build the evaluation record a facet-decided block produces.

    Deliberately the same `Evaluation` shape a model block produces, so every
    consumer -- the store, the digest, the decision counters -- handles it
    without knowing which path decided it. Scores are zero because none were
    computed, and `model` is empty because no model produced this: recording
    one would put deterministic blocks into that model's quality numbers.
    """
    return Evaluation(
        job_id=0,
        total_score=0,
        scores={key: 0 for key in SCORE_MAXIMA},
        decision="blocked",
        hard_blockers=list(blockers),
        strengths=[],
        gaps=[],
        salary_note="",
        location_note="",
        rationale=(
            "Blocked on the posting's own stored facets against this search profile; "
            "no scoring call was made."
        ),
        model="",
        market_id=job.market_id or "",
        content_confidence=job.content_confidence or content_confidence.PARTIAL_UNKNOWN,
        requirements={"must_have": [], "preferred": []},
        raw_model_score=0,
    )

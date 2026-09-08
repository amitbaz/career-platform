# Deciding hard blockers from facets, without a model call

Issue #127 (parent #118). Blocked by #125, which stores a posting's objective facets.

## The problem

Two of the hard blockers the evaluation prompt asks Gemini to detect are not judgement
calls at all:

- compensation disclosed below the user's floor;
- a role that is not remote, or that requires relocation, contrary to the user's policy.

Both are comparisons between an objective fact about the posting and a number in the
user's own search profile. Since #125 the facts are already stored on the job, extracted
once for everybody. Paying a per-user scoring call to rediscover a disqualification the
engine can already read is spending the user's provider quota on arithmetic.

## The shape of the change

**The asymmetry is the design.** The facts are shared (`job_hunter_job_facets`, one row
per posting); the thresholds are per-user (`SearchPolicy.salary_floor_eur`, or the
attributed `MarketPolicy`'s currency, floor, remote and relocation rules). The comparison
is therefore per-user, made freshly at the scoring seam, and its result is written only
to that user's `job_hunter_evaluations` row. Nothing about the decision is stored on the
job, so nothing about it can leak to another user.

Since #126 landed, scoring is itself handed the posting's facets, and the seam reads that
same object. There is no second, staler view of the posting to disagree with the one the
model would have been given, and no extra store read.

`hard_blockers.py` holds the comparison and nothing else:

- `BlockingThresholds.for_job(job, policy, market)` collects the per-user numbers — the
  currency and floor that apply (a market's city-specific floor included, via the
  existing `salary_floor_for_job`), whether remote is required, whether relocation is
  allowed.
- `hard_blockers_from_facets(facets, thresholds)` compares them against the posting's own
  facets and returns the blocker reasons, in the vocabulary the digest already renders.
- `blocked_evaluation(job, blockers)` builds the `Evaluation` record: `decision="blocked"`,
  the reasons in `hard_blockers`, every score zero, and `model=""` — no model produced it.

The pipeline runs it on the facets `_facets_for_scoring` just returned — read on an earlier
run, or read inline this one — immediately before it would score. When they yield blockers,
the deterministic `Evaluation` takes the place of the model's, and
**everything downstream is unchanged**: the same merge-following write, the same company
promotion, the same score floor, the same digest handling, the same decision counters.
That is what makes "the same outcome, recorded the same way" true by construction rather
than by a parallel implementation that has to be kept in step.

## Failing open

A missing or unknown facet must never block. A job whose posting could not be read has no
facets at all, and #126 already leaves it unscored rather than scoring it against nothing;
this seam is never reached for it. Of the jobs that do reach it, the job goes on to scoring
when:

- the posting's content confidence is `partial_unknown` — a snippet, not a posting.
  `evaluate_job` already withholds a confident decision on such content, and a block is a
  confident decision; reading a posting applies no such gate, so it has to be applied here;
- `remote_policy` or `relocation_policy` is `unknown`;
- compensation is undisclosed, or discloses no maximum;
- the disclosed currency is not the floor's currency, or the period is not annual.

The last one is deliberate. Converting SEK to EUR, or an hourly rate to a salary, needs
a rate and an hours-per-year assumption the engine has no source for, and a 13th-month
convention makes `monthly × 12` wrong in exactly the markets it would be used in. The
model reads the posting's own context and can still block such a job; guessing here
would discard real jobs to save one call.

The prompt keeps both rules for that reason. This change removes calls; it does not
remove the model's authority over the cases the facets cannot settle.

## What this deliberately does not do

A run whose provider quota is already exhausted defers its whole shortlist without reaching
the seam, so a job the facets would have blocked for free is queued rather than blocked.
Left as is: the queued job is blocked on the next run at no cost, and moving the comparison
in front of the quota gate would put a second decision path into the two evaluation loops
for a saving that arrives a day later either way.

A block also scores zero, because nothing was scored, and the match-score floor therefore
withholds it from the digest. A model-decided block could score above the floor and reach
the "Needs review / blockers" section; a facet-decided one will not. That is the one
user-visible difference, and zero is the only truthful number here — inventing a score to
preserve a digest placement would put a fabricated figure into the evaluation record and
into every quality number computed from it.

## Which comparisons

| Fact (shared)                     | Threshold (per user)                               | Blocked when |
| --------------------------------- | -------------------------------------------------- | ------------ |
| `compensation.maximum` (annual, matching currency) | `salary_floor_eur`, or the market's floor for the job's location | maximum < floor |
| `remote_policy` ∈ {hybrid, onsite} | no market, or market `remote_policy == "required"` | remote is required |
| `relocation_policy == "required"`  | no market, or market `relocation_policy == "none"` | relocation is not allowed |

Only the maximum is compared, matching the prompt's own rule: a posting that discloses
only a minimum leaves the top of its range unknown, and an unknown top is not evidence.

Sponsorship and language blockers stay with the model: no facet records them.

## What is counted

`RunSummary.blocked_by_facets` counts jobs blocked this way. They are deliberately
**not** counted in `evaluation_attempted` or `evaluated`: those two exist to detect a run
where every fresh Gemini evaluation failed, and a deterministic block — which is a
success, and makes no call — would mask exactly that.

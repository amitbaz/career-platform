# Scoring from facets (issue #126)

Status: implemented. Parent epic #114; the contract half of the expand-and-contract pair
begun in [#125](2026-09-08-job-posting-facets-design.md).

## What changes

Judging whether a job fits a person stops re-reading the job description.

The per-user scoring call receives the requirements already extracted from the posting —
each with the depth it demands — plus the candidate's context, and produces the same six
score components, total, decision, hard blockers, strengths, gaps, notes and rationale it
produces today. What it no longer receives is the posting text, because #125 already read
that once and turned it into structured facts.

The combined call that did both jobs at once is gone. A job is never both extracted and
separately evaluated from its raw description, and the temporary doubling of provider calls
#125 accepted ends here.

**The outcome must not change.** A job delivered today, with a given decision and score, is
delivered with the same decision and score afterwards. This is a refactor of how the
judgement is assembled, not of what it concludes.

## Where the saving lands

The reference run's 265,208 input tokens are dominated by descriptions being re-sent — for
every user, every day, for postings that have not changed. Afterwards:

- a description is read **once ever**, by `facets.py`, and
- each scoring call carries a short structured requirements list instead of the posting.

The effect is on both axes. Fewer calls, because extraction is not repeated per user; far
smaller prompts, because the expensive payload is gone. A user's own free-tier allowance
stretches much further, which is what makes bring-your-own-key viable beyond a handful of
people.

## What the scoring call sees

`evaluation.evaluate_job(job, facets, context, policy, gemini)`. The posting reaches it as:

- the job's own identity fields — title, company, location label, remote flag, and the
  content-confidence tier with its existing prompt hint;
- the facets: seniority, remote policy, relocation policy, hiring regions, stack, disclosed
  compensation;
- the stated requirements, must-have and preferred, each with the depth the posting demands,
  rendered as ordered numbered lists.

It does not see `job.description`. A unit test asserts a sentinel in the description never
reaches the prompt, and a pipeline test asserts the same at the seam.

The deterministic market blocks are unchanged: `evaluate_market_eligibility` and
`salary_floor_for_job` still read the job locally, at no token cost, and their verdicts still
reach the prompt. That is part of why outcomes stay put.

## Candidate support stays per-user

Whether *this* person satisfies a stated requirement is not a property of the posting, so it
cannot be shared and stays in the scoring call. What changed is only where the requirement
and its depth come from.

The response carries one `candidate_support` verdict per stated requirement, in the order the
prompt listed them. The stored requirement keeps the posting's own text and depth, taken from
the facets; only the verdict comes from the model. So a scoring call can neither invent a
requirement the posting never stated nor quietly restate one at a depth it never demanded.

A response that does not carry exactly one verdict per stated requirement is rejected rather
than read as far as it goes. Dropping a verdict the model failed to give would let an
unjudged must-have pass as though it had been judged — and an unsupported must-have at
`experience` depth or deeper is exactly what caps a score below the `possible` band.

## A job with no facets is not scored

Scoring against an empty requirements list would read as "this posting demands nothing"
instead of "nobody has read this posting", which inflates the score of precisely the jobs
least is known about. So `evaluate_job` refuses `None` facets before it makes a call, and the
pipeline leaves such a job unscored. It keeps its place in the ranking and is scored on a
later run.

`RunSummary.scoring_skipped_without_facets` counts those jobs, apart from `errors` (this is
neither damage nor a scoring failure). A number that stops being near zero means extraction
is failing and the run is quietly delivering less — which is exactly why a *provider
refusal* is counted separately, as `scoring_deferred_by_read_budget`. An ordinary
budget-exhausted day must not inflate the signal that says extraction is broken. Both are
reported on the `evaluation_capacity` log line.

## Where the reading happens

A posting a run is about to score is read **inline, immediately before scoring it**, by
`pipeline._facets_for_scoring`. That is forced: scoring cannot precede the read any more.

`store.jobs_needing_facets` is asked **once per run**, over the retry queue plus the
shortlist, so the ordinary case — a posting read on an earlier run — costs one store read and
no provider call. The set is narrowed as the run reads, so it ends the scoring loops holding
exactly the postings the run did not spend a read on.

`_extract_facets_for_run` still runs afterwards, and still last, over what scoring did not
need: the shortlist tail the offer cap never reached, and the jobs discovery rediscovered.
Rediscovered jobs remain the whole backfill — an already-scored job never re-enters the
shortlist — so the existing corpus still drains over consecutive runs with no migration
script. It is given only the ids the run did not already read, so a posting whose read failed
is never read twice in one run.

The ordering argument from #125 still holds for that pass. A provider 429 persists a pause
against the *model*, not the purpose, and the scoring loops treat `GeminiQuotaPaused` as
blocking for the rest of the run; a 429 tripped by a backfill call made first would defer
every score behind it and deliver nothing. The run's own inline reads are not subject to that
argument, because they are the unavoidable cost of scoring the job at all.

`max_jobs_per_run` bounds the *backfill*, not the run's reads as a whole. An inline read is
the unavoidable cost of scoring the job in front of it and is never refused for want of that
budget; what it spends is subtracted, and the backfill gets the remainder, floored at zero.
So a run that scores a full shortlist of postings nobody has read leaves the backfill
nothing that day, which is the right trade: the jobs the user is waiting on come first, and
the corpus drains on a quieter one. What bounds the reads themselves is what bounds scoring
— the shortlist, the retry queue and the daily offer limit.

## Running out of budget

`job_facets` stays a **non-core** purpose, so the core reserve still refuses a read before it
refuses a score. That now has a consequence worth stating: exhausting the shared reading
budget makes a *new* posting unscoreable, while a posting already read still scores normally.

The pipeline therefore tells the two provider refusals apart, which the combined call never
had to:

| Refusal | Meaning | What the run does |
| --- | --- | --- |
| `GeminiQuotaPaused` | the model is paused; a scoring call would fail too | queue the job, block the run, exactly as a paused scoring call does |
| `GeminiBudgetExceeded` | the *non-core* daily budget is out, not the scoring reserve | queue this job, carry on scoring every job whose posting was already read |
| `GeminiTemporaryCapacity` | rolling window full | wait it out, as the scoring call itself does — a user is waiting on this answer |

The third differs from the backfill pass, which gives up its turn instead: nobody is waiting
on the backfill.

## How the equivalence was verified

There is no before/after fixture, because the combined call is deleted and cannot be run
alongside its replacement. What stands in its place is the existing pipeline suite: over a
hundred tests written against the combined implementation — decisions, score caps, the
`match_score_floor` and `daily_offer_limit` behaviour, digest contents, delivery, merges,
retries — pass **unchanged in their expectations** against the facet-fed path. The six that
did change are the ones whose contract this ticket deliberately changes: a run that cannot
read a posting no longer delivers that job.

The token fall is asserted rather than assumed: the combined prompt was this prompt's
material plus the whole posting, so `test_the_scoring_prompt_does_not_carry_the_job_description`
measures the new prompt against that sum and requires it to be less than half.

## What kept its name

The module, the stored row, the provider purpose and `RunSummary`'s counters keep the name
"evaluation" although [CONTEXT.md](../../../CONTEXT.md) reserves that word for the pre-split
combined operation. The artefact they persist is still an `Evaluation`, and renaming it would
reach the `job_hunter_evaluations` table, the `job_evaluation` AI-work queue and the
pipeline's counters — a rename with no behavioural content, which is exactly what this ticket
is not. The vocabulary now lives in the module docstring instead.

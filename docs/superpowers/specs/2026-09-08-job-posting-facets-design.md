# Objective facets on jobs (issue #125)

Status: implemented. Parent epic #114; the enrichment split continues in #126.

## What this adds

A job that survives the existing non-AI filters is read once by the model and gains a set
of **facets** — the objective, per-posting facts named in [CONTEXT.md](../../../CONTEXT.md):
stated requirements and their depth, disclosed compensation, hiring-eligible regions,
remote and relocation policy, seniority, and stack. They are stored on the job, keyed to
the description they were read from, and reused by every later run.

Nothing a user sees changes. The existing combined evaluation still runs unchanged and
still decides what is delivered; facets are written and never read back into scoring. What
this produces is a corpus that knows things about jobs before anyone has been matched
against them.

## The load-bearing constraint: extraction cannot see a candidate

Objective extraction is only worth doing if its output is identical for every user. The
moment the prompt can tell who is asking, the result stops being cacheable and the whole
change is worthless.

That is enforced by the interface, not by convention:

- Extraction lives in its own module, `src/job_hunter/facets.py`. That module does not
  import `CandidateContext`, `CandidatePreferences`, `SearchPolicy` or `MarketPolicy`, and
  a test asserts it never will.
- `extract_facets()` takes a `PostingFacts` — a frozen view built from a `Job` carrying
  only the posting's own fields — and a provider client. There is no parameter a candidate
  could arrive through, so passing one is a `TypeError` rather than a review comment.

`evaluation.py` keeps the combined, candidate-aware path. The two are deliberately separate
modules: putting objective extraction next to a prompt builder that serialises a candidate
profile would leave the constraint resting on nothing but care.

## Structured source data is used before the model is asked

Two facets are already known before any provider call, and the model is asked only for the
residue:

- **Remote policy.** Ashby returns a structured `isRemote`, which lands on `Job.remote`. An
  Ashby posting with `remote=True` records `remote_policy="remote"` directly. `remote=False`
  is *not* treated as `onsite` — it distinguishes neither hybrid nor onsite — so it goes to
  the model like anything else. Greenhouse is deliberately **not** in this set even though it
  returns a structured location: its adapter derives `remote` by looking for the substring
  "remote" in the location label, so "Hybrid Remote — Berlin" arrives as `True`. Because a
  supplied facet is never asked of the model, recording that would pin a hybrid role as fully
  remote with nothing to correct it. Greenhouse's location label reaches the prompt instead.
- **Hiring-eligible regions.** `hiring_scope.determine_hiring_scope` already answers
  exactly this question deterministically, from the posting text alone, and is already the
  engine's answer to it (see `AGENTS.md` on `hiring_scope.py`). When it reads an explicit
  scope, that is recorded and the model is not asked to re-derive it. It fails open, so an
  empty scope falls through to the model.

Which facets came for free is recorded on the row (`source_supplied`), so the split can be
measured rather than assumed.

## Invalidation reuses the description hash

A job merged away mid-run has its facets **discarded**, not written against the survivor —
`save_job_facets` deliberately does not follow merges the way `save_evaluation` does. The
survivor is a different posting with its own description; writing these facets against it
would stamp them with *that* row's hash and pin one posting's facts to another's text as
permanently current, with no path back to re-extraction. An evaluation survives that
treatment because the next description change recomputes it; facets stamped that way never
expire. The survivor is extracted from its own text on a later run.

`job_hunter_job_facets.description_hash_at_extraction` is compared against
`job_hunter_jobs.description_hash`, exactly as `job_hunter_evaluations.description_hash_at_eval`
already is for re-evaluation. A changed description invalidates the facets and the next run
that reaches the job extracts again. There is no second notion of a changed posting.

## Where it runs

`pipeline._extract_facets_for_run`, over this run's shortlist and retry queue first, then
the jobs it rediscovered — and **after every evaluation the run makes**.

Rediscovered jobs are not an optimisation, they are the whole backfill. An
already-evaluated job never re-enters the shortlist (`discovery` shunts it to
`rediscovered_job_ids` before the prefilter), so without them only postings first seen today
would ever gain facets and a failed extraction would never be retried. Every job in either
group survived the non-AI filters — this run's did so this run, a rediscovered job did so on
the run that first evaluated it — so no provider call is ever spent on a posting the
prefilter or the profession gate rejected.

**The ordering is load-bearing.** A provider 429 persists a pause against the *model*, not
the purpose (`GeminiUsageTracker.record_429`), and the evaluation loops treat
`GeminiQuotaPaused` as blocking for the rest of the run. A 429 tripped by a facet call made
first would therefore defer every evaluation behind it and deliver an empty digest — this
work would have cost the user their day's offers. Running last, it can only ever spend what
the run's own offers did not need. The internal core reserve protects evaluation from the
*budget*; this ordering protects it from the provider.

The per-run bound is `max_jobs_per_run`, the shortlist size the search profile already sets
as how much AI work one run may do. Half of it is reserved for the backfill: spending in
priority order alone would mean a day that discovers a full shortlist leaves nothing for the
corpus, so the backfill would progress only on quiet days, which is not a backfill. An
unspent shortlist allowance flows to the backfill. A rolling-capacity refusal never reached
the provider, so it consumes no slot and is reported as `skipped_by_capacity` rather than
silently eating the allowance.

Backfill is therefore inherent: a job with no facets acquires them the next time a run
touches it. No migration script, no operator step.

## Failure behaviour

Parsing normalises before it validates — casing, surrounding space, and a pay figure that
arrives as `90000.0` or `"90000"` — because a rejected value fails the *whole* response, and
throwing away six correctly-read facets over a capitalisation is a bad trade. A genuinely
uninterpretable value still fails: silently recording it as "the posting did not say" would
put a fact in the database that nothing said. A fractional pay figure fails rather than
being rounded.

An extraction that fails or returns unparseable output writes nothing. The job is left
unenriched with no marker, so the next run that reaches it tries again — a bad response is
never mistaken for a permanent property of the posting. Failures are counted in
`RunSummary.facet_extraction_failed`, distinct from `evaluation_attempted`/`errors`, and
reported on their own `facet_extraction` log line. Provider quota exhaustion stops facet
work for the rest of the run without touching evaluation: facets are a non-core purpose, so
the tracker's core reserve protects evaluation from them by construction.

## Storage

A dedicated table, `public.job_hunter_job_facets`, one row per job, cascading from
`job_hunter_jobs` and governed by the same four row-level-security policies as every other
job-hunter table.

Dedicated columns rather than one document, because the acceptance criterion is that
hiring-eligible regions, remote policy, seniority and compensation are filterable in a
query without loading and parsing every row:

| Facet | Column | Index |
| --- | --- | --- |
| hiring-eligible regions | `hiring_regions text[]` | GIN |
| remote policy | `remote_policy text` (checked domain) | btree `(user_id, remote_policy)` |
| seniority | `seniority text` (checked domain) | btree `(user_id, seniority)` |
| compensation | `compensation_min/max numeric`, `currency`, `period`, `disclosed` | btree `(user_id, compensation_max)` |

Stated requirements stay in `requirements_json` — a list of `{requirement, depth, kind}` —
because they are read as a set, never filtered on individually.

## Cost, deliberately accepted

Until #126 removes the objective half of the combined evaluation, both calls run against
the same jobs: roughly 122 provider calls a day against a 500-per-day allowance, from the
reference run's 61 evaluations. That fits, and facets are charged to the non-core budget so
they can never starve evaluation, but it is real and short-lived by design.

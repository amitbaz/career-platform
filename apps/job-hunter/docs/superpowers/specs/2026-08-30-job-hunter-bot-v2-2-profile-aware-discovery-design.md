# Job Hunter Bot v2.2 Profile-Aware Discovery Design

**Date:** 2026-08-30

## 1. Purpose

Restore the precision users experienced in the earlier daily run while keeping the broader public discovery surface introduced in v2. The current v2 pipeline discovers many records, but a hand-coded ranker chooses the full Gemini shortlist before the candidate profile can influence prioritization. This concentrates output in Hacker News and We Work Remotely and can hide quieter matches from other sources.

## 2. Goals

- Add more reliable public sources without authentication, paid APIs, browser automation, or application submission.
- Analyze the candidate profile once per run into a compact, validated preference model.
- Use that model to prioritize roles before expensive full evaluations.
- Preserve deterministic hard safety gates: remote-only, blocked professions, malformed records, and the score-60 delivery floor.
- Prevent one source from consuming the entire shortlist when other sources have valid candidates.
- Keep full Gemini evaluations bounded at a default of 35 jobs per run and configurable.
- Preserve SQLite compatibility, evaluation caching, cover-letter generation, Telegram retry, and scheduled runs.

## 3. Non-goals

- No change to the Gemini model or existing component score weights.
- No employer-form submission, authenticated scraping, or paid search provider.
- No automatic migration or deletion of existing SQLite state.
- No guarantee that every source contributes a candidate when it has no candidate passing the hard gates.

## 4. Evidence and root cause

The most recent workflow discovered 1,208 records: Hacker News 581, Arbeitnow 358, We Work Remotely 150, Remote OK 100, and Remotive 19. After the strict remote/profession/relevance gate, only Hacker News (71), We Work Remotely (27), and Remotive (3) remained eligible. The global ranker then selected primarily Hacker News and We Work Remotely.

The current ranker uses title overlap, configured keywords, generic career signals, location text, and source metadata. It does not read the candidate profile. The earlier v1 run evaluated a smaller Remotive/Arbeitnow pool, which reduced noise and likely made the results feel more aligned even though it had less coverage.

## 5. Desired pipeline

```text
public sources
    -> normalize + dedupe + enrich
    -> hard gates (remote, blocked professions, malformed records)
    -> one compact profile-preference extraction call
    -> profile-aware deterministic scoring
    -> source-diverse shortlist (default 35, configurable)
    -> full Gemini evaluation
    -> score floor (>60) + explicit decision filter
    -> score-sorted Telegram and PDFs
```

The profile extraction call is separate from job evaluation. It returns structured preferences only; it never decides whether a job is deliverable.

## 6. Public discovery sources

Keep all current sources: Remotive, Arbeitnow, Remote OK, We Work Remotely, Hacker News Who Is Hiring, DuckDuckGo HTML search, and configured Ashby/Lever/Greenhouse boards.

Add two unauthenticated JSON adapters:

- Jobicy: `https://jobicy.com/api/v2/remote-jobs`. The public API is documented in the [Jobicy remote-jobs-api repository](https://github.com/Jobicy/remote-jobs-api).
- Himalayas: `https://himalayas.app/jobs/api`. The [Himalayas API documentation](https://himalayas.app/docs/remote-jobs-api) documents public pagination, location restrictions, salary ranges, and application links without an API key.

Each adapter must fail open, normalize to `Job`, preserve the source job ID and application URL, strip HTML, and expose location/remote metadata when provided. Tests use fixed fake JSON and never call the network.

## 7. Profile preference model

Add a `CandidatePreferences` dataclass with:

- `preferred_roles: list[str]`
- `preferred_seniority: list[str]`
- `must_have_signals: list[str]`
- `nice_to_have_signals: list[str]`
- `preferred_locations: list[str]`
- `avoid_signals: list[str]`
- `summary: str`

At the start of a run, call Gemini once with the candidate profile and request JSON containing exactly those fields. Validate with the standard JSON parser and reject malformed/oversized output. If extraction fails, use a deterministic fallback built from `SearchPolicy` so discovery still runs.

The profile text is never written to SQLite, logs, or generated artifacts. Only the compact preference model lives in memory for the run.

## 8. Profile-aware ranking and source diversity

Extend the ranker with `profile_priority_score(job, preferences, policy) -> int`. Keep existing hard gates outside the ranker. The score combines:

- role/seniority fit: 0–35;
- must-have and nice-to-have signal coverage: 0–30;
- location fit: 0–15;
- avoid-signal penalty: 0–10 deducted;
- source quality and freshness: 0–10.

Count unique normalized signals, cap repeated evidence, clamp to 0–100, and keep stable company/title/job-ID tie-breakers.

Select candidates with a two-pass source-diversity policy:

1. Rank each source group by profile-aware score and take up to `source_minimum_per_run` candidates from every source that has eligible jobs.
2. Fill remaining slots by global score while no source exceeds `source_max_share` of the shortlist.

Defaults are `source_minimum_per_run: 2`, `source_max_share: 0.5`, and `max_jobs_per_run: 35`. These values are configuration-driven. Diversity never creates candidates that failed the hard gates, and if a source has fewer than two eligible jobs its available candidates are used.

## 9. Gemini budget and delivery

`max_jobs_per_run` is the maximum number of full job-evaluation calls, not a target. With 18 eligible jobs, evaluate all 18; with 100, select and evaluate the best diverse 35.

The profile extraction call does not count against `max_jobs_per_run`. Cover-letter calls remain limited to `high_priority` and `package_match` decisions. Jobs scoring 60 or below remain persisted but are not sent to Telegram, retried for delivery, or rendered as PDFs.

## 10. Diagnostics

Log one run-level funnel line without private content:

```text
discovery: raw=... unique=... prefilter_rejected=... profession_rejected=... eligible=... selected=... deferred_by_budget=... sources=...
```

Also log the selected source counts in stable order and whether profile extraction used `gemini` or `fallback`. Never log the profile, job descriptions, API keys, or cover-letter text.

## 11. Error handling and compatibility

- A source failure skips only that source.
- Profile extraction failure falls back deterministically and does not abort discovery.
- Ranking or diversity selection failure falls back to existing stable ranking and still respects the full-evaluation ceiling.
- Existing evaluation caching, materials, Telegram delivery filtering, retries, and SQLite schema remain unchanged.
- Historical evaluations remain readable; new delivery rules continue to exclude scores `<=60`.

## 12. Testing and acceptance criteria

Tests must use fake HTTP/Gemini collaborators and cover:

- Jobicy and Himalayas normalization and failure isolation.
- Profile JSON parsing, validation, fallback, and no secret logging.
- Profile-aware scores favoring the candidate’s preferred role over a generic role with repeated keywords.
- Source-diverse selection with minimums, maximum share, stable tie-breakers, and fewer-than-minimum candidates.
- 18 eligible jobs evaluating 18; 100 eligible jobs evaluating exactly 35.
- Deferred-by-budget and selected-source logging.
- Score 60 excluded from Telegram/PDF/retry while score 61 remains deliverable.
- Existing v1/v2 regression coverage and `pytest -q` passing.

The release is complete when the workflow exposes more than HN/WWR when other sources have valid candidates, output ordering is stable, and the selected jobs reflect the profile preference model rather than only generic keyword overlap.

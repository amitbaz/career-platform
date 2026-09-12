# Gemini Free-Tier Guardrails & Usage Optimization Design

## Problem

The Job Hunter Bot must operate at **€0 Gemini API cost** for now, but the current implementation was built as if Gemini capacity were effectively unconstrained. On 2026-09-01 the shared Gemini project generated several hundred requests and roughly 6M input/output tokens in one day. Google Cloud billing was enabled on that project, so those requests were charged instead of using the Gemini Free Tier.

Billing has now been disabled. That prevents paid Gemini usage, but the bot still has several architectural problems:

- Gmail backfill scans a full year even though old job alerts are not useful to current discovery.
- Semantic Gmail classification can send up to 20,000 body characters to Gemini.
- The same candidate profile is repeatedly sent to Gemini for preference extraction, every job evaluation, and every cover letter.
- Gemini 3.6 Flash defaults to medium thinking and the client does not choose task-specific thinking levels.
- The client discards Gemini `usageMetadata`, so request and token usage cannot be attributed to features.
- The generic HTTP layer retries HTTP 429 responses, which can turn one quota failure into three attempts.
- A Gmail semantic quota failure does not stop later Gmail messages from attempting Gemini again.
- There is no application-level daily budget, no quota circuit breaker, and no user-visible quota status.

The bot should remain high quality while treating Gemini Free Tier capacity as a scarce shared resource.

## Current evidence

The latest successful Gmail backfill run reported:

- 1,858 matching messages in the 12-month Gmail query.
- 100 processed in that run.
- 895 still deferred after previously processed messages were skipped.

The persisted Gmail state already reaches farther back than the useful lifecycle matching window. Current constants are:

- `DISCOVERY_FRESHNESS_DAYS = 14` for staging Gmail job alerts into discovery.
- `MATCH_RECENCY_DAYS = 120` for application-event matching.

Therefore, continuing the remaining historical 12-month backfill would spend Gemini quota on messages that cannot materially improve current job discovery and are older than the lifecycle matching horizon.

## Goal

Make the Job Hunter Bot reliable and useful on the Gemini Free Tier only, with predictable degradation when quota is scarce and clear visibility into how much of the configured Free Tier is being used.

## Hard constraints

- **€0 Gemini spend is mandatory.** The Google project used by the bot must remain unlinked from a paid Cloud Billing account.
- The bot must never assume that a 429 means paid overflow is available.
- Hitting a quota/budget limit must pause or defer Gemini work rather than keep retrying.
- Job evaluation quality is the highest-priority Gemini workload.
- No migration to Supabase is included in this change; existing SQLite persistence remains authoritative.
- No implementation is included in this branch; this document and its plan are for Codex/Claude Code execution later.

## Important provider limitation

Google applies Gemini limits per project across RPM, input TPM, and RPD. Active limits vary by model/project/tier and Google exposes them in AI Studio rather than a stable runtime quota API.

For that reason, the bot will **not hard-code guessed Free Tier limits**. Production configuration must supply the current AI Studio limits for the selected model:

- `GEMINI_FREE_RPM`
- `GEMINI_FREE_TPM`
- `GEMINI_FREE_RPD`

The application will calculate utilization against these configured provider limits. If the values are missing or invalid in production, Gemini work must fail closed rather than operate without guardrails.

Google documents that daily RPD resets at midnight Pacific time. The quota tracker must use `America/Los_Angeles` for daily-window calculations regardless of the bot's Berlin timezone.

## Architecture

Introduce a small Gemini resource-management layer around the existing REST client:

1. `GeminiQuotaPolicy` — configured provider limits and internal safety ratios.
2. `GeminiUsageTracker` — persisted request/token ledger and rolling utilization calculations.
3. `GeminiClient` — task-aware generation with quota preflight, thinking/output controls, response usage capture, and 429 circuit breaking.
4. `CandidateContext` cache — one rich, reusable candidate representation instead of repeatedly sending the full CV/profile.
5. Task-specific prompt optimization — especially Gmail semantic classification.
6. Telegram quota reporting — one compact status message per bot run plus a separate warning only when the budget/circuit breaker trips.

The rest of the pipeline continues to call one Gemini abstraction. Callers identify the purpose of each request so usage can be attributed and priorities enforced.

## Gemini task classes

Every generation request must declare one of these purposes:

- `gmail_semantic`
- `candidate_context`
- `job_evaluation`
- `cover_letter`

This label is persisted with usage and controls thinking level, output limit, and budget priority.

### Priority

1. **Core:** `job_evaluation`
2. **Important:** fresh `gmail_semantic`, `candidate_context` when the cache is missing/stale
3. **Optional/deferable:** `cover_letter`

Candidate context is important but normally costs only one request per profile/model/prompt-version change.

## Free-tier budget policy

The bot will maintain two layers of protection.

### Provider limits

Configured from the active AI Studio limits for the bot's Gemini project/model:

- RPM
- input TPM
- RPD

### Internal safety ceiling

The bot may use at most **80%** of each configured provider limit. The remaining 20% is deliberate safety headroom for provider variability, retries outside the bot, and measurement uncertainty.

Before a request, the quota layer evaluates:

- attempted requests in the current minute vs 80% of RPM;
- estimated input tokens in the current minute vs 80% of TPM;
- attempted requests in the current Pacific day vs 80% of RPD;
- persisted circuit-breaker state.

Input-token preflight should avoid a separate Gemini `countTokens` request because that would itself consume API capacity. Use a conservative local text estimate for preflight; after success, replace estimates in reporting with the exact `usageMetadata` returned by Gemini.

A safe initial estimator for text prompts is `ceil(character_count / 3)`. It intentionally overestimates common English/JSON prompt usage.

### Core reserve

Non-core work must leave **25% of the internal daily request budget** available for `job_evaluation`.

This means Gmail semantic classification and cover letters cannot consume the final core reserve. Job evaluation may use the reserve until the global 80% provider ceiling is reached.

Cover letters are the first workload to be deferred when capacity becomes tight. A ready-to-apply job remains visible even if its cover letter must be generated on a later run.

## Usage persistence and observability

Add a SQLite `gemini_usage` ledger. One row represents one attempted generation request.

Required fields:

- timestamp
- model
- purpose
- status (`success`, `blocked_budget`, `quota_429`, `error`)
- estimated input tokens
- exact prompt tokens when available
- candidate/output tokens when available
- thinking tokens when available
- cached input tokens when available
- total tokens when available
- HTTP status/error code when applicable

Add a small `gemini_quota_state` table for persisted pauses:

- model
- `paused_until`
- reason
- updated timestamp

The tracker must expose:

- requests used in current Pacific day
- RPD percentage
- peak/current rolling RPM percentage
- peak/current rolling input TPM percentage
- total input/output/thinking/cached tokens for the current day
- per-purpose request/token totals
- whether the internal budget or provider circuit breaker is active

No email bodies, job descriptions, prompts, or model responses are stored in the usage ledger.

## Telegram reporting

Send one compact quota/usage message once per normal bot run, before the interactive job navigator so the navigator remains the most recent Telegram message.

Example:

`Gemini 🟢 RPD 34% · RPM peak 20% · TPM peak 17% · 21 calls · 142k tokens`

The percentages are explicitly separate because there is no single meaningful "Free Tier percentage".

Status color is based on the highest provider-limit utilization:

- 🟢 below 60%
- 🟡 60% to below 80%
- 🔴 80% or paused

If a budget limit or 429 circuit breaker activates, send one additional actionable warning for that run, for example:

`Gemini paused: daily free-tier safety budget reached. Job evaluation/material work was deferred; paid usage is not enabled.`

Do not emit one warning per skipped call.

The same numbers must also be written to structured logs with per-purpose breakdowns.

## 429 behavior and retry policy

A 429 is a quota/rate-limit refusal, not a signal to pay.

The Gemini path must bypass the generic HTTP client's current 429 retry behavior. Gemini may still retry transient network/5xx failures using the existing bounded retry policy, but **429 must not be automatically retried by the HTTP layer**.

The Gemini client should parse the provider error when possible:

- `quota_exceeded`: pause until the next Pacific-day reset.
- `rate_limit_exceeded` / `too_many_requests`: pause Gemini requests for at least 90 seconds.
- unknown 429: conservatively pause for 90 seconds and stop the current Gemini-producing phase.

Once a pause is active, subsequent Gemini calls fail fast locally without sending HTTP requests.

Gmail sync must recognize the quota-pause exception and stop semantic processing for the current batch instead of iterating through the rest of the backlog. Successfully processed state remains persisted.

The normal job-discovery pipeline should continue wherever possible. If core Gemini evaluation cannot run because the daily quota is paused, selected jobs should be deferred rather than marked as low-quality or failed evaluations.

## Gmail backfill changes

### Replace the 12-month historical window

Change Gmail backfill from 12 months to **120 days**, matching the lifecycle matching horizon.

Because persisted processing already covers more than this window, the next backfill should mostly/all consist of already-processed IDs and should naturally mark the backfill complete without manually editing SQLite state.

After completion, Gmail uses its existing history-based incremental sync.

### Skip stale job-alert semantic extraction

During backfill, if deterministic rules identify `JOB_ALERT` and the message is older than `DISCOVERY_FRESHNESS_DAYS` (14 days):

- record/process the deterministic classification;
- do not call Gemini to extract job candidates;
- do not stage the stale alert into job discovery.

Lifecycle messages inside the 120-day window may still use semantic classification when deterministic rules are insufficient because they can still improve application-state tracking.

### Compact semantic prompt

Stop blindly sending 20,000 body characters.

Build a semantic email context containing:

- sender
- subject
- snippet
- normalized body capped at **6,000 characters**
- only normalized HTTP(S) links already extracted from the message, capped at **20 links**

Preserve enough leading body content for recruiter/application context while sharply reducing token volume. Existing deterministic classification remains the first gate, so Gemini only sees messages that genuinely need semantic help.

## Candidate context cache

The current pipeline repeatedly sends the full candidate profile to Gemini. Replace that with one cached `CandidateContext` derived from the full profile.

### Cache identity

Cache by:

- SHA-256 of the source candidate profile
- Gemini model
- candidate-context prompt/schema version

A changed CV/profile, model, or schema version invalidates the cache automatically.

### CandidateContext contents

The context must preserve factual evidence needed by ranking, evaluation, and cover-letter generation, including:

- target/preferred roles
- seniority evidence
- technical skills and technologies
- frontend/product architecture experience
- leadership/ownership evidence
- agentic/AI workflow experience
- product/domain experience
- location/language/work-authorization facts present in the profile
- career-direction preferences
- company-environment preferences
- concise factual career evidence bullets suitable for cover letters
- a compact one-paragraph evaluation summary

It also carries the existing `CandidatePreferences` fields used by ranking so the separate per-run preference-extraction request disappears.

The candidate-context extraction may use the full source profile because it runs only on cache miss. It should use a strict JSON schema and reject/avoid invented facts.

## Job evaluation optimization

Job evaluation should receive the cached structured `CandidateContext`, not the full source profile.

Keep the existing six scoring components, maxima, hard-blocker rules, and decision thresholds unchanged. The goal is to remove repeated context tokens, not change ranking semantics.

Task settings:

- thinking level: `low`
- maximum output tokens: **1,200**
- JSON response mode/schema preserved

`low` is intentionally more capable than `minimal` for the core quality-sensitive task while avoiding the default medium thinking cost.

## Candidate-context extraction settings

Task settings:

- thinking level: `medium`
- maximum output tokens: **1,800**

This is a rare cache-miss operation where completeness is worth more reasoning.

## Gmail semantic settings

Task settings:

- thinking level: `minimal`
- maximum output tokens: **800**

Classification/extraction is bounded structured work and should not use default medium thinking.

## Cover-letter settings

Cover letters should use `CandidateContext` factual evidence plus the existing cover-letter template and role evaluation rather than the full source profile.

Task settings:

- thinking level: `low`
- maximum output tokens: **800**

Cover-letter generation remains optional/deferable under quota pressure. Existing no-placeholder and non-empty validation remains.

## Deferred work semantics

Quota pressure must not corrupt job state.

- If a job is selected but evaluation is blocked by the Gemini budget, leave it eligible for evaluation on a later run; do not save a failed/zero evaluation.
- If a ready-to-apply job cannot generate a cover letter because optional quota is exhausted, persist a pending-material state and retry material generation on a later run without re-evaluating the job.
- Gmail messages skipped because a quota pause occurs remain unprocessed so incremental/backfill logic can retry them later.

This separates "not evaluated yet" from "evaluated and not a match."

## Output controls

Extend `GeminiClient.generate_text` so every caller explicitly supplies:

- `purpose`
- `thinking_level`
- `max_output_tokens`

JSON callers continue to supply `json_mode` / `json_schema`.

The client sets Gemini generation config accordingly and rejects calls that omit purpose-specific resource controls in production code.

## Run-level summary model

Extend the pipeline/run summary with Gemini usage information suitable for both logging and Telegram formatting.

At minimum:

- calls attempted/succeeded/blocked
- input/output/thinking/total tokens
- RPD utilization percentage
- peak RPM utilization percentage
- peak TPM utilization percentage
- paused reason/status
- per-purpose request counts

Percentages are calculated against configured provider limits. Token totals are informational and are not mislabeled as a daily token quota.

## Configuration

Add Gemini quota settings to bot configuration loading. Provider limits come from environment variables because they are project/model-specific operational values:

- `GEMINI_FREE_RPM`
- `GEMINI_FREE_TPM`
- `GEMINI_FREE_RPD`

Fixed application policy defaults:

- provider ceiling ratio: `0.80`
- core-reserve ratio: `0.25`
- minute-rate 429 pause: `90` seconds

The selected Gemini model remains configurable through `GEMINI_MODEL` and defaults to `gemini-3.6-flash`.

README/runbook documentation must explicitly say that changing the model or Google project requires updating the three free-tier limit values from that project's AI Studio Rate Limits page.

## Zero-cost operational requirement

Application guardrails cannot detect whether somebody later reattaches Cloud Billing to the Google project. Therefore the €0 guarantee has two required layers:

1. **Google-side:** the Job Hunter Bot Gemini project remains on Free Tier with Cloud Billing disabled/unlinked.
2. **Application-side:** the bot stays below the configured Free Tier limits and fails/degrades safely on quota exhaustion.

Re-enabling billing would violate the deployment requirement even if the application budget remained under free-tier-style limits.

The Interviewer App is intentionally out of scope for this branch. It should move to its own Gemini project/API key and receive a separate usage investigation afterward so the two applications do not share quota.

## Files expected to change during implementation

New focused modules are preferred over growing `gemini.py`, `pipeline.py`, and `store.py` further.

Expected additions/changes:

- `src/job_hunter/gemini.py` — request configuration, usage parsing, typed errors, 429 handling.
- `src/job_hunter/gemini_usage.py` — quota policy, local preflight estimator, usage aggregation, circuit-breaker decisions.
- `src/job_hunter/models.py` — Gemini usage summary / candidate-context models as appropriate.
- `src/job_hunter/store.py` — usage ledger, quota state, candidate-context cache, pending material persistence helpers.
- `src/job_hunter/config.py` — Free Tier limit configuration.
- `src/job_hunter/gmail_models.py` — historical-window constants if shared there.
- `src/job_hunter/gmail_sync.py` — 120-day backfill, stale-alert semantic skip, quota-pause short circuit.
- `src/job_hunter/gmail_classifier.py` — compact semantic context and task-specific Gemini controls.
- `src/job_hunter/preferences.py` — replaced/refactored into cached candidate-context extraction while retaining ranking compatibility.
- `src/job_hunter/evaluation.py` — use CandidateContext and explicit generation controls.
- `src/job_hunter/cover_letter.py` — use CandidateContext and explicit generation controls.
- `src/job_hunter/pipeline.py` — create quota-aware client/context, defer work correctly, send usage reporting.
- `src/job_hunter/telegram.py` — quota status and warning formatting.
- `.github/workflows/daily.yml` — provide Free Tier limit secrets/variables to Gmail + normal pipeline steps.
- `README.md` — Free Tier setup, billing requirement, rate-limit configuration, Telegram status meaning.
- tests covering each changed module and workflow contract.

## Error handling

- Missing production quota configuration: fail Gemini work closed with a clear configuration error.
- 429: no automatic HTTP retry; set pause and defer work.
- 5xx/network failures: retain bounded transient retry behavior.
- Malformed Gemini usage metadata: generation may still succeed, but log the metadata problem and conservatively count the attempted request; never silently report zero utilization.
- Candidate-context extraction failure: use the existing deterministic preference fallback for ranking, but do not silently downgrade evaluation to an incomplete candidate representation. Defer evaluation until a valid context is available.
- Telegram usage-report failure must not fail the job pipeline.

## Testing strategy

Tests must use fake Gemini responses and never call the real API.

Coverage must prove:

1. Provider limits are required and parsed correctly.
2. 80% RPM/TPM/RPD ceilings block locally before an HTTP request.
3. The core reserve protects job-evaluation capacity from Gmail/cover-letter work.
4. Usage metadata is persisted and aggregated by purpose.
5. Pacific-midnight daily reset is correct from Berlin/UTC timestamps.
6. Gemini 429 is not retried by the generic HTTP layer.
7. Daily quota 429 pauses until Pacific reset.
8. Minute-rate 429 pauses for at least 90 seconds.
9. Active pauses make later calls fail locally without HTTP traffic.
10. Telegram formatting reports RPD, RPM, TPM separately and uses the correct status color.
11. Backfill query covers 120 days, not 12 months.
12. Stale deterministic job alerts do not call Gemini.
13. Fresh job alerts still receive semantic extraction when needed.
14. Semantic Gmail body/link caps are enforced.
15. Candidate context is reused on identical profile/model/schema and invalidated on changes.
16. Job evaluation receives CandidateContext rather than full candidate profile.
17. Scoring maxima/threshold behavior remains unchanged.
18. Cover-letter generation is deferred safely when optional budget is unavailable.
19. Deferred evaluations/materials are retried without corrupting prior state.
20. Full existing test suite remains green.

Final verification:

```bash
pytest -q
git diff --check
```

No live Gemini request is part of automated verification.

## Acceptance criteria

- The bot can run indefinitely with its Gemini Google project on Free Tier and billing disabled without requiring paid API usage.
- The remaining obsolete 12-month Gmail backlog is not semantically processed.
- Gmail semantic token consumption is substantially reduced through stale-alert skipping, deterministic gating, and compact prompts.
- Full candidate profile text is not resent for every job evaluation/cover letter.
- Every successful Gemini call is attributed to a purpose with exact returned token usage where available.
- The bot proactively stops at 80% of configured provider limits and preserves core evaluation capacity.
- A 429 produces at most one provider attempt for that call and activates a circuit breaker.
- Quota exhaustion defers work rather than misclassifying jobs or corrupting state.
- Telegram shows a concise, understandable Free Tier status every run and one separate warning when Gemini is paused.
- Job evaluation scoring semantics remain unchanged and use a richer cached candidate context rather than a lossy preference-only summary.
- The README makes the Google-side billing-disabled requirement explicit.

# First-Run Hardening Design

Date: 2026-09-02
Status: Approved by explicit user request to implement the fixes directly
Repository: `amitbaz/job-hunter-bot`
Branch: `fix/first-run-hardening`

## Purpose

Harden the first production run of the market-driven search feature after logs exposed four concrete problems: market-targeted public search contributed no candidates and DuckDuckGo returned HTTP 403 during canonical lookup; DuckDuckGo attempted/succeeded metrics were snapshotted before discovery and therefore always logged zero; candidate-context extraction silently degraded to fallback; and Gemini's internal 80% RPM ceiling deferred otherwise valid jobs even though daily/token quotas had ample headroom. Delivery metrics were also mislabeled because they counted digest candidates before successful Telegram delivery.

## Design

### 1. Resilient and budgeted targeted search

Introduce a small search-backend abstraction for market targeted search while treating Brave as a scarce metered accelerator rather than the default backend.

- If `BRAVE_SEARCH_API_KEY` is configured, Brave may be used for a bounded subset of the planned market queries.
- `BRAVE_MONTHLY_QUERY_LIMIT` is a hard application-side monthly ceiling and defaults to `250`, matching the user's current Brave account allowance.
- Brave attempts are persisted in a small SQLite search-usage ledger in the same state database so scheduled runs and manual reruns share one budget.
- Remaining monthly capacity is spread across the remaining UTC calendar days. A manual rerun on the same day cannot spend another full daily allocation.
- Scarce Brave queries are selected round-robin across the configured markets before repeating a market, preventing Germany or another high-share market from consuming the entire daily Brave allowance.
- Planned queries not assigned to Brave continue through DuckDuckGo HTML at zero API cost.
- If Brave fails for one of its assigned queries, fall back to DuckDuckGo for that query.
- Search results normalize into the existing `Job` shape with `market_hint` preserved.
- Do not add new direct scrapers in this fix.
- Keep `max_search_queries_per_run` unchanged.
- Add backend/query success/result-count metrics so a 200 response with zero parseable results is distinguishable from a successful search with results.
- Canonical targeted lookup must never spend Brave credits; it uses the zero-cost search path and remains fail-open if that lookup is unavailable.

Brave remains optional. Missing/invalid Brave budget configuration must fail safe by disabling Brave, not by risking paid or over-limit usage.

### 2. Candidate-context fallback visibility and single-run reuse

Candidate-context extraction remains fail-open, but it may no longer fail silently.

- Log the exception type and sanitized reason when Gemini extraction/parsing fails; never log profile contents or model response text.
- Carry source metadata on `CandidateContext` (`cache`, `gemini`, `fallback_empty_profile`, or `fallback_error`) plus a sanitized `load_error` class name.
- Cache only successful Gemini contexts, as today; fallback-error contexts are not persisted so a later run retries.
- Within one pipeline run, call candidate-context extraction exactly once and reuse the returned context everywhere.
- Include fallback reason/source in the structured run log.

### 3. Gemini burst pacing

Do not change Gemini model, RPD/RPM/TPM values, or the 80% safety ceiling.

Differentiate permanent-for-the-day budget exhaustion from temporary rolling-window pressure:

- Daily RPD/internal reserve exhaustion still raises `GeminiBudgetExceeded` and defers work.
- Provider 429 behavior remains unchanged.
- If only internal rolling RPM/TPM would block a request, calculate the earliest safe retry time from existing usage rows and raise a dedicated temporary-capacity exception with `retry_after_seconds`.
- `GeminiClient.generate_text` waits using an injectable sleeper and retries preflight once capacity is available. No provider request is made while waiting.
- If capacity still cannot be obtained after the retry, propagate the temporary exception and let existing queue behavior defer work.

This lets a run use available free daily quota without bursting above the configured safety ceiling.

### 4. Correct market telemetry

- Snapshot targeted-search attempted/succeeded/result metrics only after `collect_candidates()` has executed sources.
- `queries_planned`, `queries_attempted`, and `queries_succeeded` refer to actual targeted market search queries.
- Add `search_results` per market so successful-but-empty search can be seen.
- Keep raw/unique attribution metrics but document that attribution can change after enrichment; add `reattributed` count to make raw→unique shifts explicit.
- `delivered` in the market log must count only jobs for which a Telegram message/card delivery was successfully persisted during this run.
- In dry-run mode, `delivered=0`.
- Preserve existing log field order/labels where possible so observability improvements do not break existing log consumers/tests.

### 5. Failure behavior

- One search backend failure does not abort other queries or sources.
- Missing Brave key is not an error.
- Invalid/non-positive Brave monthly-limit configuration disables Brave for safety.
- Candidate-context extraction error does not abort the run, but it is visible.
- Temporary Gemini pacing waits rather than consuming quota or creating errors.
- Existing pending-work priority, canonical dedupe, company watch, Gmail, Telegram navigation, and cover-letter behavior remain intact.

## Tests

Add/adjust tests for:

1. Brave result normalization and market hint preservation.
2. Brave failure -> DuckDuckGo fallback.
3. No Brave key -> DuckDuckGo only.
4. Persistent 250/month Brave budgeting across manual reruns.
5. Round-robin Brave query allocation across markets.
6. Canonical lookup never consumes Brave credits.
7. Search statistics recorded after discovery, including zero-result success.
8. Candidate-context parse/API error logs fallback source without leaking profile/response.
9. Candidate context is requested once per pipeline run.
10. RPM-only internal pressure returns a retry delay; RPD exhaustion remains hard budget exhaustion.
11. Gemini client waits/retries without issuing a provider call before capacity.
12. Market `delivered` counts only successful Telegram delivery.
13. Reattribution metric explains market count shifts.
14. Full regression suite.

## Out of scope

- Changing market shares or salary floors.
- Adding authenticated job-board scraping.
- Changing Gemini model or quota values.
- Supabase migration.
- Search-result ranking redesign.

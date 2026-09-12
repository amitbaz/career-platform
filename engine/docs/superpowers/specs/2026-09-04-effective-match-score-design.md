# Effective Match Score Design

**Issue:** [#45 — Make user-facing match score reflect critical unsupported must-have requirements](https://github.com/amitbaz/job-hunter-bot/issues/45)
**Follow-up to:** PR #44 (`4487bb2`), spec+plan at `docs/plans/2026-09-04-trustworthy-job-evaluation.md`
**Date:** 2026-09-04

## Problem

Gemini scores each job on six weighted axes; the evaluator sums them into `total_score`. Separately, Gemini returns a structured requirement list: each entry tagged `must_have` or `preferred`, with a `depth` (`familiarity` / `experience` / `deep_expert`) and a `candidate_support` verdict (`supported` / `partial` / `unsupported` / `unknown`).

PR #44 added a deterministic guardrail: when any `must_have` requirement is `unsupported` and its `depth` is not `familiarity`, the job is barred from the `high_priority` and `package_match` decisions. That guardrail touches only `decision`. The numeric score is passed through untouched.

The result is that the bot's internal judgement and the number printed to the user disagree. A production example: a role whose core responsibility is end-to-end forecasting, for which the candidate has no evidence, was internally downgraded to `possible_match` but still displayed `Match: 71%` on its Telegram card. Because navigation is sorted by that number, the job outranked genuinely plausible openings.

## Current behaviour (verified)

- Components are validated and summed at `src/job_hunter/evaluation.py:277-288`; Gemini's declared `total_score` must equal the recomputed sum or an `EvaluationError` is raised.
- The guardrail flag is computed at `src/job_hunter/evaluation.py:300-305` and consumed only by the decision ladder at `:307-316`.
- `total_score` is written straight into `Evaluation` at `:320` and never mutated afterwards, anywhere.
- Persistence is a single `INTEGER` column, `evaluations.total_score` (`src/job_hunter/store.py:104`), written once per evaluation row (`:1898`) and read back newest-first (`:2029`).
- Every user-facing consumer reads that one column: the card percentage (`src/job_hunter/telegram_navigation.py:69`), the navigation sort key (`:52`), the digest line and its sort key (`src/job_hunter/telegram.py:64,68`), the delivery floors (`src/job_hunter/telegram.py:52`, `src/job_hunter/store.py:1976`, `src/job_hunter/pipeline.py:442`), and the cover-letter caption (`src/job_hunter/pipeline.py:423`).
- The Telegram selection step already discards any item whose decision is `skip` (`src/job_hunter/telegram.py:53-54`), and both the digest text and the card navigator are built from that same filtered list (`src/job_hunter/pipeline.py:737,785`).

## Decisions

**1. Cap in place; keep the raw sum beside it.**
`Evaluation.total_score` continues to mean "the score we stand behind", and gains a deterministic cap. Gemini's untouched component sum moves to a new field, `raw_model_score`, kept for diagnostics only.

Rejected alternative: add a separate `effective_score` field and leave `total_score` raw. That would require auditing and editing all seven-plus read sites, and any missed site would silently keep displaying the inflated number. It also collides with the persistence layer's additive-only migration (see decision 3).

**2. Capped jobs disappear from Telegram, by design.**
The cap lands below the configured `possible` threshold, so the decision ladder yields `skip`, and the existing Telegram filter drops the job from both the digest and the card deck. No filter changes are made. This is the intended outcome: a job whose core requirement the candidate cannot meet is not worth a card.

**3. Backfill the new column rather than defaulting it.**
`JobStore` has no versioned migration system — `_init_db` runs `CREATE TABLE IF NOT EXISTS` and then `_add_missing_columns`, which issues `ALTER TABLE ... ADD COLUMN` with a hardcoded default (`src/job_hunter/store.py:359-364`). A bare `DEFAULT 0` would make every historical row read back as `raw_model_score = 0`. The migration therefore runs an idempotent `UPDATE evaluations SET raw_model_score = total_score WHERE raw_model_score = 0` on every startup. A genuine all-zero evaluation has `total_score = 0` too, so the update is a no-op for it.

**4. Derive the cap from config, not a constant.**
The cap is `max(0, policy.thresholds.get("possible", 65) - 1)`, so it tracks the configured threshold. `HIGH_PRIORITY_THRESHOLD` remains a module constant and is untouched.

**5. Collapse the now-redundant decision gate.**
Once the score is capped below `possible`, it can no longer reach the `package` or `high_priority` rungs, so the `major_unsupported_must_have` arm of `confident_decision_available` becomes dead weight. That flag reduces to `not insufficient_content`, with a comment recording why. Behaviour is unchanged and strictly stronger *given the shipped `config/search.yml`, where `possible` (65) < `package` (75) < the hardcoded `HIGH_PRIORITY_THRESHOLD` (85)* — this is a configuration assumption, not a structural guarantee. `_capped_score`'s ceiling is `policy.thresholds["possible"] - 1`, loaded from YAML with no validation that it sits below `package` or `HIGH_PRIORITY_THRESHOLD`. A nonsensical config (e.g. `possible: 80`, `package: 75`) would cap the score at 79, which still clears `package_match`, so the collapsed gate is only as safe as the operator's threshold ordering.

**6. Thin-content downgrades are left alone.**
The other arm of the old flag — postings whose text was too sparse to judge — still gates decisions without capping the number. A thin posting is evidence that we failed to read the job, not evidence of a poor fit; capping it would hide jobs whose description merely failed to scrape. Explicitly out of scope, per the issue.

**7. Existing stored evaluations are not recomputed.**
A new evaluation row is written per run and the newest wins, so historical inflated scores age out naturally. No rewrite of stored scores.

## Carve-outs the cap must respect

| Situation | Capped? |
|---|---|
| `must_have`, `unsupported`, `depth` is `experience` or `deep_expert` | Yes |
| `must_have`, `unsupported`, `depth` is `familiarity` | No |
| `preferred`, `unsupported`, any depth | No |
| `must_have`, `partial` / `unknown` / `supported`, any depth | No |
| Any of the above, plus a Gemini hard blocker | Capped, but decision is `blocked` — hard blockers keep precedence |

## Data flow after the change

```
Gemini JSON
  -> components validated + summed          -> raw
  -> requirements validated
  -> major unsupported must-have?  yes      -> total = min(raw, possible - 1)
                                   no       -> total = raw
  -> decision ladder reads `total`
  -> Evaluation(total_score=total, raw_model_score=raw)
  -> evaluations.total_score / evaluations.raw_model_score
  -> DigestItem.score / NavigationCard.score  (unchanged code paths)
  -> "Match: X%", digest line, sort keys, delivery floors
```

## Scope boundaries

In scope: `evaluation.py` capping, `models.py` field, `store.py` column + migration + round-trip, and the tests covering all of it.

Out of scope: score component weights, discovery parameters, replacing Gemini evaluation, content-confidence redesign, unifying the three separately-defined delivery-floor constants (60 / 60 / 61), and any Supabase migration work.

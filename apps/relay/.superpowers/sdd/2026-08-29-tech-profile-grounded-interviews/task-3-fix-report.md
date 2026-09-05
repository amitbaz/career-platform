# Task 3 Fix Report

Date: 2026-08-30
Branch: `codex/tech-profile-grounded-interviews-work`
Target commit fixed: `f4a2058`

## Review findings addressed

1. Preserved `followUpLimit: 0` by resolving blueprint limits with nullish semantics instead of `||`.
2. Stopped trusting schema-valid Gemini evaluations by default. The evaluator now cross-checks supported claims, claimed expected signals, and low-relevance contradictions against the deterministic rubric pass before using model output.
3. Removed answer length from the fallback follow-up decision. The fallback now asks follow-ups only from explicit grounded checks: supported claims, relevance, signal coverage, and unsupported claims.

## Regression coverage added

- `does not create a follow-up when the rubric explicitly sets the follow-up limit to zero`
- `does not request a follow-up solely because a relevant answer is concise`
- `rejects schema-valid Gemini praise when the answer is unrelated to the exact question`
- Updated the normalization test to assert that ungrounded model claims are stripped while grounded coaching fields remain intact.

## Verification

- `npm test -- src/lib/coach.test.ts`
- `npm run lint`
- `npx next build --webpack`
- `git diff --check`

All commands passed.

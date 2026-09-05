# P1 Richer Feedback and Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explainable per-question interview feedback and evidence-backed readiness/progress insights to the P0 interview coach.

**Architecture:** Extend the existing `Evaluation` contract and Supabase evaluation rows additively, then calculate a pure `ProgressSnapshot` from hydrated profile competencies and completed sessions. The server owns evaluation validation and progress business rules; the client renders expandable feedback and explicit empty/baseline states.

**Tech Stack:** Next.js 16.3.3 App Router, TypeScript, React 19, Supabase Postgres/RPC, Zod, Vitest, Tailwind CSS.

**Spec:** `docs/superpowers/specs/2026-08-29-richer-feedback-readiness-design.md`

## Global Constraints

- Preserve strict TypeScript, `@/*` imports, two-space indentation, and existing route/repository boundaries.
- Follow red → green → refactor for every behavior change.
- Document public modules, exported functions and types, API routes, and complex domain models with purpose, inputs/outputs, side effects, failures, and invariants.
- Keep browser-only logic in client components and server-only logic in `src/lib` modules marked `server-only` when applicable.
- Use only transform and opacity for transitions; use the existing View Transitions API path for view changes.
- Do not fabricate readiness or trend data: no evidence returns `readiness: null`; one completed session returns `trend: "baseline"`.
- Keep all Supabase writes user-scoped and transactional through the existing RPC pattern.
- Run `npm test`, `npm run lint`, `npx next build --webpack`, and `git diff --check` before completion.

---

### Task 1: Expand evaluation contract and persistence

**Files:**
- Modify: `src/lib/types.ts` (`Evaluation`)
- Modify: `src/lib/coach.ts` (`evaluationSchema`, fallback and normalization paths)
- Modify: `src/lib/repositories/interviews.ts` (`mapEvaluation`, `mapSessionEvaluation`, RPC parameter mapping)
- Create: `supabase/migrations/202608290003_richer_feedback.sql`
- Test: `src/lib/coach.test.ts`
- Test: `src/lib/repositories/interviews.test.ts`
- Test: `src/lib/supabase/richer-feedback-migration.test.ts`

**Interfaces:**
- Produces `Evaluation.missingPoints: string[]`, `Evaluation.betterStructure: string[]`, and `Evaluation.improvedAnswer: string`.
- Produces additive database columns or JSON fields consumed by existing question/session evaluation hydration.
- Preserves legacy rows by mapping absent values to `[]`, `[]`, and `""`.

- [ ] **Step 1: Write failing contract tests.**

Add assertions that deterministic fallback evaluations contain non-empty coaching fields, Zod-normalized model output preserves those fields, and legacy database rows hydrate with empty defaults.

```ts
expect(evaluation.missingPoints).toEqual([expect.any(String)]);
expect(evaluation.betterStructure.length).toBeGreaterThan(0);
expect(evaluation.improvedAnswer).toEqual(expect.any(String));
```

- [ ] **Step 2: Run focused tests and confirm failure.**

Run: `npm test -- src/lib/coach.test.ts src/lib/repositories/interviews.test.ts`

Expected: FAIL because the fields do not yet exist or are not persisted.

- [ ] **Step 3: Add the fields and migration.**

Extend `Evaluation`, evaluator Zod schemas, fallback output, normalization, row mappers, and the atomic RPC payloads. Add nullable/additive columns (or a documented JSON extension matching the existing schema) with no destructive rewrite, and update both `record_interview_evidence` and session evaluation insertion to persist them.

- [ ] **Step 4: Run focused tests and confirm pass.**

Run: `npm test -- src/lib/coach.test.ts src/lib/repositories/interviews.test.ts src/lib/supabase/richer-feedback-migration.test.ts`

Expected: PASS, including legacy hydration coverage.

- [ ] **Step 5: Commit.**

```bash
git add src/lib/types.ts src/lib/coach.ts src/lib/coach.test.ts src/lib/repositories/interviews.ts src/lib/repositories/interviews.test.ts supabase/migrations/202608290003_richer_feedback.sql src/lib/supabase/richer-feedback-migration.test.ts
git commit -m "feat: persist explainable interview feedback"
```

### Task 2: Build pure readiness and progress calculations

**Files:**
- Create: `src/lib/progress.ts`
- Create: `src/lib/progress.test.ts`
- Modify: `src/lib/types.ts` (export `ProgressSnapshot`)

**Interfaces:**
- Consumes `Competency[]` and completed `InterviewSession[]`.
- Produces `calculateProgress(competencies, sessions): ProgressSnapshot`.
- `ProgressSnapshot` contains `readiness`, `latestScore`, `trend`, `recentScores`, `strongest`, `weakest`, and `recurringWeaknesses`.

- [ ] **Step 1: Write failing calculator tests.**

Cover no evidence, one session baseline, improving/declining/stable boundaries, strongest/weakest active competencies, duplicate weakness removal, and newest-first ordering.

```ts
expect(calculateProgress([], [])).toMatchObject({ readiness: null, trend: null });
expect(calculateProgress(withEvidence, [oneComplete])).toMatchObject({ trend: "baseline" });
expect(calculateProgress(withEvidence, [older, newer]).trend).toBe("improving");
```

- [ ] **Step 2: Run the focused test and confirm failure.**

Run: `npm test -- src/lib/progress.test.ts`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the deterministic calculator.**

Sort completed sessions by `completedAt`/`updatedAt`, clamp all scores to `0–10`, return null readiness without competency evidence, use documented weighted competency/session inputs for `0–100`, and classify trend from recent completed scores only. Deduplicate recurring weaknesses while retaining recent order. Do not mutate inputs.

- [ ] **Step 4: Run focused tests and confirm pass.**

Run: `npm test -- src/lib/progress.test.ts`

Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/lib/progress.ts src/lib/progress.test.ts src/lib/types.ts
git commit -m "feat: calculate readiness progress signals"
```

### Task 3: Expose progress through authenticated server data

**Files:**
- Modify: `src/app/api/interview/route.ts` (authenticated GET response)
- Modify: `src/app/page.tsx` (load and retain `ProgressSnapshot`)
- Test: `src/app/api/interview/route.test.ts`

**Interfaces:**
- Authenticated GET responses include `progress: ProgressSnapshot`.
- Unauthenticated requests continue returning HTTP 401 through `requireUser()`.
- The client consumes server-calculated progress and does not duplicate readiness formulas.

- [ ] **Step 1: Add failing route contract tests.**

Mock an authenticated user and repository data; assert the response contains a progress snapshot. Add a 401 test proving no progress data is exposed without a user.

- [ ] **Step 2: Run focused route tests and confirm failure.**

Run: `npm test -- src/app/api/profile/route.test.ts src/app/api/interview/route.test.ts`

Expected: FAIL because the response has no `progress` field.

- [ ] **Step 3: Calculate progress on the server and return it.**

Load the already user-scoped profile and recent sessions in the existing `/api/interview` GET handler, filter to completed sessions, call `calculateProgress`, and add `progress` alongside `sessions` in the authenticated response. Keep repository errors mapped to the existing safe error responses.

- [ ] **Step 4: Run focused route tests and confirm pass.**

Run: `npm test -- src/app/api/profile/route.test.ts src/app/api/interview/route.test.ts`

Expected: PASS, including unauthorized behavior.

- [ ] **Step 5: Commit.**

```bash
git add src/app/api/profile/route.ts src/app/api/interview/route.ts src/app/page.tsx src/app/api/profile/route.test.ts src/app/api/interview/route.test.ts
git commit -m "feat: expose authenticated progress snapshot"
```

### Task 4: Add expandable Results feedback

**Files:**
- Modify: `src/app/page.tsx` (Results state and evaluation cards)
- Create: `src/app/page.test.tsx` (or extract a focused Results component into a separately tested file)

**Interfaces:**
- Results cards expose a native button with `aria-expanded` and a labelled region.
- Expanded content renders question, answer, dimensions, strengths, missing points, better structure, and improved answer when present.
- Legacy/empty fields are omitted without breaking the card.

- [ ] **Step 1: Write failing UI tests.**

Assert cards are collapsed initially, toggling changes `aria-expanded`, and expanded content includes the exact question/answer and all non-empty coaching sections. Include an evaluation with legacy-empty fields.

- [ ] **Step 2: Run the focused UI test and confirm failure.**

Run: `npm test -- src/app/page.test.tsx`

Expected: FAIL because Results currently renders static cards without expansion or coaching sections.

- [ ] **Step 3: Implement accessible expandable cards.**

Track one expanded evaluation ID, render a button and `role="region"`/label relationship, preserve mobile layout, and use View Transitions or opacity/transform-only transitions. Display dimension labels from the fixed nine-dimension list and avoid displaying blank sections.

- [ ] **Step 4: Run focused UI tests and confirm pass.**

Run: `npm test -- src/app/page.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit.**

```bash
git add src/app/page.tsx src/app/page.test.tsx
git commit -m "feat: expand interview feedback details"
```

### Task 5: Add Progress insights UI and documentation

**Files:**
- Modify: `src/app/page.tsx` (Progress view)
- Modify: `README.md` (feature/setup notes if needed)
- Modify: `docs/prd-gap-analysis.md` (mark this slice addressed)
- Test: `src/app/page.test.tsx`

**Interfaces:**
- Progress view consumes `ProgressSnapshot` from Task 3.
- No-evidence state shows “Not enough data yet”; one-session state shows “Baseline established”.
- Evidence state renders readiness, latest score, trend, strongest/weakest competency, and recurring weaknesses.

- [ ] **Step 1: Write failing Progress UI tests.**

Cover no evidence, baseline, and multi-session evidence states; assert readiness copy says it is a coaching signal and not a hiring prediction.

- [ ] **Step 2: Run the focused test and confirm failure.**

Run: `npm test -- src/app/page.test.tsx`

Expected: FAIL because the current Progress view shows only readiness and competency bars.

- [ ] **Step 3: Implement the insights panels.**

Replace client-only readiness math with the server snapshot, add recent score/trend and recurring weakness panels, and preserve active competency bars. Keep explicit null/empty handling and existing transitions.

- [ ] **Step 4: Update docs and run the full verification suite.**

Run: `npm test && npm run lint && npx next build --webpack && git diff --check`

Expected: all tests pass, lint/build succeed, and no whitespace errors remain. Update the gap analysis to state that richer per-question feedback and readiness/progress insights are addressed.

- [ ] **Step 5: Commit.**

```bash
git add src/app/page.tsx src/app/page.test.tsx README.md docs/prd-gap-analysis.md
git commit -m "feat: add readiness and progress insights"
```

## Completion checklist

- [ ] Apply `202608290003_richer_feedback.sql` to a disposable Supabase project and verify old evaluation rows still hydrate.
- [ ] Run the full local verification commands from the global constraints.
- [ ] Test Google-authenticated Results and Progress flows in the deployed Vercel environment.

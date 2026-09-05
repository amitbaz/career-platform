# Tech Profile-Grounded Interviews Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace generic software-engineering interview behavior with evidence-backed profile extraction, coherent question blueprints, and rubric-grounded evaluation.

**Architecture:** Add a server-owned evidence layer and deterministic meaningful-profile gate, then generate and persist a validated five-question blueprint before a session starts. Each answer is evaluated against its persisted objective and expected signals; client screens render stored facts and scores without business-rule duplication.

**Tech Stack:** Next.js 16.3.3 App Router, TypeScript, React 19, Supabase Postgres/RPC, Zod, Gemini, Vitest, Testing Library.

**Spec:** `docs/superpowers/specs/2026-08-29-tech-profile-grounded-interviews-design.md`

## Global Constraints

- Support software-engineering roles only in this milestone; do not add job-description targeting.
- Require two concrete engineering projects/work examples, identifiable technologies, and responsibilities or outcomes before personalized interviews.
- Preserve source excerpts and never invent missing facts.
- Persist a five-question blueprint with evidence references, objectives, expected signals, rubrics, and follow-up limits before the first question.
- Evaluate exact-question relevance, supported claims, missing signals, unsupported claims, and dimension justifications.
- Deterministic fallback must not use answer length as a competence proxy or repeat a fixed score.
- Keep AI calls in server-only modules with bounded timeouts, structured validation, safe telemetry, and user-scoped transactional persistence.
- Preserve legacy hydration and use two-space TypeScript formatting, public-code documentation, and transform/opacity-only UI transitions.
- Run `npm test`, `npm run lint`, `npx next build --webpack`, and `git diff --check` before completion.

---

### Task 1: Add evidence extraction and profile quality gate

**Files:**
- Modify: `src/lib/types.ts` (evidence and profile readiness types)
- Modify: `src/lib/coach.ts` (validated evidence extraction)
- Modify: `src/lib/repositories/profile.ts` (persist evidence and readiness state)
- Modify: `src/app/api/profile/route.ts` (reject unusable profiles)
- Create: `supabase/migrations/202608290005_profile_evidence.sql`
- Test: `src/lib/coach.test.ts`
- Test: `src/lib/repositories/profile.test.ts`
- Test: `src/app/api/profile/route.test.ts`

**Interfaces:**
- `EvidenceItem` stores source excerpt, project/employer, ownership, technologies, decision, constraint, outcome, recency, and confidence.
- `ProfileReadiness` returns `{ ready: boolean; missing: string[] }`.
- `extractEngineeringEvidence(cvText: string, coverLetter: string): Promise<EvidenceItem[]>` validates Gemini JSON and preserves nulls.
- `assessProfileReadiness(evidence: EvidenceItem[]): ProfileReadiness` is deterministic.

- [ ] **Step 1: Write failing extraction and gate tests.** Assert source excerpts survive parsing, missing fields remain null, empty/generic text is rejected, and two concrete projects plus technology and responsibility/outcome evidence pass.
- [ ] **Step 2: Run focused tests and confirm failure.** `npm test -- src/lib/coach.test.ts src/lib/repositories/profile.test.ts src/app/api/profile/route.test.ts` should fail because the evidence contract and gate do not exist.
- [ ] **Step 3: Implement schema-validated extraction and persistence.** Add Zod schemas, deterministic gate, additive Supabase tables/RPC payloads, and actionable API errors; keep existing profile fields and legacy rows readable.
- [ ] **Step 4: Run focused tests and confirm pass.** Re-run the command from Step 2 and verify all new assertions pass.
- [ ] **Step 5: Commit.** `git add src/lib/types.ts src/lib/coach.ts src/lib/repositories/profile.ts src/app/api/profile/route.ts supabase/migrations/202608290005_profile_evidence.sql src/lib/coach.test.ts src/lib/repositories/profile.test.ts src/app/api/profile/route.test.ts && git commit -m "feat: extract grounded engineering evidence"`

### Task 2: Build and persist the interview blueprint

**Files:**
- Modify: `src/lib/types.ts` (blueprint and rubric types)
- Modify: `src/lib/interview-planner.ts` (blueprint validation and deterministic fallback)
- Modify: `src/lib/coach.ts` (Gemini blueprint generation)
- Modify: `src/lib/repositories/interviews.ts` (blueprint persistence/hydration)
- Modify: `supabase/migrations/202608290005_profile_evidence.sql` or create `202608290006_interview_blueprints.sql`
- Test: `src/lib/interview-planner.test.ts`
- Test: `src/lib/coach.test.ts`
- Test: `src/lib/repositories/interviews.test.ts`

**Interfaces:**
- `InterviewBlueprint` contains five `BlueprintQuestion` entries and follow-up limits.
- `generateInterviewBlueprint(profile: Profile, evidence: EvidenceItem[]): Promise<InterviewBlueprint>` returns a validated blueprint.
- `validateInterviewBlueprint(blueprint, evidence)` rejects unknown evidence IDs, missing objectives, and invalid question counts.

- [ ] **Step 1: Write failing blueprint tests.** Cover five-question shape, existing evidence references, objective preservation, malformed-model repair, deterministic fallback, and maximum three follow-ups/eight total questions.
- [ ] **Step 2: Run `npm test -- src/lib/interview-planner.test.ts src/lib/coach.test.ts src/lib/repositories/interviews.test.ts` and confirm failure.**
- [ ] **Step 3: Implement validated generation and additive persistence.** Generate once before session creation, retry malformed JSON once, then mark fallback sessions `limited-grounding`; never change objective/evidence target during later turns.
- [ ] **Step 4: Re-run focused tests and confirm pass.**
- [ ] **Step 5: Commit.** `git add src/lib/types.ts src/lib/interview-planner.ts src/lib/coach.ts src/lib/repositories/interviews.ts src/lib/interview-planner.test.ts src/lib/coach.test.ts src/lib/repositories/interviews.test.ts supabase/migrations && git commit -m "feat: persist coherent interview blueprints"`

### Task 3: Replace generic answer evaluation with rubric grounding

**Files:**
- Modify: `src/lib/types.ts` (evaluation evidence fields)
- Modify: `src/lib/coach.ts` (question-rubric evaluator and deterministic fallback)
- Modify: `src/lib/repositories/interviews.ts` (persist supported/missing/unsupported claims)
- Create: `supabase/migrations/202608290007_grounded_evaluations.sql`
- Test: `src/lib/coach.test.ts`
- Test: `src/lib/repositories/interviews.test.ts`

**Interfaces:**
- `GroundedEvaluation` extends `Evaluation` with `relevance`, `supportedClaims`, `expectedSignalsPresent`, `unsupportedClaims`, and `dimensionReasons`.
- `evaluateAnswer(question, blueprint, profile, answer, transcript): Promise<GroundedEvaluation>` scores the exact question.

- [ ] **Step 1: Write failing tests** for unrelated answers, supported profile facts, unsupported claims, materially different answer scores, and complete dimension reasons.
- [ ] **Step 2: Run focused tests and confirm failure.**
- [ ] **Step 3: Implement structured evaluator and fallback.** Require all rubric fields in Zod, ground improved-answer outlines in evidence, and make fallback relevance/completeness checks explicit rather than length-based.
- [ ] **Step 4: Re-run focused tests and confirm pass.**
- [ ] **Step 5: Commit.** `git add src/lib/types.ts src/lib/coach.ts src/lib/repositories/interviews.ts src/lib/coach.test.ts src/lib/repositories/interviews.test.ts supabase/migrations/202608290007_grounded_evaluations.sql && git commit -m "feat: ground interview evaluation in rubrics"`

### Task 4: Wire blueprint and grounded evaluation through the interview API

**Files:**
- Modify: `src/app/api/interview/route.ts`
- Modify: `src/app/api/interview/route.test.ts`
- Modify: `src/app/page.tsx`
- Test: `src/app/api/interview/route.test.ts`

**Interfaces:**
- Authenticated `POST { action: "start" }` rejects profiles failing `ProfileReadiness`, generates/persists a blueprint, and returns its first question.
- Authenticated `POST { action: "respond" }` evaluates against the persisted blueprint question and preserves its objective/evidence target.

- [ ] **Step 1: Write failing route tests** for profile-gate rejection, blueprint-backed first question, exact-question evaluation input, and safe provider failure handling.
- [ ] **Step 2: Run `npm test -- src/app/api/interview/route.test.ts` and confirm failure.**
- [ ] **Step 3: Implement server orchestration.** Keep all profile/evidence/blueprint reads user-scoped and keep the client as a renderer of server decisions.
- [ ] **Step 4: Re-run route tests and confirm pass.**
- [ ] **Step 5: Commit.** `git add src/app/api/interview/route.ts src/app/api/interview/route.test.ts src/app/page.tsx && git commit -m "feat: run grounded interview sessions"`

### Task 5: Update profile, Results, and Progress UX

**Files:**
- Modify: `src/app/page.tsx`
- Modify: `src/app/results-feedback-cards.tsx`
- Modify: `README.md`
- Modify: `docs/prd-gap-analysis.md`
- Test: `src/app/page.test.tsx`

- [ ] **Step 1: Write failing UI tests** for actionable profile-gate errors, evidence-backed question labels, unrelated-answer feedback, limited-grounding legacy sessions, and preserved readiness states.
- [ ] **Step 2: Run `npm test -- src/app/page.test.tsx` and confirm failure.**
- [ ] **Step 3: Implement UI changes.** Show source-grounding metadata, exact relevance/missing/unsupported feedback, and clear no-fabrication copy; preserve accessible disclosures and existing transition constraints.
- [ ] **Step 4: Update README and gap analysis** to describe the software-engineering scope and mark only this slice addressed; retain the deferred web-research and executable-workspace commitments.
- [ ] **Step 5: Run full verification.** `npm test && npm run lint && npx next build --webpack && git diff --check`.
- [ ] **Step 6: Commit.** `git add src/app/page.tsx src/app/results-feedback-cards.tsx src/app/page.test.tsx README.md docs/prd-gap-analysis.md && git commit -m "feat: show grounded interview coaching"`

### Task 6: Real-environment acceptance

- [ ] Apply all new migrations to the linked disposable Supabase project with `supabase db push --include-all`.
- [ ] Verify old profile/session rows hydrate and a profile with insufficient evidence cannot start a personalized session.
- [ ] Verify an authenticated deployed session asks profile-grounded questions, produces different scores for unrelated versus relevant answers, and ties feedback to the exact question.
- [ ] Record any provider/model limitation as an actionable issue; do not claim completion without the deployed flow evidence.


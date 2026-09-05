# Career Brain Release 2 Relay Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recenter Relay around an explainable recommended-practice command center, manual application/story/coach-memory management, and plan-driven short practice sessions while preserving the existing generic interview flow and leaving the job-hunter untouched.

**Architecture:** Keep the existing authenticated client shell, but split the large `page.tsx` into focused views and typed API calls. Add a deterministic baseline recommendation service and server-side Career Brain read/write APIs. Add separate transactional plan-driven conversation and hands-on session starts instead of weakening the existing generic interview contracts.

**Tech Stack:** Next.js 16.3.3, React 19.2.8, TypeScript 5, Tailwind CSS 4, Supabase Postgres/Auth/RLS, `@supabase/supabase-js` 2.x, Vitest 4.1.11, existing Gemini provider and coach code.

**Spec:** `docs/superpowers/specs/2026-08-31-career-brain-release-2-relay-rework-design.md`

## Global Constraints

- Home's primary action is `Start recommended practice`.
- Recommendation selection is deterministic and never calls an LLM.
- Release 2 does not automatically create or reconcile coach observations.
- Existing generic conversation keeps the exact five-question backbone.
- Plan-driven conversation supports 1–5 base questions.
- A planned conversation cannot be explicitly completed while any currently persisted question, including a follow-up, is unanswered.
- Candidate facts remain grounded in candidate evidence; job descriptions shape questions but do not prove candidate experience.
- All Career Brain APIs use `requireUser()` and server-only repositories; browser code never supplies a trusted `userId`.
- Started/completed plan context is immutable in normal UI flows.
- Interview evidence survives post-session practice-plan bookkeeping failures.
- No Google Sheet synchronization/import and no job-hunter changes.
- Follow existing mobile-first View Transition and animation rules.
- Follow red → green → refactor and make one scoped commit per task.
- Verify migrations on disposable/development Supabase before production.

## File Structure

Create:

```text
supabase/migrations/
  202608310001_planned_practice_sessions.sql

src/lib/
  practice-recommendation.ts
  practice-recommendation.test.ts
  career-dashboard.ts
  career-dashboard.test.ts
  coach-memory.ts
  coach-memory.test.ts
  career-story.ts
  career-story.test.ts
  practice-service.ts
  practice-service.test.ts
  release2-flow.test.ts

src/app/api/
  career/dashboard/route.ts
  career/dashboard/route.test.ts
  opportunities/route.ts
  opportunities/route.test.ts
  stories/route.ts
  stories/route.test.ts
  observations/route.ts
  observations/route.test.ts
  practice/route.ts
  practice/route.test.ts
  interview/route.test.ts

src/app/
  api-client.ts
  relay-shell.tsx
  views/
    home-view.tsx
    home-view.test.tsx
    applications-view.tsx
    applications-view.test.tsx
    practice-view.tsx
    practice-view.test.tsx
    stories-view.tsx
    stories-view.test.tsx
    coach-view.tsx
    coach-view.test.tsx
```

Modify:

```text
src/lib/types.ts
src/lib/coach.ts
src/lib/coach.test.ts
src/lib/repositories/interviews.ts
src/lib/repositories/interviews.test.ts
src/lib/repositories/opportunities.ts
src/lib/repositories/opportunities.test.ts
src/lib/repositories/practice-plans.ts
src/app/api/interview/route.ts
src/app/page.tsx
src/app/page.test.tsx
README.md
```

Existing Profile, Progress, Interview, and Results JSX may remain in `relay-shell.tsx` if extracting it is unrelated churn. New Release 2 views must not be added to the old `page.tsx` monolith.

---

### Task 1: Deterministic baseline recommendation engine

**Files:**
- Create: `src/lib/practice-recommendation.ts`
- Create: `src/lib/practice-recommendation.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**

```ts
export type PracticeRecommendationSignal = {
  kind:
    | "upcoming_interview"
    | "interviewing_opportunity"
    | "reviewed_observation"
    | "story_bank_gap"
    | "progress_weakness"
    | "applied_opportunity"
    | "first_practice"
    | "fallback";
  label: string;
  detail: string;
};

export type PracticeRecommendation = {
  format: PracticeFormat;
  primaryFocus: string;
  secondaryFocus: string | null;
  rationale: string;
  estimatedMinutes: number;
  successCriteria: string[];
  primaryOpportunityId: string | null;
  supportingOpportunityIds: string[];
  signals: PracticeRecommendationSignal[];
};

export type PracticeRecommendationInput = {
  opportunities: Opportunity[];
  observations: CoachObservation[];
  stories: CareerStory[];
  progress: ProgressSnapshot;
  recentSessions: InterviewSession[];
  recentPlans: PracticePlan[];
  now: Date;
};

export function recommendPractice(input: PracticeRecommendationInput): PracticeRecommendation;
```

- [ ] **Step 1: Write RED precedence tests**

Add fixed fixtures and cover all required precedence branches. Include these assertions:

```ts
it("prioritizes an interview in three days over a generic weakness", () => {
  const result = recommendPractice({
    ...baseInput,
    now: new Date("2026-08-31T08:00:00Z"),
    opportunities: [{
      ...opportunity,
      status: "interviewing",
      nextInterviewAt: "2026-09-03T10:00:00Z",
    }],
    progress: { ...progress, recurringWeaknesses: ["Architecture framing"] },
  });
  expect(result).toMatchObject({ format: "role_prep", primaryOpportunityId: opportunity.id });
});

it("uses corrected observation text", () => {
  const result = recommendPractice({
    ...baseInput,
    observations: [{
      ...observation,
      reviewState: "corrected",
      importance: 0.9,
      userCorrection: "Make ownership explicit.",
    }],
  });
  expect(result.primaryFocus).toContain("Make ownership explicit");
});
```

Also test: any interviewing opportunity; confirmed high-importance observation; dismissed observation ignored; unreviewed observation ignored; applied/interviewing plus zero confirmed stories; weakest/recurring progress signal; applied opportunity; first-practice fallback; full-simulation fallback; offer/rejected/withdrawn/closed do not create urgency.

- [ ] **Step 2: Run RED**

```bash
npm test -- src/lib/practice-recommendation.test.ts
```

Expected: FAIL because the recommendation types/function do not exist.

- [ ] **Step 3: Add the DTO types above to `types.ts`**

Keep them non-persisted read models.

- [ ] **Step 4: Implement the pure selector**

Use explicit helpers:

```ts
const terminalStatuses = new Set<OpportunityStatus>(["offer", "rejected", "withdrawn", "closed"]);

function effectiveObservationText(item: CoachObservation): string {
  return item.reviewState === "corrected" && item.userCorrection?.trim()
    ? item.userCorrection.trim()
    : item.claim.trim();
}
```

Apply this exact precedence:

```text
1 future interview within 7 days
2 any interviewing opportunity
3 confirmed/corrected observation with importance >= 0.6
4 applied/interviewing opportunity + zero confirmed stories
5 weakest/recurring ProgressSnapshot signal
6 applied opportunity
7 zero completed sessions
8 full_simulation fallback
```

Use explicit `now`, deterministic sorting, and no randomness.

- [ ] **Step 5: Run GREEN**

```bash
npm test -- src/lib/practice-recommendation.test.ts
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lib/types.ts src/lib/practice-recommendation.ts src/lib/practice-recommendation.test.ts
git commit -m "feat: add baseline practice recommendation"
```

---

### Task 2: Transactional plan-driven conversation and hands-on session starts

**Files:**
- Create: `supabase/migrations/202608310001_planned_practice_sessions.sql`
- Modify: `src/lib/repositories/interviews.ts`
- Modify: `src/lib/repositories/interviews.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**

```ts
export type PracticeSessionContext = {
  practicePlanId: string;
  opportunityId: string | null;
};

export function assertPracticeConversationBlueprint(blueprint: InterviewBlueprint): void;

export async function createSessionWithPracticeBlueprint(
  supabase: SupabaseClient,
  userId: string,
  blueprint: InterviewBlueprint,
  context: PracticeSessionContext,
): Promise<InterviewSession>;

export async function createHandsOnPracticeSession(
  supabase: SupabaseClient,
  userId: string,
  exercise: HandsOnExercise,
  context: PracticeSessionContext,
): Promise<InterviewSession>;
```

Keep `createSessionWithBlueprint(...)` and `createHandsOnSession(...)` unchanged for generic/manual flows.

- [ ] **Step 1: Write RED repository tests**

```ts
it("accepts three planned base questions but generic backbone still rejects them", () => {
  expect(() => assertPracticeConversationBlueprint(practiceBlueprint(3))).not.toThrow();
  expect(() => assertConversationPlan(practiceBlueprint(3).questions)).toThrow();
});

it("calls planned conversation RPC with context", async () => {
  await createSessionWithPracticeBlueprint(supabase as never, "user-1", practiceBlueprint(3), {
    practicePlanId: "plan-1",
    opportunityId: "opp-1",
  });
  expect(rpc).toHaveBeenCalledWith(
    "create_planned_conversation_session_with_blueprint",
    expect.objectContaining({ p_practice_plan_id: "plan-1", p_opportunity_id: "opp-1" }),
  );
});

it("calls planned hands-on RPC with context", async () => {
  await createHandsOnPracticeSession(supabase as never, "user-1", exercise, {
    practicePlanId: "plan-1",
    opportunityId: null,
  });
  expect(rpc).toHaveBeenCalledWith(
    "start_hands_on_practice_session",
    expect.objectContaining({ p_practice_plan_id: "plan-1", p_exercise: exercise }),
  );
});
```

- [ ] **Step 2: Run RED**

```bash
npm test -- src/lib/repositories/interviews.test.ts
```

Expected: FAIL on the new interfaces.

- [ ] **Step 3: Update blueprint documentation and add `PracticeSessionContext`**

Document `InterviewBlueprint` as reusable: generic interviews use five base questions, planned practice may use 1–5. Do not rename it.

- [ ] **Step 4: Implement TypeScript validation/wrappers**

```ts
if (blueprint.questions.length < 1 || blueprint.questions.length > 5) {
  throw new RepositoryError("Planned practice must contain between one and five base questions.", "INVALID_PLAN");
}
blueprint.questions.forEach((question, index) => {
  if (question.sequence !== index + 1 || question.isFollowUp) {
    throw new RepositoryError("Planned practice questions must be contiguous base questions.", "INVALID_PLAN");
  }
});
```

Map the same persisted blueprint fields already used by the generic RPC.

- [ ] **Step 5: Create migration with both start RPCs**

Widen only the session metadata constraint:

```sql
alter table public.interview_sessions
  drop constraint if exists interview_sessions_blueprint_max_questions_check;

alter table public.interview_sessions
  add constraint interview_sessions_blueprint_max_questions_check
  check (blueprint_max_questions between 1 and 8);
```

Do not modify `create_conversation_session_with_blueprint`; it must still enforce its exact five-question backbone.

Create `create_planned_conversation_session_with_blueprint(p_blueprint jsonb, p_practice_plan_id uuid, p_opportunity_id uuid default null)` and `start_hands_on_practice_session(p_practice_plan_id uuid, p_opportunity_id uuid, p_exercise jsonb)`.

Both functions must:

```text
security invoker
derive auth.uid()
lock the owned ready PracticePlan FOR UPDATE
verify optional opportunity ownership
verify the opportunity is linked to the plan
honor any primary opportunity designation
create the session with practice_plan_id and opportunity_id
set the plan status to started in the same transaction
return session_id
reject a second start of the same plan
```

Conversation RPC additionally validates 1–5 contiguous base questions, existing category/difficulty constraints, and `blueprint_max_questions` 1–8. Hands-on RPC requires a JSON object exercise and stores it unchanged on a `hands-on` session.

- [ ] **Step 6: Verify SQL invariants on disposable Supabase**

```bash
supabase db push
```

Verify: old generic RPC rejects three questions; new conversation RPC accepts 1/3/5 and rejects 0/6; both new RPCs reject cross-user/mismatched context; both atomically set session context and plan status; repeat start fails.

- [ ] **Step 7: Run GREEN**

```bash
npm test -- src/lib/repositories/interviews.test.ts
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add supabase/migrations/202608310001_planned_practice_sessions.sql src/lib/types.ts src/lib/repositories/interviews.ts src/lib/repositories/interviews.test.ts
git commit -m "feat: add planned practice session starts"
```

---

### Task 3: Practice-plan-specific blueprint generation

**Files:**
- Modify: `src/lib/coach.ts`
- Modify: `src/lib/coach.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**

```ts
export type PracticeBlueprintContext = {
  primaryOpportunity: Opportunity | null;
  supportingOpportunities: Opportunity[];
  observations: CoachObservation[];
  stories: CareerStory[];
};

export async function generatePracticeBlueprint(
  profile: Profile,
  evidence: EvidenceItem[],
  plan: PracticePlan,
  context: PracticeBlueprintContext,
): Promise<InterviewBlueprint>;
```

- [ ] **Step 1: Write RED question-count tests**

```ts
it.each([
  ["targeted_drill", 3],
  ["story_work", 3],
  ["self_presentation", 2],
  ["behavioral", 3],
  ["technical_communication", 3],
  ["role_prep", 4],
  ["full_simulation", 5],
] as const)("generates %s with %d base questions", async (format, count) => {
  const blueprint = await generatePracticeBlueprint(profile, evidence, { ...plan, format }, context);
  expect(blueprint.questions).toHaveLength(count);
});
```

Add a role-prep test proving job-description requirements may shape prompts while evidence IDs still come from candidate evidence.

- [ ] **Step 2: Run RED**

```bash
npm test -- src/lib/coach.test.ts
```

- [ ] **Step 3: Implement exact base counts**

```ts
function baseQuestionCountFor(format: PracticeFormat): number {
  switch (format) {
    case "self_presentation": return 2;
    case "role_prep": return 4;
    case "full_simulation": return 5;
    case "hands_on": return 0;
    default: return 3;
  }
}
```

`hands_on` never calls this generator.

- [ ] **Step 4: Implement grounded practice generation**

Reuse existing structured provider conventions. Include plan focus, format, success criteria, exact question count, primary/supporting job context, reviewed effective observations, confirmed stories, and candidate profile evidence.

The prompt contract must state:

```text
Job requirements are targets to probe, not candidate evidence.
Candidate factual claims must be grounded in supplied evidence or confirmed story facts.
Do not invent company interview-process facts.
```

- [ ] **Step 5: Test `nextTurn` with a 2- and 3-question blueprint**

Remove only any hidden fixed-five assumption; preserve evaluation and follow-up rubric behavior.

- [ ] **Step 6: Run GREEN**

```bash
npm test -- src/lib/coach.test.ts
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/lib/types.ts src/lib/coach.ts src/lib/coach.test.ts
git commit -m "feat: generate plan-specific practice blueprints"
```

---

### Task 4: Practice orchestration and `/api/practice`

**Files:**
- Create: `src/lib/practice-service.ts`
- Create: `src/lib/practice-service.test.ts`
- Create: `src/app/api/practice/route.ts`
- Create: `src/app/api/practice/route.test.ts`
- Create: `src/app/api/interview/route.test.ts`
- Modify: `src/app/api/interview/route.ts`
- Modify: `src/lib/repositories/practice-plans.ts`

**Interfaces:**

```ts
export type ManualPracticeRequest = {
  format: PracticeFormat;
  primaryFocus: string;
  secondaryFocus?: string | null;
  estimatedMinutes?: number | null;
  successCriteria?: string[];
  primaryOpportunityId?: string | null;
};

export async function startRecommendedPractice(
  supabase: SupabaseClient,
  userId: string,
  now: Date,
): Promise<{ plan: PracticePlan; session: InterviewSession }>;

export async function startManualPractice(
  supabase: SupabaseClient,
  userId: string,
  request: ManualPracticeRequest,
): Promise<{ plan: PracticePlan; session: InterviewSession }>;

export async function completeLinkedPracticePlanBestEffort(
  supabase: SupabaseClient,
  userId: string,
  session: InterviewSession,
): Promise<{ warning: string | null }>;
```

- [ ] **Step 1: Write RED orchestration tests**

```ts
it("recomputes recommended practice on the server", async () => {
  await startRecommendedPractice(supabase as never, "user-1", now);
  expect(recommendPracticeMock).toHaveBeenCalled();
  expect(createPracticePlanMock).toHaveBeenCalledWith(
    expect.anything(),
    "user-1",
    expect.objectContaining({ status: "ready" }),
  );
});

it("dispatches hands-on through the transactional planned hands-on wrapper", async () => {
  await startManualPractice(supabase as never, "user-1", {
    format: "hands_on",
    primaryFocus: "React implementation",
  });
  expect(createHandsOnPracticeSessionMock).toHaveBeenCalled();
});

it("keeps interview completion successful when plan completion fails", async () => {
  updatePracticePlanMock.mockRejectedValue(new Error("failed"));
  await expect(completeLinkedPracticePlanBestEffort(supabase as never, "user-1", completedSession))
    .resolves.toEqual({ warning: expect.any(String) });
});
```

- [ ] **Step 2: Run RED**

```bash
npm test -- src/lib/practice-service.test.ts
```

- [ ] **Step 3: Implement context loading and ready plan creation**

Load profile, sessions/progress, opportunities, observations, stories, and recent plans. `startRecommendedPractice` calls `recommendPractice`; manual start validates user input. Both create one `ready` `PracticePlan` and set primary/supporting opportunity links before session creation.

- [ ] **Step 4: Dispatch delivery through Task 2 interfaces**

```text
non-hands-on -> generatePracticeBlueprint -> createSessionWithPracticeBlueprint
hands-on     -> handsOnExercise(profile) -> createHandsOnPracticeSession
```

The transactional session RPC changes the plan to `started`. Reload the plan and return `{ plan, session }`.

- [ ] **Step 5: Mark pre-session failures safely**

If a ready plan exists but generation/session start fails before a session exists:

```ts
await updatePracticePlan(supabase, userId, plan.id, {
  status: "failed",
  generationError: userSafeGenerationFailure(error),
});
throw error;
```

- [ ] **Step 6: Make linked-plan completion best-effort**

After current interview completion has successfully saved the completed session/evidence, call:

```ts
const { warning } = await completeLinkedPracticePlanBestEffort(supabase, user.id, completed);
```

Return HTTP 200 with the completed session even if this produces `practicePlanWarning`; log the bookkeeping error.

- [ ] **Step 7: Make explicit conversation completion plan-aware**

Use exactly this rule:

```ts
export function canExplicitlyCompleteConversation(session: InterviewSession): boolean {
  if (!session.practicePlanId) {
    return session.questions.filter((question) => question.answer).length >= 5;
  }
  return session.questions.every((question) => Boolean(question.answer));
}
```

For planned practice, this deliberately includes persisted follow-up questions. If a follow-up exists and is unanswered, explicit completion is rejected. Generic/manual behavior remains the existing five-answer rule.

- [ ] **Step 8: Implement `/api/practice`**

GET authenticates and returns current recommendation plus recent plans. POST accepts only:

```json
{ "action": "start_recommended" }
```

or validated manual fields:

```json
{
  "action": "start_manual",
  "format": "targeted_drill",
  "primaryFocus": "Architecture decision framing",
  "estimatedMinutes": 12,
  "primaryOpportunityId": null
}
```

Never trust a browser-supplied recommendation object.

- [ ] **Step 9: Add interview-route tests for changed completion branches**

Add these exact cases:

```text
planned 3 base questions all answered, no follow-up -> allowed
planned 3 base questions answered + persisted unanswered follow-up -> rejected
same planned session after follow-up answer -> allowed
generic conversation with fewer than 5 answers -> rejected
generic conversation with at least 5 answers -> allowed
plan-completion bookkeeping error after session completion -> HTTP 200 + warning
```

- [ ] **Step 10: Run GREEN**

```bash
npm test -- src/lib/practice-service.test.ts src/app/api/practice/route.test.ts src/app/api/interview/route.test.ts
```

Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add src/lib/practice-service.ts src/lib/practice-service.test.ts src/lib/repositories/practice-plans.ts src/app/api/practice src/app/api/interview/route.ts src/app/api/interview/route.test.ts
git commit -m "feat: orchestrate persisted recommended practice"
```

---

### Task 5: Career Dashboard and coach-memory evidence read models

**Files:**
- Create: `src/lib/coach-memory.ts`
- Create: `src/lib/coach-memory.test.ts`
- Create: `src/lib/career-dashboard.ts`
- Create: `src/lib/career-dashboard.test.ts`
- Create: `src/app/api/career/dashboard/route.ts`
- Create: `src/app/api/career/dashboard/route.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**

```ts
export type CoachEvidenceDisplay = {
  kind: "profile_evidence" | "question_evaluation" | "career_story" | "opportunity_event";
  label: string;
  summary: string;
  role: ObservationEvidenceRole;
  reason: string | null;
};

export type CoachObservationSummary = CoachObservation & {
  effectiveText: string;
  evidence: CoachEvidenceDisplay[];
};

export type CareerStorySummary = CareerStory & {
  evidenceCount: number;
};

export type CareerDashboard = {
  profile: Profile;
  coachMode: "demo" | "live";
  progress: ProgressSnapshot;
  recentSessions: InterviewSession[];
  opportunities: Opportunity[];
  upcomingOpportunities: Opportunity[];
  observations: CoachObservationSummary[];
  stories: CareerStorySummary[];
  recentPracticePlans: PracticePlan[];
  recommendation: PracticeRecommendation;
};

export async function loadCareerDashboard(
  supabase: SupabaseClient,
  userId: string,
  now: Date,
  coachMode: "demo" | "live",
): Promise<CareerDashboard>;
```

- [ ] **Step 1: Write RED evidence-resolution tests**

Resolve each typed evidence source to a user-safe label/summary. Assert corrected observations expose `effectiveText = userCorrection`; original `claim` remains available separately.

- [ ] **Step 2: Implement `coach-memory.ts`**

Resolve only owned source rows. Return concise profile excerpt, question/evaluation, story title, or opportunity-event display. Never require browser-side joins or raw UUID-only display.

- [ ] **Step 3: Write RED dashboard tests**

```ts
const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");
expect(dashboard.coachMode).toBe("demo");
expect(dashboard.recommendation).toBeDefined();
expect(dashboard.observations.every((item) => item.reviewState !== "dismissed")).toBe(true);
```

- [ ] **Step 4: Implement dashboard aggregation**

Load repositories in parallel where safe; calculate progress from completed sessions; resolve active observations; sort future interviews; call `recommendPractice` with explicit `now`.

- [ ] **Step 5: Implement `GET /api/career/dashboard`**

```ts
const coachMode = process.env.GEMINI_API_KEY ? "live" : "demo";
```

Authenticate with `requireUser()`, call `loadCareerDashboard`, and return one canonical dashboard payload.

- [ ] **Step 6: Run GREEN**

```bash
npm test -- src/lib/coach-memory.test.ts src/lib/career-dashboard.test.ts src/app/api/career/dashboard/route.test.ts
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/lib/types.ts src/lib/coach-memory.ts src/lib/coach-memory.test.ts src/lib/career-dashboard.ts src/lib/career-dashboard.test.ts src/app/api/career/dashboard
git commit -m "feat: add Career Brain dashboard read model"
```

---

### Task 6: Application lifecycle API

**Files:**
- Create: `src/app/api/opportunities/route.ts`
- Create: `src/app/api/opportunities/route.test.ts`
- Modify: `src/lib/repositories/opportunities.ts`
- Modify: `src/lib/repositories/opportunities.test.ts`

**Interfaces:**

```ts
export async function addOpportunityNote(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
  note: string,
): Promise<OpportunityEvent>;
```

GET returns `{ opportunities: Opportunity[] }`. POST actions are `create`, `update`, `transition`, `schedule_interview`, and `add_note`.

- [ ] **Step 1: Write RED route/repository tests**

Test that `initialStatus: "applied"` calls `createOpportunity` then `transitionOpportunity`, never a direct lifecycle-column update. Test scheduling uses `scheduleOpportunityInterview`. Test `addOpportunityNote` creates one owned append-only `note` event.

- [ ] **Step 2: Run RED**

```bash
npm test -- src/app/api/opportunities/route.test.ts src/lib/repositories/opportunities.test.ts
```

- [ ] **Step 3: Implement `addOpportunityNote`**

Insert exactly one event row:

```ts
{
  user_id: userId,
  opportunity_id: opportunityId,
  event_type: "note",
  note: note.trim(),
  metadata: {},
}
```

Reject blank notes. Do not expose event update/delete.

- [ ] **Step 4: Implement explicit request parsing**

Validate required/optional strings, status union, 0–100 match score, and a valid future ISO interview timestamp. Do not cast raw request JSON to domain input.

- [ ] **Step 5: Implement GET/POST**

All lifecycle actions call Release 1 repository APIs; no direct status or `next_interview_at` writes.

- [ ] **Step 6: Run GREEN**

```bash
npm test -- src/app/api/opportunities/route.test.ts src/lib/repositories/opportunities.test.ts
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/app/api/opportunities src/lib/repositories/opportunities.ts src/lib/repositories/opportunities.test.ts
git commit -m "feat: expose application lifecycle API"
```

---

### Task 7: Story and coach-memory review APIs plus deterministic story completeness

**Files:**
- Create: `src/lib/career-story.ts`
- Create: `src/lib/career-story.test.ts`
- Create: `src/app/api/stories/route.ts`
- Create: `src/app/api/stories/route.test.ts`
- Create: `src/app/api/observations/route.ts`
- Create: `src/app/api/observations/route.test.ts`
- Modify: `src/lib/types.ts`

**Interfaces:**

```ts
export type CareerStoryDraftFields = Pick<
  CareerStory,
  "situation" | "responsibility" | "problem" | "actions" | "alternatives" |
  "tradeoffs" | "ownership" | "outcome" | "lessons"
>;

export function careerStoryCompleteness(story: CareerStoryDraftFields): number;
```

- [ ] **Step 1: Write RED completeness tests**

Use six factual dimensions:

```text
context/problem
responsibility/ownership
actions/decisions
tradeoff/alternative
outcome
lesson/reflection
```

Return `coveredDimensions / 6`; never score delivery/style.

- [ ] **Step 2: Implement completeness helper and run GREEN**

```bash
npm test -- src/lib/career-story.test.ts
```

Expected: PASS.

- [ ] **Step 3: Write RED Story route tests**

Actions: `create`, `update`, `confirm`, `retire`, `attach_profile_evidence`. Server computes completeness and ignores browser-supplied completeness. Confirm sets `reviewState = "confirmed"` plus a server timestamp. Retire preserves row/provenance.

- [ ] **Step 4: Implement Story GET/POST**

Use existing story repositories and typed evidence source `{ kind: "profile_evidence", profileEvidenceId }`.

- [ ] **Step 5: Write RED Observation route tests**

Only allow `confirm`, `correct`, `dismiss`. `correct` requires non-empty replacement text. There is no normal create action.

- [ ] **Step 6: Implement Observation GET/POST**

GET returns active/history read models with resolved evidence from `coach-memory.ts`; POST calls `reviewCoachObservation` typed reviews.

- [ ] **Step 7: Run GREEN**

```bash
npm test -- src/lib/career-story.test.ts src/app/api/stories/route.test.ts src/app/api/observations/route.test.ts
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/lib/career-story.ts src/lib/career-story.test.ts src/app/api/stories src/app/api/observations
git commit -m "feat: expose stories and coach memory review"
```

---

### Task 8: Extract typed Relay client shell before adding new UX

**Files:**
- Create: `src/app/api-client.ts`
- Create: `src/app/relay-shell.tsx`
- Modify: `src/app/page.tsx`
- Modify: `src/app/page.test.tsx`

**Interfaces:**

```tsx
import { RelayShell } from "@/app/relay-shell";

export default function App() {
  return <RelayShell />;
}
```

`api-client.ts` exports typed dashboard, opportunity, story, observation, recommended-practice, and manual-practice request functions.

- [ ] **Step 1: Freeze existing behavior with current page tests**

```bash
npm test -- src/app/page.test.tsx
```

Before moving code, add any missing regression assertions for sign-in/out, onboarding/profile review, generic conversation, hands-on, transcription, results, and progress.

- [ ] **Step 2: Extract `ApiError` and the generic JSON helper to `api-client.ts`**

Preserve existing 401 and safe-error semantics.

- [ ] **Step 3: Move the current client shell to `relay-shell.tsx` without redesign**

Keep current view values and handlers for this step.

- [ ] **Step 4: Run GREEN after pure refactor**

```bash
npm test -- src/app/page.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/app/page.tsx src/app/page.test.tsx src/app/relay-shell.tsx src/app/api-client.ts
git commit -m "refactor: split Relay client shell"
```

---

### Task 9: Home command center and Applications UX

**Files:**
- Create: `src/app/views/home-view.tsx`
- Create: `src/app/views/home-view.test.tsx`
- Create: `src/app/views/applications-view.tsx`
- Create: `src/app/views/applications-view.test.tsx`
- Modify: `src/app/relay-shell.tsx`
- Modify: `src/app/page.test.tsx`

**Home interface:**

```ts
type HomeViewProps = {
  dashboard: CareerDashboard;
  busy: boolean;
  onStartRecommended: () => Promise<void>;
  onOpenApplications: () => void;
  onOpenStories: () => void;
  onOpenCoach: () => void;
  onOpenProgress: () => void;
};
```

- [ ] **Step 1: Write RED Home tests**

Assert a dominant `Start recommended practice` CTA, visible rationale/signals, upcoming applications, honest empty states, story/progress summaries, and a readiness-disabled CTA when grounded practice cannot start.

- [ ] **Step 2: Implement Home in this visual order**

```text
Recommended practice
Applications needing attention
What Relay is noticing
Story bank
Progress
```

Show format, minutes, rationale, up to three signals, success criteria, and primary opportunity.

- [ ] **Step 3: Write RED Applications tests**

Cover create considering, create already-applied, edit, transition, schedule, timeline, active/terminal filtering.

- [ ] **Step 4: Implement Applications view**

Use a mobile-first list plus selected detail/editor. Keep job description in detail. Make terminal actions explicit. Display actual `nextInterviewAt` separately from the event's recorded `occurredAt`.

- [ ] **Step 5: Wire shell to canonical dashboard and mutations**

After auth/profile load, use `/api/career/dashboard` as the Career Brain read model. Add `applications` view. Successful mutations refresh dashboard. Starting recommended practice calls `/api/practice`, stores the returned session, clears answer/checkpoint state, and navigates to `interview`.

- [ ] **Step 6: Run GREEN**

```bash
npm test -- src/app/views/home-view.test.tsx src/app/views/applications-view.test.tsx src/app/page.test.tsx
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/app/views/home-view.tsx src/app/views/home-view.test.tsx src/app/views/applications-view.tsx src/app/views/applications-view.test.tsx src/app/relay-shell.tsx src/app/page.test.tsx
git commit -m "feat: add preparation command center"
```

---

### Task 10: Stories, Coach, and manual Practice UX

**Files:**
- Create: `src/app/views/stories-view.tsx`
- Create: `src/app/views/stories-view.test.tsx`
- Create: `src/app/views/coach-view.tsx`
- Create: `src/app/views/coach-view.test.tsx`
- Create: `src/app/views/practice-view.tsx`
- Create: `src/app/views/practice-view.test.tsx`
- Modify: `src/app/relay-shell.tsx`
- Modify: `src/app/page.test.tsx`

- [ ] **Step 1: Write RED Stories tests**

Cover create/edit, six-dimension completeness wording, confirm, retire, provenance count, and attach profile evidence.

- [ ] **Step 2: Implement Stories view**

Fields mirror Situation, Responsibility/Problem, Actions/Decisions, Alternatives/Tradeoffs, Ownership, Outcome, Lessons, and Tags. Present completeness as factual coverage, never answer quality.

- [ ] **Step 3: Write RED Coach tests**

Cover corrected effective text, original claim in detail/history, `Why does Relay think this?` evidence, confirm/correct/dismiss, dismissed default filtering, and the no-observations empty state.

- [ ] **Step 4: Implement Coach view**

Keep confidence/importance secondary. Do not add a create-observation UI.

- [ ] **Step 5: Write RED Practice tests**

Cover current recommendation, manual focus/format form, optional opportunity, recent plans/sessions, and hands-on option.

- [ ] **Step 6: Implement manual Practice**

Human-readable labels map to every `PracticeFormat`. Submit through `start_manual` and use the same `{ plan, session }` navigation path as recommended start.

- [ ] **Step 7: Replace primary navigation**

```text
Home
Applications
Practice
Stories
Coach
Profile
```

Progress remains reachable from Home. Interview/Results remain transient. Refresh dashboard after Story or Observation mutations.

- [ ] **Step 8: Run GREEN**

```bash
npm test -- src/app/views/stories-view.test.tsx src/app/views/coach-view.test.tsx src/app/views/practice-view.test.tsx src/app/page.test.tsx
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/app/views/stories-view.tsx src/app/views/stories-view.test.tsx src/app/views/coach-view.tsx src/app/views/coach-view.test.tsx src/app/views/practice-view.tsx src/app/views/practice-view.test.tsx src/app/relay-shell.tsx src/app/page.test.tsx
git commit -m "feat: add stories coach and manual practice views"
```

---

### Task 11: End-to-end Release 2 regression and documentation

**Files:**
- Create: `src/lib/release2-flow.test.ts`
- Modify: `README.md`

- [ ] **Step 1: Add a dedicated integration-style flow test**

`src/lib/release2-flow.test.ts` covers:

```text
recommendation
-> ready PracticePlan
-> 3-question planned session with plan/opportunity context
-> answers and any persisted follow-up
-> session completion
-> PracticePlan completion
```

Also assert: generic manual conversation rejects a non-five backbone; legacy null-context session hydrates; planned session with an unanswered persisted follow-up cannot explicitly complete; plan bookkeeping failure returns a warning without invalidating completed interview evidence.

- [ ] **Step 2: Run full suite**

```bash
npm test
```

Expected: PASS.

- [ ] **Step 3: Run lint and build**

```bash
npm run lint
npx next build --webpack
```

Expected: both succeed.

- [ ] **Step 4: Verify migration against disposable/development Supabase**

```bash
supabase db push
```

Verify exact generic five-question behavior, planned 1–5 behavior, transactional conversation/hands-on starts, no double-start, cross-user rejection, and legacy null context.

- [ ] **Step 5: Smoke-test an authenticated development deployment**

```text
Home recommendation + rationale
add already-applied role
schedule interview -> near-term role_prep recommendation
create/confirm story
review observation if development data exists
start/finish short recommended practice
start generic full simulation -> five questions
start hands-on practice
```

Do not seed fake durable observations into production for this test.

- [ ] **Step 6: Update README accurately**

Add:

```md
- Career Brain command center with application tracking, story bank, inspectable coach memory, and explainable recommended practice.
- Recommended practice currently uses deterministic explicit Career Brain/progress signals; automatic observation learning and richer adaptive prioritization arrive in the next release.
- The external job-hunter bot is still independent and has not yet been migrated to Supabase.
```

Do not claim automatic learning exists.

- [ ] **Step 7: Boundary/security diff check**

Confirm no job-hunter files/config/secrets changed, no service-role secret reaches browser code, and no client component directly writes Career Brain tables.

- [ ] **Step 8: Final verification**

```bash
npm test && npm run lint && npx next build --webpack
```

Expected: all commands succeed.

- [ ] **Step 9: Commit**

```bash
git add src/lib/release2-flow.test.ts README.md
git commit -m "docs: describe Relay command center"
```

## Completion Gate

Release 2 is complete only when:

- Home's dominant action is an explainable recommended practice.
- Recommendation is deterministic and unit tested.
- Upcoming interviews/applications affect recommendation.
- Reviewed/corrected/dismissed observations are respected; unreviewed observations do not control training.
- Applications can be created, edited, transitioned, scheduled, and viewed with history.
- Stories can be created, edited, confirmed, retired, and shown with factual completeness/provenance.
- Coach observations can be inspected with evidence and Confirm/Correct/Dismiss works.
- Recommended/manual practice persists a PracticePlan and links the session.
- Planned conversation supports 1–5 base questions and cannot finish while a persisted follow-up is unanswered.
- Existing generic conversation remains exact five-question backbone.
- Hands-on remains functional with plan context.
- Completed interview evidence survives plan-bookkeeping failure.
- Existing onboarding/profile/history/progress still works.
- Empty Career Brain tables are usable.
- No automatic observation learning, Google Tracker integration, or job-hunter migration is added.

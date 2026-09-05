# Practice-First Profile Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users start useful personalized practice with a sparse-but-valid profile, using the interview itself to uncover real engineering examples instead of blocking practice until those examples already exist.

**Architecture:** Preserve `ProfileReadiness` as a deterministic source-grounding diagnostic, but remove its authorization role. Route sparse generic conversations through a deterministic five-question discovery blueprint using the existing `limited-grounding` contract, keep evidence-rich planning unchanged, and make evaluation/UX explicitly safe for candidate facts first supplied during practice. No database migration or automatic Career Brain fact/story promotion is part of this slice.

**Tech Stack:** Next.js 16.3.3, React 19.2.8, TypeScript 5, Tailwind CSS 4, Supabase Postgres/Auth/RLS, `@supabase/supabase-js` 2.x, Vitest 4.1.11, existing Gemini provider and coach code.

**Spec:** `docs/superpowers/specs/2026-09-01-practice-first-profile-grounding-design.md`

## Global Constraints

- A completely missing profile still blocks personalized practice with `Create your profile first.`
- `profile.readiness.ready === false` must never block conversational or plan-driven practice by itself.
- `ProfileReadiness` remains persisted and keeps the existing `{ ready, missing }` shape.
- Sparse generic conversations keep the existing exact five-question backbone: `introduction`, `experience`, `technical`, `architecture`, `behavioral`.
- Sparse generic conversations use the existing `InterviewBlueprint.status === "limited-grounding"`; do not add a new database enum/status in this slice.
- Evidence-rich generic conversations keep the existing Gemini generation, validation, repair, and deterministic provider-failure fallback path.
- Plan-driven Practice remains profile-required but readiness-independent.
- Questions may use partial source evidence when it is genuinely relevant; they must never invent project names, technologies, ownership, metrics, outcomes, or other candidate facts.
- Candidate facts first supplied in a no-evidence discovery answer are valid session evidence and must not be called unsupported solely because they were absent from the CV.
- Practice answers are not automatically promoted into `profile_evidence`, source documents, career stories, or persistent coach observations.
- Existing generic and planned-practice persistence contracts remain unchanged; no Supabase migration is expected.
- Keep the job-hunter repository untouched.
- Follow red → green → refactor and make one scoped commit per task.
- Before completion run targeted tests, the full test suite, lint, and a production build with `npx next build --webpack`.

## File Structure

Modify only the existing implementation/testing surfaces below unless a failing test proves an additional directly related file is required:

```text
src/lib/types.ts
src/lib/interview-planner.ts
src/lib/interview-planner.test.ts
src/lib/coach.ts
src/lib/coach.test.ts
src/lib/practice-service.test.ts
src/app/api/interview/route.ts
src/app/api/interview/route.test.ts
src/app/relay-shell.tsx
src/app/results-feedback-cards.tsx
src/app/page.test.tsx
```

Do not add a migration. Do not add a new Practice format. Do not refactor `relay-shell.tsx` beyond the copy/behavior required by this design.

---

### Task 1: Turn sparse readiness into a deterministic discovery blueprint

**Files:**
- Modify: `src/lib/types.ts`
- Modify: `src/lib/interview-planner.ts`
- Modify: `src/lib/interview-planner.test.ts`
- Modify: `src/lib/coach.ts`
- Modify: `src/lib/coach.test.ts`

**Interfaces:**

`ProfileReadiness` keeps the same runtime shape. Change only its documentation from a permission gate to a source-grounding diagnostic.

Add this pure planner export:

```ts
export function buildExperienceDiscoveryBlueprint(
  profile: Pick<ProfileDraft,
    | "role"
    | "seniority"
    | "summary"
    | "narrative"
    | "expertise"
    | "characteristics"
    | "competencies"
  >,
  evidence: EvidenceItem[],
  readiness: ProfileReadiness,
  now?: Date,
): InterviewBlueprint;
```

`generateInterviewBlueprint(...)` keeps its public signature. It becomes the single decision point:

```ts
const readiness = assessProfileReadiness(evidence);
if (!readiness.ready) {
  return buildExperienceDiscoveryBlueprint(profile, evidence, readiness);
}
// current evidence-rich Gemini path continues unchanged
```

- [ ] **Step 1: Write RED planner tests for the sparse-profile contract**

In `src/lib/interview-planner.test.ts`, import `buildExperienceDiscoveryBlueprint` and add a sparse profile fixture plus this exact behavior coverage:

```ts
it("builds the exact generic five-question backbone for a sparse profile", () => {
  const readiness = {
    ready: false,
    missing: [
      "two concrete engineering projects or work examples",
      "responsibilities or outcomes",
    ],
  };

  const result = buildExperienceDiscoveryBlueprint(
    {
      role: "Frontend Engineer",
      seniority: "Senior",
      summary: "Frontend engineer",
      narrative: "Builds React product interfaces.",
      expertise: ["React", "TypeScript"],
      characteristics: ["Pragmatic"],
      competencies: [{ name: "React", relevance: 1 }],
    },
    [],
    readiness,
    new Date("2026-09-01T12:00:00.000Z"),
  );

  expect(result.status).toBe("limited-grounding");
  expect(result.fallbackReason).toContain("limited concrete example detail");
  expect(result.questions.map((item) => item.category)).toEqual([
    "introduction",
    "experience",
    "technical",
    "architecture",
    "behavioral",
  ]);
  expect(result.questions).toHaveLength(5);
  expect(result.questions.every((item) => item.evidenceIds.length === 0)).toBe(true);
  expect(result.questions[1].prompt).toContain("even if it does not feel like a strong interview story yet");
  expect(result.questions[1].prompt).toContain("personally responsible");
});
```

Add a partial-evidence case:

```ts
it("anchors discovery questions only to partial evidence that really exists", () => {
  const evidence = [{
    id: "evidence-1",
    sourceKind: "cv" as const,
    sourceExcerpt: "Worked on a React migration for checkout.",
    projectOrEmployer: "Checkout migration",
    ownership: null,
    technologies: ["React"],
    decision: null,
    constraint: null,
    outcome: null,
    recency: "2025",
    confidence: 0.82,
  }];

  const result = buildExperienceDiscoveryBlueprint(
    sparseProfile,
    evidence,
    {
      ready: false,
      missing: ["two concrete engineering projects or work examples", "responsibilities or outcomes"],
    },
    new Date("2026-09-01T12:00:00.000Z"),
  );

  const referencedIds = result.questions.flatMap((item) => item.evidenceIds);
  expect(referencedIds.every((id) => id === "evidence-1")).toBe(true);
  expect(JSON.stringify(result.questions)).not.toContain("30%");
  expect(JSON.stringify(result.questions)).not.toContain("led the migration");
});
```

The second test intentionally checks non-invention: the builder may use the known migration/React anchor, but must not infer leadership or a metric.

- [ ] **Step 2: Run the planner tests and verify RED**

```bash
npx vitest run src/lib/interview-planner.test.ts
```

Expected: FAIL because `buildExperienceDiscoveryBlueprint` does not exist.

- [ ] **Step 3: Re-document `ProfileReadiness` in `src/lib/types.ts`**

Replace the current gate-oriented comment with:

```ts
/**
 * Deterministic assessment of how much source-backed detail is available for
 * interview grounding. `ready === false` is advisory: it selects broader
 * discovery-oriented practice and must not by itself block a session start.
 */
export type ProfileReadiness = {
  ready: boolean;
  missing: string[];
};
```

Do not rename fields or change persistence.

- [ ] **Step 4: Implement the pure discovery builder in `src/lib/interview-planner.ts`**

Import `ProfileReadiness` from `@/lib/types`.

Use the existing generic limits (`defaultMaxFollowUps`, `defaultMaxQuestions`) and the existing `normalizedSeniority(...)` helper. Add a fixed category order:

```ts
const discoveryCategories: QuestionCategory[] = [
  "introduction",
  "experience",
  "technical",
  "architecture",
  "behavioral",
];
```

Add deterministic prompt helpers. The returned text should be equivalent to:

```ts
function discoveryPrompt(category: QuestionCategory, role: string | null, subject: string): string {
  switch (category) {
    case "introduction":
      return `Give me a concise introduction to yourself and the ${roleDescriptor(role)} work you have mainly been doing recently. You do not need a polished story yet.`;
    case "experience":
      return "Think of one piece of work you remember clearly, even if it does not feel like a strong interview story yet. What was happening, and what part were you personally responsible for?";
    case "technical":
      return `Choose one real technical problem or decision from your work${subject ? ` involving ${subject}` : ""}. What options or constraints shaped what you did?`;
    case "architecture":
      return "Think of a real feature, system, or project you worked on. What requirements or constraints mattered most, and how did the technical approach take shape?";
    case "behavioral":
      return "Think of a time collaboration, ambiguity, disagreement, or delivery pressure made the work harder. What did you do, and what happened next?";
    default:
      return `Tell me about a real example from your ${roleDescriptor(role)} work.`;
  }
}
```

Build five `PlannedQuestion` records with:

```ts
{
  id: `discovery-${sequence}-${category}`,
  sequence,
  category,
  competencyId: null,
  competencyName: category === "introduction" ? null : selectedScopeName,
  difficulty: normalizedSeniority(profile.seniority ?? ""),
  isFollowUp: false,
  prompt: discoveryPrompt(category, profile.role, selectedScopeName ?? ""),
  answer: null,
  createdAt,
}
```

For each question, reuse the existing `evidenceForQuestion(question, evidence)` matcher. Pass the match through `defaultBlueprintQuestion(...)` so a genuinely relevant partial evidence item can retain its identifier and confidence. Then override the final prompt/objective/missing-signal guidance with discovery-oriented wording.

The final object must be:

```ts
return {
  status: "limited-grounding",
  fallbackReason: readiness.missing.length > 0
    ? `Your source profile has limited concrete example detail (${readiness.missing.join(", ")}), so this session starts broader and helps you uncover real projects, ownership, decisions, and outcomes as you answer.`
    : "Your source profile has limited concrete example detail, so this session starts broader and helps you uncover real engineering examples as you answer.",
  maxFollowUps: defaultMaxFollowUps,
  maxQuestions: defaultMaxQuestions,
  createdAt,
  questions,
};
```

For questions with no evidence match, `evidenceIds` must be `[]` and `sourceConfidence` must be `null`.

- [ ] **Step 5: Run the planner tests and make them GREEN**

```bash
npx vitest run src/lib/interview-planner.test.ts
```

Expected: PASS.

- [ ] **Step 6: Write a RED coach-level test proving sparse profiles bypass Gemini planning**

In `src/lib/coach.test.ts`:

```ts
it("uses deterministic discovery planning when source readiness is incomplete", async () => {
  vi.stubEnv("GEMINI_API_KEY", "private-test-key");
  const fetchSpy = vi.spyOn(globalThis, "fetch");

  const result = await generateInterviewBlueprint(
    {
      ...blueprintProfile,
      competencies: [{ name: "React", relevance: 1 }],
    },
    [],
  );

  expect(result.status).toBe("limited-grounding");
  expect(result.questions).toHaveLength(5);
  expect(result.questions[1].prompt).toContain("strong interview story");
  expect(fetchSpy).not.toHaveBeenCalled();
});
```

Keep an existing evidence-rich generator test in the same file and add/retain an assertion that the configured Gemini path still calls `fetch` when readiness is true.

- [ ] **Step 7: Run the coach test and verify RED**

```bash
npx vitest run src/lib/coach.test.ts -t "uses deterministic discovery planning"
```

Expected: FAIL because `generateInterviewBlueprint` still enters the current model path for sparse evidence.

- [ ] **Step 8: Branch inside `generateInterviewBlueprint(...)`**

Update the import from `@/lib/interview-planner` to include `buildExperienceDiscoveryBlueprint`.

Immediately after creating the planning timestamp and before making any Gemini request:

```ts
const readiness = assessProfileReadiness(evidence);
if (!readiness.ready) {
  return buildExperienceDiscoveryBlueprint(profile, evidence, readiness, new Date(createdAt));
}
```

Do not change the remaining evidence-rich generation/repair/fallback code.

- [ ] **Step 9: Run targeted Task 1 tests**

```bash
npx vitest run src/lib/interview-planner.test.ts src/lib/coach.test.ts
```

Expected: PASS.

- [ ] **Step 10: Commit Task 1**

```bash
git add src/lib/types.ts src/lib/interview-planner.ts src/lib/interview-planner.test.ts src/lib/coach.ts src/lib/coach.test.ts
git commit -m "feat: add sparse-profile discovery planning"
```

---

### Task 2: Remove the interview-start hard gate and lock both practice paths to the same policy

**Files:**
- Modify: `src/app/api/interview/route.ts`
- Modify: `src/app/api/interview/route.test.ts`
- Modify: `src/lib/practice-service.test.ts`

**Interfaces:**

No API request shape changes.

After this task, the generic start contract is:

```text
no profile       -> HTTP 400
sparse profile   -> HTTP 200 + limited-grounding discovery session
ready profile    -> HTTP 200 + current grounded/fallback session
```

Plan-driven `startRecommendedPractice(...)` and `startManualPractice(...)` remain unchanged in production code; a regression test makes their readiness independence explicit.

- [ ] **Step 1: Replace the old route rejection test with a RED start-success test**

In `src/app/api/interview/route.test.ts`, replace:

```ts
it("rejects profiles that do not meet the readiness gate before planning an interview", ...)
```

with a test equivalent to:

```ts
it("starts discovery practice when profile source grounding is incomplete", async () => {
  const sparseProfile = {
    ...profile,
    evidence: [],
    readiness: {
      ready: false,
      missing: ["two concrete engineering projects or work examples"],
    },
  };
  const blueprint = {
    status: "limited-grounding" as const,
    fallbackReason: "Your source profile has limited concrete example detail, so this session starts broader.",
    maxFollowUps: 3,
    maxQuestions: 8,
    createdAt: "2026-09-01T12:00:00.000Z",
    questions: [
      {
        ...question(1, null),
        id: "discovery-1-introduction",
        objective: "Establish recent engineering context without requiring a polished example.",
        evidenceIds: [],
        expectedSignals: ["role summary", "recent ownership"],
        missingSignalPrompts: ["Name one area of work you remember clearly."],
        rubricCriteria: ["Explain recent work clearly."],
        followUpLimit: 0,
        sourceConfidence: null,
      },
    ],
  };
  const persisted = session([{ ...question(1, null), id: "database-question-1" }]);
  persisted.blueprint = {
    ...blueprint,
    questions: blueprint.questions.map((item) => ({ ...item, id: "database-question-1" })),
  };

  mocks.getProfile.mockResolvedValue(sparseProfile);
  mocks.generateInterviewBlueprint.mockResolvedValue(blueprint);
  mocks.createSessionWithBlueprint.mockResolvedValue(persisted);
  mocks.initialQuestion.mockReturnValue(blueprint.questions[0].prompt);

  const response = await POST(new Request("http://localhost/api/interview", {
    method: "POST",
    body: JSON.stringify({ action: "start", mode: "conversation" }),
  }));

  expect(response.status).toBe(200);
  expect(mocks.generateInterviewBlueprint).toHaveBeenCalledWith(sparseProfile, []);
  expect(mocks.createSessionWithBlueprint).toHaveBeenCalled();
  expect((await response.json()).session.blueprint.status).toBe("limited-grounding");
});
```

- [ ] **Step 2: Add/retain a no-profile test**

Ensure the route still proves the real prerequisite:

```ts
it("still requires a profile before starting personalized practice", async () => {
  mocks.getProfile.mockResolvedValue(null);

  const response = await POST(new Request("http://localhost/api/interview", {
    method: "POST",
    body: JSON.stringify({ action: "start", mode: "conversation" }),
  }));

  expect(response.status).toBe(400);
  expect(await response.json()).toEqual({ error: "Create your profile first." });
  expect(mocks.generateInterviewBlueprint).not.toHaveBeenCalled();
});
```

- [ ] **Step 3: Run route tests and verify RED**

```bash
npx vitest run src/app/api/interview/route.test.ts
```

Expected: the sparse-profile start test FAILS with HTTP 400 under the current gate.

- [ ] **Step 4: Delete only the readiness rejection from `src/app/api/interview/route.ts`**

Remove this branch entirely:

```ts
if (!profile.readiness?.ready) {
  return NextResponse.json({
    error: `Add ${profile.readiness?.missing.join(", ") ?? "more evidence"} before starting a personalized interview.`,
    readiness: profile.readiness,
  }, { status: 400 });
}
```

Keep:

```ts
if (!profile) return NextResponse.json({ error: "Create your profile first." }, { status: 400 });
```

Then always call:

```ts
const blueprint = await generateInterviewBlueprint(profile, profile.evidence ?? []);
```

for conversation starts. Task 1 now decides grounded vs discovery planning.

- [ ] **Step 5: Run route tests and make them GREEN**

```bash
npx vitest run src/app/api/interview/route.test.ts
```

Expected: PASS.

- [ ] **Step 6: Add a plan-driven Practice regression test**

In `src/lib/practice-service.test.ts`, use the existing fixtures/mocks and add:

```ts
it("starts plan-driven practice even when the profile readiness diagnostic is false", async () => {
  const sparseProfile = {
    ...profile,
    readiness: {
      ready: false,
      missing: ["two concrete engineering projects or work examples"],
    },
  };
  mocks.getProfile.mockResolvedValue(sparseProfile);

  await startRecommendedPractice(supabase as never, "user-1", now);

  expect(mocks.generatePracticeBlueprint).toHaveBeenCalledWith(
    sparseProfile,
    sparseProfile.evidence,
    expect.objectContaining({ id: "plan-1" }),
    expect.anything(),
  );
  expect(mocks.createSessionWithPracticeBlueprint).toHaveBeenCalled();
});
```

This is a characterization/regression assertion: production `practice-service.ts` should require no change because it already checks profile existence rather than readiness.

- [ ] **Step 7: Run Task 2 tests**

```bash
npx vitest run src/app/api/interview/route.test.ts src/lib/practice-service.test.ts
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

```bash
git add src/app/api/interview/route.ts src/app/api/interview/route.test.ts src/lib/practice-service.test.ts
git commit -m "fix: make profile readiness non-blocking"
```

---

### Task 3: Make no-evidence discovery answers safe to evaluate as newly supplied session evidence

**Files:**
- Modify: `src/lib/coach.ts`
- Modify: `src/lib/coach.test.ts`

**Interfaces:**

No public type changes.

Add this private semantic helper:

```ts
function hasSourceEvidenceTarget(question: BlueprintQuestion): boolean {
  return question.evidenceIds.length > 0;
}
```

For questions where it returns `false`, `unsupportedClaims` must not contain candidate career details merely because they were not pre-existing source evidence.

- [ ] **Step 1: Write RED deterministic-evaluation coverage**

In `src/lib/coach.test.ts`, construct a discovery `BlueprintQuestion` with:

```ts
{
  ...planned({
    id: "discovery-question-1",
    category: "experience",
    prompt: "Think of one piece of work you remember clearly. What part were you responsible for?",
  }),
  objective: "Discover a real work example and personal ownership.",
  evidenceIds: [],
  expectedSignals: ["role", "decision", "impact"],
  missingSignalPrompts: ["What changed because of your work?"],
  rubricCriteria: [
    "Name a real work example.",
    "Describe personal responsibility.",
    "Explain one action or decision and its result when remembered.",
  ],
  followUpLimit: 1,
  sourceConfidence: null,
}
```

Stub `GEMINI_API_KEY` empty so the deterministic evaluator runs. Assert:

```ts
const relevant = await evaluateAnswer(
  discoveryQuestion,
  discoveryBlueprint,
  blueprintProfile,
  "At my previous company I rebuilt our questionnaire editor. I shaped the component architecture and coordinated the migration with the team.",
  "interviewer: Think of one piece of work you remember clearly.",
);

expect(relevant.relevance).toBeGreaterThan(5);
expect(relevant.unsupportedClaims).toEqual([]);
expect(relevant.supportedClaims.length).toBeGreaterThan(0);
```

Also compare an unrelated answer of similar length:

```ts
expect(relevant.relevance).toBeGreaterThan(unrelated.relevance);
```

- [ ] **Step 2: Write RED model-normalization coverage**

Stub Gemini to return a valid evaluation that incorrectly puts a first-person discovery detail into `unsupportedClaims`:

```ts
unsupportedClaims: ["I rebuilt our questionnaire editor."],
```

For a question with `evidenceIds: []`, assert the normalized result returns:

```ts
expect(result.unsupportedClaims).toEqual([]);
```

For an otherwise equivalent question with `evidenceIds: ["evidence-1"]`, retain the current grounded behavior; do not globally erase unsupported claims.

- [ ] **Step 3: Run the new tests and verify RED**

```bash
npx vitest run src/lib/coach.test.ts -t "discovery"
```

Expected: at least the model-normalization assertion FAILS because current normalization preserves provider-returned unsupported claims regardless of evidence targets.

- [ ] **Step 4: Implement the evidence-target distinction in deterministic evaluation**

Add:

```ts
function hasSourceEvidenceTarget(question: BlueprintQuestion): boolean {
  return question.evidenceIds.length > 0;
}
```

At the top of the existing private `unsupportedClaims(...)` helper:

```ts
if (!hasSourceEvidenceTarget(question)) return [];
```

Do **not** weaken relevance scoring or expected-signal coverage. A discovery answer can still be weak because it is vague, irrelevant, or misses ownership/decision/outcome signals.

- [ ] **Step 5: Enforce the same rule after Gemini normalization**

Where the normalized grounded evaluation is returned, change only the unsupported-claim field to:

```ts
unsupportedClaims: hasSourceEvidenceTarget(question)
  ? normalized.unsupportedClaims.slice(0, 3)
  : [],
```

Keep the existing filtering of `supportedClaims` against the candidate's actual answer.

- [ ] **Step 6: Add the policy to the evaluator prompt**

In the prompt assembled for Gemini evaluation, add a compact rule equivalent to:

```text
Grounding rule: when question.evidenceIds is empty, this is a discovery/general objective. Treat first-person career details in the candidate's answer as newly supplied session evidence. Do not mark them unsupported merely because they were absent from the source profile. Never invent missing details; improved answers may only reuse facts actually supplied by the candidate or already grounded by the question context.
```

When `evidenceIds` is non-empty, keep the current evidence-grounded instructions.

- [ ] **Step 7: Verify follow-up behavior remains useful**

Add/retain a test showing a vague discovery answer can still trigger a follow-up through low relevance, zero supported claims, or low expected-signal coverage even though `unsupportedClaims` is empty.

```ts
expect(turn.followUp).not.toBeNull();
expect(turn.evaluation.unsupportedClaims).toEqual([]);
```

The purpose is to remove false accusations, not to make vague answers pass.

- [ ] **Step 8: Run Task 3 tests**

```bash
npx vitest run src/lib/coach.test.ts
```

Expected: PASS.

- [ ] **Step 9: Commit Task 3**

```bash
git add src/lib/coach.ts src/lib/coach.test.ts
git commit -m "fix: evaluate discovery answers without false grounding errors"
```

---

### Task 4: Replace gate/error framing with practice-first UX copy

**Files:**
- Modify: `src/app/relay-shell.tsx`
- Modify: `src/app/results-feedback-cards.tsx`
- Modify: `src/app/page.test.tsx`

**Interfaces:**

No component props or route contracts change.

Use these user-facing semantics:

```text
ready profile   -> source profile has enough detail for evidence-grounded practice
sparse profile  -> user can practice now; broader questions will help uncover stronger examples
limited plan    -> “Broader practice”, with the actual blueprint reason below it
```

- [ ] **Step 1: Change the existing readiness-copy test to the new product contract**

`src/app/page.test.tsx` already has a test that currently expects:

```text
Profile evidence gate still needs two concrete engineering projects or work examples, identifiable technologies, responsibilities or outcomes.
```

Change that test fixture to keep `readiness.ready: false` and assert:

```ts
expect(screen.getByText(/You can practice now\./)).toBeInTheDocument();
expect(screen.getByText(/help you uncover stronger/i)).toBeInTheDocument();
expect(screen.queryByText(/Profile evidence gate/i)).not.toBeInTheDocument();
```

Also retain/introduce an evidence-rich assertion:

```ts
expect(screen.getByText("Your source profile has enough detail for evidence-grounded practice.")).toBeInTheDocument();
```

- [ ] **Step 2: Change the limited-grounding feedback-card test to RED**

The existing `labels limited-grounding legacy sessions clearly` test directly renders `ResultsFeedbackCards` and currently expects `Limited grounding`.

Change only the heading expectation:

```ts
expect(within(details).getByText("Broader practice")).toBeInTheDocument();
expect(within(details).getByText("Gemini returned invalid blueprint JSON after one repair attempt.")).toBeInTheDocument();
```

The reason remains visible so provider fallback and sparse-profile discovery are distinguishable without a new enum.

- [ ] **Step 3: Add a shell-level discovery banner assertion**

Use `mockRoutes(...)` and an active conversation fixture whose blueprint is:

```ts
{
  status: "limited-grounding",
  fallbackReason: "Your source profile has limited concrete example detail, so this session starts broader and helps you uncover real examples as you answer.",
  maxFollowUps: 3,
  maxQuestions: 8,
  createdAt: "2026-09-01T12:00:00.000Z",
  questions: [...],
}
```

Start/navigate into the conversation through the real shell interaction and assert:

```ts
expect(screen.getByText("Broader practice")).toBeInTheDocument();
expect(screen.getByText(/helps you uncover real examples/i)).toBeInTheDocument();
```

- [ ] **Step 4: Run UI tests and verify RED**

```bash
npx vitest run src/app/page.test.tsx
```

Expected: FAIL on the old gate/`Limited grounding` copy.

- [ ] **Step 5: Update `profileReadinessCopy(...)` in `src/app/relay-shell.tsx`**

Use:

```ts
function profileReadinessCopy(readiness: Profile["readiness"]): string | null {
  if (!readiness) return null;
  return readiness.ready
    ? "Your source profile has enough detail for evidence-grounded practice."
    : "You can practice now. Relay will start broader and help you uncover stronger project, ownership, and outcome examples as you answer.";
}
```

Do not render missing categories as a blocker. If later UI wants to list them, they must remain secondary guidance.

- [ ] **Step 6: Change limited-grounding headings in `relay-shell.tsx`**

For both active conversation and results surfaces, replace:

```tsx
<p ...>Limited grounding</p>
```

with:

```tsx
<p ...>Broader practice</p>
```

Keep `session.blueprint.fallbackReason` as the primary explanatory body.

Change the null-reason fallback copy to neutral wording:

```text
This session is using broader questions because Relay has less source grounding for this practice than usual.
```

Do not say that the user must add information before continuing.

- [ ] **Step 7: Change the feedback-card heading/default copy in `src/app/results-feedback-cards.tsx`**

Replace the same `Limited grounding` heading with `Broader practice` and use the same neutral null-reason fallback. Preserve the actual `fallbackReason` verbatim when present.

- [ ] **Step 8: Run Task 4 tests**

```bash
npx vitest run src/app/page.test.tsx
```

Expected: PASS.

- [ ] **Step 9: Commit Task 4**

```bash
git add src/app/relay-shell.tsx src/app/results-feedback-cards.tsx src/app/page.test.tsx
git commit -m "fix: frame sparse profiles as practice opportunities"
```

---

## Final Verification

Run the focused behavioral suite first:

```bash
npx vitest run \
  src/lib/interview-planner.test.ts \
  src/lib/coach.test.ts \
  src/lib/practice-service.test.ts \
  src/app/api/interview/route.test.ts \
  src/app/page.test.tsx
```

Expected: PASS.

Then run all repository checks required by `AGENTS.md`:

```bash
npm test
npm run lint
npx next build --webpack
```

Expected: all PASS with no TypeScript/build errors.

Manually verify these acceptance cases against a signed-in development account:

```text
1. Upload/confirm a CV whose extracted readiness is false.
2. Open Practice/Home and confirm the account is not treated as blocked.
3. Start a generic conversational interview.
4. Confirm the start succeeds and the banner says “Broader practice”.
5. Confirm the experience question invites recalling an imperfect/unfinished example rather than demanding a pre-written one.
6. Answer with a real work example that was not in the CV.
7. Confirm feedback does not label that autobiographical detail unsupported solely for being absent from the CV.
8. Confirm a vague/irrelevant answer still receives weaker relevance feedback or a follow-up.
9. Complete the session and confirm no new profile evidence or career story was silently created.
10. Repeat with an evidence-rich profile and confirm the normal grounded interview path still works.
```

Use `git status --short` at the end. The implementation branch should contain only the scoped code/test changes above plus these already-approved design/plan docs.

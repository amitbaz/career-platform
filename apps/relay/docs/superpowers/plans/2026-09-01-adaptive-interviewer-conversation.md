# Adaptive Interviewer Conversation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Relay's template-rendering interviewer with a three-stage adaptive pipeline — a private assessor, a deterministic director, and an interviewer call that authors speech and never sees the rubric.

**Architecture:** Every turn runs assessor (model) → director (pure function) → interviewer (model). The director owns every decision that matters — follow up or advance, rescue or press, when to stop — computed from coverage state, mode policy, and rescue budgets. The interviewer call receives a decided `Intent` plus persona and structured evidence, and returns one line of speech. Coverage state is derived from persisted rows on every turn, never stored.

**Tech Stack:** Next.js (App Router), TypeScript strict, Zod, Vitest + jsdom, Supabase Postgres with `security invoker` RPCs, Gemini via `modelJson`.

**Spec:** `docs/superpowers/specs/2026-09-01-adaptive-interviewer-conversation-design.md`

## Global Constraints

- TypeScript strict; import via the `@/*` alias for `src/` modules.
- Two-space indentation, semicolons, PascalCase types/components, camelCase values.
- Server-side modules start with `import "server-only";` — this includes every new `src/lib/` module in this plan except pure-data/pure-function modules that carry no secrets. Follow the existing file's lead: `coach.ts` has it, `competencies.ts` does not.
- Tests are co-located as `*.test.ts` beside the code. Only files under `src/` are collected.
- Red → green → refactor. Write the failing test, run it, watch it fail, then implement.
- Never log answer text, CV text, or job-description text. Logs carry operation, model, status, intent kind, and failure reason only (spec §13.3).
- The interviewer call must never receive `objective`, `expectedSignals`, or `rubricCriteria` (spec §11.1). This is the single most important invariant in this plan.
- Migrations are `security invoker` and re-`grant execute` on the exact new signature. Never `security definer`.
- Run `npm test` and `npm run lint` before each commit; `npx next build --webpack` before the final one.

---

### Task 1: Core types for intents, modes, and coverage

**Files:**
- Modify: `src/lib/types.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `RoundId`, `InterviewMode`, `ProbeAspect`, `RescueStyle`, `AdvanceReason`, `Intent`, `AssistanceRecord`, `ModePolicy`, `CoverageTarget`, `TargetStatus`, `TargetState`, `AssessmentRead`. Modified `PlannedQuestion`, `InterviewSession`, `InterviewBlueprint`.

This task is types only — no behaviour, so no test of its own. It compiles or it does not, and every later task's tests depend on it. Type-only tasks are the one exception to the test-first rule in this plan.

- [ ] **Step 1: Add the new types**

Add to `src/lib/types.ts`, after the existing `Difficulty` declaration:

```ts
export type RoundId = "recruiter" | "tech-lead" | "hr" | "founder" | "code-review";
export type InterviewMode = "coach" | "real";

export type ProbeAspect =
  | "specifics"
  | "ownership"
  | "tradeoff"
  | "outcome"
  | "collaboration"
  | "hindsight";

export type RescueStyle = "narrow" | "hook" | "reframe" | "park";

export type AdvanceReason =
  | "satisfied"
  | "line-exhausted"
  | "rescue-budget-spent"
  | "turn-budget";

/**
 * A single decided interviewer move. The director produces exactly one per
 * turn; the interviewer model authors speech for it and decides nothing.
 *
 * `basis` and `claim` carry the candidate's own words, so the interviewer can
 * react to what was actually said without ever receiving the rubric.
 */
export type Intent =
  | { kind: "open"; targetId: string }
  | { kind: "probe"; targetId: string; aspect: ProbeAspect; basis: string }
  | { kind: "challenge"; targetId: string; claim: string }
  | { kind: "rescue"; targetId: string; style: RescueStyle; hook: string | null }
  | { kind: "advance"; targetId: string; reason: AdvanceReason }
  | { kind: "hypothetical"; targetId: string; basis: string }
  | { kind: "candidate-questions" }
  | { kind: "close" };

export type IntentKind = Intent["kind"];

/** One rescue actually applied to a question, for results display and Release 3. */
export type AssistanceRecord = {
  style: RescueStyle;
  at: string;
};

export type ModePolicy = {
  rescuesPerQuestion: number;
  rescuesPerSession: number;
  rescueStyles: RescueStyle[];
  pushback: "light" | "firm";
  parkAndReturn: boolean;
  acknowledgeStruggle: boolean;
};

/**
 * A competency the round must cover. Replaces pre-written prompt text: the
 * blueprint now says what to find out, not what to say.
 *
 * `objective`, `expectedSignals` and `rubricCriteria` are assessor-only and
 * must never reach the interviewer call (spec §11.1).
 */
export type CoverageTarget = {
  id: string;
  competencyId: string | null;
  competencyName: string | null;
  category: QuestionCategory;
  evidenceIds: string[];
  difficulty: Difficulty;
  objective: string;
  expectedSignals: string[];
  rubricCriteria: string[];
  required: boolean;
};

export type TargetStatus = "unasked" | "open" | "satisfied" | "parked" | "skipped";

export type TargetState = {
  target: CoverageTarget;
  status: TargetStatus;
  turnsSpent: number;
  rescuesSpent: number;
  askedIntents: Intent[];
};

/** The assessor's coarse read of the answer, separate from its rubric scoring. */
export type AssessmentRead = "answered" | "partial" | "evasive" | "stuck";
```

- [ ] **Step 2: Modify the existing types**

In `PlannedQuestion`, change `prompt` and add four fields:

```ts
export type PlannedQuestion = {
  id: string;
  sequence: number;
  category: QuestionCategory;
  competencyId: string | null;
  competencyName: string | null;
  difficulty: Difficulty;
  isFollowUp: boolean;
  /** Null until the interviewer authors it at reveal time. */
  prompt: string | null;
  answer: string | null;
  createdAt: string;
  /** The director intent that produced this question's prompt. */
  askedIntent: Intent | null;
  assistance: AssistanceRecord[];
  /** True when the candidate did not attempt the question; never scored. */
  nonAnswer: boolean;
  objective?: string;
  evidenceIds?: string[];
  expectedSignals?: string[];
  missingSignalPrompts?: string[];
  rubricCriteria?: string[];
  followUpLimit?: number;
  sourceConfidence?: number | null;
  parentQuestionId?: string | null;
};
```

In `InterviewBlueprint`, add the round fields and targets while keeping the legacy `questions` array — legacy sessions still carry it (spec §12):

```ts
export type InterviewBlueprint = {
  status: BlueprintStatus;
  fallbackReason: string | null;
  maxFollowUps: number;
  maxQuestions: number;
  createdAt: string;
  /** Legacy pre-written questions. Present on sessions created before this release. */
  questions: BlueprintQuestion[];
  roundId: RoundId;
  turnBudget: number;
  targets: CoverageTarget[];
};
```

In `InterviewSession`, add:

```ts
  roundId: RoundId;
  mode: InterviewMode;
  opportunityId: string | null;
  /** True once any turn fell back to a deterministic evaluation or line (spec §13.2). */
  degraded: boolean;
```

- [ ] **Step 3: Verify the project still typechecks where it can**

Run: `npx tsc --noEmit`
Expected: errors ONLY in files this plan modifies later — `coach.ts`, `interview-planner.ts`, `repositories/interviews.ts`, `route.ts`, `relay-shell.tsx`, and their tests. These are expected and get fixed by their own tasks. If an unrelated file errors, the type change is wrong.

- [ ] **Step 4: Commit**

```bash
git add src/lib/types.ts
git commit -m "feat: add intent, mode, and coverage types for the adaptive interviewer"
```

---

### Task 2: Round definitions and mode policies

**Files:**
- Create: `src/lib/interview-rounds.ts`
- Test: `src/lib/interview-rounds.test.ts`

**Interfaces:**
- Consumes: Task 1 types.
- Produces:
  - `type InterviewRound = { id: RoundId; label: string; agenda: string; register: string; personaStake: string; moves: IntentKind[]; probeAspects: ProbeAspect[]; outOfScope: string[]; opening: IntentKind; closing: IntentKind }`
  - `roundFor(id: RoundId): InterviewRound`
  - `modePolicyFor(mode: InterviewMode): ModePolicy`
  - `IMPLEMENTED_ROUNDS: RoundId[]`

Pure data plus two lookups. No `server-only` — this module holds no secrets and the client needs `IMPLEMENTED_ROUNDS` for the mode picker in Task 11.

- [ ] **Step 1: Write the failing test**

Create `src/lib/interview-rounds.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { IMPLEMENTED_ROUNDS, modePolicyFor, roundFor } from "@/lib/interview-rounds";

describe("roundFor", () => {
  it("returns the tech lead round with its full repertoire", () => {
    const round = roundFor("tech-lead");
    expect(round.id).toBe("tech-lead");
    expect(round.moves).toEqual(
      expect.arrayContaining(["open", "probe", "challenge", "rescue", "advance", "hypothetical", "candidate-questions", "close"]),
    );
    expect(round.opening).toBe("open");
    expect(round.closing).toBe("candidate-questions");
  });

  it("keeps salary and live coding out of scope for the tech lead round", () => {
    const round = roundFor("tech-lead");
    expect(round.outOfScope).toEqual(
      expect.arrayContaining(["salary", "notice period", "live coding"]),
    );
  });

  it("gives every round a persona stake that names a motivation", () => {
    for (const id of IMPLEMENTED_ROUNDS) {
      expect(roundFor(id).personaStake.length).toBeGreaterThan(40);
    }
  });
});

describe("modePolicyFor", () => {
  it("lets coach mode rescue more often and park", () => {
    const coach = modePolicyFor("coach");
    expect(coach.rescuesPerQuestion).toBe(2);
    expect(coach.rescuesPerSession).toBe(5);
    expect(coach.rescueStyles).toEqual(["narrow", "hook", "reframe", "park"]);
    expect(coach.parkAndReturn).toBe(true);
    expect(coach.acknowledgeStruggle).toBe(true);
  });

  it("limits real mode to a single narrowing rescue and no parking", () => {
    const real = modePolicyFor("real");
    expect(real.rescuesPerQuestion).toBe(1);
    expect(real.rescuesPerSession).toBe(2);
    expect(real.rescueStyles).toEqual(["narrow"]);
    expect(real.parkAndReturn).toBe(false);
    expect(real.acknowledgeStruggle).toBe(false);
    expect(real.pushback).toBe("firm");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/interview-rounds.test.ts`
Expected: FAIL — `Failed to resolve import "@/lib/interview-rounds"`.

- [ ] **Step 3: Write the implementation**

Create `src/lib/interview-rounds.ts`:

```ts
import type { InterviewMode, IntentKind, ModePolicy, ProbeAspect, RoundId } from "@/lib/types";

/**
 * A round is one interview in a real loop, conducted by one person with one
 * agenda. The director may only issue intents whose kind appears in `moves`,
 * which is what gives a session a stable identity instead of drifting between
 * registers (spec §7).
 */
export type InterviewRound = {
  id: RoundId;
  label: string;
  agenda: string;
  register: string;
  /**
   * The interviewer's motivation, not a costume. No invented name, biography,
   * or anecdotes -- a consistent motivation produces a consistent voice, and
   * fabricated personality reads worse, not better (spec §7.2).
   */
  personaStake: string;
  moves: IntentKind[];
  probeAspects: ProbeAspect[];
  outOfScope: string[];
  opening: IntentKind;
  closing: IntentKind;
};

/** Rounds with a built repertoire. Others in `RoundId` are specified but deferred (spec §15). */
export const IMPLEMENTED_ROUNDS: RoundId[] = ["tech-lead"];

const TECH_LEAD: InterviewRound = {
  id: "tech-lead",
  label: "Tech lead evaluation",
  agenda: "Can this person actually own what they claim to have owned?",
  register:
    "Direct, unhurried, specific. Follows one thread to its end before opening another. Sceptical of unsupported claims, not hostile.",
  personaStake:
    "You are the senior engineer this candidate would work alongside. You are deciding whether they can own frontend architecture without supervision. You have read their CV and you do not accept claims without specifics.",
  moves: ["open", "probe", "challenge", "rescue", "advance", "hypothetical", "candidate-questions", "close"],
  probeAspects: ["specifics", "ownership", "tradeoff", "outcome", "collaboration", "hindsight"],
  outOfScope: ["salary", "notice period", "visa status", "company values", "why us", "live coding", "take-home logistics"],
  opening: "open",
  closing: "candidate-questions",
};

const ROUNDS: Partial<Record<RoundId, InterviewRound>> = {
  "tech-lead": TECH_LEAD,
};

/** Throws for a deferred round rather than silently degrading to a wrong repertoire. */
export function roundFor(id: RoundId): InterviewRound {
  const round = ROUNDS[id];
  if (!round) throw new Error(`Interview round "${id}" is specified but not implemented yet.`);
  return round;
}

const COACH: ModePolicy = {
  rescuesPerQuestion: 2,
  rescuesPerSession: 5,
  rescueStyles: ["narrow", "hook", "reframe", "park"],
  pushback: "light",
  parkAndReturn: true,
  acknowledgeStruggle: true,
};

// Real mode is accurate rather than punitive: a real interviewer rephrases once
// out of politeness and then moves on, and a blank costs the candidate.
const REAL: ModePolicy = {
  rescuesPerQuestion: 1,
  rescuesPerSession: 2,
  rescueStyles: ["narrow"],
  pushback: "firm",
  parkAndReturn: false,
  acknowledgeStruggle: false,
};

export function modePolicyFor(mode: InterviewMode): ModePolicy {
  return mode === "coach" ? COACH : REAL;
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/interview-rounds.test.ts`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add src/lib/interview-rounds.ts src/lib/interview-rounds.test.ts
git commit -m "feat: define interview rounds and mode policies"
```

---

### Task 3: Derive coverage state from persisted rows

**Files:**
- Create: `src/lib/interview-coverage.ts`
- Test: `src/lib/interview-coverage.test.ts`

**Interfaces:**
- Consumes: Task 1 types.
- Produces: `deriveCoverageState(targets: CoverageTarget[], questions: PlannedQuestion[], evaluations: Evaluation[]): TargetState[]`, `rescuesSpentInSession(questions: PlannedQuestion[]): number`, `targetIdOf(intent: Intent): string | null`.

Each POST is stateless, so coverage is recomputed per turn rather than stored — a stored copy could drift from the rows it describes (spec §9.2).

- [ ] **Step 1: Write the failing test**

Create `src/lib/interview-coverage.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { deriveCoverageState, rescuesSpentInSession, targetIdOf } from "@/lib/interview-coverage";
import type { CoverageTarget, Evaluation, PlannedQuestion } from "@/lib/types";

function target(id: string, overrides: Partial<CoverageTarget> = {}): CoverageTarget {
  return {
    id,
    competencyId: `comp-${id}`,
    competencyName: `Competency ${id}`,
    category: "experience",
    evidenceIds: [],
    difficulty: "senior",
    objective: `Probe ${id}.`,
    expectedSignals: ["ownership", "impact"],
    rubricCriteria: ["Name a concrete example."],
    required: true,
    ...overrides,
  };
}

function question(id: string, overrides: Partial<PlannedQuestion> = {}): PlannedQuestion {
  return {
    id,
    sequence: 1,
    category: "experience",
    competencyId: null,
    competencyName: null,
    difficulty: "senior",
    isFollowUp: false,
    prompt: "asked",
    answer: "answered",
    createdAt: "2026-09-01T00:00:00.000Z",
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    ...overrides,
  };
}

function evaluation(questionId: string, signals: string[]): Evaluation {
  return {
    questionId,
    competencyId: null,
    competency: "Competency a",
    score: 7,
    relevance: 8,
    dimensions: {} as Evaluation["dimensions"],
    strengths: [],
    needsWork: [],
    missingPoints: ["x"],
    betterStructure: ["y"],
    improvedAnswer: "z",
    supportedClaims: ["something"],
    expectedSignalsPresent: signals,
    unsupportedClaims: [],
    dimensionReasons: {} as Evaluation["dimensionReasons"],
  } as Evaluation;
}

describe("deriveCoverageState", () => {
  it("reports an untouched target as unasked", () => {
    const state = deriveCoverageState([target("a")], [], []);
    expect(state[0].status).toBe("unasked");
    expect(state[0].turnsSpent).toBe(0);
    expect(state[0].askedIntents).toEqual([]);
  });

  it("marks a target satisfied only when every expected signal is present", () => {
    const asked = question("q1", {
      askedIntent: { kind: "open", targetId: "a" },
    });
    const partial = deriveCoverageState([target("a")], [asked], [evaluation("q1", ["ownership"])]);
    expect(partial[0].status).toBe("open");

    const complete = deriveCoverageState([target("a")], [asked], [evaluation("q1", ["ownership", "impact"])]);
    expect(complete[0].status).toBe("satisfied");
  });

  it("marks a target parked when its last intent was a park rescue", () => {
    const asked = question("q1", {
      askedIntent: { kind: "rescue", targetId: "a", style: "park", hook: null },
    });
    const state = deriveCoverageState([target("a")], [asked], []);
    expect(state[0].status).toBe("parked");
  });

  it("collects every intent already issued for a target", () => {
    const first = question("q1", { askedIntent: { kind: "open", targetId: "a" } });
    const second = question("q2", {
      sequence: 2,
      askedIntent: { kind: "probe", targetId: "a", aspect: "ownership", basis: "the migration" },
    });
    const state = deriveCoverageState([target("a")], [first, second], []);
    expect(state[0].askedIntents).toHaveLength(2);
    expect(state[0].turnsSpent).toBe(2);
  });

  it("counts rescues per target and per session", () => {
    const first = question("q1", {
      askedIntent: { kind: "rescue", targetId: "a", style: "narrow", hook: null },
      assistance: [{ style: "narrow", at: "2026-09-01T00:00:00.000Z" }],
    });
    const second = question("q2", {
      sequence: 2,
      askedIntent: { kind: "rescue", targetId: "b", style: "hook", hook: "the migration" },
      assistance: [{ style: "hook", at: "2026-09-01T00:01:00.000Z" }],
    });
    const state = deriveCoverageState([target("a"), target("b")], [first, second], []);
    expect(state[0].rescuesSpent).toBe(1);
    expect(state[1].rescuesSpent).toBe(1);
    expect(rescuesSpentInSession([first, second])).toBe(2);
  });

  it("does not count a non-answer turn as progress toward satisfaction", () => {
    const blank = question("q1", {
      askedIntent: { kind: "open", targetId: "a" },
      nonAnswer: true,
    });
    const state = deriveCoverageState([target("a")], [blank], []);
    expect(state[0].status).toBe("open");
  });
});

describe("targetIdOf", () => {
  it("returns null for session-level intents", () => {
    expect(targetIdOf({ kind: "close" })).toBeNull();
    expect(targetIdOf({ kind: "candidate-questions" })).toBeNull();
  });

  it("returns the target for question-level intents", () => {
    expect(targetIdOf({ kind: "open", targetId: "a" })).toBe("a");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/interview-coverage.test.ts`
Expected: FAIL — `Failed to resolve import "@/lib/interview-coverage"`.

- [ ] **Step 3: Write the implementation**

Create `src/lib/interview-coverage.ts`:

```ts
import type { CoverageTarget, Evaluation, Intent, PlannedQuestion, TargetState, TargetStatus } from "@/lib/types";

/** Session-level intents carry no target; question-level intents all do. */
export function targetIdOf(intent: Intent): string | null {
  return "targetId" in intent ? intent.targetId : null;
}

export function rescuesSpentInSession(questions: PlannedQuestion[]): number {
  return questions.reduce((total, question) => total + question.assistance.length, 0);
}

function statusFor(
  target: CoverageTarget,
  intents: Intent[],
  answeredEvaluations: Evaluation[],
): TargetStatus {
  if (intents.length === 0) return "unasked";

  const signalsPresent = new Set(answeredEvaluations.flatMap((item) => item.expectedSignalsPresent));
  const covered = target.expectedSignals.length > 0
    && target.expectedSignals.every((signal) => signalsPresent.has(signal));
  if (covered) return "satisfied";

  const last = intents[intents.length - 1];
  if (last.kind === "rescue" && last.style === "park") return "parked";
  return "open";
}

/**
 * Rebuilds the director's view of the session from persisted rows. Recomputed
 * every turn rather than stored, so it cannot drift from the questions and
 * evaluations it describes (spec §9.2).
 *
 * A non-answer turn contributes an intent and a spent turn but never evidence,
 * so it can never satisfy a target (spec §3.4).
 */
export function deriveCoverageState(
  targets: CoverageTarget[],
  questions: PlannedQuestion[],
  evaluations: Evaluation[],
): TargetState[] {
  return targets.map((target) => {
    const forTarget = questions.filter((question) => question.askedIntent && targetIdOf(question.askedIntent) === target.id);
    const intents = forTarget.map((question) => question.askedIntent as Intent);
    const scored = forTarget.filter((question) => !question.nonAnswer);
    const scoredIds = new Set(scored.map((question) => question.id));
    const relevantEvaluations = evaluations.filter((item) => scoredIds.has(item.questionId));

    return {
      target,
      status: statusFor(target, intents, relevantEvaluations),
      turnsSpent: forTarget.length,
      rescuesSpent: forTarget.reduce((total, question) => total + question.assistance.length, 0),
      askedIntents: intents,
    };
  });
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/interview-coverage.test.ts`
Expected: PASS, 8 tests.

If `Evaluation` lacks `questionId` or `expectedSignalsPresent`, check `src/lib/types.ts` — `GroundedEvaluation` carries them. Widen the parameter type to `GroundedEvaluation[]` rather than casting.

- [ ] **Step 5: Commit**

```bash
git add src/lib/interview-coverage.ts src/lib/interview-coverage.test.ts
git commit -m "feat: derive interview coverage state from persisted turns"
```

---

### Task 4: The director

**Files:**
- Create: `src/lib/interview-director.ts`
- Test: `src/lib/interview-director.test.ts`

**Interfaces:**
- Consumes: Task 1 types, Task 2 `roundFor`/`modePolicyFor`, Task 3 `deriveCoverageState`/`rescuesSpentInSession`.
- Produces:
  - `type DirectorInput = { round: InterviewRound; policy: ModePolicy; states: TargetState[]; currentTargetId: string | null; read: AssessmentRead; unsupportedClaims: string[]; answer: string; turnsUsed: number; turnBudget: number; sessionRescues: number }`
  - `type DirectorDecision = { intent: Intent; assistance: AssistanceRecord | null }`
  - `decideIntent(input: DirectorInput): DirectorDecision`

This is the task the whole design exists to make testable. Every rule in spec §9.3 is asserted here without a model call.

- [ ] **Step 1: Write the failing test**

Create `src/lib/interview-director.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { decideIntent, type DirectorInput } from "@/lib/interview-director";
import { modePolicyFor, roundFor } from "@/lib/interview-rounds";
import type { CoverageTarget, Intent, TargetState } from "@/lib/types";

function target(id: string, required = true): CoverageTarget {
  return {
    id,
    competencyId: `comp-${id}`,
    competencyName: `Competency ${id}`,
    category: "experience",
    evidenceIds: [],
    difficulty: "senior",
    objective: `Probe ${id}.`,
    expectedSignals: ["ownership"],
    rubricCriteria: ["Name a concrete example."],
    required,
  };
}

function state(id: string, overrides: Partial<TargetState> = {}): TargetState {
  return {
    target: target(id),
    status: "unasked",
    turnsSpent: 0,
    rescuesSpent: 0,
    askedIntents: [],
    ...overrides,
  };
}

function input(overrides: Partial<DirectorInput> = {}): DirectorInput {
  return {
    round: roundFor("tech-lead"),
    policy: modePolicyFor("coach"),
    states: [state("a")],
    currentTargetId: "a",
    read: "answered",
    unsupportedClaims: [],
    answer: "I owned the design system migration.",
    turnsUsed: 1,
    turnBudget: 8,
    sessionRescues: 0,
    ...overrides,
  };
}

describe("decideIntent — stuck candidates", () => {
  it("rescues rather than probes when the candidate is stuck", () => {
    const decision = decideIntent(input({ read: "stuck", answer: "i am having a blackout" }));
    expect(decision.intent.kind).toBe("rescue");
    expect(decision.assistance).not.toBeNull();
  });

  it("never probes or challenges a stuck candidate", () => {
    for (const policy of [modePolicyFor("coach"), modePolicyFor("real")]) {
      const decision = decideIntent(input({ read: "stuck", policy }));
      expect(["probe", "challenge"]).not.toContain(decision.intent.kind);
    }
  });

  it("advances once the per-question rescue budget is spent", () => {
    const spent = state("a", {
      status: "open",
      turnsSpent: 2,
      rescuesSpent: 2,
      askedIntents: [{ kind: "open", targetId: "a" }],
    });
    const decision = decideIntent(input({
      read: "stuck",
      states: [spent, state("b")],
      policy: modePolicyFor("coach"),
    }));
    expect(decision.intent.kind).toBe("advance");
    expect(decision.assistance).toBeNull();
  });

  it("parks in coach mode but never in real mode", () => {
    const stuckTwice = state("a", {
      status: "open",
      turnsSpent: 1,
      rescuesSpent: 1,
      askedIntents: [{ kind: "rescue", targetId: "a", style: "narrow", hook: null }],
    });
    const coach = decideIntent(input({ read: "stuck", states: [stuckTwice, state("b")], policy: modePolicyFor("coach") }));
    expect(coach.intent).toMatchObject({ kind: "rescue", style: "park" });

    const real = decideIntent(input({ read: "stuck", states: [stuckTwice, state("b")], policy: modePolicyFor("real") }));
    expect(real.intent.kind).toBe("advance");
  });

  it("stops rescuing once the session budget is spent", () => {
    const decision = decideIntent(input({ read: "stuck", sessionRescues: 5, policy: modePolicyFor("coach") }));
    expect(decision.intent.kind).toBe("advance");
  });
});

describe("decideIntent — repetition", () => {
  it("never issues an intent already asked for the target", () => {
    const asked: Intent[] = [
      { kind: "open", targetId: "a" },
      { kind: "probe", targetId: "a", aspect: "specifics", basis: "x" },
    ];
    const decision = decideIntent(input({
      read: "partial",
      states: [state("a", { status: "open", turnsSpent: 2, askedIntents: asked })],
    }));
    if (decision.intent.kind === "probe") {
      expect(decision.intent.aspect).not.toBe("specifics");
    }
  });

  it("advances when every available probe aspect is exhausted", () => {
    const asked: Intent[] = roundFor("tech-lead").probeAspects.map((aspect) => ({
      kind: "probe" as const,
      targetId: "a",
      aspect,
      basis: "x",
    }));
    const decision = decideIntent(input({
      read: "partial",
      states: [state("a", { status: "open", turnsSpent: 6, askedIntents: asked }), state("b")],
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", reason: "line-exhausted" });
  });
});

describe("decideIntent — coverage", () => {
  it("advances to the next target when the current one is satisfied", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "satisfied", turnsSpent: 2 }), state("b")],
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b", reason: "satisfied" });
  });

  it("prefers an unasked required target over deepening when turns run short", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "open", turnsSpent: 1 }), state("b"), state("c")],
      turnsUsed: 6,
      turnBudget: 8,
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", reason: "turn-budget" });
  });

  it("returns to a parked target when turns remain and nothing required is unasked", () => {
    const decision = decideIntent(input({
      states: [
        state("a", { status: "satisfied", turnsSpent: 2 }),
        state("b", {
          status: "parked",
          turnsSpent: 1,
          rescuesSpent: 1,
          askedIntents: [{ kind: "rescue", targetId: "b", style: "park", hook: null }],
        }),
      ],
      currentTargetId: "a",
    }));
    expect(decision.intent).toMatchObject({ kind: "advance", targetId: "b" });
  });

  it("closes with the round's closing move when the turn budget is exhausted", () => {
    const decision = decideIntent(input({
      states: [state("a", { status: "satisfied", turnsSpent: 3 })],
      turnsUsed: 8,
      turnBudget: 8,
    }));
    expect(decision.intent.kind).toBe("candidate-questions");
  });
});

describe("decideIntent — pressure", () => {
  it("challenges an unsupported claim in real mode", () => {
    const decision = decideIntent(input({
      policy: modePolicyFor("real"),
      read: "partial",
      unsupportedClaims: ["cut load time by 80%"],
      states: [state("a", { status: "open", turnsSpent: 1, askedIntents: [{ kind: "open", targetId: "a" }] })],
    }));
    expect(decision.intent).toMatchObject({ kind: "challenge", claim: "cut load time by 80%" });
  });

  it("only issues intents in the round's repertoire", () => {
    const round = roundFor("tech-lead");
    const reads = ["answered", "partial", "evasive", "stuck"] as const;
    for (const read of reads) {
      const decision = decideIntent(input({ read }));
      expect(round.moves).toContain(decision.intent.kind);
    }
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/interview-director.test.ts`
Expected: FAIL — `Failed to resolve import "@/lib/interview-director"`.

- [ ] **Step 3: Write the implementation**

Create `src/lib/interview-director.ts`:

```ts
import type { InterviewRound } from "@/lib/interview-rounds";
import type {
  AssessmentRead,
  AssistanceRecord,
  Intent,
  ModePolicy,
  ProbeAspect,
  RescueStyle,
  TargetState,
} from "@/lib/types";

export type DirectorInput = {
  round: InterviewRound;
  policy: ModePolicy;
  states: TargetState[];
  currentTargetId: string | null;
  read: AssessmentRead;
  unsupportedClaims: string[];
  answer: string;
  turnsUsed: number;
  turnBudget: number;
  sessionRescues: number;
};

export type DirectorDecision = {
  intent: Intent;
  /** Non-null exactly when the intent spends rescue budget. */
  assistance: AssistanceRecord | null;
};

/**
 * The order probe aspects are tried. Specifics first because an unsupported
 * claim is usually a missing detail rather than a missing idea; hindsight last
 * because it only makes sense once the story is on the table.
 */
const ASPECT_ORDER: ProbeAspect[] = ["specifics", "ownership", "tradeoff", "outcome", "collaboration", "hindsight"];

function currentState(input: DirectorInput): TargetState | null {
  return input.states.find((state) => state.target.id === input.currentTargetId) ?? null;
}

function askedAspects(state: TargetState): Set<ProbeAspect> {
  return new Set(
    state.askedIntents.flatMap((intent) => (intent.kind === "probe" ? [intent.aspect] : [])),
  );
}

function usedRescueStyles(state: TargetState): Set<RescueStyle> {
  return new Set(
    state.askedIntents.flatMap((intent) => (intent.kind === "rescue" ? [intent.style] : [])),
  );
}

function turnsRemaining(input: DirectorInput): number {
  return Math.max(0, input.turnBudget - input.turnsUsed);
}

/** Targets that still need a first question, required ones first. */
function unaskedTargets(input: DirectorInput): TargetState[] {
  return input.states
    .filter((state) => state.status === "unasked")
    .sort((left, right) => Number(right.target.required) - Number(left.target.required));
}

function parkedTargets(input: DirectorInput): TargetState[] {
  return input.states.filter((state) => state.status === "parked");
}

function advance(input: DirectorInput, reason: Intent extends { reason: infer R } ? R : never): DirectorDecision | null {
  const next = unaskedTargets(input)[0] ?? parkedTargets(input)[0] ?? null;
  if (!next) return null;
  return { intent: { kind: "advance", targetId: next.target.id, reason }, assistance: null };
}

function closing(input: DirectorInput): DirectorDecision {
  return { intent: { kind: input.round.closing }, assistance: null } as DirectorDecision;
}

/**
 * Chooses the next rescue style: styles the mode allows, that have not already
 * been used on this target, in escalating order. `park` is last because setting
 * the question aside is the strongest move available.
 */
function nextRescueStyle(state: TargetState, policy: ModePolicy): RescueStyle | null {
  const used = usedRescueStyles(state);
  const order: RescueStyle[] = ["narrow", "hook", "reframe", "park"];
  return order.find((style) => policy.rescueStyles.includes(style) && !used.has(style)) ?? null;
}

function hookFor(state: TargetState): string | null {
  return state.target.competencyName;
}

/**
 * Computes the single move the interviewer makes next.
 *
 * Ordering matters and encodes spec §9.3: a stuck candidate is never probed;
 * running out of turns outranks deepening an open thread; a target is finished
 * when its signals are present, not after a fixed number of questions.
 */
export function decideIntent(input: DirectorInput): DirectorDecision {
  const state = currentState(input);
  const remaining = turnsRemaining(input);
  const unasked = unaskedTargets(input);
  const requiredUnasked = unasked.filter((item) => item.target.required);

  // Rule 6: a non-answer never earns a harder question.
  if (input.read === "stuck" && state) {
    const questionBudget = state.rescuesSpent < input.policy.rescuesPerQuestion;
    const sessionBudget = input.sessionRescues < input.policy.rescuesPerSession;
    const style = nextRescueStyle(state, input.policy);
    const parkAllowed = style !== "park" || input.policy.parkAndReturn;

    if (questionBudget && sessionBudget && style && parkAllowed) {
      return {
        intent: { kind: "rescue", targetId: state.target.id, style, hook: style === "hook" ? hookFor(state) : null },
        assistance: { style, at: new Date().toISOString() },
      };
    }
    return advance(input, "rescue-budget-spent") ?? closing(input);
  }

  // Rule 4: unasked required coverage outranks deepening when turns run short.
  if (requiredUnasked.length > 0 && remaining <= requiredUnasked.length) {
    return advance(input, "turn-budget") ?? closing(input);
  }

  if (remaining <= 0) return closing(input);

  // Rule 2: a satisfied target is finished, whatever its question count.
  if (!state || state.status === "satisfied") {
    return advance(input, "satisfied") ?? closing(input);
  }

  if (state.status === "unasked") {
    return { intent: { kind: "open", targetId: state.target.id }, assistance: null };
  }

  const unsupported = input.unsupportedClaims[0];
  const alreadyChallenged = state.askedIntents.some((intent) => intent.kind === "challenge" && intent.claim === unsupported);
  if (unsupported && !alreadyChallenged && input.round.moves.includes("challenge")) {
    return { intent: { kind: "challenge", targetId: state.target.id, claim: unsupported }, assistance: null };
  }

  // Rule 1: never repeat an intent already issued for this target.
  const asked = askedAspects(state);
  const aspect = ASPECT_ORDER.find((item) => input.round.probeAspects.includes(item) && !asked.has(item));
  if (aspect) {
    return {
      intent: { kind: "probe", targetId: state.target.id, aspect, basis: input.answer },
      assistance: null,
    };
  }

  return advance(input, "line-exhausted") ?? closing(input);
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/interview-director.test.ts`
Expected: PASS, 12 tests.

If the `advance` helper's generic `reason` parameter fights the compiler, replace its signature with an explicit `reason: AdvanceReason` and import `AdvanceReason` from types. The inference trick is not worth a fight.

- [ ] **Step 5: Commit**

```bash
git add src/lib/interview-director.ts src/lib/interview-director.test.ts
git commit -m "feat: add the deterministic interview director"
```

---

### Task 5: Interviewer voice — guardrail validation

**Files:**
- Create: `src/lib/interviewer-voice.ts`
- Test: `src/lib/interviewer-voice.test.ts`

**Interfaces:**
- Consumes: Task 1 types.
- Produces:
  - `type LineViolation = "rubric-leak" | "contact-details" | "too-long" | "question-not-last" | "no-question" | "repeats-asked" | "coaching"`
  - `validateInterviewerLine(line: string, context: { forbiddenRubricText: string[]; askedPrompts: string[]; policy: ModePolicy }): LineViolation | null`
  - `deterministicLine(intent: Intent, competencyName: string | null): string`

Validation is pure and gets its own task because it is the safety net for the entire release. The model call that uses it is Task 6.

- [ ] **Step 1: Write the failing test**

Create `src/lib/interviewer-voice.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { deterministicLine, validateInterviewerLine } from "@/lib/interviewer-voice";
import { modePolicyFor } from "@/lib/interview-rounds";

const context = {
  forbiddenRubricText: ["Probe Frontend Architecture with concrete evidence.", "ownership"],
  askedPrompts: ["Tell me about the design system migration."],
  policy: modePolicyFor("real"),
};

describe("validateInterviewerLine", () => {
  it("accepts a short, single, final question", () => {
    expect(validateInterviewerLine("What did you change first?", context)).toBeNull();
  });

  it("rejects rubric text", () => {
    const line = "Probe Frontend Architecture with concrete evidence. What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("rubric-leak");
  });

  it("rejects contact details and URLs", () => {
    expect(validateInterviewerLine("Your CV lists amitbaz2@gmail.com. What did you own?", context)).toBe("contact-details");
    expect(validateInterviewerLine("You link linkedin.com/in/amit-baz. What did you own?", context)).toBe("contact-details");
    expect(validateInterviewerLine("Your number is +49 177 2276319. What did you own?", context)).toBe("contact-details");
  });

  it("rejects more than two sentences", () => {
    const line = "You mentioned the migration. It sounds involved. It ran for months. What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("too-long");
  });

  it("rejects a line whose question is not last", () => {
    const line = "What did you own? Take your time with that.";
    expect(validateInterviewerLine(line, context)).toBe("question-not-last");
  });

  it("rejects a line with no question", () => {
    expect(validateInterviewerLine("That sounds like a big migration.", context)).toBe("no-question");
  });

  it("rejects a paraphrase of a question already asked", () => {
    const line = "Tell me about the design system migration.";
    expect(validateInterviewerLine(line, { ...context, askedPrompts: [line] })).toBe("repeats-asked");
  });

  it("rejects praise in real mode but allows a brief acknowledgement in coach mode", () => {
    const line = "Great answer. What did you own?";
    expect(validateInterviewerLine(line, context)).toBe("coaching");
    expect(validateInterviewerLine(line, { ...context, policy: modePolicyFor("coach") })).toBeNull();
  });
});

describe("deterministicLine", () => {
  it("returns a distinct line for every intent kind", () => {
    const lines = new Set([
      deterministicLine({ kind: "open", targetId: "a" }, "Frontend Architecture"),
      deterministicLine({ kind: "probe", targetId: "a", aspect: "ownership", basis: "x" }, "Frontend Architecture"),
      deterministicLine({ kind: "challenge", targetId: "a", claim: "80% faster" }, "Frontend Architecture"),
      deterministicLine({ kind: "rescue", targetId: "a", style: "narrow", hook: null }, "Frontend Architecture"),
      deterministicLine({ kind: "advance", targetId: "b", reason: "satisfied" }, "System Design"),
      deterministicLine({ kind: "hypothetical", targetId: "a", basis: "x" }, "Frontend Architecture"),
      deterministicLine({ kind: "candidate-questions" }, null),
      deterministicLine({ kind: "close" }, null),
    ]);
    expect(lines.size).toBe(8);
  });

  it("passes its own validation", () => {
    const line = deterministicLine({ kind: "probe", targetId: "a", aspect: "ownership", basis: "x" }, "Frontend Architecture");
    expect(validateInterviewerLine(line, { forbiddenRubricText: [], askedPrompts: [], policy: modePolicyFor("real") })).toBeNull();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/interviewer-voice.test.ts`
Expected: FAIL — `Failed to resolve import "@/lib/interviewer-voice"`.

- [ ] **Step 3: Write the implementation**

Create `src/lib/interviewer-voice.ts`:

```ts
import type { Intent, ModePolicy } from "@/lib/types";

export type LineViolation =
  | "rubric-leak"
  | "contact-details"
  | "too-long"
  | "question-not-last"
  | "no-question"
  | "repeats-asked"
  | "coaching";

export type LineContext = {
  /** Objective, expected signal, and rubric strings that must never be echoed. */
  forbiddenRubricText: string[];
  askedPrompts: string[];
  policy: ModePolicy;
};

const EMAIL = /[\w.+-]+@[\w-]+\.[\w.]+/;
const URL = /\b(?:https?:\/\/|www\.)\S+|\b[\w-]+\.(?:com|io|dev|de|org|net|co)\/\S*/i;
const PHONE = /(?:\+\d[\d\s().-]{7,})|(?:\b\d{3,}[\s.-]\d{3,}[\s.-]\d{3,}\b)/;
const PRAISE = /\b(great|excellent|perfect|well done|nice|good answer|brilliant|impressive)\b/i;

function sentences(line: string): string[] {
  return line.split(/(?<=[.!?])\s+/).map((part) => part.trim()).filter(Boolean);
}

function normalize(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();
}

/** Jaccard overlap on word sets: cheap, and enough to catch a reworded repeat. */
function similarity(left: string, right: string): number {
  const a = new Set(normalize(left).split(" ").filter((word) => word.length > 3));
  const b = new Set(normalize(right).split(" ").filter((word) => word.length > 3));
  if (a.size === 0 || b.size === 0) return 0;
  const shared = [...a].filter((word) => b.has(word)).length;
  return shared / new Set([...a, ...b]).size;
}

/**
 * Enforces spec §11 on interviewer output. Returns the violated rule, or null
 * when the line is acceptable.
 *
 * This is a net, not the primary defence: the interviewer call never receives
 * rubric text in the first place (spec §11.1). Catching a leak here means the
 * prompt assembly is wrong, not merely that the model misbehaved.
 */
export function validateInterviewerLine(line: string, context: LineContext): LineViolation | null {
  const trimmed = line.trim();
  const lower = trimmed.toLowerCase();

  for (const forbidden of context.forbiddenRubricText) {
    const needle = forbidden.trim().toLowerCase();
    if (needle.length > 12 && lower.includes(needle)) return "rubric-leak";
  }

  if (EMAIL.test(trimmed) || URL.test(trimmed) || PHONE.test(trimmed)) return "contact-details";

  const parts = sentences(trimmed);
  if (parts.length > 2) return "too-long";
  if (!trimmed.includes("?")) return "no-question";
  if (!parts[parts.length - 1].includes("?")) return "question-not-last";

  if (context.askedPrompts.some((asked) => similarity(asked, trimmed) > 0.6)) return "repeats-asked";

  if (!context.policy.acknowledgeStruggle && PRAISE.test(trimmed)) return "coaching";

  return null;
}

/**
 * The degraded path (spec §13.2). One short line per intent kind, used only
 * when the interviewer call fails or fails validation twice. This is the sole
 * surviving use of templates in the system.
 */
export function deterministicLine(intent: Intent, competencyName: string | null): string {
  const subject = competencyName ?? "that work";
  switch (intent.kind) {
    case "open":
      return `Tell me about your work on ${subject}?`;
    case "probe":
      return `What part of that was yours specifically?`;
    case "challenge":
      return `How do you know ${intent.claim} was the result?`;
    case "rescue":
      return `Let's make it smaller — what is one thing you changed?`;
    case "advance":
      return `Let's move on — what can you tell me about ${subject}?`;
    case "hypothetical":
      return `If that constraint doubled, what would you change first?`;
    case "candidate-questions":
      return `That is what I wanted to cover — what would you like to ask me?`;
    case "close":
      return `Thanks, that is everything from my side — anything you want to add?`;
  }
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/interviewer-voice.test.ts`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add src/lib/interviewer-voice.ts src/lib/interviewer-voice.test.ts
git commit -m "feat: add interviewer line guardrails and degraded fallback lines"
```

---

### Task 6: Assessor read, and the interviewer model call

**Files:**
- Modify: `src/lib/coach.ts` (`turnSchema` near line 53; `turnPrompt` near line 1548; `evaluateTurn` near line 1573)
- Modify: `src/lib/coach.test.ts`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces:
  - `assessorSchema` — `{ read: AssessmentRead; evaluation: groundedEvaluationSchema }`, replacing `turnSchema`
  - `speakIntent(intent: Intent, context: SpeakContext): Promise<string>` exported from `coach.ts`
  - `type SpeakContext = { round: InterviewRound; policy: ModePolicy; competencyName: string | null; evidence: EvidenceItem[]; opportunity: Pick<Opportunity, "company" | "role" | "jobDescription"> | null; transcript: string; askedPrompts: string[]; forbiddenRubricText: string[] }`

- [ ] **Step 1: Write the failing tests**

Add to `src/lib/coach.test.ts`:

```ts
describe("speakIntent", () => {
  it("never puts rubric text in the interviewer prompt", async () => {
    const captured: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      captured.push(String(init.body));
      return new Response(JSON.stringify({
        candidates: [{ content: { parts: [{ text: JSON.stringify({ line: "What did you own there?" }) }] } }],
      }), { status: 200 });
    }));

    await speakIntent(
      { kind: "probe", targetId: "a", aspect: "ownership", basis: "the migration" },
      {
        round: roundFor("tech-lead"),
        policy: modePolicyFor("real"),
        competencyName: "Frontend Architecture",
        evidence: [],
        opportunity: null,
        transcript: "interviewer: hello\ncandidate: hi",
        askedPrompts: [],
        forbiddenRubricText: ["Probe Frontend Architecture with concrete evidence.", "ownership signal"],
      },
    );

    expect(captured).toHaveLength(1);
    expect(captured[0]).not.toContain("Probe Frontend Architecture with concrete evidence.");
    expect(captured[0]).not.toContain("ownership signal");
    expect(captured[0]).not.toContain("rubricCriteria");
    expect(captured[0]).not.toContain("expectedSignals");
  });

  it("falls back to the deterministic line when validation fails twice", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: JSON.stringify({ line: "Mail me at a@b.com. What did you own?" }) }] } }],
    }), { status: 200 })));

    const line = await speakIntent(
      { kind: "probe", targetId: "a", aspect: "ownership", basis: "x" },
      {
        round: roundFor("tech-lead"),
        policy: modePolicyFor("real"),
        competencyName: "Frontend Architecture",
        evidence: [],
        opportunity: null,
        transcript: "",
        askedPrompts: [],
        forbiddenRubricText: [],
      },
    );

    expect(line).toBe(deterministicLine({ kind: "probe", targetId: "a", aspect: "ownership", basis: "x" }, "Frontend Architecture"));
  });

  it("never sends raw CV text, only structured evidence fields", async () => {
    const captured: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      captured.push(String(init.body));
      return new Response(JSON.stringify({
        candidates: [{ content: { parts: [{ text: JSON.stringify({ line: "What did you own there?" }) }] } }],
      }), { status: 200 });
    }));

    await speakIntent(
      { kind: "open", targetId: "a" },
      {
        round: roundFor("tech-lead"),
        policy: modePolicyFor("real"),
        competencyName: "Frontend Architecture",
        evidence: [{
          id: "e1",
          sourceKind: "cv",
          sourceExcerpt: "Amit Baz | +49 177 2276319 | amitbaz2@gmail.com",
          projectOrEmployer: "Acme",
          ownership: "Owned the design system migration",
          technologies: ["React"],
          decision: null,
          constraint: null,
          outcome: null,
          recency: null,
          confidence: 0.8,
        } as EvidenceItem],
        opportunity: null,
        transcript: "",
        askedPrompts: [],
        forbiddenRubricText: [],
      },
    );

    expect(captured[0]).toContain("Owned the design system migration");
    expect(captured[0]).not.toContain("amitbaz2@gmail.com");
    expect(captured[0]).not.toContain("2276319");
  });
});
```

Add the imports these tests need at the top of `coach.test.ts`:

```ts
import { speakIntent } from "@/lib/coach";
import { modePolicyFor, roundFor } from "@/lib/interview-rounds";
import { deterministicLine } from "@/lib/interviewer-voice";
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx vitest run src/lib/coach.test.ts -t "speakIntent"`
Expected: FAIL — `speakIntent is not exported`.

- [ ] **Step 3: Replace `turnSchema` with the assessor schema**

In `src/lib/coach.ts`, replace the `turnSchema` declaration (line 53):

```ts
// The assessor scores privately and reports a coarse read. It no longer
// authors questions: speech is a separate call that never sees this rubric.
const assessorSchema = z.object({
  read: z.enum(["answered", "partial", "evasive", "stuck"]),
  evaluation: groundedEvaluationSchema,
});
const interviewerLineSchema = z.object({ line: z.string().min(1) });
```

Delete `turnSchema` entirely.

- [ ] **Step 4: Rewrite `turnPrompt` as the assessor prompt**

Replace `turnPrompt` (line 1548) with:

```ts
/**
 * The assessor prompt. Scores privately against the rubric and classifies the
 * answer. It authors no candidate-facing text, so the rubric can appear here
 * safely -- that is the whole point of the split (spec §6.1).
 */
function assessorPrompt(
  question: BlueprintQuestion,
  profile: Pick<ProfileDraft, "role" | "seniority" | "expertise" | "narrative">,
  answer: string,
  transcript: string,
): string {
  return [
    "You are an experienced senior software-engineering interviewer.",
    "Privately evaluate the latest answer against the exact question rubric.",
    "Return only valid JSON.",
    "Ground the scoring in the exact question objective, expected signals, and the candidate's answer.",
    "Do not praise, coach, reveal scores, or invent facts.",
    "Classify the answer with `read`:",
    "  answered  - a genuine attempt that addressed the question",
    "  partial   - a genuine attempt that left the objective largely uncovered",
    "  evasive   - talked at length without engaging the question",
    "  stuck     - did not attempt the question: said they do not know, cannot",
    "              find words, are blanking, or asked to move on.",
    "`stuck` is about the absence of an attempt, never about a weak attempt.",
    hasSourceEvidenceTarget(question)
      ? ""
      : "Grounding rule: question.evidenceIds is empty, so this is a discovery/general objective. Treat first-person career details in the candidate's answer as newly supplied session evidence. Do not mark them unsupported merely because they were absent from the source profile. Never invent missing details; improved answers may only reuse facts actually supplied by the candidate or already grounded by the question context.",
    `Question: ${JSON.stringify(question)}`,
    `Rubric criteria: ${JSON.stringify(question.rubricCriteria ?? [])}`,
    `Profile: ${JSON.stringify(profile)}`,
    `Transcript: ${transcript}`,
    `Latest answer: ${answer}`,
  ].filter(Boolean).join("\n");
}
```

- [ ] **Step 5: Add the interviewer call**

Add to `src/lib/coach.ts`:

```ts
export type SpeakContext = {
  round: InterviewRound;
  policy: ModePolicy;
  competencyName: string | null;
  evidence: EvidenceItem[];
  opportunity: Pick<Opportunity, "company" | "role" | "jobDescription"> | null;
  transcript: string;
  askedPrompts: string[];
  /** Strings the line must not echo; never themselves sent to the model. */
  forbiddenRubricText: string[];
};

/**
 * Structured evidence only. The interviewer refers to what a CV says; it never
 * recites it, and `sourceExcerpt` -- which carries raw CV text including
 * contact details -- is deliberately excluded (spec §11.2).
 */
function evidenceForSpeech(evidence: EvidenceItem[]): Array<Record<string, unknown>> {
  return evidence.slice(0, 6).map((item) => ({
    projectOrEmployer: item.projectOrEmployer,
    ownership: item.ownership,
    technologies: item.technologies,
    decision: item.decision,
    constraint: item.constraint,
    outcome: item.outcome,
  }));
}

function intentInstruction(intent: Intent, policy: ModePolicy): string {
  switch (intent.kind) {
    case "open":
      return "Open a new thread on the subject below. Ask what they worked on, in your own words.";
    case "probe":
      return `Press on "${intent.aspect}" in what they just said: "${intent.basis}". Ask for the missing specific.`;
    case "challenge":
      return `They claimed "${intent.claim}" without support. Ask how they know, without accusing them.`;
    case "rescue":
      return rescueInstruction(intent.style, intent.hook, policy);
    case "advance":
      return "Close the current thread briefly and open the new subject below.";
    case "hypothetical":
      return `Pose one short hypothetical grounded in what they described: "${intent.basis}".`;
    case "candidate-questions":
      return "Signal that you have covered what you wanted and invite their questions.";
    case "close":
      return "Close the conversation.";
  }
}

function rescueInstruction(style: RescueStyle, hook: string | null, policy: ModePolicy): string {
  const acknowledge = policy.acknowledgeStruggle
    ? "Acknowledge the difficulty in at most one short clause first. "
    : "Do not acknowledge the difficulty. ";
  switch (style) {
    case "narrow":
      return `${acknowledge}They could not answer. Ask a much smaller version of the same question.`;
    case "hook":
      return `${acknowledge}They could not answer. Hand them a concrete starting point${hook ? ` from their work on ${hook}` : ""} and ask them to start there.`;
    case "reframe":
      return `${acknowledge}They could not answer. Ask for the same material as a story about one specific occasion.`;
    case "park":
      return `${acknowledge}They could not answer. Say you will come back to it, and move to the new subject below.`;
  }
}

/**
 * Authors one line of interviewer speech for an already-decided intent.
 *
 * This call NEVER receives the objective, expected signals, rubric criteria, or
 * any score. That exclusion is structural, not filtered, and is the fix for the
 * rubric leak that commit 02ec2c1 worked around by removing model-authored
 * questions altogether.
 */
export async function speakIntent(intent: Intent, context: SpeakContext): Promise<string> {
  const prompt = [
    context.round.personaStake,
    `Your manner: ${context.round.register}`,
    `You never raise: ${context.round.outOfScope.join(", ")}.`,
    "Say one thing only, in at most two sentences, ending with exactly one question.",
    "Never quote a CV or job description. Never state the candidate's contact details.",
    context.policy.pushback === "firm"
      ? "Do not praise, encourage, or hint."
      : "You may acknowledge difficulty in one short clause. Never coach or hint at an answer.",
    `Your move: ${intentInstruction(intent, context.policy)}`,
    context.competencyName ? `Subject: ${context.competencyName}` : "",
    `What you know about their work: ${JSON.stringify(evidenceForSpeech(context.evidence))}`,
    context.opportunity
      ? `You are hiring for ${context.opportunity.role} at ${context.opportunity.company}. Role context: ${(context.opportunity.jobDescription ?? "").slice(0, 1200)}`
      : "",
    `Conversation so far:\n${context.transcript}`,
    context.askedPrompts.length
      ? `Already asked -- do not repeat or paraphrase these:\n${context.askedPrompts.join("\n")}`
      : "",
  ].filter(Boolean).join("\n");

  const lineContext = {
    forbiddenRubricText: context.forbiddenRubricText,
    askedPrompts: context.askedPrompts,
    policy: context.policy,
  };

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const result = await modelJson(
      "interviewer line",
      attempt === 0 ? prompt : `${prompt}\nYour previous attempt broke a rule. Try again, shorter and more direct.`,
      interviewerLineSchema,
    );
    if (!result) break;
    const violation = validateInterviewerLine(result.line, lineContext);
    if (!violation) return result.line.trim();
    console.warn("[gemini] interviewer line rejected", { operation: "interviewer line", intent: intent.kind, violation });
  }

  return deterministicLine(intent, context.competencyName);
}
```

Add the imports at the top of `coach.ts`:

```ts
import { modePolicyFor, roundFor, type InterviewRound } from "@/lib/interview-rounds";
import { deterministicLine, validateInterviewerLine } from "@/lib/interviewer-voice";
import type { AssessmentRead, Intent, ModePolicy, RescueStyle } from "@/lib/types";
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `npx vitest run src/lib/coach.test.ts -t "speakIntent"`
Expected: PASS, 3 tests. Other `coach.test.ts` tests will still fail — `evaluateTurn` is rewired in Task 7.

- [ ] **Step 7: Commit**

```bash
git add src/lib/coach.ts src/lib/coach.test.ts
git commit -m "feat: split the interview turn into a private assessor and an interviewer voice"
```

---

### Task 7: Wire the pipeline into `nextTurn`, delete the templates

**Files:**
- Modify: `src/lib/coach.ts` (`evaluateTurn`, `nextTurn`, `initialQuestion`; delete `promptForPlan`, `deterministicFollowUp`, `cvExcerpt`, `shouldAskFollowUp`, `followUpDraft`)
- Modify: `src/lib/coach.test.ts`

**Interfaces:**
- Consumes: Tasks 1–6.
- Produces:
  - `nextTurn(input: NextTurnInput): Promise<NextTurnResult>`
  - `type NextTurnInput = { profile; session: InterviewSession; answeredQuestion: PlannedQuestion; answer: string; blueprint: InterviewBlueprint; evidence: EvidenceItem[]; opportunity: Pick<Opportunity, "company" | "role" | "jobDescription"> | null }`
  - `type NextTurnResult = { evaluation: GroundedEvaluation | null; nonAnswer: boolean; intent: Intent; prompt: string; assistance: AssistanceRecord | null; targetId: string | null; degraded: boolean }`
  - `openingTurn(input: Omit<NextTurnInput, "answeredQuestion" | "answer">): Promise<{ intent: Intent; prompt: string; targetId: string }>` replacing `initialQuestion`

- [ ] **Step 1: Write the failing tests**

Add to `src/lib/coach.test.ts`. These are the three regression tests from spec §14.3 — the literal acceptance criteria for the release:

```ts
describe("nextTurn — regressions from the observed session", () => {
  it("rescues instead of probing harder when the candidate blanks", async () => {
    stubGemini({ read: "stuck", evaluation: sampleGroundedEvaluation() }, { line: "Let's make it smaller — what is one screen you changed?" });

    const result = await nextTurn(nextTurnInput({ answer: "i am having a blackout" }));

    expect(result.intent.kind).toBe("rescue");
    expect(result.nonAnswer).toBe(true);
    expect(result.evaluation).toBeNull();
    expect(result.assistance).not.toBeNull();
  });

  it("never lets an unpunctuated CV header reach a question", async () => {
    const captured: string[] = [];
    stubGeminiCapturing(captured, { read: "answered", evaluation: sampleGroundedEvaluation() }, { line: "What did you own there?" });

    await nextTurn(nextTurnInput({
      evidence: [{
        id: "e1",
        sourceKind: "cv",
        sourceExcerpt: "Amit Baz Senior Product Engineer | Berlin, Germany | +49 177 2276319 | amitbaz2@gmail.com",
        projectOrEmployer: "Acme",
        ownership: "Owned frontend architecture",
        technologies: ["React"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: null,
        confidence: 0.9,
      } as EvidenceItem],
    }));

    const interviewerCall = captured[captured.length - 1];
    expect(interviewerCall).not.toContain("2276319");
    expect(interviewerCall).not.toContain("amitbaz2@gmail.com");
  });

  it("does not ask the same follow-up twice across different targets", async () => {
    const first = await nextTurn(nextTurnInput({
      answeredQuestion: answeredQuestion("q1", { kind: "open", targetId: "a" }),
    }));
    const second = await nextTurn(nextTurnInput({
      answeredQuestion: answeredQuestion("q2", { kind: "open", targetId: "b" }),
    }));
    expect(first.prompt).not.toBe(second.prompt);
  });
});
```

Add these helpers near the top of `coach.test.ts`:

```ts
function stubGemini(assessor: unknown, line: unknown) {
  const responses = [assessor, line];
  let call = 0;
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    candidates: [{ content: { parts: [{ text: JSON.stringify(responses[Math.min(call++, 1)]) }] } }],
  }), { status: 200 })));
}

function stubGeminiCapturing(sink: string[], assessor: unknown, line: unknown) {
  const responses = [assessor, line];
  let call = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
    sink.push(String(init.body));
    return new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: JSON.stringify(responses[Math.min(call++, 1)]) }] } }],
    }), { status: 200 });
  }));
}
```

Write `sampleGroundedEvaluation()`, `nextTurnInput()`, and `answeredQuestion()` as local fixture builders matching the existing fixture style in `coach.test.ts`. Reuse the file's existing profile and blueprint fixtures rather than inventing new ones.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `npx vitest run src/lib/coach.test.ts -t "regressions from the observed session"`
Expected: FAIL — `nextTurn` does not accept a single input object yet.

- [ ] **Step 3: Rewrite `evaluateTurn` and `nextTurn`**

Replace `evaluateTurn`, `nextTurn`, `initialQuestion`, `promptForPlan`, `deterministicFollowUp`, `cvExcerpt`, `shouldAskFollowUp`, and `followUpDraft` in `src/lib/coach.ts` with:

```ts
export type NextTurnInput = {
  profile: Pick<ProfileDraft, "role" | "seniority" | "expertise" | "narrative">;
  session: InterviewSession;
  answeredQuestion: PlannedQuestion;
  answer: string;
  blueprint: InterviewBlueprint;
  evidence: EvidenceItem[];
  opportunity: Pick<Opportunity, "company" | "role" | "jobDescription"> | null;
};

export type NextTurnResult = {
  /** Null when the candidate did not attempt the question (spec §3.4). */
  evaluation: GroundedEvaluation | null;
  nonAnswer: boolean;
  intent: Intent;
  prompt: string;
  assistance: AssistanceRecord | null;
  targetId: string | null;
  degraded: boolean;
};

function targetById(blueprint: InterviewBlueprint, id: string | null): CoverageTarget | null {
  return blueprint.targets.find((target) => target.id === id) ?? null;
}

function forbiddenRubricText(blueprint: InterviewBlueprint): string[] {
  return blueprint.targets.flatMap((target) => [target.objective, ...target.expectedSignals, ...target.rubricCriteria]);
}

function askedPromptsOf(session: InterviewSession): string[] {
  return session.questions.map((question) => question.prompt).filter((prompt): prompt is string => Boolean(prompt));
}

/**
 * Runs one full turn: assess privately, decide deterministically, then speak.
 *
 * The order is load-bearing. The director never sees model-authored prose, and
 * the interviewer never sees the rubric.
 */
export async function nextTurn(input: NextTurnInput): Promise<NextTurnResult> {
  const { blueprint, session, answeredQuestion, answer } = input;
  const round = roundFor(blueprint.roundId);
  const policy = modePolicyFor(session.mode);
  const rubric = groundedQuestion(answeredQuestion, blueprint);
  const transcript = session.messages.map((message) => `${message.role}: ${message.content}`).join("\n");

  const assessment = await modelJson(
    "answer evaluation",
    assessorPrompt(rubric, input.profile, answer, transcript),
    assessorSchema,
  );
  let degraded = assessment === null;

  const read: AssessmentRead = assessment?.read ?? "answered";
  const nonAnswer = read === "stuck";
  const evaluation = nonAnswer
    ? null
    : assessment
      ? validateGroundedModelEvaluation(rubric, answer, assessment.evaluation).evaluation
      : groundedEvaluationFor(rubric, answer);

  const currentTargetId = answeredQuestion.askedIntent ? targetIdOf(answeredQuestion.askedIntent) : null;
  const states = deriveCoverageState(blueprint.targets, session.questions, session.evaluations);

  const decision = decideIntent({
    round,
    policy,
    states,
    currentTargetId,
    read,
    unsupportedClaims: evaluation?.unsupportedClaims ?? [],
    answer,
    turnsUsed: session.questions.filter((question) => question.answer !== null).length,
    turnBudget: blueprint.turnBudget,
    sessionRescues: rescuesSpentInSession(session.questions),
  });

  const nextTargetId = targetIdOf(decision.intent);
  const nextTarget = targetById(blueprint, nextTargetId);

  const prompt = await speakIntent(decision.intent, {
    round,
    policy,
    competencyName: nextTarget?.competencyName ?? null,
    evidence: input.evidence,
    opportunity: input.opportunity,
    transcript,
    askedPrompts: askedPromptsOf(session),
    forbiddenRubricText: forbiddenRubricText(blueprint),
  });

  if (prompt === deterministicLine(decision.intent, nextTarget?.competencyName ?? null)) degraded = true;

  return {
    evaluation,
    nonAnswer,
    intent: decision.intent,
    prompt,
    assistance: decision.assistance,
    targetId: nextTargetId,
    degraded,
  };
}

/** Authors the session's first question. Replaces `initialQuestion`. */
export async function openingTurn(input: Omit<NextTurnInput, "answeredQuestion" | "answer">): Promise<{
  intent: Intent;
  prompt: string;
  targetId: string;
}> {
  const { blueprint, session } = input;
  const round = roundFor(blueprint.roundId);
  const first = blueprint.targets[0];
  if (!first) throw new Error("An interview blueprint needs at least one coverage target.");
  const intent: Intent = { kind: round.opening === "open" ? "open" : "open", targetId: first.id };

  const prompt = await speakIntent(intent, {
    round,
    policy: modePolicyFor(session.mode),
    competencyName: first.competencyName,
    evidence: input.evidence,
    opportunity: input.opportunity,
    transcript: "",
    askedPrompts: [],
    forbiddenRubricText: forbiddenRubricText(blueprint),
  });

  return { intent, prompt, targetId: first.id };
}
```

Add the imports:

```ts
import { deriveCoverageState, rescuesSpentInSession, targetIdOf } from "@/lib/interview-coverage";
import { decideIntent } from "@/lib/interview-director";
import type { AssistanceRecord, CoverageTarget } from "@/lib/types";
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run src/lib/coach.test.ts`
Expected: the three regression tests PASS. Existing tests that assert template text now fail — that is correct, those templates are gone.

- [ ] **Step 5: Delete the obsolete tests**

Remove every test in `coach.test.ts` asserting `promptForPlan`, `deterministicFollowUp`, or `cvExcerpt` output. Do not rewrite them to assert new template text; the point is that no template is on the normal path. Coverage for the behaviour they guarded now lives in Tasks 3–5.

- [ ] **Step 6: Run the full file**

Run: `npx vitest run src/lib/coach.test.ts`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/lib/coach.ts src/lib/coach.test.ts
git commit -m "feat: run interview turns through assessor, director, and interviewer"
```

---

### Task 8: Blueprint becomes a coverage plan

**Files:**
- Modify: `src/lib/interview-planner.ts` (delete `promptFor` at line 94, `blueprintPrompt`, `discoveryPrompt` at line 506; add target builders; relax `validateInterviewBlueprint` at line 425)
- Modify: `src/lib/interview-planner.test.ts`

**Interfaces:**
- Consumes: Task 1 types.
- Produces:
  - `buildCoverageTargets(profile, evidence, opportunity, roundId): CoverageTarget[]`
  - `validateInterviewBlueprint` no longer requires prompt text and validates targets instead
  - `generateInterviewBlueprint` in `coach.ts` gains a third parameter: `options: { roundId: RoundId; opportunity: Pick<Opportunity, "gaps" | "jobDescription"> | null }`, and populates `roundId`, `turnBudget` (default 8), and `targets` on the blueprint it returns

- [ ] **Step 1: Write the failing test**

Add to `src/lib/interview-planner.test.ts`:

```ts
describe("buildCoverageTargets", () => {
  it("produces targets with rubric material and no prompt text", () => {
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead");
    expect(targets.length).toBeGreaterThan(0);
    for (const target of targets) {
      expect(target.objective.length).toBeGreaterThan(0);
      expect(target.rubricCriteria.length).toBeGreaterThan(0);
      expect(target).not.toHaveProperty("prompt");
    }
  });

  it("makes every opportunity gap a required target when anchored", () => {
    const opportunity = { ...sampleOpportunity(), gaps: ["Testing strategy", "Observability"] };
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), opportunity, "tech-lead");
    const required = targets.filter((target) => target.required).map((target) => target.competencyName);
    expect(required).toEqual(expect.arrayContaining(["Testing strategy", "Observability"]));
  });

  it("gives every target a unique id", () => {
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead");
    expect(new Set(targets.map((target) => target.id)).size).toBe(targets.length);
  });
});

describe("validateInterviewBlueprint", () => {
  it("accepts a blueprint whose questions have no prompt text", () => {
    const blueprint = sampleBlueprint({ targets: buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead") });
    expect(() => validateInterviewBlueprint(blueprint)).not.toThrow();
  });

  it("rejects a blueprint with no required target", () => {
    const targets = buildCoverageTargets(sampleProfile(), sampleEvidence(), null, "tech-lead")
      .map((target) => ({ ...target, required: false }));
    expect(() => validateInterviewBlueprint(sampleBlueprint({ targets }))).toThrow(/required/i);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/interview-planner.test.ts -t "buildCoverageTargets"`
Expected: FAIL — `buildCoverageTargets is not exported`.

- [ ] **Step 3: Implement the target builder**

In `src/lib/interview-planner.ts`, delete `promptFor`, `blueprintPrompt`, and `discoveryPrompt` and every call site. Add:

```ts
/**
 * Builds what the round must find out, not what it will say. Prompt text is
 * authored live by the interviewer call (spec §9.1).
 *
 * When anchored to an opportunity, every gap becomes a required target: a tech
 * lead's real agenda is the places the candidate looks thin against the spec,
 * and that list is already computed.
 */
export function buildCoverageTargets(
  profile: Pick<Profile, "role" | "competencies">,
  evidence: EvidenceItem[],
  opportunity: Pick<Opportunity, "gaps" | "jobDescription"> | null,
  roundId: RoundId,
): CoverageTarget[] {
  const round = roundFor(roundId);
  const gapTargets = (opportunity?.gaps ?? []).map((gap, index) => ({
    id: `gap-${index}`,
    competencyId: null,
    competencyName: gap,
    category: "experience" as QuestionCategory,
    evidenceIds: [],
    difficulty: "senior" as Difficulty,
    objective: `Establish whether the candidate has real experience with ${gap}.`,
    expectedSignals: [gap, "ownership", "outcome"],
    rubricCriteria: [
      `Name a concrete example involving ${gap}.`,
      "Describe the decision they personally made.",
      "Explain the outcome or trade-off.",
    ],
    required: true,
  }));

  const competencyTargets = [...profile.competencies]
    .sort((left, right) => right.relevance - left.relevance)
    .slice(0, Math.max(1, 5 - gapTargets.length))
    .map((competency, index) => ({
      id: `competency-${index}`,
      competencyId: competency.id ?? null,
      competencyName: competency.name,
      category: "experience" as QuestionCategory,
      evidenceIds: evidence
        .filter((item) => item.technologies.some((tech) => competency.name.toLowerCase().includes(tech.toLowerCase())))
        .map((item) => item.id ?? "")
        .filter(Boolean),
      difficulty: "senior" as Difficulty,
      objective: `Establish the candidate's real ownership within ${competency.name}.`,
      expectedSignals: [competency.name, "ownership", "outcome"],
      rubricCriteria: [
        `Name a concrete example from ${competency.name}.`,
        "Describe the ownership or decision involved.",
        "Explain the outcome or trade-off.",
      ],
      required: index === 0,
    }));

  const all = [...gapTargets, ...competencyTargets];
  // A round with no repertoire for a category cannot cover it; drop rather than
  // plan something the director may never issue.
  return round.moves.includes("open") ? all : [];
}
```

- [ ] **Step 4: Relax and extend `validateInterviewBlueprint`**

In `validateInterviewBlueprint` (line 425), delete:

```ts
if (!question.prompt.trim()) throw new Error("Interview blueprint questions need...");
```

and add:

```ts
  if (blueprint.targets.length === 0) throw new Error("An interview blueprint needs at least one coverage target.");
  if (!blueprint.targets.some((target) => target.required)) {
    throw new Error("An interview blueprint needs at least one required coverage target.");
  }
  if (new Set(blueprint.targets.map((target) => target.id)).size !== blueprint.targets.length) {
    throw new Error("Interview blueprint coverage targets need unique ids.");
  }
  for (const target of blueprint.targets) {
    if (!target.rubricCriteria.length) throw new Error("Every coverage target needs rubric criteria.");
  }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npx vitest run src/lib/interview-planner.test.ts`
Expected: PASS. Delete planner tests asserting generated prompt text — that responsibility has moved.

- [ ] **Step 6: Commit**

```bash
git add src/lib/interview-planner.ts src/lib/interview-planner.test.ts
git commit -m "feat: plan interviews as coverage targets instead of pre-written prompts"
```

---

### Task 9: Migration — columns and RPC signatures

**Files:**
- Create: `supabase/migrations/202609010001_adaptive_interviewer.sql`
- Create: `src/lib/supabase/adaptive-interviewer-migration.test.ts`

**Interfaces:**
- Consumes: nothing at runtime.
- Produces: `interview_sessions.round_id`, `interview_sessions.mode`; `interview_questions.asked_intent`, `.assistance`, `.non_answer`, nullable `.prompt`; `record_conversation_turn` with three new parameters; `create_conversation_session_with_blueprint` accepting `round_id`, `mode`, and `targets`.

Read `supabase/migrations/202608290010_follow_up_rubric_contract.sql` before writing this — it holds the current definitions of both functions, and this migration replaces them wholesale.

- [ ] **Step 1: Write the failing test**

Create `src/lib/supabase/adaptive-interviewer-migration.test.ts`, following `legacy-blueprint-migration.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const sql = readFileSync("supabase/migrations/202609010001_adaptive_interviewer.sql", "utf8");

describe("adaptive interviewer migration", () => {
  it("adds the session round and mode columns with backward-compatible defaults", () => {
    expect(sql).toMatch(/add column round_id text not null default 'tech-lead'/);
    expect(sql).toMatch(/add column mode text not null default 'real'/);
  });

  it("adds the session degraded flag", () => {
    expect(sql).toMatch(/add column degraded boolean not null default false/);
    expect(sql).toMatch(/p_degraded boolean/);
  });

  it("adds the question intent, assistance, and non-answer columns", () => {
    expect(sql).toMatch(/add column asked_intent jsonb/);
    expect(sql).toMatch(/add column assistance jsonb not null default '\[\]'::jsonb/);
    expect(sql).toMatch(/add column non_answer boolean not null default false/);
  });

  it("drops the not-null constraint on question prompts", () => {
    expect(sql).toMatch(/alter column prompt drop not null/);
  });

  it("replaces record_conversation_turn with the intent parameters", () => {
    expect(sql).toMatch(/create or replace function public\.record_conversation_turn/);
    expect(sql).toMatch(/p_asked_intent jsonb/);
    expect(sql).toMatch(/p_assistance jsonb/);
    expect(sql).toMatch(/p_non_answer boolean/);
  });

  it("keeps every function security invoker and re-grants the new signature", () => {
    expect(sql).not.toMatch(/security definer/);
    expect(sql.match(/security invoker/g)?.length).toBeGreaterThanOrEqual(2);
    expect(sql).toMatch(/grant execute on function public\.record_conversation_turn\([^)]*boolean[^)]*\) to authenticated/);
  });

  it("does not add an opportunity_id column that already exists", () => {
    expect(sql).not.toMatch(/add column opportunity_id/);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/supabase/adaptive-interviewer-migration.test.ts`
Expected: FAIL — `ENOENT`, the migration does not exist.

- [ ] **Step 3: Write the migration**

Create `supabase/migrations/202609010001_adaptive_interviewer.sql`. Start from the existing `record_conversation_turn` body in `202608290010_follow_up_rubric_contract.sql` and change only what this release needs:

```sql
alter table public.interview_sessions
  add column round_id text not null default 'tech-lead',
  add column mode text not null default 'real'
    check (mode in ('coach', 'real')),
  -- Release 3 must not derive confident observations from a degraded session.
  add column degraded boolean not null default false;

-- opportunity_id already exists (202608300006_session_career_context.sql).

alter table public.interview_questions
  add column asked_intent jsonb,
  add column assistance jsonb not null default '[]'::jsonb,
  add column non_answer boolean not null default false;

-- A question's text no longer exists until the interviewer authors it.
alter table public.interview_questions
  alter column prompt drop not null;
```

Then re-create `record_conversation_turn`. Copy the function body verbatim from `202608290010_follow_up_rubric_contract.sql` — do not rewrite it from scratch, it carries ownership checks, row locking, and follow-up sequence shifting that must be preserved — and append `p_asked_intent jsonb`, `p_assistance jsonb`, `p_non_answer boolean`, and `p_degraded boolean` to the parameter list. Changes to the body:

```sql
  -- Once degraded, a session stays degraded.
  update public.interview_sessions
  set degraded = degraded or coalesce(p_degraded, false),
      updated_at = now()
  where id = v_question.session_id
    and user_id = v_user_id;
```

and:

- The follow-up insert sets `asked_intent`, `assistance`, and `non_answer` on the new row, and drops the `length(trim(coalesce(p_follow_up ->> 'prompt', ''))) = 0` guard's fatality — an authored prompt is always present now, but a null prompt must not raise.
- The `elsif p_next_question_id is not null` branch also writes `asked_intent = p_asked_intent`.

Additionally, mark the answered question:

```sql
  update public.interview_questions
  set non_answer = p_non_answer,
      assistance = coalesce(p_assistance, '[]'::jsonb),
      updated_at = now()
  where id = p_question_id
    and user_id = v_user_id;
```

Skip `record_interview_evidence` entirely when `p_non_answer` is true — a non-answer is never scored (spec §11.3):

```sql
  if not p_non_answer then
    perform * from public.record_interview_evidence(/* ...unchanged arguments... */);
  end if;
```

Then re-create `create_conversation_session_with_blueprint` to read `p_blueprint ->> 'roundId'`, `p_blueprint ->> 'mode'`, and `p_blueprint -> 'targets'`, inserting one `interview_questions` row per target with a null prompt. Drop the `jsonb_array_length(...) <> 5` check — target counts vary.

Finish with `revoke`/`grant execute` on both new signatures, exactly matching the parameter type lists.

- [ ] **Step 4: Run the test to verify it passes**

Run: `npx vitest run src/lib/supabase/adaptive-interviewer-migration.test.ts`
Expected: PASS, 6 tests.

- [ ] **Step 5: Apply the migration**

Apply to the Supabase project through your usual path (`supabase db push`, or the SQL editor). Then confirm:

```sql
select column_name from information_schema.columns
where table_name = 'interview_questions' and column_name in ('asked_intent', 'assistance', 'non_answer');
```
Expected: three rows.

- [ ] **Step 6: Commit**

```bash
git add supabase/migrations/202609010001_adaptive_interviewer.sql src/lib/supabase/adaptive-interviewer-migration.test.ts
git commit -m "feat: migrate interview storage for intents, assistance, and rounds"
```

---

### Task 10: Repository wiring

**Files:**
- Modify: `src/lib/repositories/interviews.ts` (`recordConversationTurn` near line 610; `createSessionWithBlueprint` near line 451; the row mapper)
- Modify: `src/lib/repositories/interviews.test.ts`

**Interfaces:**
- Consumes: Tasks 1, 9.
- Produces:
  - `ConversationTurnPersistence` gains `askedIntent: Intent | null`, `assistance: AssistanceRecord[]`, `nonAnswer: boolean`, `degraded: boolean`
  - `createSessionWithBlueprint(supabase, userId, blueprint, options: { roundId: RoundId; mode: InterviewMode; opportunityId: string | null }): Promise<InterviewSession>`
  - `revealFirstQuestion(supabase, userId, session: InterviewSession, opening: { intent: Intent; prompt: string; targetId: string }): Promise<InterviewSession>` — writes the authored prompt and intent onto the first unanswered question row
  - `questionIdForTarget(session: InterviewSession, targetId: string): string | null` — the unanswered question row carrying that coverage target, matched on `competencyName`, falling back to the next unanswered row by `sequence`

- [ ] **Step 1: Write the failing test**

Add to `src/lib/repositories/interviews.test.ts`:

```ts
it("passes the intent, assistance, and non-answer flag to the RPC", async () => {
  const rpc = vi.fn(async () => ({ data: [{ session_id: "s1" }], error: null }));
  const supabase = fakeSupabase({ rpc });

  await recordConversationTurn(supabase, "u1", "q1", "an answer", sampleEvaluation(), {
    nextQuestionId: "q2",
    nextPrompt: "What did you own there?",
    followUp: null,
    askedIntent: { kind: "probe", targetId: "a", aspect: "ownership", basis: "x" },
    assistance: [{ style: "narrow", at: "2026-09-01T00:00:00.000Z" }],
    nonAnswer: false,
  });

  expect(rpc).toHaveBeenCalledWith("record_conversation_turn", expect.objectContaining({
    p_asked_intent: { kind: "probe", targetId: "a", aspect: "ownership", basis: "x" },
    p_assistance: [{ style: "narrow", at: "2026-09-01T00:00:00.000Z" }],
    p_non_answer: false,
  }));
});

it("maps the new question columns onto the session", async () => {
  const session = await getSession(fakeSupabaseWithQuestionRow({
    asked_intent: { kind: "open", targetId: "a" },
    assistance: [{ style: "hook", at: "2026-09-01T00:00:00.000Z" }],
    non_answer: true,
    prompt: null,
  }), "u1", "s1");

  const question = session!.questions[0];
  expect(question.askedIntent).toEqual({ kind: "open", targetId: "a" });
  expect(question.assistance).toHaveLength(1);
  expect(question.nonAnswer).toBe(true);
  expect(question.prompt).toBeNull();
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/lib/repositories/interviews.test.ts`
Expected: FAIL — the extra `ConversationTurnPersistence` fields do not typecheck.

- [ ] **Step 3: Extend the persistence type and RPC call**

```ts
export type ConversationTurnPersistence = {
  nextQuestionId: string | null;
  nextPrompt: string | null;
  followUp: FollowUpDraft | null;
  askedIntent: Intent | null;
  assistance: AssistanceRecord[];
  nonAnswer: boolean;
  degraded: boolean;
};
```

Add the two helpers this release needs:

```ts
/**
 * Writes the interviewer's opening line onto the first question row. The
 * blueprint creates rows with a null prompt (spec §9.1), so a session is not
 * showable until this runs.
 */
export async function revealFirstQuestion(
  supabase: SupabaseClient,
  userId: string,
  session: InterviewSession,
  opening: { intent: Intent; prompt: string; targetId: string },
): Promise<InterviewSession> {
  const first = session.questions.find((question) => question.answer === null);
  if (!first) throw new RepositoryError("The new interview has no question to reveal.", "NO_OWNED_ROW");
  const { error } = await supabase
    .from("interview_questions")
    .update({ prompt: opening.prompt, asked_intent: opening.intent, asked_at: new Date().toISOString() })
    .eq("id", first.id)
    .eq("user_id", userId);
  if (error) throw new RepositoryError("Could not start your interview.", error.code);
  const refreshed = await getSession(supabase, userId, session.id);
  if (!refreshed) throw new RepositoryError("Could not reload the new interview session.", "NO_OWNED_ROW");
  return refreshed;
}

/** Maps a coverage target to the unanswered question row that will carry it. */
export function questionIdForTarget(session: InterviewSession, targetId: string): string | null {
  const target = session.blueprint?.targets.find((item) => item.id === targetId) ?? null;
  const unanswered = session.questions.filter((question) => question.answer === null);
  const byCompetency = target?.competencyName
    ? unanswered.find((question) => question.competencyName === target.competencyName)
    : undefined;
  return (byCompetency ?? unanswered.sort((left, right) => left.sequence - right.sequence)[0])?.id ?? null;
}
```

In `recordConversationTurn`, add to the `rpc` argument object:

```ts
    p_asked_intent: next.askedIntent,
    p_assistance: next.assistance,
    p_non_answer: next.nonAnswer,
```

- [ ] **Step 4: Map the new columns**

In the question row mapper, add:

```ts
    askedIntent: (row.asked_intent as Intent | null) ?? null,
    assistance: Array.isArray(row.assistance) ? (row.assistance as AssistanceRecord[]) : [],
    nonAnswer: row.non_answer === true,
    prompt: stringValue(row.prompt) || null,
```

In the session mapper, add:

```ts
    roundId: (stringValue(row.round_id) as RoundId) || "tech-lead",
    mode: (stringValue(row.mode) as InterviewMode) || "real",
    opportunityId: stringValue(row.opportunity_id) || null,
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npx vitest run src/lib/repositories/interviews.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lib/repositories/interviews.ts src/lib/repositories/interviews.test.ts
git commit -m "feat: persist interview intents, assistance, and round metadata"
```

---

### Task 11: API route

**Files:**
- Modify: `src/app/api/interview/route.ts` (the `start` branch near line 60; the `respond` branch near line 86)
- Modify: `src/app/api/interview/route.test.ts`

**Interfaces:**
- Consumes: Tasks 1–10.
- Produces: `POST { action: "start", roundId, mode, opportunityId }`; the `respond` branch calling `nextTurn` with the new input shape.

- [ ] **Step 1: Write the failing test**

Add to `src/app/api/interview/route.test.ts`:

```ts
it("starts a session with the requested round and mode", async () => {
  const response = await POST(jsonRequest({ action: "start", roundId: "tech-lead", mode: "coach" }));
  const body = await response.json();
  expect(response.status).toBe(200);
  expect(body.session.mode).toBe("coach");
  expect(body.session.roundId).toBe("tech-lead");
  expect(body.session.questions[0].prompt).toBeTruthy();
});

it("rejects a round that is specified but not implemented", async () => {
  const response = await POST(jsonRequest({ action: "start", roundId: "founder", mode: "real" }));
  expect(response.status).toBe(400);
});

it("defaults to real mode when none is given", async () => {
  const response = await POST(jsonRequest({ action: "start" }));
  const body = await response.json();
  expect(body.session.mode).toBe("real");
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/app/api/interview/route.test.ts`
Expected: FAIL — `mode` is undefined on the returned session.

- [ ] **Step 3: Rewrite the `start` branch**

```ts
    if (body.action === "start") {
      if (body.mode === "hands-on") {
        const session = await createHandsOnSession(supabase, user.id, handsOnExercise(profile));
        return NextResponse.json({ session });
      }

      const roundId = (typeof body.roundId === "string" ? body.roundId : "tech-lead") as RoundId;
      if (!IMPLEMENTED_ROUNDS.includes(roundId)) {
        return NextResponse.json({ error: "That interview round is not available yet." }, { status: 400 });
      }
      const mode: InterviewMode = body.mode === "coach" ? "coach" : "real";
      const opportunityId = typeof body.opportunityId === "string" ? body.opportunityId : null;
      const opportunity = opportunityId ? await getOpportunity(supabase, user.id, opportunityId) : null;

      const blueprint = await generateInterviewBlueprint(profile, profile.evidence ?? [], { roundId, opportunity });
      const session = await createSessionWithBlueprint(supabase, user.id, blueprint, { roundId, mode, opportunityId });

      const opening = await openingTurn({
        profile,
        session,
        blueprint,
        evidence: profile.evidence ?? [],
        opportunity,
      });
      const revealed = await revealFirstQuestion(supabase, user.id, session, opening);
      return NextResponse.json({ session: visibleConversation(revealed) });
    }
```

`revealFirstQuestion` writes the authored prompt and intent onto the first question row. Add it to `src/lib/repositories/interviews.ts` as a small update alongside Task 10's changes if it is not already there.

- [ ] **Step 4: Rewrite the `respond` branch**

```ts
      const turn = await nextTurn({
        profile,
        session: visibleConversation(session),
        answeredQuestion: hydratedQuestion,
        answer,
        blueprint: session.blueprint!,
        evidence: profile.evidence ?? [],
        opportunity: session.opportunityId ? await getOpportunity(supabase, user.id, session.opportunityId) : null,
      });

      const updated = await recordConversationTurn(
        supabase,
        user.id,
        question.id,
        answer,
        turn.evaluation ?? emptyEvaluationFor(hydratedQuestion),
        {
          nextQuestionId: turn.targetId ? questionIdForTarget(session, turn.targetId) : null,
          nextPrompt: turn.prompt,
          followUp: null,
          askedIntent: turn.intent,
          assistance: turn.assistance ? [turn.assistance] : [],
          nonAnswer: turn.nonAnswer,
          degraded: turn.degraded,
        },
      );
```

`questionIdForTarget` comes from Task 10. `emptyEvaluationFor` supplies placeholder values for a non-answer turn — the RPC skips `record_interview_evidence` entirely when `p_non_answer` is true (Task 9), so nothing here is stored. Add it to `route.ts`:

```ts
/**
 * Placeholder evaluation values for a non-answer turn. The RPC skips evidence
 * recording when `p_non_answer` is true, so these are never persisted; they
 * exist only to satisfy the RPC's non-null parameters (spec §11.3).
 */
function emptyEvaluationFor(question: PlannedQuestion): Evaluation {
  return {
    questionId: question.id,
    competencyId: question.competencyId,
    competency: question.competencyName ?? "Communication",
    score: 0,
    relevance: 0,
    dimensions: Object.fromEntries(EVALUATION_DIMENSIONS.map((key) => [key, 0])) as Evaluation["dimensions"],
    strengths: [],
    needsWork: [],
    missingPoints: ["Not attempted."],
    betterStructure: ["Not attempted."],
    improvedAnswer: "Not attempted.",
    supportedClaims: [],
    expectedSignalsPresent: [],
    unsupportedClaims: [],
    dimensionReasons: Object.fromEntries(
      EVALUATION_DIMENSIONS.map((key) => [key, "Not attempted."]),
    ) as Evaluation["dimensionReasons"],
  } as Evaluation;
}
```

`EVALUATION_DIMENSIONS` is the `dimensions` tuple at `coach.ts:32`. Export it from `coach.ts` rather than duplicating the list.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `npx vitest run src/app/api/interview/route.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/app/api/interview/route.ts src/app/api/interview/route.test.ts src/lib/repositories/interviews.ts
git commit -m "feat: start and run interview sessions by round and mode"
```

---

### Task 12: UI — mode choice, pending state, assistance in results

**Files:**
- Modify: `src/app/relay-shell.tsx` (the interviewer message block near line 667)
- Modify: `src/app/page.test.tsx`

**Interfaces:**
- Consumes: Tasks 1–11.
- Produces: no new exports; UI behaviour only.

- [ ] **Step 1: Write the failing test**

Add to `src/app/page.test.tsx`:

```ts
it("offers coach and real mode before a practice session starts", async () => {
  renderShell();
  expect(await screen.findByRole("radio", { name: /coach/i })).toBeInTheDocument();
  expect(screen.getByRole("radio", { name: /real/i })).toBeInTheDocument();
});

it("shows a pending state on the interviewer while the next question is authored", async () => {
  renderShell({ pendingTurn: true });
  expect(await screen.findByText(/thinking/i)).toBeInTheDocument();
});

it("shows the assistance that produced a score", async () => {
  renderResults({ questions: [answeredQuestionWithAssistance(2)] });
  expect(await screen.findByText(/after two rescues/i)).toBeInTheDocument();
});

it("does not show the grounding provenance line during a live conversation", async () => {
  renderShell({ activeSession: sessionWithUnansweredQuestion() });
  expect(screen.queryByText(/^Grounded in/)).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npx vitest run src/app/page.test.tsx`
Expected: FAIL — no mode radios rendered.

- [ ] **Step 3: Implement the UI changes**

Four changes in `src/app/relay-shell.tsx`.

**1. Mode picker** on the practice start screen. Default Real:

```tsx
const [mode, setMode] = useState<InterviewMode>("real");

<fieldset className="mb-4">
  <legend className="mb-2 text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">
    Interview mode
  </legend>
  {([
    { value: "real", label: "Real", hint: "Lets you fail, so the feedback is honest." },
    { value: "coach", label: "Coach", hint: "Helps you when you get stuck." },
  ] as const).map((option) => (
    <label key={option.value} className="flex cursor-pointer items-start gap-3 py-2">
      <input
        type="radio"
        name="interview-mode"
        value={option.value}
        checked={mode === option.value}
        onChange={() => setMode(option.value)}
        className="mt-1"
      />
      <span>
        <span className="block text-sm font-medium">{option.label}</span>
        <span className="block text-xs text-[var(--ink-muted)]">{option.hint}</span>
      </span>
    </label>
  ))}
</fieldset>
```

Send it with the start request: `body: JSON.stringify({ action: "start", mode, roundId: "tech-lead", opportunityId })`.

**2. Pending state.** Two sequential model calls take 2–6 seconds (spec §13.1); a silent gap that long reads as broken. Render while the turn is in flight:

```tsx
{pendingTurn && (
  <div className="rounded-2xl border border-[var(--line)] p-5">
    <p className="mb-3 text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Interviewer</p>
    <p className="animate-pulse text-sm text-[var(--ink-muted)]">Thinking…</p>
  </div>
)}
```

`animate-pulse` animates `opacity` only, which satisfies the hardware-acceleration rule in `AGENTS.md`. Do not substitute a height or width animation.

**3. Provenance line.** Delete lines 667–673 — the `Grounded in …` paragraph — from the live conversation view:

```tsx
{message.role === "interviewer" && blueprintQuestion && (
  <p className="mb-3 text-xs leading-5 text-[#537053]">
    {blueprintQuestion.evidenceIds.length === 0
      ? "Broader question — draws on what you actually say, not a fixed source example."
      : `Grounded in ${/* ... */}`}
  </p>
)}
```

A real interviewer does not narrate its sources. This is interface metadata and belongs in the results card, where `02ec2c1` already moved the objective and expected signals.

**4. Assistance in results.** Where the results card renders a question's score:

```tsx
function assistanceLabel(assistance: AssistanceRecord[]): string | null {
  if (assistance.length === 0) return null;
  const count = assistance.length === 1 ? "one rescue" : `${assistance.length} rescues`;
  return `reached after ${count}`;
}

<p className="text-sm">
  {question.nonAnswer
    ? "Not attempted"
    : <>
        {evaluation.score.toFixed(1)}
        {assistanceLabel(question.assistance) && (
          <span className="text-[var(--ink-muted)]"> · {assistanceLabel(question.assistance)}</span>
        )}
      </>}
</p>
```

A Coach-mode score shown alone flatters the candidate, so the assistance suffix is not optional (spec §8.4). A non-answer shows "Not attempted" rather than a zero, because it was never scored (spec §11.3).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `npx vitest run src/app/page.test.tsx`
Expected: PASS.

- [ ] **Step 5: Run the full suite, lint, and build**

```bash
npm test
npm run lint
npx next build --webpack
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/app/relay-shell.tsx src/app/page.test.tsx
git commit -m "feat: choose interview mode and surface assistance alongside scores"
```

---

### Task 13: End-to-end flow test

**Files:**
- Create: `src/lib/adaptive-interviewer-flow.test.ts`

**Interfaces:**
- Consumes: every prior task.

Follows `release2-flow.test.ts`. This is the release gate in test form (spec §16).

- [ ] **Step 1: Write the test**

```ts
import { describe, expect, it } from "vitest";

describe("adaptive interviewer flow", () => {
  it("completes a real-mode session covering every required target", async () => {
    const session = await runScriptedSession({ mode: "real", answers: strongAnswers() });
    const states = coverageAtEnd(session);
    expect(states.filter((state) => state.target.required).every((state) => state.status === "satisfied")).toBe(true);
    expect(session.questions.every((question) => question.assistance.length === 0)).toBe(true);
  });

  it("rescues a blanking candidate in coach mode and returns to the parked target", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "ok — I owned the design system migration at Acme."],
    });
    const rescues = session.questions.flatMap((question) => question.assistance);
    expect(rescues.length).toBeGreaterThan(0);
    expect(rescues.map((rescue) => rescue.style)).toContain("park");
    expect(session.questions.some((question) => question.nonAnswer)).toBe(true);
  });

  it("never scores a non-answer", async () => {
    const session = await runScriptedSession({ mode: "coach", answers: ["i am having a blackout"] });
    const blanks = session.questions.filter((question) => question.nonAnswer).map((question) => question.id);
    expect(session.evaluations.some((item) => blanks.includes(item.questionId))).toBe(false);
  });

  it("asks a different question every turn", async () => {
    const session = await runScriptedSession({ mode: "real", answers: strongAnswers() });
    const prompts = session.questions.map((question) => question.prompt).filter(Boolean);
    expect(new Set(prompts).size).toBe(prompts.length);
  });

  it("grounds questions in the job when anchored to an opportunity", async () => {
    const session = await runScriptedSession({
      mode: "real",
      answers: strongAnswers(),
      opportunity: { company: "Acme", role: "Senior Frontend Engineer", jobDescription: "React, testing strategy, observability", gaps: ["Observability"] },
    });
    const targets = session.blueprint!.targets.filter((target) => target.required).map((target) => target.competencyName);
    expect(targets).toContain("Observability");
  });
});
```

Write `runScriptedSession`, `strongAnswers`, and `coverageAtEnd` as local helpers driving the repository fakes already used by `release2-flow.test.ts`, with Gemini stubbed to return a fixed assessor read per scripted answer and a distinct line per call.

- [ ] **Step 2: Run the test**

Run: `npx vitest run src/lib/adaptive-interviewer-flow.test.ts`
Expected: PASS, 5 tests. Any failure here is a real integration defect — fix the source, not the test.

- [ ] **Step 3: Full verification**

```bash
npm test
npm run lint
npx next build --webpack
```
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/lib/adaptive-interviewer-flow.test.ts
git commit -m "test: cover the adaptive interviewer flow end to end"
```

- [ ] **Step 5: Run a live session in each mode**

The final release gate is subjective on purpose (spec §16). Start a Coach session and a Real session against a real profile and judge whether the conversation feels like a person. The tests prove the three known failures are gone; they cannot prove the quality being sought is present.

---

## Post-implementation

The spec's §15 deferred rounds each need only a round definition added to `interview-rounds.ts` plus an entry in `IMPLEMENTED_ROUNDS`. The engine does not change. The code-discussion round (§15.4) is the exception — it needs the candidate's artifact in the interviewer's context and its own probe aspects, so it warrants its own plan.

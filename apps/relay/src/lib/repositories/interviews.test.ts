import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { buildFallbackInterviewBlueprint } from "@/lib/interview-planner";
import {
  assertConversationPlan,
  assertPracticeConversationBlueprint,
  completeHandsOnSession,
  createHandsOnPracticeSession,
  createSessionWithBlueprint,
  createSessionWithPlan,
  createSessionWithPracticeBlueprint,
  linkSessionCareerContext,
  listReadinessEvidence,
  mapSession,
  questionIdForTarget,
  recordAnswerAndEvaluation,
  recordConversationTurn,
  revealFirstQuestion,
} from "@/lib/repositories/interviews";
import { RepositoryError } from "@/lib/repositories/profile";
import type {
  Competency,
  CoverageTarget,
  Evaluation,
  EvidenceItem,
  HandsOnExercise,
  InterviewBlueprint,
  InterviewSession,
  PlannedQuestion,
  ProfileDraft,
} from "@/lib/types";

/** A minimal, realistic `Evaluation` for RPC-argument-shape tests that don't care about scoring content. */
function sampleEvaluation(): Evaluation {
  return {
    score: 7,
    questionId: null,
    competencyId: null,
    competency: "Communication",
    dimensions: {},
    strengths: [],
    needsWork: [],
    missingPoints: [],
    betterStructure: [],
    improvedAnswer: "",
  };
}

/** A single required coverage target with a stable, test-chosen id -- mirrors `buildCoverageTargets`' shape. */
function sampleTarget(overrides: Partial<CoverageTarget> = {}): CoverageTarget {
  return {
    id: "target-1",
    competencyId: null,
    competencyName: "React architecture",
    category: "experience",
    evidenceIds: ["evidence-1"],
    difficulty: "senior",
    objective: "Probe the migration ownership and impact.",
    expectedSignals: ["ownership", "impact"],
    rubricCriteria: ["Name the decision.", "Describe the trade-off.", "State the outcome."],
    required: true,
    ...overrides,
  };
}

type Row = Record<string, unknown>;
type QueryResult = { data: unknown; error: { code: string } | null };

/**
 * A minimal legacy session row shape -- as it would have existed before
 * this task added `practice_plan_id`/`opportunity_id` -- for compatibility
 * tests. Neither Career Brain column is present, matching a real
 * pre-migration row (columns are nullable, never backfilled).
 */
const legacyRow = (overrides: Row = {}): Row => ({
  id: "session-legacy", user_id: "user-1", kind: "conversation", status: "active",
  started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
  overall_score: null, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
  ...overrides,
});

/**
 * A chainable table-stub builder, matching the pattern established in
 * `practice-plans.test.ts`. It is awaitable directly (implements `.then`)
 * so callers that never terminate with `.maybeSingle()`/`.order()`/`.in()`
 * -- e.g. the plain `.update(...).eq(...).eq(...)` in
 * `linkSessionCareerContext` -- resolve to `result` too. `capture.eq`
 * records every `[field, value]` pair passed to `.eq(...)` on this
 * builder, in call order, so tests can assert the actual scoping used --
 * not just that `.eq` was called some number of times.
 */
function tableStub(
  result: QueryResult,
  capture?: { insert?: Row | Row[]; update?: Row; eq?: Array<[string, unknown]> },
) {
  const builder: Record<string, unknown> = {
    insert: (row: Row | Row[]) => {
      if (capture) capture.insert = row;
      return builder;
    },
    update: (patch: Row) => {
      if (capture) capture.update = patch;
      return builder;
    },
    select: () => builder,
    eq: (field: string, value: unknown) => {
      if (capture) (capture.eq ??= []).push([field, value]);
      return builder;
    },
    in: async () => result,
    order: async () => result,
    maybeSingle: async () => result,
    then: (resolve: (value: QueryResult) => void) => resolve(result),
  };
  return builder;
}

/**
 * A full career-context supabase double for `linkSessionCareerContext`.
 * `interview_questions`, `hands_on_checkpoints`, and `session_evaluations`
 * all resolve empty so `hydrateSession`'s reload skips both
 * `question_evaluations` (no question ids to look up) and `competencies`
 * (no competency ids to resolve) entirely -- keeping the double to exactly
 * the tables `linkSessionCareerContext` and its reload actually touch.
 */
function careerContextSupabase(options: {
  sessionRow: Row | null;
  planRow?: Row | null;
  opportunityRow?: Row | null;
  planOpportunityRows?: Row[];
  captures?: {
    session?: { update?: Row; eq?: Array<[string, unknown]> };
    plan?: { eq?: Array<[string, unknown]> };
    opportunity?: { eq?: Array<[string, unknown]> };
    planOpportunities?: { eq?: Array<[string, unknown]> };
  };
}) {
  const captures = options.captures ?? {};
  const tables: Record<string, ReturnType<typeof tableStub>> = {
    interview_sessions: tableStub({ data: options.sessionRow, error: null }, captures.session),
    interview_questions: tableStub({ data: [], error: null }),
    hands_on_checkpoints: tableStub({ data: [], error: null }),
    session_evaluations: tableStub({ data: [], error: null }),
  };
  if (options.planRow !== undefined) {
    tables.practice_plans = tableStub({ data: options.planRow, error: null }, captures.plan);
  }
  if (options.opportunityRow !== undefined) {
    tables.opportunities = tableStub({ data: options.opportunityRow, error: null }, captures.opportunity);
  }
  if (options.planOpportunityRows !== undefined) {
    tables.practice_plan_opportunities = tableStub(
      { data: options.planOpportunityRows, error: null },
      captures.planOpportunities,
    );
  }
  const from = vi.fn((table: string) => tables[table]);
  return { from };
}

const fallbackProfile: ProfileDraft = {
  role: "Frontend Engineer",
  seniority: "Senior",
  summary: "Frontend engineer focused on performance and delivery.",
  narrative: "Owns frontend platforms and reliability work.",
  expertise: ["React", "TypeScript", "Next.js"],
  characteristics: ["Pragmatic"],
  competencies: [
    { name: "React architecture", relevance: 1 },
    { name: "System design", relevance: 0.8 },
  ],
};

const fallbackEvidence: EvidenceItem[] = [
  {
    id: "evidence-1",
    sourceKind: "cv",
    sourceExcerpt: "Led a React migration for checkout.",
    projectOrEmployer: "Checkout Platform",
    ownership: "Owned the frontend migration end to end.",
    technologies: ["React", "TypeScript"],
    decision: "Split a large route into smaller bundles.",
    constraint: "Tight launch window.",
    outcome: "Cut bundle size by 28%.",
    recency: "2025-02",
    confidence: 0.94,
  },
  {
    id: "evidence-2",
    sourceKind: "cv",
    sourceExcerpt: "Built observability for API regressions.",
    projectOrEmployer: "Reliability Tooling",
    ownership: "Designed the dashboard and alerting flow.",
    technologies: ["Next.js", "Postgres"],
    decision: "Added release health dashboards.",
    constraint: "Small team with limited bandwidth.",
    outcome: "Reduced incident triage time by 35%.",
    recency: "2024-11",
    confidence: 0.91,
  },
];

const fallbackCompetencies: Competency[] = [
  {
    id: "fallback-competency-1",
    name: "React architecture",
    relevance: 1,
    expectedLevel: "senior",
    estimatedLevel: null,
    confidence: null,
    lastPracticedAt: null,
    questionCount: 0,
    averageScore: null,
    recentScore: null,
    strengths: [],
    weaknesses: [],
  },
  {
    id: "0d7f2d0c-8e26-4ae6-b4b2-f9f44c4f4ab8",
    name: "System design",
    relevance: 0.8,
    expectedLevel: "senior",
    estimatedLevel: null,
    confidence: null,
    lastPracticedAt: null,
    questionCount: 0,
    averageScore: null,
    recentScore: null,
    strengths: [],
    weaknesses: [],
  },
];

describe("mapSession", () => {
  it("hydrates legacy sessions with null Career Brain context", () => {
    const session = mapSession(legacyRow(), [], [], [], new Map());

    expect(session.practicePlanId).toBeNull();
    expect(session.opportunityId).toBeNull();
  });

  it("hydrates persisted Career Brain context", () => {
    const session = mapSession(
      legacyRow({ practice_plan_id: "plan-1", opportunity_id: "opp-1" }),
      [], [], [], new Map(),
    );

    expect(session.practicePlanId).toBe("plan-1");
    expect(session.opportunityId).toBe("opp-1");
  });

  it("maps persisted questions into an ordered plan and transcript", () => {
    const session = mapSession(
      {
        id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
        overall_score: null, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
      },
      [{
        id: "question-1", sequence: 1, category: "experience", competency_id: "competency-1",
        difficulty: "senior", is_follow_up: false, prompt: "Tell me about React.", answer: "I owned it.",
        created_at: "2026-08-29T10:01:00.000Z", answered_at: "2026-08-29T10:02:00.000Z",
      }],
      [{ id: "evaluation-1", question_id: "question-1", overall_score: 8, dimensions: { depth: 8 }, strengths: ["Specific"], weaknesses: ["Short"] }],
      [],
      new Map([["competency-1", "React architecture"]]),
    );

    expect(session.questions).toMatchObject([{ id: "question-1", competencyName: "React architecture", answer: "I owned it." }]);
    expect(session.messages).toEqual([
      { id: "question-1:question", role: "interviewer", content: "Tell me about React.", createdAt: "2026-08-29T10:01:00.000Z" },
      { id: "question-1:answer", role: "candidate", content: "I owned it.", createdAt: "2026-08-29T10:02:00.000Z" },
    ]);
    expect(session.evaluations).toMatchObject([{ competency: "React architecture", score: 8, dimensions: { depth: 8 } }]);
    expect(session.evaluations).toMatchObject([{
      missingPoints: [],
      betterStructure: [],
      improvedAnswer: "",
    }]);
  });

  it("reveals only the active question so the candidate never sees the rest of the plan up front", () => {
    const session = mapSession(
      {
        id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
        overall_score: null, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
      },
      [
        {
          id: "question-1", sequence: 1, category: "introduction", competency_id: null,
          difficulty: "senior", is_follow_up: false, prompt: "Introduce yourself.", answer: "I own frontend architecture.",
          created_at: "2026-08-29T10:01:00.000Z", answered_at: "2026-08-29T10:02:00.000Z",
        },
        {
          id: "question-2", sequence: 2, category: "experience", competency_id: null,
          difficulty: "senior", is_follow_up: false, prompt: "Tell me about the migration.", answer: null,
          created_at: "2026-08-29T10:01:00.000Z", answered_at: null,
        },
        {
          id: "question-3", sequence: 3, category: "technical", competency_id: null,
          difficulty: "senior", is_follow_up: false, prompt: "How did you handle focus state?", answer: null,
          created_at: "2026-08-29T10:01:00.000Z", answered_at: null,
        },
      ],
      [], [], new Map(),
    );

    expect(session.questions).toHaveLength(3);
    expect(session.messages.map((message) => message.id)).toEqual([
      "question-1:question",
      "question-1:answer",
      "question-2:question",
    ]);
  });

  it("replays a set-aside row's unscored attempts without also showing its own blanked prompt again", () => {
    // question-2 was parked after two blackouts: each unscored attempt is
    // logged in `non_answers` (the row's `prompt` column is overwritten by
    // each re-ask, so `non_answers` is the only record of what was actually
    // asked and answered). question-3 is where the director moved the
    // interview to after parking (issue #10).
    const session = mapSession(
      {
        id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
        overall_score: null, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
      },
      [
        {
          id: "question-1", sequence: 1, category: "introduction", competency_id: null,
          difficulty: "senior", is_follow_up: false, prompt: "Introduce yourself.", answer: "I own frontend architecture.",
          created_at: "2026-08-29T10:01:00.000Z", answered_at: "2026-08-29T10:02:00.000Z",
        },
        {
          id: "question-2", sequence: 2, category: "experience", competency_id: null,
          difficulty: "senior", is_follow_up: false,
          prompt: "Which constraint shaped that decision the most?", answer: null,
          created_at: "2026-08-29T10:03:00.000Z", answered_at: null,
          set_aside_at: "2026-08-29T10:05:00.000Z", set_aside_reason: "parked",
          non_answers: [
            { prompt: "What surprised you most about that outcome?", answer: "i don't know", at: "2026-08-29T10:03:30.000Z" },
            { prompt: "Which constraint shaped that decision the most?", answer: "i am having a blackout", at: "2026-08-29T10:04:30.000Z" },
          ],
        },
        {
          id: "question-3", sequence: 3, category: "behavioral", competency_id: null,
          difficulty: "senior", is_follow_up: false,
          prompt: "How did the team respond once that shipped?", answer: null,
          created_at: "2026-08-29T10:05:00.000Z", answered_at: null,
        },
      ],
      [], [], new Map(),
    );

    expect(session.messages.map((message) => message.id)).toEqual([
      "question-1:question",
      "question-1:answer",
      "question-2:attempt-0:question",
      "question-2:attempt-0:answer",
      "question-2:attempt-1:question",
      "question-2:attempt-1:answer",
      "question-3:question",
    ]);
    expect(session.messages.map((message) => message.content)).toEqual([
      "Introduce yourself.",
      "I own frontend architecture.",
      "What surprised you most about that outcome?",
      "i don't know",
      "Which constraint shaped that decision the most?",
      "i am having a blackout",
      "How did the team respond once that shipped?",
    ]);
  });

  it("orders question evaluations by their persisted question sequence before hydrating results feedback", () => {
    const mapped = mapSession(
      {
        id: "session-1", user_id: "user-1", kind: "conversation", status: "complete",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: "2026-08-29T10:30:00.000Z", exercise: {}, result_summary: {},
        overall_score: 7, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:30:00.000Z",
      },
      [
        {
          id: "question-1", sequence: 1, category: "experience", competency_id: "competency-1",
          difficulty: "senior", is_follow_up: false, prompt: "Tell me about the migration.", answer: "I phased by route.",
          created_at: "2026-08-29T10:01:00.000Z", answered_at: "2026-08-29T10:02:00.000Z",
        },
        {
          id: "question-2", sequence: 2, category: "technical", competency_id: "competency-2",
          difficulty: "senior", is_follow_up: false, prompt: "How did you handle focus state?", answer: "I kept it outside each row.",
          created_at: "2026-08-29T10:03:00.000Z", answered_at: "2026-08-29T10:04:00.000Z",
        },
      ],
      [
        { id: "evaluation-2", question_id: "question-2", overall_score: 6, dimensions: {}, strengths: ["Scoped focus state"], weaknesses: ["Quantify latency"] },
        { id: "evaluation-1", question_id: "question-1", overall_score: 8, dimensions: {}, strengths: ["Clear rollout"], weaknesses: ["Name the rollback trigger"] },
      ],
      [],
      new Map([
        ["competency-1", "React architecture"],
        ["competency-2", "Performance"],
      ]),
    );

    expect(mapped.evaluations.map((evaluation) => evaluation.competency)).toEqual([
      "React architecture",
      "Performance",
    ]);
    expect(mapped.questions.map((question) => question.prompt)).toEqual([
      "Tell me about the migration.",
      "How did you handle focus state?",
    ]);
  });

  it("hydrates legacy conversation sessions as limited-grounding when no persisted blueprint exists", () => {
    const mapped = mapSession(
      {
        id: "session-legacy", user_id: "user-1", kind: "conversation", status: "complete",
        started_at: "2026-08-28T10:00:00.000Z", completed_at: "2026-08-28T10:30:00.000Z", exercise: {}, result_summary: {},
        overall_score: 7, created_at: "2026-08-28T10:00:00.000Z", updated_at: "2026-08-28T10:30:00.000Z",
      },
      [{
        id: "question-legacy-1",
        sequence: 1,
        category: "experience",
        competency_id: "competency-legacy",
        difficulty: "senior",
        is_follow_up: false,
        prompt: "Tell me about your background.",
        answer: "I work on frontend systems.",
        created_at: "2026-08-28T10:01:00.000Z",
        answered_at: "2026-08-28T10:02:00.000Z",
      }],
      [],
      [],
      new Map([["competency-legacy", "React architecture"]]),
    );

    expect(mapped.blueprint).toMatchObject({
      status: "limited-grounding",
      fallbackReason: "Legacy session created before grounded blueprints were persisted.",
      maxFollowUps: 3,
      maxQuestions: 8,
      questions: [],
    });
  });

  it("hydrates grounded evaluation claims and reasons from persisted question feedback", () => {
    const mapped = mapSession(
      {
        id: "session-1", user_id: "user-1", kind: "conversation", status: "complete",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: "2026-08-29T10:30:00.000Z", exercise: {}, result_summary: {},
        overall_score: 7, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:30:00.000Z",
      },
      [
        {
          id: "question-1",
          sequence: 1,
          category: "experience",
          competency_id: "competency-1",
          difficulty: "senior",
          is_follow_up: false,
          prompt: "Tell me about the migration.",
          answer: "I led the rollout.",
          objective: "Probe the migration ownership and impact.",
          evidence_ids: ["evidence-1"],
          expected_signals: ["ownership", "impact"],
          missing_signal_prompts: ["Name the trade-off."],
          follow_up_limit: 1,
          source_confidence: 0.94,
          created_at: "2026-08-29T10:01:00.000Z",
          answered_at: "2026-08-29T10:02:00.000Z",
        },
      ],
      [{
        id: "evaluation-1",
        question_id: "question-1",
        overall_score: 8,
        relevance: 8.6,
        supported_claims: ["led the rollout"],
        expected_signals_present: ["ownership", "impact"],
        unsupported_claims: ["We shipped everything perfectly."],
        dimension_reasons: {
          relevance: "It directly answers the migration prompt.",
          correctness: "The rollout claim is grounded in the question.",
        },
        missing_points: ["Add the trade-off."],
        better_structure: ["Explain the rollout phases."],
        improved_answer: "I led the rollout and measured the impact.",
        dimensions: { relevance: 8.5 },
        strengths: ["Specific rollout"],
        weaknesses: ["Add the trade-off"],
      }],
      [],
      new Map([["competency-1", "React architecture"]]),
    );

    expect(mapped.evaluations).toEqual([expect.objectContaining({
      competency: "React architecture",
      score: 8,
      relevance: 8.6,
      supportedClaims: ["led the rollout"],
      expectedSignalsPresent: ["ownership", "impact"],
      unsupportedClaims: ["We shipped everything perfectly."],
      dimensionReasons: {
        relevance: "It directly answers the migration prompt.",
        correctness: "The rollout claim is grounded in the question.",
      },
      missingPoints: ["Add the trade-off."],
      betterStructure: ["Explain the rollout phases."],
      improvedAnswer: "I led the rollout and measured the impact.",
    })]);
  });

  it("hydrates persisted blueprint metadata and limited-grounding state", () => {
    const mapped = mapSession(
      {
        id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
        overall_score: null, blueprint_status: "limited-grounding", blueprint_fallback_reason: "Gemini returned invalid blueprint JSON.",
        blueprint_max_follow_ups: 2, blueprint_max_questions: 7,
        created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
      },
      [{
        id: "question-1",
        sequence: 1,
        category: "experience",
        competency_id: "competency-1",
        difficulty: "senior",
        is_follow_up: false,
        prompt: "Tell me about React.",
        answer: null,
        objective: "Probe the migration ownership and impact.",
        evidence_ids: ["evidence-1"],
        expected_signals: ["role", "impact"],
        missing_signal_prompts: ["Name the trade-off."],
        rubric_criteria: ["Name the project.", "Describe the trade-off.", "State the outcome."],
        follow_up_limit: 1,
        source_confidence: 0.94,
        parent_question_id: null,
        created_at: "2026-08-29T10:01:00.000Z",
      }],
      [],
      [],
      new Map([["competency-1", "React architecture"]]),
    );

    expect(mapped.blueprint).toMatchObject({
      status: "limited-grounding",
      fallbackReason: "Gemini returned invalid blueprint JSON.",
      maxFollowUps: 2,
      maxQuestions: 7,
      questions: [expect.objectContaining({
        id: "question-1",
        competencyId: "competency-1",
        objective: "Probe the migration ownership and impact.",
        evidenceIds: ["evidence-1"],
        expectedSignals: ["role", "impact"],
        followUpLimit: 1,
        rubricCriteria: ["Name the project.", "Describe the trade-off.", "State the outcome."],
        parentQuestionId: null,
      })],
    });
    expect(mapped.questions[0]).toMatchObject({
      objective: "Probe the migration ownership and impact.",
      evidenceIds: ["evidence-1"],
      expectedSignals: ["role", "impact"],
      missingSignalPrompts: ["Name the trade-off."],
      rubricCriteria: ["Name the project.", "Describe the trade-off.", "State the outcome."],
      followUpLimit: 1,
      sourceConfidence: 0.94,
      parentQuestionId: null,
    });
  });

  it("maps the new question columns onto the session", () => {
    const session = mapSession(
      legacyRow(),
      [{
        id: "question-1", sequence: 1, category: "experience", competency_id: null,
        difficulty: "senior", is_follow_up: false, prompt: null, answer: null,
        asked_intent: { kind: "open", targetId: "question-1" },
        assistance: [{ style: "hook", at: "2026-09-01T00:00:00.000Z" }],
        non_answer: true,
        created_at: "2026-09-01T00:00:00.000Z",
      }],
      [], [], new Map(),
    );

    const question = session.questions[0];
    expect(question.askedIntent).toEqual({ kind: "open", targetId: "question-1" });
    expect(question.assistance).toHaveLength(1);
    expect(question.nonAnswer).toBe(true);
    expect(question.prompt).toBeNull();
  });

  it("hydrates round, mode, and degraded state onto the session", () => {
    const session = mapSession(
      legacyRow({ round_id: "founder", mode: "coach", degraded: true }),
      [], [], [], new Map(),
    );

    expect(session.roundId).toBe("founder");
    expect(session.mode).toBe("coach");
    expect(session.degraded).toBe(true);
  });

  it("defaults round, mode, and degraded state for legacy rows that predate this release", () => {
    const session = mapSession(legacyRow(), [], [], [], new Map());

    expect(session.roundId).toBe("tech-lead");
    expect(session.mode).toBe("real");
    expect(session.degraded).toBe(false);
  });

  /**
   * The load-bearing reconstruction Ruling C depends on: `nextTurn` reads
   * `blueprint.targets` on every stateless turn (a fresh reload), and
   * `deriveCoverageState` matches a persisted `askedIntent.targetId` against
   * `target.id` for every target in THIS reload's list. If a reload ever
   * produced different target ids than the ones already embedded in
   * persisted `askedIntent`s, every target would look perpetually unasked.
   * Using each row's own database id as its target's id is what makes the
   * id stable across reloads (a primary key never changes) -- this test
   * proves that for two separate targets, and proves `required` (added by
   * Task 9.1, defaulting to `true`) survives the round trip in both
   * directions, not just the all-default-true case.
   */
  it("reconstructs blueprint.targets from persisted question rows, keyed by each row's own id", () => {
    const mapped = mapSession(
      legacyRow({ round_id: "tech-lead", mode: "real" }),
      [
        {
          id: "question-1", sequence: 1, category: "experience", competency_id: null,
          competency_name: "Ownership", difficulty: "senior", is_follow_up: false,
          prompt: null, answer: null, objective: "Probe ownership.",
          evidence_ids: ["evidence-1"], expected_signals: ["ownership"],
          rubric_criteria: ["Names the decision."], required: true,
          created_at: "2026-09-01T10:00:00.000Z",
        },
        {
          id: "question-2", sequence: 2, category: "technical", competency_id: null,
          competency_name: "System design", difficulty: "senior", is_follow_up: false,
          prompt: null, answer: null, objective: "Probe system design.",
          evidence_ids: [], expected_signals: ["trade-off"], rubric_criteria: [],
          required: false,
          created_at: "2026-09-01T10:00:00.000Z",
        },
        {
          // A follow-up row (Task 9's `record_conversation_turn` inserts these) --
          // it must never become a coverage target.
          id: "question-3-followup", sequence: 3, category: "technical", competency_id: null,
          difficulty: "senior", is_follow_up: true, parent_question_id: "question-2",
          prompt: "A follow-up prompt.", answer: null, objective: "Follow-up objective.",
          created_at: "2026-09-01T10:00:00.000Z",
        },
      ],
      [], [], new Map(),
    );

    const targets = mapped.blueprint!.targets;
    expect(targets).toHaveLength(2);
    expect(targets[0]).toMatchObject({ id: "question-1", competencyName: "Ownership", required: true });
    expect(targets[1]).toMatchObject({ id: "question-2", competencyName: "System design", required: false });
  });

  it("rejects a plan that is not the exact five-question backbone before persistence", () => {
    expect(() => assertConversationPlan([
      { id: "1", sequence: 1, category: "introduction", competencyId: null, competencyName: null, difficulty: "senior", isFollowUp: false, prompt: "one", answer: null, createdAt: "", askedIntent: null, assistance: [], nonAnswer: false, setAsideAt: null, setAsideReason: null, nonAnswers: [] },
      { id: "2", sequence: 2, category: "experience", competencyId: null, competencyName: null, difficulty: "senior", isFollowUp: false, prompt: "two", answer: null, createdAt: "", askedIntent: null, assistance: [], nonAnswer: false, setAsideAt: null, setAsideReason: null, nonAnswers: [] },
      { id: "3", sequence: 3, category: "technical", competencyId: null, competencyName: null, difficulty: "senior", isFollowUp: false, prompt: "three", answer: null, createdAt: "", askedIntent: null, assistance: [], nonAnswer: false, setAsideAt: null, setAsideReason: null, nonAnswers: [] },
      { id: "4", sequence: 4, category: "architecture", competencyId: null, competencyName: null, difficulty: "senior", isFollowUp: false, prompt: "four", answer: null, createdAt: "", askedIntent: null, assistance: [], nonAnswer: false, setAsideAt: null, setAsideReason: null, nonAnswers: [] },
    ])).toThrow("five-question backbone");
  });

  it("creates the exact backbone through the atomic plan RPC", async () => {
    const calls: Array<{ name: string; payload: unknown }> = [];
    const sessionRow = {
      id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
      started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
      overall_score: null, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
    };
    const emptyQuery = {
      eq: () => emptyQuery,
      order: async () => ({ data: [], error: null }),
    };
    const sessionQuery = {
      eq: () => sessionQuery,
      maybeSingle: async () => ({ data: sessionRow, error: null }),
    };
    const supabase = {
      rpc: async (name: string, payload: unknown) => {
        calls.push({ name, payload });
        return { data: [{ session_id: "session-1" }], error: null };
      },
      from: (table: string) => ({ select: () => table === "interview_sessions" ? sessionQuery : emptyQuery }),
    };
    const plan = ["introduction", "experience", "technical", "architecture", "behavioral"].map((category, index) => ({
      id: `question-${index + 1}`, sequence: index + 1, category, competencyId: null, competencyName: null,
      difficulty: "senior", isFollowUp: false, prompt: `prompt-${index + 1}`, answer: null, createdAt: "",
    }));

    const session = await createSessionWithPlan(supabase as never, "user-1", plan as never);

    expect(session.id).toBe("session-1");
    expect(calls).toEqual([{
      name: "create_conversation_session_with_plan",
      payload: { p_plan: expect.arrayContaining([expect.objectContaining({ sequence: 1, category: "introduction" })]) },
    }]);
  });

  it("persists a grounded blueprint through the atomic blueprint RPC", async () => {
    const calls: Array<{ name: string; payload: unknown }> = [];
    const sessionRow = {
      id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
      started_at: "2026-08-29T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
      overall_score: null, blueprint_status: "grounded", blueprint_fallback_reason: null,
      created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
    };
    const emptyQuery = {
      eq: () => emptyQuery,
      order: async () => ({ data: [], error: null }),
      in: async () => ({ data: [], error: null }),
    };
    const sessionQuery = {
      eq: () => sessionQuery,
      maybeSingle: async () => ({ data: sessionRow, error: null }),
    };
    const supabase = {
      rpc: async (name: string, payload: unknown) => {
        calls.push({ name, payload });
        return { data: [{ session_id: "session-1" }], error: null };
      },
      from: (table: string) => ({ select: () => table === "interview_sessions" ? sessionQuery : emptyQuery }),
    };
    const blueprint: InterviewBlueprint = {
      status: "grounded",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [
        {
          id: "question-1",
          sequence: 1,
          category: "introduction",
          competencyId: null,
          competencyName: null,
          difficulty: "senior",
          isFollowUp: false,
          prompt: "Give me a concise introduction to yourself and the frontend work you have owned recently.",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Establish recent engineering ownership.",
          evidenceIds: [],
          expectedSignals: ["role summary"],
          missingSignalPrompts: ["Name the recent engineering area you owned."],
          followUpLimit: 0,
          sourceConfidence: null,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-2",
          sequence: 2,
          category: "experience",
          competencyId: "competency-1",
          competencyName: "React architecture",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "Tell me about the migration.",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Probe the migration ownership and impact.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["role", "impact"],
          missingSignalPrompts: ["Name the trade-off."],
          followUpLimit: 1,
          sourceConfidence: 0.94,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-3",
          sequence: 3,
          category: "technical",
          competencyId: "competency-1",
          competencyName: "React architecture",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "Walk me through the route-splitting decision.",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Probe the route-splitting trade-off.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["decision"],
          missingSignalPrompts: ["What option did you reject?"],
          followUpLimit: 1,
          sourceConfidence: 0.94,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-4",
          sequence: 4,
          category: "architecture",
          competencyId: "competency-2",
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "How did you shape observability for API regressions?",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Probe observability system design choices.",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["requirements"],
          missingSignalPrompts: ["What alert trade-off mattered most?"],
          followUpLimit: 1,
          sourceConfidence: 0.91,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
        {
          id: "question-5",
          sequence: 5,
          category: "behavioral",
          competencyId: "competency-2",
          competencyName: "System design",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "How did you align the team on release health?",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Probe collaboration during observability delivery.",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["collaboration"],
          missingSignalPrompts: ["Who did you need alignment from?"],
          followUpLimit: 0,
          sourceConfidence: 0.91,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [
        sampleTarget({
          id: "target-1",
          competencyId: null,
          competencyName: null,
          category: "introduction",
          evidenceIds: [],
          objective: "Establish recent engineering ownership.",
          expectedSignals: ["role summary"],
          rubricCriteria: [],
        }),
        sampleTarget({
          id: "target-2",
          competencyId: "competency-1",
          competencyName: "React architecture",
          category: "experience",
          evidenceIds: ["evidence-1"],
          objective: "Probe the migration ownership and impact.",
          expectedSignals: ["role", "impact"],
          rubricCriteria: ["Name the trade-off."],
        }),
      ],
    };

    const session = await createSessionWithBlueprint(supabase as never, "user-1", blueprint, {
      roundId: "tech-lead",
      mode: "real",
    });

    expect(session.id).toBe("session-1");
    expect(calls).toEqual([{
      name: "create_conversation_session_with_blueprint",
      payload: expect.objectContaining({
        p_blueprint: expect.objectContaining({
          roundId: "tech-lead",
          mode: "real",
          status: "grounded",
          max_follow_ups: 3,
          max_questions: 8,
          targets: expect.arrayContaining([
            expect.objectContaining({
              sequence: 2,
              objective: "Probe the migration ownership and impact.",
              evidence_ids: ["evidence-1"],
              required: true,
            }),
          ]),
        }),
      }),
    }]);
    // The legacy `questions` key must not be sent -- the live RPC only reads
    // `p_blueprint.targets` (Task 9's migration drops the old `questions` key
    // entirely).
    expect((calls[0].payload as { p_blueprint: Record<string, unknown> }).p_blueprint.questions).toBeUndefined();
  });

  it("strips non-UUID fallback competency ids before the blueprint RPC while keeping persisted UUID links", async () => {
    const calls: Array<{ name: string; payload: unknown }> = [];
    const sessionRow = {
      id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
      started_at: "2026-08-30T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
      overall_score: null, blueprint_status: "limited-grounding", blueprint_fallback_reason: "Gemini returned invalid blueprint JSON after one repair attempt.",
      created_at: "2026-08-30T10:00:00.000Z", updated_at: "2026-08-30T10:00:00.000Z",
    };
    const emptyQuery = {
      eq: () => emptyQuery,
      order: async () => ({ data: [], error: null }),
      in: async () => ({ data: [], error: null }),
    };
    const sessionQuery = {
      eq: () => sessionQuery,
      maybeSingle: async () => ({ data: sessionRow, error: null }),
    };
    const supabase = {
      rpc: async (name: string, payload: unknown) => {
        calls.push({ name, payload });
        return { data: [{ session_id: "session-1" }], error: null };
      },
      from: (table: string) => ({ select: () => table === "interview_sessions" ? sessionQuery : emptyQuery }),
    };
    // buildFallbackInterviewBlueprint doesn't populate roundId/turnBudget/targets
    // itself (see interview-planner.test.ts's own comment on this) -- Task 8's
    // withCoveragePlan is the one merge point that does, so tests construct
    // targets directly, the same way interview-planner.test.ts does.
    const blueprint: InterviewBlueprint = {
      ...buildFallbackInterviewBlueprint(
        fallbackProfile,
        fallbackCompetencies,
        fallbackEvidence,
        new Date("2026-08-30T10:00:00.000Z"),
      ),
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [
        sampleTarget({ id: "target-1", competencyId: "fallback-competency-1" }),
        sampleTarget({ id: "target-2", competencyId: "0d7f2d0c-8e26-4ae6-b4b2-f9f44c4f4ab8" }),
      ],
    };

    await createSessionWithBlueprint(supabase as never, "user-1", blueprint, {
      roundId: "tech-lead",
      mode: "real",
    });

    expect(calls).toEqual([{
      name: "create_conversation_session_with_blueprint",
      payload: expect.objectContaining({
        p_blueprint: expect.objectContaining({
          status: "limited-grounding",
          targets: [
            expect.objectContaining({
              sequence: 1,
              // Not a UUID, so it cannot satisfy the competency_id foreign
              // key -- persistableCompetencyId strips it to null.
              competency_id: null,
            }),
            expect.objectContaining({
              sequence: 2,
              competency_id: "0d7f2d0c-8e26-4ae6-b4b2-f9f44c4f4ab8",
            }),
          ],
        }),
      }),
    }]);
  });

  it("persists competency names when blueprint questions do not have UUIDs and hydrates them back", async () => {
    const calls: Array<{ name: string; payload: unknown }> = [];
    const sessionRow = {
      id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
      started_at: "2026-08-30T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
      overall_score: null, blueprint_status: "grounded", blueprint_fallback_reason: null,
      created_at: "2026-08-30T10:00:00.000Z", updated_at: "2026-08-30T10:00:00.000Z",
    };
    const emptyQuery = {
      eq: () => emptyQuery,
      in: async () => ({ data: [], error: null }),
      order: async () => ({ data: [], error: null }),
    };
    const sessionQuery = {
      eq: () => sessionQuery,
      maybeSingle: async () => ({ data: sessionRow, error: null }),
    };
    const blueprintQuestion = (sequence: number, category: PlannedQuestion["category"], competencyName: string | null, competencyId: string | null = null) => ({
      id: `blueprint-question-${sequence}`,
      sequence,
      category,
      competencyId,
      competencyName,
      difficulty: "senior" as const,
      isFollowUp: false,
      prompt: `Question ${sequence}`,
      answer: null,
      createdAt: "2026-08-30T10:00:00.000Z",
      objective: `Objective ${sequence}`,
      evidenceIds: [],
      expectedSignals: ["signal"],
      missingSignalPrompts: ["Prompt"],
      followUpLimit: 1,
      sourceConfidence: 0.8,
      askedIntent: null,
      assistance: [],
      nonAnswer: false,
      setAsideAt: null,
      setAsideReason: null,
      nonAnswers: [],
    });
    const supabase = {
      rpc: async (name: string, payload: unknown) => {
        calls.push({ name, payload });
        return { data: [{ session_id: "session-1" }], error: null };
      },
      from: (table: string) => ({ select: () => table === "interview_sessions" ? sessionQuery : emptyQuery }),
    };
    const blueprint = {
      status: "grounded" as const,
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-30T10:00:00.000Z",
      questions: [
        blueprintQuestion(1, "introduction", null),
        blueprintQuestion(2, "experience", "Backend systems"),
        blueprintQuestion(3, "technical", "System design", "system-design"),
        blueprintQuestion(4, "architecture", "Reliability"),
        blueprintQuestion(5, "behavioral", "Communication"),
      ],
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [
        sampleTarget({ id: "target-1", category: "introduction", competencyId: null, competencyName: null }),
        sampleTarget({ id: "target-2", category: "experience", competencyId: null, competencyName: "Backend systems" }),
        sampleTarget({ id: "target-3", category: "technical", competencyId: null, competencyName: "System design" }),
        sampleTarget({ id: "target-4", category: "architecture", competencyId: null, competencyName: "Reliability" }),
        sampleTarget({ id: "target-5", category: "behavioral", competencyId: null, competencyName: "Communication" }),
      ],
    };

    await createSessionWithBlueprint(supabase as never, "user-1", blueprint, {
      roundId: "tech-lead",
      mode: "real",
    });

    expect(calls).toEqual([{
      name: "create_conversation_session_with_blueprint",
      payload: expect.objectContaining({
        p_blueprint: expect.objectContaining({
          targets: expect.arrayContaining([
            expect.objectContaining({
              sequence: 2,
              competency_id: null,
              competency_name: "Backend systems",
            }),
          ]),
        }),
      }),
    }]);

    const mapped = mapSession(
      sessionRow,
      [
        {
          id: "question-1",
          sequence: 1,
          category: "introduction",
          competency_id: null,
          competency_name: null,
          difficulty: "senior",
          is_follow_up: false,
          prompt: "one",
          answer: null,
          created_at: "2026-08-30T10:01:00.000Z",
        },
        {
          id: "question-2",
          sequence: 2,
          category: "experience",
          competency_id: null,
          competency_name: "Backend systems",
          difficulty: "senior",
          is_follow_up: false,
          prompt: "two",
          answer: "I owned it.",
          created_at: "2026-08-30T10:02:00.000Z",
          answered_at: "2026-08-30T10:03:00.000Z",
        },
      ],
      [],
      [],
      new Map(),
    );

    expect(mapped.questions[1]).toMatchObject({
      competencyId: null,
      competencyName: "Backend systems",
    });
  });

  it("hydrates persisted hands-on evaluations and interviewer history", () => {
    const mapped = mapSession(
      {
        id: "hands-on-1", user_id: "user-1", kind: "hands-on", status: "complete",
        started_at: "2026-08-29T10:00:00.000Z", completed_at: "2026-08-29T11:00:00.000Z",
        exercise: { interviewerOpening: "Clarify the brief first." }, result_summary: { summary: "Review" },
        overall_score: 7, created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T11:00:00.000Z",
      },
      [],
      [],
      [{
        id: "checkpoint-1", code: "const result = true;", note: "I separated state ownership.",
        interviewer_prompt: "How will you prevent stale responses?", created_at: "2026-08-29T10:20:00.000Z",
      }],
      new Map([["architecture-id", "React architecture"]]),
      [{
        id: "evaluation-1", competency_id: "architecture-id", overall_score: 7,
        dimensions: { structure: 8 }, strengths: ["Clear ownership"], weaknesses: ["Add cancellation"],
      }],
    );

    expect(mapped.evaluations).toEqual([expect.objectContaining({
      competencyId: "architecture-id",
      competency: "React architecture",
      score: 7,
      missingPoints: [],
      betterStructure: [],
      improvedAnswer: "",
    })]);
    expect(mapped.messages.map((message) => message.content)).toEqual([
      "Clarify the brief first.",
      "Checkpoint: I separated state ownership.",
      "How will you prevent stale responses?",
    ]);
  });

  it("records an answer and its next persisted question in one RPC", async () => {
    const calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
    const supabase = rpcHydrationClient(calls, "conversation");

    await recordConversationTurn(
      supabase as never,
      "user-1",
      "question-1",
      "I compared the trade-offs.",
      {
        score: 7,
        competencyId: "react-id",
        competency: "React",
        relevance: 8.4,
        dimensions: {},
        strengths: ["Specific"],
        needsWork: ["Quantify"],
        missingPoints: ["Name the fallback path."],
        betterStructure: ["Lead with the requirement, then the trade-off."],
        improvedAnswer: "I would start with the requirement, compare the trade-offs, and justify the fallback path.",
        supportedClaims: ["compared the trade-offs"],
        expectedSignalsPresent: ["trade-off"],
        unsupportedClaims: ["It was easy and perfect."],
        dimensionReasons: { relevance: "It answers the exact trade-off question." },
      },
      {
        nextQuestionId: "question-2",
        nextPrompt: "How would you design the system?",
        followUp: null,
        askedIntent: { kind: "open", targetId: "question-2" },
        assistance: [],
        nonAnswer: false,
        degraded: false,
        setAsideReason: null,
      },
    );

    expect(calls).toEqual([{
      name: "record_conversation_turn",
      payload: expect.objectContaining({
        p_question_id: "question-1",
        p_next_question_id: "question-2",
        p_next_prompt: "How would you design the system?",
        p_follow_up: null,
        p_relevance: 8.4,
        p_missing_points: ["Name the fallback path."],
        p_better_structure: ["Lead with the requirement, then the trade-off."],
        p_improved_answer: "I would start with the requirement, compare the trade-offs, and justify the fallback path.",
        p_supported_claims: ["compared the trade-offs"],
        p_expected_signals_present: ["trade-off"],
        p_unsupported_claims: ["It was easy and perfect."],
        p_dimension_reasons: { relevance: "It answers the exact trade-off question." },
      }),
    }]);
  });

  it("records a follow-up draft with the full rubric contract", async () => {
    const calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
    const supabase = rpcHydrationClient(calls, "conversation");

    await recordConversationTurn(
      supabase as never,
      "user-1",
      "question-1",
      "I compared the trade-offs.",
      {
        score: 7,
        competencyId: "react-id",
        competency: "React",
        relevance: 8.4,
        dimensions: {},
        strengths: ["Specific"],
        needsWork: ["Quantify"],
        missingPoints: ["Name the fallback path."],
        betterStructure: ["Lead with the requirement, then the trade-off."],
        improvedAnswer: "I would start with the requirement, compare the trade-offs, and justify the fallback path.",
        supportedClaims: ["compared the trade-offs"],
        expectedSignalsPresent: ["trade-off"],
        unsupportedClaims: ["It was easy and perfect."],
        dimensionReasons: { relevance: "It answers the exact trade-off question." },
      },
      {
        nextQuestionId: null,
        nextPrompt: null,
        followUp: {
          category: "technical",
          competencyId: "react-id",
          competencyName: "React",
          difficulty: "senior",
          isFollowUp: true,
          prompt: "Make the migration decision more concrete.",
          objective: "Probe the migration trade-off decision.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["decision", "trade-off", "impact"],
          missingSignalPrompts: ["Name the constraint or rejected option."],
          followUpLimit: 1,
          sourceConfidence: 0.94,
          rubricCriteria: [
            "Name the decision being revisited.",
            "Explain the constraint or rejected option.",
            "Describe the trade-off and impact.",
          ],
        } as never,
        askedIntent: { kind: "probe", targetId: "question-1", aspect: "specifics", basis: "compared the trade-offs" },
        assistance: [],
        nonAnswer: false,
        degraded: false,
        setAsideReason: null,
      },
    );

    expect(calls).toEqual([{
      name: "record_conversation_turn",
      payload: expect.objectContaining({
        p_follow_up: expect.objectContaining({
          objective: "Probe the migration trade-off decision.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["decision", "trade-off", "impact"],
          missingSignalPrompts: ["Name the constraint or rejected option."],
          followUpLimit: 1,
          sourceConfidence: 0.94,
          rubricCriteria: [
            "Name the decision being revisited.",
            "Explain the constraint or rejected option.",
            "Describe the trade-off and impact.",
          ],
        }),
      }),
    }]);
  });

  it("passes the intent, assistance, and non-answer flag to the RPC", async () => {
    const calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
    const supabase = rpcHydrationClient(calls, "conversation");

    await recordConversationTurn(
      supabase as never,
      "user-1",
      "question-1",
      "an answer",
      sampleEvaluation(),
      {
        nextQuestionId: "question-2",
        nextPrompt: "What did you own there?",
        followUp: null,
        askedIntent: { kind: "probe", targetId: "question-1", aspect: "ownership", basis: "an answer" },
        assistance: [{ style: "narrow", at: "2026-09-01T00:00:00.000Z" }],
        nonAnswer: false,
        degraded: true,
        setAsideReason: null,
      },
    );

    expect(calls).toEqual([{
      name: "record_conversation_turn",
      payload: expect.objectContaining({
        p_asked_intent: { kind: "probe", targetId: "question-1", aspect: "ownership", basis: "an answer" },
        p_assistance: [{ style: "narrow", at: "2026-09-01T00:00:00.000Z" }],
        p_non_answer: false,
        p_degraded: true,
      }),
    }]);
  });

  it("records richer evaluation coaching through the question-evidence RPC", async () => {
    const calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
    const supabase = rpcHydrationClient(calls, "conversation");

    await recordAnswerAndEvaluation(
      supabase as never,
      "user-1",
      "question-1",
      "I compared the trade-offs.",
      {
        score: 7,
        competencyId: "react-id",
        competency: "React",
        relevance: 8.2,
        dimensions: {},
        strengths: ["Specific"],
        needsWork: ["Quantify"],
        missingPoints: ["Name the fallback path."],
        betterStructure: ["Lead with the requirement, then the trade-off."],
        improvedAnswer: "I would start with the requirement, compare the trade-offs, and justify the fallback path.",
        supportedClaims: ["compared the trade-offs"],
        expectedSignalsPresent: ["trade-off"],
        unsupportedClaims: ["It was easy and perfect."],
        dimensionReasons: { relevance: "It answers the exact trade-off question." },
      },
    );

    expect(calls).toEqual([{
      name: "record_interview_evidence",
      payload: expect.objectContaining({
        p_question_id: "question-1",
        p_missing_points: ["Name the fallback path."],
        p_better_structure: ["Lead with the requirement, then the trade-off."],
        p_improved_answer: "I would start with the requirement, compare the trade-offs, and justify the fallback path.",
        p_relevance: 8.2,
        p_supported_claims: ["compared the trade-offs"],
        p_expected_signals_present: ["trade-off"],
        p_unsupported_claims: ["It was easy and perfect."],
        p_dimension_reasons: { relevance: "It answers the exact trade-off question." },
      }),
    }]);
  });

  it("completes hands-on evaluation and competency evidence in one RPC", async () => {
    const calls: Array<{ name: string; payload: Record<string, unknown> }> = [];
    const supabase = rpcHydrationClient(calls, "hands-on", "complete");

    await completeHandsOnSession(
      supabase as never,
      "user-1",
      "session-1",
      {
        overallScore: 7,
        summary: "A useful signal.",
        evaluations: [{
          score: 7,
          competencyId: null,
          competency: "React architecture",
          relevance: 8.1,
          dimensions: { structure: 8 },
          strengths: ["Clear ownership"],
          needsWork: ["Add cancellation"],
          missingPoints: ["Call out keyboard focus recovery."],
          betterStructure: ["Start with interaction states, then discuss implementation."],
          improvedAnswer: "I would begin with the interaction states, then explain how the implementation preserves keyboard focus.",
          supportedClaims: ["preserves keyboard focus"],
          expectedSignalsPresent: ["ownership"],
          unsupportedClaims: ["It was perfect."],
          dimensionReasons: { relevance: "It answers the exact trade-off question." },
        }],
      },
    );

    expect(calls).toEqual([{
      name: "complete_hands_on_session",
      payload: expect.objectContaining({
        p_session_id: "session-1",
        p_overall_score: 7,
        p_evaluations: [expect.objectContaining({
          competency: "React architecture",
          score: 7,
          missing_points: ["Call out keyboard focus recovery."],
          better_structure: ["Start with interaction states, then discuss implementation."],
          improved_answer: "I would begin with the interaction states, then explain how the implementation preserves keyboard focus.",
          relevance: 8.1,
          supported_claims: ["preserves keyboard focus"],
          expected_signals_present: ["ownership"],
          unsupported_claims: ["It was perfect."],
          dimension_reasons: { relevance: "It answers the exact trade-off question." },
        })],
      }),
    }]);
  });
});

/**
 * A minimal grounded practice blueprint with `questionCount` contiguous base
 * questions (sequence 1..questionCount, none a follow-up) -- the shape
 * `assertPracticeConversationBlueprint` and `createSessionWithPracticeBlueprint`
 * accept, as opposed to the generic five-question backbone.
 */
function practiceBlueprint(questionCount: number): InterviewBlueprint {
  const categories: PlannedQuestion["category"][] = [
    "technical", "behavioral", "architecture", "experience", "introduction",
  ];
  return {
    status: "grounded",
    fallbackReason: null,
    maxFollowUps: 1,
    maxQuestions: questionCount,
    createdAt: "2026-08-31T10:00:00.000Z",
    questions: Array.from({ length: questionCount }, (_, index) => ({
      id: `practice-question-${index + 1}`,
      sequence: index + 1,
      category: categories[index % categories.length],
      competencyId: null,
      competencyName: null,
      difficulty: "senior",
      isFollowUp: false,
      prompt: `Practice prompt ${index + 1}`,
      answer: null,
      createdAt: "2026-08-31T10:00:00.000Z",
      objective: `Practice objective ${index + 1}`,
      evidenceIds: [],
      expectedSignals: ["signal"],
      missingSignalPrompts: ["Name the missing signal."],
      rubricCriteria: ["Meet the objective."],
      followUpLimit: 0,
      sourceConfidence: null,
      askedIntent: null,
      assistance: [],
      nonAnswer: false,
      setAsideAt: null,
      setAsideReason: null,
      nonAnswers: [],
    })),
    // A practice-plan blueprint never consumes the round/coverage-target system.
    roundId: "tech-lead",
    turnBudget: 8,
    targets: [],
  };
}

const practiceExercise: HandsOnExercise = {
  title: "Debug the flaky retry loop",
  durationMinutes: 20,
  briefing: "The retry loop occasionally double-submits. Find and fix it.",
  requirements: ["Identify the race condition", "Add a regression test"],
  starterCode: "function retry() {}",
  interviewerOpening: "Let's look at this retry loop together.",
};

/**
 * A `{ rpc, from }` supabase double for the planned-practice start RPCs.
 * `rpc` is a `vi.fn()` so tests can assert the exact RPC name and payload
 * via `toHaveBeenCalledWith`, matching the pattern used elsewhere in this
 * file. The session reload after the RPC call resolves the same way
 * `rpcHydrationClient` does: an empty legacy conversation row plus empty
 * question/checkpoint/evaluation tables.
 */
function practiceRpcSupabase() {
  const sessionRow = {
    id: "session-1", user_id: "user-1", kind: "conversation", status: "active",
    started_at: "2026-08-31T10:00:00.000Z", completed_at: null, exercise: {}, result_summary: {},
    overall_score: null, created_at: "2026-08-31T10:00:00.000Z", updated_at: "2026-08-31T10:00:00.000Z",
  };
  const emptyQuery = {
    eq: () => emptyQuery,
    in: async () => ({ data: [], error: null }),
    order: async () => ({ data: [], error: null }),
  };
  const sessionQuery = {
    eq: () => sessionQuery,
    maybeSingle: async () => ({ data: sessionRow, error: null }),
  };
  const rpc = vi.fn(async () => ({ data: [{ session_id: "session-1" }], error: null }));
  const from = (table: string) => ({ select: () => table === "interview_sessions" ? sessionQuery : emptyQuery });
  return { rpc, from };
}

describe("questionIdForTarget", () => {
  const rows: Row[] = [
    {
      id: "question-1", sequence: 1, category: "experience", competency_id: null,
      competency_name: "Ownership", difficulty: "senior", is_follow_up: false,
      prompt: "Tell me about the migration.", answer: "I owned it.",
      created_at: "2026-09-01T10:00:00.000Z", answered_at: "2026-09-01T10:01:00.000Z",
    },
    {
      id: "question-2", sequence: 2, category: "technical", competency_id: null,
      competency_name: "System design", difficulty: "senior", is_follow_up: false,
      prompt: null, answer: null, created_at: "2026-09-01T10:00:00.000Z",
    },
  ];

  it("returns the unanswered question row whose id matches the target id", () => {
    const session = mapSession(legacyRow(), rows, [], [], new Map());

    expect(questionIdForTarget(session, "question-2")).toBe("question-2");
  });

  it("returns null when the matching row has already been answered", () => {
    // Ruling C's invariant only ever routes a director targetId at an
    // unasked target -- a probe/challenge/rescue on an already-answered
    // target becomes a follow-up row instead (a different code path). This
    // guards against a caller bug reusing a stale, already-answered id.
    const session = mapSession(legacyRow(), rows, [], [], new Map());

    expect(questionIdForTarget(session, "question-1")).toBeNull();
  });

  it("returns null when no question row carries that id", () => {
    const session = mapSession(legacyRow(), rows, [], [], new Map());

    expect(questionIdForTarget(session, "no-such-id")).toBeNull();
  });
});

describe("revealFirstQuestion", () => {
  it("writes the authored prompt and intent onto the first unanswered row, scoped by user id", async () => {
    const sessionRow = legacyRow();
    const questionRow = {
      id: "question-1", sequence: 1, category: "experience", competency_id: null,
      difficulty: "senior", is_follow_up: false, prompt: null, answer: null,
      created_at: "2026-08-29T10:00:00.000Z",
    };
    const questionsCapture: { update?: Row; eq?: Array<[string, unknown]> } = {};
    const tables: Record<string, ReturnType<typeof tableStub>> = {
      interview_sessions: tableStub({ data: sessionRow, error: null }),
      interview_questions: tableStub({ data: [questionRow], error: null }, questionsCapture),
      hands_on_checkpoints: tableStub({ data: [], error: null }),
      session_evaluations: tableStub({ data: [], error: null }),
      question_evaluations: tableStub({ data: [], error: null }),
      competencies: tableStub({ data: [], error: null }),
    };
    const supabase = { from: (table: string) => tables[table] };
    const session = mapSession(sessionRow, [questionRow], [], [], new Map());

    const refreshed = await revealFirstQuestion(supabase as never, "user-1", session, {
      intent: { kind: "open", targetId: "question-1" },
      prompt: "Tell me about a project you owned end to end.",
      targetId: "question-1",
    });

    expect(refreshed.id).toBe(sessionRow.id);
    expect(questionsCapture.update).toEqual({
      prompt: "Tell me about a project you owned end to end.",
      asked_intent: { kind: "open", targetId: "question-1" },
      asked_at: expect.any(String),
    });
    // The update itself is the FIRST pair of `.eq(...)` calls on this table --
    // later pairs belong to the session reload's own `interview_questions`
    // query, which reuses the same table stub.
    expect(questionsCapture.eq?.slice(0, 2)).toEqual([["id", "question-1"], ["user_id", "user-1"]]);
  });

  it("throws when every question already has an answer", async () => {
    const sessionRow = legacyRow();
    const answeredRow = {
      id: "question-1", sequence: 1, category: "experience", competency_id: null,
      difficulty: "senior", is_follow_up: false, prompt: "Already asked.", answer: "Already answered.",
      created_at: "2026-08-29T10:00:00.000Z",
    };
    const session = mapSession(sessionRow, [answeredRow], [], [], new Map());
    const supabase = { from: () => tableStub({ data: null, error: null }) };

    await expect(
      revealFirstQuestion(supabase as never, "user-1", session, {
        intent: { kind: "open", targetId: "question-1" },
        prompt: "Anything else to reveal?",
        targetId: "question-1",
      }),
    ).rejects.toThrow("no question to reveal");
  });
});

describe("planned practice session starts", () => {
  it("accepts three planned base questions but generic backbone still rejects them", () => {
    expect(() => assertPracticeConversationBlueprint(practiceBlueprint(3))).not.toThrow();
    expect(() => assertConversationPlan(practiceBlueprint(3).questions)).toThrow();
  });

  it("calls planned conversation RPC with context", async () => {
    const { rpc, from } = practiceRpcSupabase();
    const supabase = { rpc, from };

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
    const { rpc, from } = practiceRpcSupabase();
    const supabase = { rpc, from };

    await createHandsOnPracticeSession(supabase as never, "user-1", practiceExercise, {
      practicePlanId: "plan-1",
      opportunityId: null,
    });

    expect(rpc).toHaveBeenCalledWith(
      "start_hands_on_practice_session",
      expect.objectContaining({ p_practice_plan_id: "plan-1", p_exercise: practiceExercise }),
    );
  });
});

describe("linkSessionCareerContext", () => {
  it("throws when the session is not owned by the user", async () => {
    const supabase = careerContextSupabase({ sessionRow: null });

    await expect(
      linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
        practicePlanId: null,
        opportunityId: null,
      }),
    ).rejects.toThrow("interview session was not found");
  });

  it("rejects an opportunity that is not associated with the requested plan", async () => {
    const planOpportunitiesCapture: { eq?: Array<[string, unknown]> } = {};
    const supabase = careerContextSupabase({
      sessionRow: legacyRow(),
      planRow: { id: "plan-1", user_id: "user-1" },
      opportunityRow: { id: "opp-1", user_id: "user-1" },
      planOpportunityRows: [], // plan-1 has no linked opportunities at all
      captures: { planOpportunities: planOpportunitiesCapture },
    });

    await expect(
      linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
        practicePlanId: "plan-1",
        opportunityId: "opp-1",
      }),
    ).rejects.toMatchObject(new RepositoryError(
      "The practice plan and opportunity do not match.",
      "INVALID_PLAN_CONTEXT",
    ));

    // The mismatch must be checked by querying practice_plan_opportunities
    // scoped by BOTH user id and plan id -- not merely by call count.
    expect(planOpportunitiesCapture.eq).toEqual([
      ["user_id", "user-1"],
      ["practice_plan_id", "plan-1"],
    ]);
  });

  it("rejects an opportunity that is associated but differs from the plan's primary opportunity", async () => {
    const supabase = careerContextSupabase({
      sessionRow: legacyRow(),
      planRow: { id: "plan-1", user_id: "user-1" },
      opportunityRow: { id: "opp-2", user_id: "user-1" },
      planOpportunityRows: [
        { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-1", relevance: "primary" },
        { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-2", relevance: "supporting" },
      ],
    });

    await expect(
      linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
        practicePlanId: "plan-1",
        opportunityId: "opp-2",
      }),
    ).rejects.toMatchObject(new RepositoryError(
      "The practice plan and opportunity do not match.",
      "INVALID_PLAN_CONTEXT",
    ));
  });

  it("links a session to a practice plan and its primary opportunity, scoped by user id throughout", async () => {
    const sessionCapture: { update?: Row; eq?: Array<[string, unknown]> } = {};
    const planCapture: { eq?: Array<[string, unknown]> } = {};
    const opportunityCapture: { eq?: Array<[string, unknown]> } = {};
    const planOpportunitiesCapture: { eq?: Array<[string, unknown]> } = {};
    const supabase = careerContextSupabase({
      sessionRow: legacyRow({ practice_plan_id: "plan-1", opportunity_id: "opp-1" }),
      planRow: { id: "plan-1", user_id: "user-1" },
      opportunityRow: { id: "opp-1", user_id: "user-1" },
      planOpportunityRows: [
        { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-1", relevance: "primary" },
      ],
      captures: {
        session: sessionCapture,
        plan: planCapture,
        opportunity: opportunityCapture,
        planOpportunities: planOpportunitiesCapture,
      },
    });

    const session = await linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
      practicePlanId: "plan-1",
      opportunityId: "opp-1",
    });

    expect(session.practicePlanId).toBe("plan-1");
    expect(session.opportunityId).toBe("opp-1");
    expect(sessionCapture.update).toEqual(expect.objectContaining({
      practice_plan_id: "plan-1",
      opportunity_id: "opp-1",
    }));
    // interview_sessions is touched three times (ownership check, update,
    // post-update reload) and every one of them must be scoped by BOTH the
    // session id and the requesting user id -- this is the assertion the
    // task's deliberate-failure check exercises.
    expect(sessionCapture.eq).toEqual([
      ["id", "session-legacy"], ["user_id", "user-1"],
      ["id", "session-legacy"], ["user_id", "user-1"],
      ["id", "session-legacy"], ["user_id", "user-1"],
    ]);
    expect(planCapture.eq).toEqual([["id", "plan-1"], ["user_id", "user-1"]]);
    expect(opportunityCapture.eq).toEqual([["id", "opp-1"], ["user_id", "user-1"]]);
    expect(planOpportunitiesCapture.eq).toEqual([["user_id", "user-1"], ["practice_plan_id", "plan-1"]]);
  });

  it("links a session to a non-primary opportunity when the plan has associated opportunities but no primary designation", async () => {
    // Spec section 17.5 case 5's "if one exists" branch: when a plan has no
    // `primary` link at all, `linkSessionCareerContext` only needs the
    // requested opportunity to be associated with the plan -- there is no
    // primary to match against.
    const supabase = careerContextSupabase({
      sessionRow: legacyRow({ practice_plan_id: "plan-1", opportunity_id: "opp-2" }),
      planRow: { id: "plan-1", user_id: "user-1" },
      opportunityRow: { id: "opp-2", user_id: "user-1" },
      planOpportunityRows: [
        { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-1", relevance: "supporting" },
        { user_id: "user-1", practice_plan_id: "plan-1", opportunity_id: "opp-2", relevance: "supporting" },
      ],
    });

    const session = await linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
      practicePlanId: "plan-1",
      opportunityId: "opp-2",
    });

    expect(session.practicePlanId).toBe("plan-1");
    expect(session.opportunityId).toBe("opp-2");
  });

  it("links a session to only a practice plan, without requiring any opportunity association check", async () => {
    const supabase = careerContextSupabase({
      sessionRow: legacyRow({ practice_plan_id: "plan-1" }),
      planRow: { id: "plan-1", user_id: "user-1" },
      // No opportunity/planOpportunity tables stubbed: this must not query them.
    });

    const session = await linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
      practicePlanId: "plan-1",
      opportunityId: null,
    });

    expect(session.practicePlanId).toBe("plan-1");
    expect(session.opportunityId).toBeNull();
  });

  it("clears both links when passed an all-null context, without touching plan or opportunity tables", async () => {
    const supabase = careerContextSupabase({
      sessionRow: legacyRow({ practice_plan_id: null, opportunity_id: null }),
    });

    const session = await linkSessionCareerContext(supabase as never, "user-1", "session-legacy", {
      practicePlanId: null,
      opportunityId: null,
    });

    expect(session.practicePlanId).toBeNull();
    expect(session.opportunityId).toBeNull();
  });
});

function rpcHydrationClient(
  calls: Array<{ name: string; payload: Record<string, unknown> }>,
  kind: "conversation" | "hands-on",
  status: "active" | "complete" = "active",
) {
  const sessionRow = {
    id: "session-1", user_id: "user-1", kind, status,
    started_at: "2026-08-29T10:00:00.000Z", completed_at: status === "complete" ? "2026-08-29T11:00:00.000Z" : null,
    exercise: {}, result_summary: {}, overall_score: status === "complete" ? 7 : null,
    created_at: "2026-08-29T10:00:00.000Z", updated_at: "2026-08-29T10:00:00.000Z",
  };
  const emptyQuery = {
    eq: () => emptyQuery,
    in: async () => ({ data: [], error: null }),
    order: async () => ({ data: [], error: null }),
  };
  const sessionQuery = {
    eq: () => sessionQuery,
    maybeSingle: async () => ({ data: sessionRow, error: null }),
  };
  return {
    rpc: async (name: string, payload: Record<string, unknown>) => {
      calls.push({ name, payload });
      return { data: [{ session_id: "session-1" }], error: null };
    },
    from: (table: string) => ({ select: () => table === "interview_sessions" ? sessionQuery : emptyQuery }),
  };
}

/**
 * A generic, table-backed Supabase double for read-only paged queries: each
 * `.eq()`/`.in()` narrows the in-memory row set for that table, `.order()`
 * sorts it (real sorting, not a no-op -- `selectAllPages` relies on a stable
 * sort to keep row order identical across the separate `.range()` calls that
 * back each page), and `.range(from, to)` is the terminal call, slicing the
 * sorted/filtered rows and resolving to `{ data, error: null }`. Tables not
 * passed in behave as empty. Unlike `tableStub`/`careerContextSupabase`,
 * this mock does not distinguish "which query is running" -- it just
 * filters/sorts/slices rows -- which is enough for `listReadinessEvidence`'s
 * straight-line reads, while still exercising real multi-page paging.
 *
 * `.select(columns)` genuinely PROJECTS: rows come back carrying only the
 * requested columns. Without that, every read behaved like `select("*")` here
 * and a column missing from a production select list would surface as
 * `undefined` only in production, invisible to this suite -- so the whole-row
 * `toEqual` assertions below would have been guarding nothing. Projection
 * happens in `.range()`, not in `.select()`, because PostgREST filters
 * server-side against the full row and narrows only the response: `.eq(
 * "user_id", ...)` has to keep working on a column no select list asks for.
 */
function mockSupabase(tables: Record<string, Row[]>) {
  const from = vi.fn((table: string) => {
    let rows = tables[table] ?? [];
    let projection: string[] | null = null;
    const project = (row: Row): Row =>
      projection === null
        ? row
        : Object.fromEntries(projection.filter((column) => column in row).map((column) => [column, row[column]]));
    const builder = {
      select: (columns = "*") => {
        projection = columns === "*" ? null : columns.split(",").map((column) => column.trim());
        return builder;
      },
      eq: (field: string, value: unknown) => {
        rows = rows.filter((row) => row[field] === value);
        return builder;
      },
      in: (field: string, values: unknown[]) => {
        const allowed = new Set(values);
        rows = rows.filter((row) => allowed.has(row[field]));
        return builder;
      },
      // Only the `is` form is modelled, because it is the only one production
      // uses. `not(field, "is", true)` is Postgres `field IS NOT TRUE`, which
      // -- unlike `neq` -- keeps rows where the column is NULL.
      not: (field: string, operator: string, value: unknown) => {
        if (operator !== "is") throw new Error(`unsupported not() operator: ${operator}`);
        rows = rows.filter((row) => row[field] !== value);
        return builder;
      },
      order: (field: string, options?: { ascending?: boolean }) => {
        const direction = options?.ascending === false ? -1 : 1;
        rows = [...rows].sort((a, b) => {
          const left = String(a[field]);
          const right = String(b[field]);
          return left < right ? -direction : left > right ? direction : 0;
        });
        return builder;
      },
      range: async (from: number, to: number) => ({ data: rows.slice(from, to + 1).map(project), error: null }),
    };
    return builder;
  });
  return { from };
}

/**
 * The status a finished session is actually persisted with. Typed off
 * `InterviewSession` rather than inlined so that these fixtures cannot drift
 * from the literal the completion RPCs write and `hydrateSession` reads -- the
 * drift that once made `listReadinessEvidence` return nothing for every real
 * user while the suite stayed green.
 */
const COMPLETED_STATUS: InterviewSession["status"] = "complete";

describe("listReadinessEvidence", () => {
  it("flattens graded answers with the session conditions needed to weight them", async () => {
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: COMPLETED_STATUS, mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1",
          user_id: "user-1",
          session_id: "session-1",
          category: "technical",
          competency_id: "competency-1",
          assistance: [{ style: "hook", at: "2026-09-01T10:00:00.000Z" }],
          non_answer: false,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1",
          user_id: "user-1",
          question_id: "question-1",
          overall_score: 8,
          created_at: "2026-09-01T10:05:00.000Z",
        },
      ],
      competencies: [
        { id: "competency-1", user_id: "user-1", name: "React architecture", relevance: 0.9 },
      ],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toEqual([
      {
        questionEvaluationId: "eval-1",
        sessionId: "session-1",
        recordedAt: "2026-09-01T10:05:00.000Z",
        score: 8,
        competencyId: "competency-1",
        competencyName: "React architecture",
        category: "technical",
        relevance: 0.9,
        mode: "real",
        degraded: false,
        assistanceCount: 1,
      },
    ]);
  });

  it("skips questions the candidate never attempted", async () => {
    // non_answer: true rows are never scored, so they must never become evidence.
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: COMPLETED_STATUS, mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1", user_id: "user-1", session_id: "session-1", category: "behavioral",
          competency_id: "competency-1", assistance: [], non_answer: false,
        },
        {
          id: "question-2", user_id: "user-1", session_id: "session-1", category: "technical",
          competency_id: "competency-1", assistance: [], non_answer: true,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1", user_id: "user-1", question_id: "question-1", overall_score: 6,
          created_at: "2026-09-01T10:05:00.000Z",
        },
        {
          // Should never occur in practice -- non-answers are never graded -- but proves
          // the skip is driven by the question's non_answer flag, not by a missing evaluation.
          id: "eval-2", user_id: "user-1", question_id: "question-2", overall_score: 9,
          created_at: "2026-09-01T10:06:00.000Z",
        },
      ],
      competencies: [
        { id: "competency-1", user_id: "user-1", name: "Ownership", relevance: 1 },
      ],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toHaveLength(1);
    expect(evidence[0].questionEvaluationId).toBe("eval-1");
  });

  it("skips sessions that are not completed", async () => {
    // An in-flight session's partial grades must not move readiness.
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: "active", mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1", user_id: "user-1", session_id: "session-1", category: "technical",
          competency_id: "competency-1", assistance: [], non_answer: false,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1", user_id: "user-1", question_id: "question-1", overall_score: 7,
          created_at: "2026-09-01T10:05:00.000Z",
        },
      ],
      competencies: [
        { id: "competency-1", user_id: "user-1", name: "React architecture", relevance: 0.9 },
      ],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toEqual([]);
  });

  it("defaults relevance to 1 when an answer has no competency", async () => {
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: COMPLETED_STATUS, mode: "coach", degraded: true },
      ],
      interview_questions: [
        {
          id: "question-1", user_id: "user-1", session_id: "session-1", category: "communication",
          competency_id: null, assistance: [], non_answer: false,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1", user_id: "user-1", question_id: "question-1", overall_score: 5,
          created_at: "2026-09-01T10:05:00.000Z",
        },
      ],
      competencies: [],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toEqual([
      {
        questionEvaluationId: "eval-1",
        sessionId: "session-1",
        recordedAt: "2026-09-01T10:05:00.000Z",
        score: 5,
        competencyId: null,
        competencyName: null,
        category: "communication",
        relevance: 1,
        mode: "coach",
        degraded: true,
        assistanceCount: 0,
      },
    ]);
  });

  it("counts a session persisted the way the app actually persists one", async () => {
    // Regression guard: the read once filtered on "completed", a status nothing
    // in the app ever writes, so every real user's readiness was empty forever.
    // `completeSession` and all three completion RPCs write "complete".
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: "complete", mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1", user_id: "user-1", session_id: "session-1", category: "technical",
          competency_id: null, assistance: [], non_answer: false,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1", user_id: "user-1", question_id: "question-1", overall_score: 7,
          created_at: "2026-09-01T10:05:00.000Z",
        },
      ],
      competencies: [],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toHaveLength(1);
    expect(evidence[0].sessionId).toBe("session-1");
  });

  it("keeps answers whose non_answer flag was never written", async () => {
    // The filter is `is not true`, not `neq true`: a null flag means "nobody
    // recorded a non-answer", so the row is a graded answer like any other.
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: COMPLETED_STATUS, mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1", user_id: "user-1", session_id: "session-1", category: "technical",
          competency_id: null, assistance: [], non_answer: null,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1", user_id: "user-1", question_id: "question-1", overall_score: 6,
          created_at: "2026-09-01T10:05:00.000Z",
        },
      ],
      competencies: [],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence.map((row) => row.questionEvaluationId)).toEqual(["eval-1"]);
  });

  it("drops rows that do not join back to a completed session", async () => {
    // The questions and evaluations reads are no longer narrowed by an id list
    // (unbounded `.in(...)` lists overflow the request URL), so the join loop is
    // the only thing enforcing the narrowing. This proves it still does: an
    // in-flight session's question, and an evaluation whose question was
    // filtered out as a non-answer, must both produce nothing.
    const supabase = mockSupabase({
      interview_sessions: [
        { id: "session-1", user_id: "user-1", status: COMPLETED_STATUS, mode: "real", degraded: false },
        { id: "session-2", user_id: "user-1", status: "active", mode: "real", degraded: false },
      ],
      interview_questions: [
        {
          id: "question-1", user_id: "user-1", session_id: "session-1", category: "technical",
          competency_id: null, assistance: [], non_answer: false,
        },
        {
          id: "question-2", user_id: "user-1", session_id: "session-2", category: "technical",
          competency_id: null, assistance: [], non_answer: false,
        },
        {
          id: "question-3", user_id: "user-1", session_id: "session-1", category: "technical",
          competency_id: null, assistance: [], non_answer: true,
        },
      ],
      question_evaluations: [
        {
          id: "eval-1", user_id: "user-1", question_id: "question-1", overall_score: 7,
          created_at: "2026-09-01T10:05:00.000Z",
        },
        {
          id: "eval-2", user_id: "user-1", question_id: "question-2", overall_score: 9,
          created_at: "2026-09-01T10:06:00.000Z",
        },
        {
          id: "eval-3", user_id: "user-1", question_id: "question-3", overall_score: 9,
          created_at: "2026-09-01T10:07:00.000Z",
        },
        {
          id: "eval-4", user_id: "user-1", question_id: "question-missing", overall_score: 9,
          created_at: "2026-09-01T10:08:00.000Z",
        },
      ],
      competencies: [],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence.map((row) => row.questionEvaluationId)).toEqual(["eval-1"]);
  });

  it("returns an empty list when the user has no completed sessions", async () => {
    const supabase = mockSupabase({
      interview_sessions: [],
      interview_questions: [],
      question_evaluations: [],
      competencies: [],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toEqual([]);
  });

  it("pages through more rows than a single request can hold, with no duplicates and none dropped", async () => {
    // `selectAllPages` requests 1000 rows per page; seed one row over that so a
    // single table's read genuinely spans two `.range()` calls. Rows are
    // generated rather than hand-written, and this exercises the real
    // production PAGE_SIZE rather than an injected smaller one, so the test
    // proves the actual paging boundary rather than a stand-in for it.
    const rowCount = 1001;
    const sessions: Row[] = [
      { id: "session-1", user_id: "user-1", status: COMPLETED_STATUS, mode: "real", degraded: false },
    ];
    const questions: Row[] = Array.from({ length: rowCount }, (_, index) => ({
      id: `question-${index}`, user_id: "user-1", session_id: "session-1", category: "technical",
      competency_id: null, assistance: [], non_answer: false,
    }));
    const evaluations: Row[] = Array.from({ length: rowCount }, (_, index) => ({
      id: `eval-${index}`, user_id: "user-1", question_id: `question-${index}`, overall_score: 5,
      created_at: "2026-09-01T10:00:00.000Z",
    }));
    const supabase = mockSupabase({
      interview_sessions: sessions,
      interview_questions: questions,
      question_evaluations: evaluations,
      competencies: [],
    });

    const evidence = await listReadinessEvidence(supabase as never, "user-1");

    expect(evidence).toHaveLength(rowCount);
    expect(new Set(evidence.map((row) => row.questionEvaluationId)).size).toBe(rowCount);
  });
});

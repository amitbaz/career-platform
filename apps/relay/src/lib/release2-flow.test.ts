import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CoverageTarget,
  Evaluation,
  EvidenceItem,
  InterviewSession,
  Message,
  Opportunity,
  PlannedQuestion,
  PracticePlan,
  Profile,
} from "@/lib/types";
import type { ConversationTurnPersistence } from "@/lib/repositories/interviews";

/**
 * End-to-end Release 2 regression: proves the deterministic recommendation
 * engine (`practice-recommendation.ts`), the practice orchestrator
 * (`practice-service.ts`), and the conversation engine (`coach.ts`) actually
 * hand off to one another correctly, something no existing test checks --
 * every other test in this branch mocks at least one of those three modules
 * away from its neighbors (`practice-service.test.ts` mocks `@/lib/coach`
 * and `@/lib/practice-recommendation`; `api/practice/route.test.ts` mocks
 * `@/lib/practice-service` whole; `api/interview/route.test.ts` mocks
 * `@/lib/coach` whole). This file mocks ONLY the repository layer (the
 * actual Postgres/RPC calls) -- `recommendPractice`, `startRecommendedPractice`,
 * `generatePracticeBlueprint`, `nextTurn`, `completeSession` (the pure
 * summarizer), and `canExplicitlyCompleteConversation` all run for real.
 *
 * `GEMINI_API_KEY` is stubbed empty so `generatePracticeBlueprint`/`nextTurn`
 * take their deterministic fallback paths (see `modelJson` in
 * `src/lib/coach.ts`, which returns `null` with zero network calls when no
 * key is configured) -- the same technique `coach.test.ts` uses. A `fetch`
 * spy proves no network call is ever made.
 *
 * The mocked `createSessionWithPracticeBlueprint` and `recordConversationTurn`
 * are hand-rolled stand-ins for two Postgres RPCs
 * (`create_planned_conversation_session_with_blueprint` and
 * `record_conversation_turn`, both in
 * `supabase/migrations/202608290010_follow_up_rubric_contract.sql` and
 * `supabase/migrations/202608310001_planned_practice_sessions.sql`) --
 * that DB round trip cannot run in Vitest, and Task 11's ruling bars this
 * branch from touching a live Supabase project (see `applyConversationTurn`
 * below). The mock for `createSessionWithPracticeBlueprint`
 * still calls the REAL `assertPracticeConversationBlueprint` before
 * constructing a session, so a `generatePracticeBlueprint` bug that produced
 * an illegal blueprint (wrong count, gapped sequence) is caught exactly as
 * the live RPC would reject it.
 */

const mocks = vi.hoisted(() => ({
  getProfile: vi.fn(),
  listOpportunities: vi.fn(),
  listCoachObservations: vi.fn(),
  listCareerStories: vi.fn(),
  listRecentSessions: vi.fn(),
  listPracticePlans: vi.fn(),
  createPracticePlan: vi.fn(),
  getPracticePlan: vi.fn(),
  setPracticePlanOpportunities: vi.fn(),
  updatePracticePlan: vi.fn(),
  createSessionWithPracticeBlueprint: vi.fn(),
  createHandsOnPracticeSession: vi.fn(),
  recordConversationTurn: vi.fn(),
  completeSession: vi.fn(),
  listReadinessEvidence: vi.fn(),
}));

vi.mock("@/lib/repositories/profile", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/profile")>("@/lib/repositories/profile");
  return { ...actual, getProfile: mocks.getProfile };
});
vi.mock("@/lib/repositories/opportunities", () => ({ listOpportunities: mocks.listOpportunities }));
vi.mock("@/lib/repositories/observations", () => ({ listCoachObservations: mocks.listCoachObservations }));
vi.mock("@/lib/repositories/stories", () => ({ listCareerStories: mocks.listCareerStories }));
// Spreading `actual` keeps `assertConversationPlan`, `assertPracticeConversationBlueprint`,
// and `mapSession` REAL -- only the functions that would otherwise hit Postgres are replaced.
vi.mock("@/lib/repositories/interviews", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/interviews")>("@/lib/repositories/interviews");
  return {
    ...actual,
    listRecentSessions: mocks.listRecentSessions,
    createSessionWithPracticeBlueprint: mocks.createSessionWithPracticeBlueprint,
    createHandsOnPracticeSession: mocks.createHandsOnPracticeSession,
    recordConversationTurn: mocks.recordConversationTurn,
    completeSession: mocks.completeSession,
    listReadinessEvidence: mocks.listReadinessEvidence,
  };
});
vi.mock("@/lib/repositories/practice-plans", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/practice-plans")>("@/lib/repositories/practice-plans");
  return {
    ...actual,
    listPracticePlans: mocks.listPracticePlans,
    createPracticePlan: mocks.createPracticePlan,
    getPracticePlan: mocks.getPracticePlan,
    setPracticePlanOpportunities: mocks.setPracticePlanOpportunities,
    updatePracticePlan: mocks.updatePracticePlan,
  };
});

// `@/lib/coach`, `@/lib/practice-recommendation`, `@/lib/readiness`, and
// `@/lib/practice-service` are deliberately NOT mocked anywhere in this file.
import { completeSession as summarizeSession, EVALUATION_DIMENSIONS, nextTurn } from "@/lib/coach";
import { canExplicitlyCompleteConversation } from "@/lib/conversation-completion";
import {
  completeLinkedPracticePlanBestEffort,
  startRecommendedPractice,
} from "@/lib/practice-service";
import { isPreWrittenQuestion, resolveNextQuestionWrite } from "@/lib/interview-turn-write";
import {
  assertConversationPlan,
  assertPracticeConversationBlueprint,
  completeSession,
  mapSession,
  recordConversationTurn,
} from "@/lib/repositories/interviews";

const supabase = { client: true };
const NOW = new Date("2026-08-31T09:00:00.000Z");
const NOW_ISO = NOW.toISOString();

const evidence: EvidenceItem[] = [
  {
    id: "evidence-1",
    sourceKind: "cv",
    sourceExcerpt: "Led the Acme checkout platform migration end to end.",
    projectOrEmployer: "Acme Checkout Platform",
    ownership: "Owned the migration end to end.",
    technologies: ["React", "TypeScript"],
    decision: "Split the migration into two phases.",
    constraint: "Tight launch window.",
    outcome: "Cut checkout errors by 30%.",
    recency: "2025-06",
    confidence: 0.92,
  },
  {
    id: "evidence-2",
    sourceKind: "cv",
    sourceExcerpt: "Built reliability tooling for the platform team.",
    projectOrEmployer: "Reliability Tooling",
    ownership: "Designed the alerting flow.",
    technologies: ["Next.js", "Postgres"],
    decision: "Added release health dashboards.",
    constraint: "Small team bandwidth.",
    outcome: "Reduced triage time by 35%.",
    recency: "2025-01",
    confidence: 0.88,
  },
];

const profile: Profile = {
  userId: "user-1",
  role: "Staff Frontend Engineer",
  seniority: "Staff",
  summary: "Frontend engineer focused on platform reliability and delivery.",
  narrative: "Owns frontend platforms end to end.",
  expertise: ["React", "TypeScript"],
  characteristics: ["Pragmatic"],
  competencies: [],
  evidence,
  readiness: { ready: true, missing: [] },
  source: { cvText: "At Acme I led a checkout platform migration.", coverLetter: "" },
  createdAt: "2026-08-20T10:00:00.000Z",
  updatedAt: "2026-08-20T10:00:00.000Z",
};

/**
 * A single active application with no scheduled interview and no confirmed
 * story -- this deliberately lands on `recommendPractice`'s "story gap"
 * branch (design section 5 precedence #4): no near-term interview, no
 * interviewing-status opportunity, no reviewed observation, an active
 * application, and zero confirmed stories. That branch is the one that
 * both (a) sets `format: "story_work"` -- whose `baseQuestionCountFor` is
 * 3, giving the "3-question planned session" the brief asks for -- and
 * (b) links a primary opportunity, giving the "plan/opportunity context"
 * the brief asks for. See `buildStoryGapRecommendation` in
 * `src/lib/practice-recommendation.ts`.
 */
const opportunity: Opportunity = {
  id: "opp-1",
  userId: "user-1",
  company: "NorthStar Robotics",
  role: "Staff Frontend Engineer",
  status: "applied",
  location: "Remote",
  remote: true,
  jobUrl: null,
  jobDescription: "Own the internal design system.",
  sourceLabel: null,
  sourceSystem: "manual",
  sourceExternalId: null,
  matchScore: null,
  strengths: [],
  gaps: [],
  notes: null,
  appliedAt: "2026-08-20T10:00:00.000Z",
  nextInterviewAt: null,
  createdAt: "2026-08-20T10:00:00.000Z",
  updatedAt: "2026-08-20T10:00:00.000Z",
};

function interviewerTranscript(questions: PlannedQuestion[]): Message[] {
  return [...questions]
    .sort((left, right) => left.sequence - right.sequence)
    .flatMap((question) => {
      const interviewer: Message = {
        id: `${question.id}:question`,
        role: "interviewer",
        content: question.prompt!,
        createdAt: question.createdAt,
      };
      if (!question.answer) return [interviewer];
      return [interviewer, {
        id: `${question.id}:answer`,
        role: "candidate" as const,
        content: question.answer,
        createdAt: question.createdAt,
      }];
    });
}

/**
 * A hand-rolled stand-in for the `record_conversation_turn` Postgres RPC
 * (`supabase/migrations/202609010001_adaptive_interviewer.sql`): marks the
 * answered question, then EITHER inserts a follow-up row (`p_follow_up`) OR
 * updates the row named by `p_next_question_id` in place. Used as the mocked
 * `recordConversationTurn` implementation below so this test exercises the
 * exact persisted-state transition the real RPC performs, without a live
 * database.
 *
 * The follow-up branch reproduces the RPC's guard, including the limit read
 * off the PARENT row's persisted `follow_up_limit` -- 0 for every practice
 * introduction. That is what makes this file a real detector for the class of
 * bug where a planned-practice turn is routed through the adaptive
 * follow-up path: the RPC raises, and so does this.
 */
function applyConversationTurn(
  session: InterviewSession,
  questionId: string,
  answer: string,
  evaluation: Evaluation,
  next: ConversationTurnPersistence,
): InterviewSession {
  let questions = session.questions.map((question) => (
    question.id === questionId
      ? {
        ...question,
        answer: next.nonAnswer ? question.answer : answer,
        assistance: next.assistance,
        nonAnswer: next.nonAnswer,
      }
      : question
  ));
  const answered = questions.find((question) => question.id === questionId);
  if (!answered) throw new Error(`Unknown question id: ${questionId}`);

  if (next.followUp) {
    const followUps = questions.filter((question) => question.isFollowUp).length;
    const parentFollowUps = questions.filter((question) => question.parentQuestionId === answered.id).length;
    if (
      questions.length >= session.blueprint!.maxQuestions
      || followUps >= session.blueprint!.maxFollowUps
      || parentFollowUps >= (answered.followUpLimit ?? 0)
      || answered.isFollowUp
    ) {
      throw new Error("Conversation follow-up limit reached");
    }
    questions = [
      ...questions.map((question) => (
        question.sequence > answered.sequence ? { ...question, sequence: question.sequence + 1 } : question
      )),
      {
        ...answered,
        id: `${answered.id}-follow-up`,
        sequence: answered.sequence + 1,
        isFollowUp: true,
        parentQuestionId: answered.id,
        prompt: next.followUp.prompt,
        answer: null,
        assistance: [],
        nonAnswer: false,
        askedIntent: next.askedIntent,
      },
    ];
  } else if (next.nextQuestionId) {
    const nextQuestionId = next.nextQuestionId;
    questions = questions.map((question) => (
      question.id === nextQuestionId && question.answer === null
        ? { ...question, prompt: next.nextPrompt ?? question.prompt, askedIntent: next.askedIntent }
        : question
    ));
  }

  return {
    ...session,
    questions,
    evaluations: next.nonAnswer ? session.evaluations : [...session.evaluations, evaluation],
    messages: interviewerTranscript(questions),
  };
}

/**
 * Placeholder evaluation values for a non-answer turn. Mirrors
 * `emptyEvaluationFor` in `src/app/api/interview/route.ts`: the RPC skips
 * evidence recording when `p_non_answer` is true, so these are never
 * persisted; they exist only to satisfy the RPC's non-null parameters (spec
 * §11.3). Not imported from `route.ts` because that copy is a private,
 * unexported helper local to the route module.
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

/** Mirrors `api/interview/route.ts`'s "respond" action: find the next unanswered question, evaluate it, persist the turn. */
async function answerNextQuestion(session: InterviewSession, answer: string): Promise<InterviewSession> {
  const question = session.questions.find((item) => !item.answer);
  if (!question) throw new Error("No unanswered question remains.");
  const turn = await nextTurn({
    profile: { role: profile.role, seniority: profile.seniority, expertise: profile.expertise, narrative: profile.narrative },
    session,
    answeredQuestion: question,
    answer,
    blueprint: session.blueprint!,
    evidence: profile.evidence ?? [],
    opportunity: null,
  });
  // `write` reuses the route's real `resolveNextQuestionWrite`, not a local
  // copy -- a copy is exactly what let this helper drift out of step with
  // `route.ts`. `setAsideReason` below is hardcoded to null rather than
  // derived, and `question` above is picked with a plain `find`, not
  // `currentQuestion`/`isAwaitingAnswer`. Both are safe here only because
  // this harness drives pre-written scripted sessions, whose rows are never
  // set aside (see the pre-written/coverage-plan distinction noted below,
  // near `isPreWrittenQuestion`'s other use in this file).
  const write = resolveNextQuestionWrite(session, question, turn);
  return recordConversationTurn(supabase as never, "user-1", question.id, answer, turn.evaluation ?? emptyEvaluationFor(question), {
    nextQuestionId: write.nextQuestionId,
    nextPrompt: turn.prompt,
    followUp: write.followUp,
    askedIntent: turn.intent,
    assistance: [...question.assistance, ...(turn.assistance ? [turn.assistance] : [])],
    nonAnswer: turn.nonAnswer && !isPreWrittenQuestion(question),
    degraded: turn.degraded,
    setAsideReason: null,
  });
}

describe("Release 2 flow: recommendation through practice-plan completion", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // No key -> `modelJson` (src/lib/coach.ts) returns null immediately for
    // every AI call, so `generatePracticeBlueprint`/`nextTurn` take their
    // deterministic fallback paths with zero network calls. Same technique
    // `coach.test.ts` uses throughout.
    vi.stubEnv("GEMINI_API_KEY", "");
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("drives a story-gap recommendation through a 3-question planned session, completion, and plan completion", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    mocks.getProfile.mockResolvedValue(profile);
    mocks.listOpportunities.mockResolvedValue([opportunity]);
    mocks.listCoachObservations.mockResolvedValue([]);
    mocks.listCareerStories.mockResolvedValue([]);
    mocks.listRecentSessions.mockResolvedValue([]);
    mocks.listPracticePlans.mockResolvedValue([]);
    // No readiness evidence yet -- every dimension is unresolved, so this
    // flow's story-gap branch (precedence #4) still wins over the readiness
    // weakness branch (#5), exactly as it did under the old progress model.
    mocks.listReadinessEvidence.mockResolvedValue([]);

    let currentPlan: PracticePlan;
    mocks.createPracticePlan.mockImplementation(async (_client, userId, input) => {
      currentPlan = {
        id: "plan-1",
        userId,
        status: input.status ?? "ready",
        primaryFocus: input.primaryFocus,
        secondaryFocus: input.secondaryFocus ?? null,
        rationale: input.rationale ?? "",
        format: input.format,
        estimatedMinutes: input.estimatedMinutes ?? null,
        successCriteria: input.successCriteria ?? [],
        priorityScore: null,
        priorityFactors: {},
        generationError: null,
        completedAt: null,
        createdAt: NOW_ISO,
        updatedAt: NOW_ISO,
        opportunities: [],
      };
      return currentPlan;
    });
    mocks.setPracticePlanOpportunities.mockImplementation(async (_client, userId, planId, links) => {
      currentPlan = {
        ...currentPlan,
        opportunities: links.map((link: { opportunityId: string; relevance?: string }) => ({
          userId,
          practicePlanId: planId,
          opportunityId: link.opportunityId,
          relevance: link.relevance ?? "supporting",
          createdAt: NOW_ISO,
        })),
      };
      return currentPlan;
    });
    mocks.getPracticePlan.mockImplementation(async () => ({ ...currentPlan, status: "started" }));
    mocks.updatePracticePlan.mockImplementation(async (_client, _userId, _planId, patch) => {
      currentPlan = { ...currentPlan, ...patch };
      return currentPlan;
    });

    let liveSession: InterviewSession;
    mocks.createSessionWithPracticeBlueprint.mockImplementation(async (_client, userId, blueprint, context) => {
      // Real, unmocked assertion -- the same guard the transactional start
      // RPC (`create_planned_conversation_session_with_blueprint`) enforces
      // server-side. A `generatePracticeBlueprint` bug that produced a
      // non-contiguous or over-count blueprint would throw here, exactly as
      // the live RPC would reject it in production.
      assertPracticeConversationBlueprint(blueprint);
      const questions: PlannedQuestion[] = blueprint.questions.map((question: PlannedQuestion) => ({ ...question, answer: null }));
      // `mapSession` reconstructs a coverage target from EVERY non-follow-up
      // row of ANY conversation session, so a planned practice session is
      // reloaded with a full set of targets its plan never asked for. The
      // fixture must carry them, or this file tests a session shape that does
      // not exist in production -- the gap that let a planned-practice turn
      // reach the adaptive follow-up path unnoticed.
      const targets: CoverageTarget[] = questions
        .filter((question) => !question.isFollowUp)
        .map((question) => ({
          id: question.id,
          competencyId: question.competencyId,
          competencyName: question.competencyName,
          category: question.category,
          evidenceIds: question.evidenceIds ?? [],
          difficulty: question.difficulty,
          objective: question.objective ?? "",
          expectedSignals: question.expectedSignals ?? [],
          rubricCriteria: question.rubricCriteria ?? [],
          required: true,
        }));
      liveSession = {
        id: "session-1",
        userId,
        kind: "conversation",
        // A planned practice session, not a round-based real interview.
        roundId: "tech-lead",
        mode: "coach",
        degraded: false,
        status: "active",
        startedAt: NOW_ISO,
        completedAt: null,
        exercise: {},
        resultSummary: {},
        overallScore: null,
        questions,
        blueprint: { ...blueprint, targets },
        checkpoints: [],
        evaluations: [],
        messages: interviewerTranscript(questions),
        createdAt: NOW_ISO,
        updatedAt: NOW_ISO,
        practicePlanId: context.practicePlanId,
        opportunityId: context.opportunityId,
      };
      return liveSession;
    });
    mocks.recordConversationTurn.mockImplementation(async (_client, _userId, questionId, answer, evaluation, next) => {
      liveSession = applyConversationTurn(liveSession, questionId, answer, evaluation, next);
      return liveSession;
    });
    mocks.completeSession.mockImplementation(async (_client, _userId, _sessionId, result) => {
      liveSession = {
        ...liveSession,
        status: "complete",
        completedAt: NOW_ISO,
        overallScore: result.overallScore,
        resultSummary: { summary: result.summary },
      };
      return liveSession;
    });

    // --- recommendation -> ready PracticePlan ---
    const { plan, session: started } = await startRecommendedPractice(supabase as never, "user-1", NOW);

    expect(plan.format).toBe("story_work");
    expect(plan.primaryFocus).toContain("NorthStar Robotics");
    expect(plan.status).toBe("started");
    expect(plan.opportunities).toEqual([
      expect.objectContaining({ opportunityId: "opp-1", relevance: "primary" }),
    ]);
    expect(started.practicePlanId).toBe("plan-1");
    expect(started.opportunityId).toBe("opp-1");
    // story_work's base question count (see baseQuestionCountFor in coach.ts) is 3.
    expect(started.blueprint?.questions).toHaveLength(3);
    expect(started.blueprint?.status).toBe("limited-grounding"); // no GEMINI_API_KEY -> deterministic fallback, not an AI response
    expect(fetchSpy).not.toHaveBeenCalled(); // fully deterministic: no network call was ever made

    // --- three base questions ---
    let session = started;
    session = await answerNextQuestion(
      session,
      "In my role as a staff frontend engineer, I recently owned the Acme checkout platform migration end to end.",
    );
    expect(session.questions).toHaveLength(3); // introduction's follow-up limit is 0 -- never eligible for a follow-up

    session = await answerNextQuestion(
      session,
      "In my role owning the Acme checkout platform I made the decision to split the rollout into two phases and we measured a 30% reduction in checkout errors after launch.",
    );
    expect(session.questions).toHaveLength(3); // this answer matches every expected signal, so no follow-up is warranted

    // The final base question, answered deliberately weakly -- under the old
    // dynamic follow-up pipeline this would have earned a persisted
    // follow-up. Task 7 removed that mechanism: the director only ever moves
    // between pre-existing coverage targets. Those targets are reconstructed
    // from these very rows, so `decideIntent` does return an intent naming one
    // of them -- but a pre-written row is not driven by the coverage plan, so
    // `resolveNextQuestionWrite` writes no next question and the session
    // simply has no more questions to ask. Routing it through the follow-up
    // branch instead would raise "Conversation follow-up limit reached" out of
    // `applyConversationTurn`, exactly as the live RPC does.
    session = await answerNextQuestion(session, "I used React.");
    expect(session.questions).toHaveLength(3); // no dynamic follow-up is ever created anymore
    expect(session.questions.some((question) => question.isFollowUp)).toBe(false);
    expect(session.questions.every((question) => Boolean(question.answer))).toBe(true);
    expect(canExplicitlyCompleteConversation(session)).toBe(true);

    // --- session completion ---
    const summary = summarizeSession(session);
    const completed = await completeSession(supabase as never, "user-1", session.id, summary);
    expect(completed.status).toBe("complete");
    expect(completed.overallScore).toBeGreaterThan(0);

    // --- PracticePlan completion ---
    const { warning } = await completeLinkedPracticePlanBestEffort(supabase as never, "user-1", completed);
    expect(warning).toBeNull();
    expect(currentPlan!.status).toBe("completed");
    expect(currentPlan!.completedAt).toBe(completed.completedAt);

    // "plan bookkeeping failure returns a warning without invalidating
    // completed interview evidence" -- a second, independent bookkeeping
    // attempt fails, but the interview evidence already returned above is
    // untouched: every answer is still there.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.updatePracticePlan.mockRejectedValueOnce(new Error("db unavailable"));
    const secondAttempt = await completeLinkedPracticePlanBestEffort(supabase as never, "user-1", completed);
    expect(secondAttempt.warning).toEqual(expect.any(String));
    expect(completed.questions.every((question) => Boolean(question.answer))).toBe(true);
    expect(completed.questions).toHaveLength(3);
    expect(completed.evaluations).toHaveLength(3);
    consoleError.mockRestore();
  });

  it("rejects a non-five-question backbone for the generic conversation", () => {
    const backbone = (["introduction", "experience", "technical", "architecture", "behavioral"] as const)
      .map((category, index) => ({
        id: `question-${index + 1}`,
        sequence: index + 1,
        category,
        competencyId: null,
        competencyName: null,
        difficulty: "senior" as const,
        isFollowUp: false,
        prompt: `Backbone prompt ${index + 1}`,
        answer: null,
        createdAt: NOW_ISO,
        askedIntent: null,
        assistance: [],
        nonAnswer: false,
        setAsideAt: null,
        setAsideReason: null,
        nonAnswers: [],
      }));

    expect(() => assertConversationPlan(backbone)).not.toThrow();
    expect(() => assertConversationPlan(backbone.slice(0, 4))).toThrow(/exact five-question backbone/);
  });

  it("hydrates a legacy session with no persisted practice-plan or opportunity context", () => {
    // A row shaped like a session created before Release 2 added
    // practice_plan_id/opportunity_id and before grounded blueprints were
    // persisted at all -- no blueprint_status/blueprint_fallback_reason
    // columns, and question rows with no objective/expectedSignals.
    const legacyRow = {
      id: "legacy-session-1",
      user_id: "user-1",
      kind: "conversation",
      status: "complete",
      started_at: "2026-01-05T09:00:00.000Z",
      completed_at: "2026-01-05T09:40:00.000Z",
      exercise: {},
      result_summary: { summary: "Solid session." },
      overall_score: 7.2,
      created_at: "2026-01-05T09:00:00.000Z",
      updated_at: "2026-01-05T09:40:00.000Z",
      practice_plan_id: null,
      opportunity_id: null,
    };
    const legacyQuestionRows = (["introduction", "experience", "technical", "architecture", "behavioral"] as const)
      .map((category, index) => ({
        id: `legacy-question-${index + 1}`,
        sequence: index + 1,
        category,
        difficulty: "senior",
        is_follow_up: false,
        prompt: `Legacy prompt ${index + 1}`,
        answer: `Legacy answer ${index + 1}`,
        created_at: "2026-01-05T09:00:00.000Z",
        answered_at: "2026-01-05T09:05:00.000Z",
      }));

    const session = mapSession(legacyRow, legacyQuestionRows, [], [], new Map());

    expect(session.practicePlanId).toBeNull();
    expect(session.opportunityId).toBeNull();
    expect(session.questions).toHaveLength(5);
    expect(session.questions.every((question) => Boolean(question.answer))).toBe(true);
    expect(session.blueprint?.status).toBe("limited-grounding");
    expect(session.blueprint?.fallbackReason).toContain("Legacy session");
  });
});

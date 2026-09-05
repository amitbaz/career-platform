import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Competency, InterviewSession, PlannedQuestion, Profile } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  getProfile: vi.fn(),
  getSession: vi.fn(),
  listRecentSessions: vi.fn(),
  listReadinessEvidence: vi.fn(),
  createHandsOnSession: vi.fn(),
  createSessionWithPlan: vi.fn(),
  createSessionWithBlueprint: vi.fn(),
  revealFirstQuestion: vi.fn(),
  questionIdForTarget: vi.fn(),
  recordConversationTurn: vi.fn(),
  saveHandsOnCheckpoint: vi.fn(),
  completeSession: vi.fn(),
  completeHandsOnSession: vi.fn(),
  buildInterviewPlan: vi.fn(),
  generateInterviewBlueprint: vi.fn(),
  nextTurn: vi.fn(),
  openingTurn: vi.fn(),
  summarizeSession: vi.fn(),
  evaluateHandsOn: vi.fn(),
  handsOnCheckpoint: vi.fn(),
  handsOnExercise: vi.fn(),
  generatePracticeBlueprint: vi.fn(),
  assessProfileReadiness: vi.fn(),
  updatePracticePlan: vi.fn(),
  getOpportunity: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/repositories/profile", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/profile")>("@/lib/repositories/profile");
  return { ...actual, getProfile: mocks.getProfile };
});
vi.mock("@/lib/repositories/interviews", () => ({
  getSession: mocks.getSession,
  listRecentSessions: mocks.listRecentSessions,
  listReadinessEvidence: mocks.listReadinessEvidence,
  createHandsOnSession: mocks.createHandsOnSession,
  createSessionWithPlan: mocks.createSessionWithPlan,
  createSessionWithBlueprint: mocks.createSessionWithBlueprint,
  createSessionWithPracticeBlueprint: vi.fn(),
  createHandsOnPracticeSession: vi.fn(),
  revealFirstQuestion: mocks.revealFirstQuestion,
  questionIdForTarget: mocks.questionIdForTarget,
  recordConversationTurn: mocks.recordConversationTurn,
  saveHandsOnCheckpoint: mocks.saveHandsOnCheckpoint,
  completeSession: mocks.completeSession,
  completeHandsOnSession: mocks.completeHandsOnSession,
}));
// The practice-plan bookkeeping the route triggers after a completion runs for
// real (via `completeLinkedPracticePlanBestEffort`); only its persistence is
// stubbed, so these tests exercise the actual best-effort path.
vi.mock("@/lib/repositories/practice-plans", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/practice-plans")>("@/lib/repositories/practice-plans");
  return { ...actual, updatePracticePlan: mocks.updatePracticePlan };
});
vi.mock("@/lib/interview-planner", () => ({ buildInterviewPlan: mocks.buildInterviewPlan }));
vi.mock("@/lib/repositories/opportunities", () => ({ getOpportunity: mocks.getOpportunity }));
vi.mock("@/lib/coach", () => ({
  nextTurn: mocks.nextTurn,
  openingTurn: mocks.openingTurn,
  completeSession: mocks.summarizeSession,
  evaluateHandsOn: mocks.evaluateHandsOn,
  handsOnCheckpoint: mocks.handsOnCheckpoint,
  handsOnExercise: mocks.handsOnExercise,
  generateInterviewBlueprint: mocks.generateInterviewBlueprint,
  generatePracticeBlueprint: mocks.generatePracticeBlueprint,
  assessProfileReadiness: mocks.assessProfileReadiness,
  // The `dimensions` tuple `coach.ts` exports as `EVALUATION_DIMENSIONS`,
  // duplicated here only because this module is mocked wholesale -- keep in
  // sync with `coach.ts`'s internal `dimensions` constant.
  EVALUATION_DIMENSIONS: [
    "correctness",
    "depth",
    "clarity",
    "structure",
    "practicalExperience",
    "tradeOffAwareness",
    "communication",
    "confidence",
    "relevance",
  ] as const,
}));

import { GET, POST } from "@/app/api/interview/route";

const competency: Competency = {
  id: "react-id",
  name: "React architecture",
  relevance: 5,
  expectedLevel: "senior",
  estimatedLevel: "senior",
  confidence: "high",
  lastPracticedAt: "2026-08-29T10:00:00.000Z",
  questionCount: 4,
  averageScore: 8.4,
  recentScore: 9,
  strengths: ["Frames trade-offs clearly."],
  weaknesses: ["Adds too much implementation detail before the decision."],
};

const profile: Profile = {
  userId: "user-1",
  role: "Frontend Engineer",
  seniority: "Senior",
  summary: "Frontend engineer",
  narrative: "Owns frontend platforms.",
  expertise: ["React"],
  characteristics: ["Pragmatic"],
  competencies: [competency],
  source: { cvText: "At Acme I led a React migration.", coverLetter: "" },
  readiness: { ready: true, missing: [] },
  createdAt: "2026-08-29T10:00:00.000Z",
  updatedAt: "2026-08-29T10:00:00.000Z",
};

function question(sequence: number, answer: string | null): PlannedQuestion {
  const categories: PlannedQuestion["category"][] = [
    "introduction", "experience", "technical", "architecture", "behavioral",
  ];
  return {
    id: `question-${sequence}`,
    sequence,
    category: categories[Math.min(sequence - 1, categories.length - 1)],
    competencyId: sequence === 1 ? null : "react-id",
    competencyName: sequence === 1 ? null : "React architecture",
    difficulty: "senior",
    isFollowUp: sequence > 5,
    prompt: `Question ${sequence}`,
    answer,
    createdAt: "2026-08-29T10:00:00.000Z",
    // This fixture predates the director's intent/assistance pipeline.
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
  };
}

function session(
  questions: PlannedQuestion[],
  status: InterviewSession["status"] = "active",
  practicePlanId: string | null = null,
): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    degraded: false,
    status,
    startedAt: "2026-08-29T10:00:00.000Z",
    completedAt: status === "complete" ? "2026-08-29T11:00:00.000Z" : null,
    exercise: {},
    resultSummary: status === "complete" ? { summary: "Complete" } : {},
    overallScore: status === "complete" ? 7 : null,
    questions,
    checkpoints: [],
    evaluations: [],
    messages: [],
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    practicePlanId,
    opportunityId: null,
  };
}

/** A planned practice conversation: three base questions plus any follow-ups already persisted. */
function plannedSession(questions: PlannedQuestion[], status: InterviewSession["status"] = "active"): InterviewSession {
  return session(questions, status, "plan-1");
}

function jsonRequest(body: Record<string, unknown>): Request {
  return new Request("http://localhost/api/interview", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

describe("POST /api/interview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.getProfile.mockResolvedValue(profile);
    mocks.updatePracticePlan.mockResolvedValue({ id: "plan-1", status: "completed" });
    mocks.getOpportunity.mockResolvedValue(null);
    mocks.questionIdForTarget.mockReturnValue(null);
  });

  it("returns 401 before reading a request body when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));
    const json = vi.fn();

    const response = await POST({ json } as unknown as Request);

    expect(response.status).toBe(401);
    expect(json).not.toHaveBeenCalled();
  });

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
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [],
    };
    const persisted = session([{ ...question(1, null), id: "database-question-1" }]);
    persisted.blueprint = {
      ...blueprint,
      questions: blueprint.questions.map((item) => ({ ...item, id: "database-question-1" })),
    };
    const revealed = { ...persisted, mode: "real" as const, roundId: "tech-lead" as const };

    mocks.getProfile.mockResolvedValue(sparseProfile);
    mocks.generateInterviewBlueprint.mockResolvedValue(blueprint);
    mocks.createSessionWithBlueprint.mockResolvedValue(persisted);
    mocks.openingTurn.mockResolvedValue({
      intent: { kind: "open", targetId: "database-question-1" },
      prompt: blueprint.questions[0].prompt,
      targetId: "database-question-1",
    });
    mocks.revealFirstQuestion.mockResolvedValue(revealed);

    const response = await POST(jsonRequest({ action: "start", mode: "conversation" }));

    expect(response.status).toBe(200);
    expect(mocks.generateInterviewBlueprint).toHaveBeenCalledWith(sparseProfile, [], { roundId: "tech-lead", opportunity: null });
    expect(mocks.createSessionWithBlueprint).toHaveBeenCalledWith(expect.anything(), "user-1", blueprint, { roundId: "tech-lead", mode: "real" });
    expect((await response.json()).session.blueprint.status).toBe("limited-grounding");
  });

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

  it("starts a session with the requested round and mode", async () => {
    // Deliberately empty and DISTINCT from the reloaded session's blueprint
    // below: this is the pre-persistence blueprint `generateInterviewBlueprint`
    // returns, whose `targets` (if it had any) would carry transient
    // gap-0/competency-0 ids. `openingTurn` must never see this one.
    const blueprint = {
      status: "grounded" as const,
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [],
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [],
    };
    // The RELOADED blueprint `createSessionWithBlueprint` returns on the
    // session, with a real, non-empty, database-row-id-shaped target --
    // distinct from `blueprint` above so a regression that passes the wrong
    // blueprint to `openingTurn` (Fix 2) is actually caught below.
    const reloadedBlueprint = {
      ...blueprint,
      targets: [
        {
          id: "question-1",
          competencyId: null,
          competencyName: null,
          category: "introduction" as const,
          evidenceIds: [],
          difficulty: "senior" as const,
          objective: "Establish recent engineering context.",
          expectedSignals: ["ownership"],
          rubricCriteria: ["Name one concrete example."],
          required: true,
        },
      ],
    };
    const createdSession = session([question(1, null)]);
    createdSession.blueprint = reloadedBlueprint;
    const revealed = {
      ...session([{ ...question(1, null), prompt: "Tell me about your background." }]),
      mode: "coach" as const,
      roundId: "tech-lead" as const,
    };
    mocks.generateInterviewBlueprint.mockResolvedValue(blueprint);
    mocks.createSessionWithBlueprint.mockResolvedValue(createdSession);
    mocks.openingTurn.mockResolvedValue({
      intent: { kind: "open", targetId: "question-1" },
      prompt: "Tell me about your background.",
      targetId: "question-1",
    });
    mocks.revealFirstQuestion.mockResolvedValue(revealed);

    const response = await POST(jsonRequest({ action: "start", roundId: "tech-lead", mode: "coach" }));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.session.mode).toBe("coach");
    expect(body.session.roundId).toBe("tech-lead");
    expect(body.session.questions[0].prompt).toBeTruthy();
    expect(mocks.generateInterviewBlueprint).toHaveBeenCalledWith(profile, [], { roundId: "tech-lead", opportunity: null });
    expect(mocks.createSessionWithBlueprint).toHaveBeenCalledWith(expect.anything(), "user-1", blueprint, { roundId: "tech-lead", mode: "coach" });
    // Fix 2's regression guard: `openingTurn` must receive the RELOADED
    // `session.blueprint` (stable, row-id-shaped targets), never the
    // pre-persistence `blueprint` variable above (empty/transient-id targets).
    expect(mocks.openingTurn).toHaveBeenCalledWith(expect.objectContaining({ blueprint: reloadedBlueprint }));
    expect(mocks.openingTurn).not.toHaveBeenCalledWith(expect.objectContaining({ blueprint }));
    expect(mocks.revealFirstQuestion).toHaveBeenCalledWith(expect.anything(), "user-1", createdSession, expect.anything());
  });

  it("rejects a round that is specified but not implemented", async () => {
    const response = await POST(jsonRequest({ action: "start", roundId: "founder", mode: "real" }));

    expect(response.status).toBe(400);
    expect(mocks.generateInterviewBlueprint).not.toHaveBeenCalled();
  });

  it("defaults to real mode when none is given", async () => {
    const revealed = {
      ...session([{ ...question(1, null), prompt: "Tell me about your background." }]),
      mode: "real" as const,
      roundId: "tech-lead" as const,
    };
    mocks.generateInterviewBlueprint.mockResolvedValue({
      status: "grounded" as const,
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [],
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [],
    });
    mocks.createSessionWithBlueprint.mockResolvedValue(session([question(1, null)]));
    mocks.openingTurn.mockResolvedValue({
      intent: { kind: "open", targetId: "question-1" },
      prompt: "Tell me about your background.",
      targetId: "question-1",
    });
    mocks.revealFirstQuestion.mockResolvedValue(revealed);

    const response = await POST(jsonRequest({ action: "start" }));
    const body = await response.json();

    expect(body.session.mode).toBe("real");
  });

  it("logs the underlying failure while keeping the public error generic", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.getSession.mockRejectedValue(new Error("database function is unavailable"));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "respond", sessionId: "session-1", answer: "An answer." }),
    }));

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "Could not complete your interview request." });
    expect(consoleError).toHaveBeenCalledWith("[api/interview] request failed", expect.objectContaining({
      message: "database function is unavailable",
    }));
    consoleError.mockRestore();
  });

  it("routes a same-target continuation through the follow-up branch, not nextQuestionId", async () => {
    // Question 5 carries an `askedIntent`, so it is an adaptive coverage-target
    // row rather than a pre-written one -- the discriminator
    // `resolveNextQuestionWrite` uses to tell the two session kinds apart.
    const current = session([1, 2, 3, 4, 5].map((sequence) => (
      sequence === 5
        ? { ...question(sequence, null), askedIntent: { kind: "open" as const, targetId: "question-5" } }
        : question(sequence, "answered")
    )));
    const followUp = question(6, null);
    mocks.getSession.mockResolvedValue(current);
    mocks.nextTurn.mockResolvedValue({
      evaluation: { score: 5, competencyId: "react-id", competency: "React architecture", dimensions: {}, strengths: [], needsWork: ["Clarify"] },
      nonAnswer: false,
      intent: { kind: "probe", targetId: "question-5", aspect: "tradeoff", basis: "short answer" },
      prompt: "Which trade-off did you choose?",
      assistance: null,
      targetId: "question-5",
      degraded: false,
    });
    mocks.recordConversationTurn.mockResolvedValue(session([
      ...current.questions.slice(0, 4),
      question(5, "short answer"),
      { ...followUp, prompt: "Which trade-off did you choose?" },
    ]));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "respond", sessionId: "session-1", answer: "short answer" }),
    }));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.session.status).toBe("active");
    expect(body.session.questions).toHaveLength(6);
    expect(mocks.recordConversationTurn).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      "question-5",
      "short answer",
      expect.anything(),
      expect.objectContaining({
        // `intent.targetId` ("question-5") equals the just-answered question's
        // own id -- a same-target continuation. It can't be persisted as
        // `nextQuestionId` back onto that row (already-answered by this same
        // call), so it goes through `followUp` instead -- see
        // `followUpDraftForContinuation`'s doc comment in route.ts.
        nextQuestionId: null,
        followUp: {
          category: "behavioral",
          competencyId: "react-id",
          competencyName: "React architecture",
          difficulty: "senior",
          isFollowUp: true,
          prompt: "Which trade-off did you choose?",
          objective: "",
          evidenceIds: [],
          expectedSignals: [],
          missingSignalPrompts: [],
          rubricCriteria: [],
          followUpLimit: 1,
          sourceConfidence: null,
        },
        nextPrompt: "Which trade-off did you choose?",
        askedIntent: { kind: "probe", targetId: "question-5", aspect: "tradeoff", basis: "short answer" },
        nonAnswer: false,
        degraded: false,
      }),
    );
    expect(mocks.completeSession).not.toHaveBeenCalled();
  });

  it("never derives a follow-up for a planned-practice session's pre-written questions", async () => {
    // The C1 shape: pre-written prompts and no `askedIntent`, so `mapSession`
    // hands the director a full set of reconstructed coverage targets the plan
    // was never designed for, and `decideIntent` advances to the very row being
    // answered. Persisting that as a follow-up made the RPC raise
    // "Conversation follow-up limit reached" on the practice introduction
    // (whose `follow_up_limit` is 0) and returned HTTP 500.
    const current = plannedSession([question(1, null), question(2, null), question(3, null)]);
    mocks.getSession.mockResolvedValue(current);
    mocks.questionIdForTarget.mockReturnValue("question-1");
    mocks.nextTurn.mockResolvedValue({
      evaluation: { score: 7, competencyId: null, competency: "Communication", dimensions: {}, strengths: [], needsWork: [] },
      nonAnswer: false,
      intent: { kind: "advance", targetId: "question-1", reason: "satisfied" },
      prompt: "An adaptive line the planned session must not adopt.",
      assistance: null,
      targetId: "question-1",
      degraded: false,
    });
    mocks.recordConversationTurn.mockResolvedValue(plannedSession([
      question(1, "an answer"), question(2, null), question(3, null),
    ]));

    const response = await POST(jsonRequest({ action: "respond", sessionId: "session-1", answer: "an answer" }));

    expect(response.status).toBe(200);
    expect(mocks.recordConversationTurn).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      "question-1",
      "an answer",
      expect.anything(),
      expect.objectContaining({ nextQuestionId: null, followUp: null }),
    );
  });

  it("records a pre-written question the assessor could not score instead of re-asking it", async () => {
    const current = plannedSession([question(1, null), question(2, null), question(3, null)]);
    mocks.getSession.mockResolvedValue(current);
    mocks.nextTurn.mockResolvedValue({
      evaluation: null,
      nonAnswer: true,
      intent: { kind: "rescue", targetId: "question-1", style: "narrow", hook: null },
      prompt: "A smaller version of the same question?",
      assistance: { style: "narrow", at: "2026-09-01T09:00:00.000Z" },
      targetId: "question-1",
      degraded: false,
    });
    mocks.recordConversationTurn.mockResolvedValue(plannedSession([
      question(1, "i don't know"), question(2, null), question(3, null),
    ]));

    const response = await POST(jsonRequest({ action: "respond", sessionId: "session-1", answer: "i don't know" }));

    expect(response.status).toBe(200);
    // A pre-written row carries no new prompt for a rescue to land on, so
    // leaving it unanswered would show the candidate the identical question
    // with their answer discarded.
    expect(mocks.recordConversationTurn).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      "question-1",
      "i don't know",
      expect.objectContaining({ improvedAnswer: "Not attempted." }),
      expect.objectContaining({ nonAnswer: false }),
    );
  });

  it("starts a conversation from the generated blueprint and returns the first question", async () => {
    const blueprint = {
      status: "grounded" as const,
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 5,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [
        {
          id: "blueprint-question-1",
          sequence: 1,
          category: "introduction" as const,
          competencyId: null,
          competencyName: null,
          difficulty: "senior" as const,
          isFollowUp: false,
          prompt: "Tell me about the migration.",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Understand the candidate's recent work.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["ownership"],
          missingSignalPrompts: ["Name one concrete example."],
          rubricCriteria: [
            "Name the project or work example.",
            "Describe the ownership or decision involved.",
            "Explain the outcome or trade-off.",
          ],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [],
    };
    // The reloaded session's own `questions` rows already carry the
    // blueprint-shaped fields directly (Task 10's `mapQuestion`), not just
    // `session.blueprint.questions` -- mirror that here rather than only the
    // bare `question()` fixture shape.
    const persisted = session([
      {
        ...question(1, null),
        ...blueprint.questions[0],
        id: "database-question-1",
      },
    ]);
    persisted.blueprint = {
      ...blueprint,
      questions: blueprint.questions.map((item) => ({ ...item, id: "database-question-1" })),
    };

    mocks.generateInterviewBlueprint.mockResolvedValue(blueprint);
    mocks.createSessionWithBlueprint.mockResolvedValue(persisted);
    mocks.openingTurn.mockResolvedValue({
      intent: { kind: "open", targetId: "database-question-1" },
      prompt: "Tell me about the migration.",
      targetId: "database-question-1",
    });
    mocks.revealFirstQuestion.mockResolvedValue(persisted);

    const response = await POST(jsonRequest({ action: "start" }));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(mocks.generateInterviewBlueprint).toHaveBeenCalledWith(profile, [], { roundId: "tech-lead", opportunity: null });
    expect(mocks.createSessionWithBlueprint).toHaveBeenCalledWith(expect.anything(), "user-1", blueprint, { roundId: "tech-lead", mode: "real" });
    expect(body.session.blueprint.questions[0].objective).toBe("Understand the candidate's recent work.");
    expect(body.session.blueprint.questions[0].id).toBe("database-question-1");
    expect(body.session.questions[0]).toMatchObject({
      objective: "Understand the candidate's recent work.",
      evidenceIds: ["evidence-1"],
      expectedSignals: ["ownership"],
      missingSignalPrompts: ["Name one concrete example."],
      rubricCriteria: [
        "Name the project or work example.",
        "Describe the ownership or decision involved.",
        "Explain the outcome or trade-off.",
      ],
      followUpLimit: 1,
      sourceConfidence: 0.9,
    });
    expect(body.session.questions[0].prompt).toBe("Tell me about the migration.");
  });

  it("passes the persisted blueprint into exact-question evaluation", async () => {
    const persistedBlueprint = {
      status: "grounded" as const,
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 5,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [
        {
          id: "question-1",
          sequence: 1,
          category: "experience" as const,
          competencyId: "react-id",
          competencyName: "React architecture",
          difficulty: "senior" as const,
          isFollowUp: false,
          prompt: "Tell me about the checkout migration.",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Probe the migration ownership and impact.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["ownership", "trade-off", "impact"],
          missingSignalPrompts: ["Name the trade-off you accepted."],
          rubricCriteria: [
            "Name the project or work example.",
            "Describe the candidate's role and ownership.",
            "Explain the decision, trade-off, and outcome.",
          ],
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
          sequence: 2,
          category: "architecture" as const,
          competencyId: "system-design-id",
          competencyName: "System design",
          difficulty: "senior" as const,
          objective: "Probe the system design decision.",
          evidenceIds: ["evidence-2"],
          expectedSignals: ["decision", "constraint"],
          missingSignalPrompts: ["Name the design constraint."],
          rubricCriteria: [
            "Name the system design challenge.",
            "Describe the constraint or alternative.",
            "Explain the trade-off and result.",
          ],
          followUpLimit: 1,
          sourceConfidence: 0.91,
          prompt: "How would you shape observability?",
          answer: null,
          id: "question-2",
          isFollowUp: false,
          createdAt: "2026-08-29T10:00:00.000Z",
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [],
    };
    const activeSession = session([question(1, null), question(2, null)]);
    activeSession.blueprint = persistedBlueprint;
    mocks.getSession.mockResolvedValue(activeSession);
    mocks.nextTurn.mockResolvedValue({
      evaluation: { score: 8, competencyId: "react-id", competency: "React architecture", dimensions: {}, strengths: ["Specific"], needsWork: [] },
      nonAnswer: false,
      intent: { kind: "advance", targetId: "question-2", reason: "satisfied" },
      prompt: "How would you shape observability?",
      assistance: null,
      targetId: "question-2",
      degraded: false,
    });
    mocks.recordConversationTurn.mockResolvedValue(session([question(1, "A complete answer."), question(2, null)]));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "respond", sessionId: "session-1", answer: "A complete answer." }),
    }));

    expect(response.status).toBe(200);
    expect(mocks.nextTurn).toHaveBeenCalledWith({
      profile,
      session: expect.anything(),
      answeredQuestion: expect.objectContaining({
        id: "question-1",
        objective: "Probe the migration ownership and impact.",
        evidenceIds: ["evidence-1"],
        expectedSignals: ["ownership", "trade-off", "impact"],
        missingSignalPrompts: ["Name the trade-off you accepted."],
        rubricCriteria: [
          "Name the project or work example.",
          "Describe the candidate's role and ownership.",
          "Explain the decision, trade-off, and outcome.",
        ],
        followUpLimit: 1,
        sourceConfidence: 0.94,
        prompt: "Tell me about the checkout migration.",
      }),
      answer: "A complete answer.",
      blueprint: persistedBlueprint,
      evidence: [],
      opportunity: null,
    });
  });

  it("falls back to a limited-grounding blueprint without failing the start request", async () => {
    const limitedBlueprint = {
      status: "limited-grounding" as const,
      fallbackReason: "Gemini returned invalid blueprint JSON after one repair attempt.",
      maxFollowUps: 3,
      maxQuestions: 5,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [
        {
          id: "blueprint-question-1",
          sequence: 1,
          category: "introduction" as const,
          competencyId: null,
          competencyName: null,
          difficulty: "senior" as const,
          isFollowUp: false,
          prompt: "Tell me about yourself.",
          answer: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          objective: "Understand the candidate's background.",
          evidenceIds: [],
          expectedSignals: ["ownership"],
          missingSignalPrompts: ["Name one concrete example."],
          followUpLimit: 0,
          sourceConfidence: null,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
      roundId: "tech-lead" as const,
      turnBudget: 8,
      targets: [],
    };
    const persisted = session([
      {
        ...question(1, null),
        id: "blueprint-question-1",
        prompt: "Tell me about yourself.",
      },
    ]);
    persisted.blueprint = limitedBlueprint;

    mocks.generateInterviewBlueprint.mockResolvedValue(limitedBlueprint);
    mocks.createSessionWithBlueprint.mockResolvedValue(persisted);
    mocks.openingTurn.mockResolvedValue({
      intent: { kind: "open", targetId: "blueprint-question-1" },
      prompt: "Tell me about yourself.",
      targetId: "blueprint-question-1",
    });
    mocks.revealFirstQuestion.mockResolvedValue(persisted);

    const response = await POST(jsonRequest({ action: "start" }));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.session.blueprint.status).toBe("limited-grounding");
    expect(body.session.blueprint.fallbackReason).toContain("invalid blueprint JSON");
  });

  it("completes naturally only after the final persisted question is answered", async () => {
    const current = session([1, 2, 3, 4, 5].map((sequence) => question(sequence, sequence < 5 ? "answered" : null)));
    const answered = session([1, 2, 3, 4, 5].map((sequence) => question(sequence, "answered")));
    const completed = session(answered.questions, "complete");
    mocks.getSession.mockResolvedValue(current);
    mocks.nextTurn.mockResolvedValue({
      evaluation: { score: 8, competencyId: "react-id", competency: "React architecture", dimensions: {}, strengths: ["Specific"], needsWork: [] },
      nonAnswer: false,
      intent: { kind: "close" },
      prompt: "Do you have any questions for me?",
      assistance: null,
      targetId: null,
      degraded: false,
    });
    mocks.recordConversationTurn.mockResolvedValue(answered);
    mocks.summarizeSession.mockReturnValue({ overallScore: 8, summary: "Complete" });
    mocks.completeSession.mockResolvedValue(completed);

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "respond", sessionId: "session-1", answer: "A complete answer." }),
    }));
    const body = await response.json();

    expect(body.session.status).toBe("complete");
    expect(mocks.completeSession).toHaveBeenCalledOnce();
  });

  it("allows an explicit finish after five answers even when a follow-up remains", async () => {
    const current = session([
      ...[1, 2, 3, 4, 5].map((sequence) => question(sequence, "answered")),
      question(6, null),
    ]);
    const completed = session(current.questions, "complete");
    mocks.getSession.mockResolvedValue(current);
    mocks.summarizeSession.mockReturnValue({ overallScore: 7, summary: "Complete" });
    mocks.completeSession.mockResolvedValue(completed);

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "complete", sessionId: "session-1" }),
    }));

    expect(response.status).toBe(200);
    expect(mocks.completeSession).toHaveBeenCalledOnce();
  });

  it("rejects an explicit finish on a generic conversation with fewer than five answers", async () => {
    mocks.getSession.mockResolvedValue(session([1, 2, 3, 4].map((sequence) => question(sequence, "answered"))));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "complete", sessionId: "session-1" }),
    }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Answer at least five questions before completing this interview." });
    expect(mocks.completeSession).not.toHaveBeenCalled();
  });

  it("allows an explicit finish on planned practice once its three base questions are answered", async () => {
    const planned = plannedSession([1, 2, 3].map((sequence) => question(sequence, "answered")));
    mocks.getSession.mockResolvedValue(planned);
    mocks.summarizeSession.mockReturnValue({ overallScore: 8, summary: "Complete" });
    mocks.completeSession.mockResolvedValue(plannedSession(planned.questions, "complete"));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "complete", sessionId: "session-1" }),
    }));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(mocks.completeSession).toHaveBeenCalledOnce();
    expect(mocks.updatePracticePlan).toHaveBeenCalledWith(expect.anything(), "user-1", "plan-1", {
      status: "completed",
      completedAt: "2026-08-29T11:00:00.000Z",
    });
    // The key is always present so clients can read it as a nullable field.
    expect(body).toHaveProperty("practicePlanWarning", null);
  });

  it("rejects an explicit finish on planned practice while a persisted follow-up is unanswered", async () => {
    mocks.getSession.mockResolvedValue(plannedSession([
      ...[1, 2, 3].map((sequence) => question(sequence, "answered")),
      question(6, null),
    ]));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "complete", sessionId: "session-1" }),
    }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Answer every question in this practice before completing it." });
    expect(mocks.completeSession).not.toHaveBeenCalled();
  });

  it("allows the same planned practice to finish once the follow-up is answered", async () => {
    const planned = plannedSession([
      ...[1, 2, 3].map((sequence) => question(sequence, "answered")),
      question(6, "follow-up answered"),
    ]);
    mocks.getSession.mockResolvedValue(planned);
    mocks.summarizeSession.mockReturnValue({ overallScore: 8, summary: "Complete" });
    mocks.completeSession.mockResolvedValue(plannedSession(planned.questions, "complete"));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "complete", sessionId: "session-1" }),
    }));

    expect(response.status).toBe(200);
    expect(mocks.completeSession).toHaveBeenCalledOnce();
  });

  it("keeps a completed session when practice-plan bookkeeping fails", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const planned = plannedSession([1, 2, 3].map((sequence) => question(sequence, "answered")));
    mocks.getSession.mockResolvedValue(planned);
    mocks.summarizeSession.mockReturnValue({ overallScore: 8, summary: "Complete" });
    mocks.completeSession.mockResolvedValue(plannedSession(planned.questions, "complete"));
    mocks.updatePracticePlan.mockRejectedValue(new Error("practice_plans update failed"));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "complete", sessionId: "session-1" }),
    }));
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.session.status).toBe("complete");
    expect(body.practicePlanWarning).toEqual(expect.any(String));
    expect(consoleError).toHaveBeenCalledWith(
      "[practice-service] practice plan completion failed",
      expect.objectContaining({ message: "practice_plans update failed" }),
    );
    consoleError.mockRestore();
  });

  it("marks the plan completed when a planned conversation finishes naturally", async () => {
    const current = plannedSession([
      question(1, "answered"),
      question(2, "answered"),
      question(3, null),
    ]);
    const answered = plannedSession([1, 2, 3].map((sequence) => question(sequence, "answered")));
    mocks.getSession.mockResolvedValue(current);
    mocks.nextTurn.mockResolvedValue({
      evaluation: { score: 8, competencyId: "react-id", competency: "React architecture", dimensions: {}, strengths: [], needsWork: [] },
      nonAnswer: false,
      intent: { kind: "close" },
      prompt: "Do you have any questions for me?",
      assistance: null,
      targetId: null,
      degraded: false,
    });
    mocks.recordConversationTurn.mockResolvedValue(answered);
    mocks.summarizeSession.mockReturnValue({ overallScore: 8, summary: "Complete" });
    mocks.completeSession.mockResolvedValue(plannedSession(answered.questions, "complete"));

    const response = await POST(new Request("http://localhost/api/interview", {
      method: "POST",
      body: JSON.stringify({ action: "respond", sessionId: "session-1", answer: "A complete answer." }),
    }));
    const body = await response.json();

    expect(body.session.status).toBe("complete");
    expect(body).toHaveProperty("practicePlanWarning", null);
    expect(mocks.updatePracticePlan).toHaveBeenCalledWith(expect.anything(), "user-1", "plan-1", expect.objectContaining({
      status: "completed",
    }));
  });
});

describe("GET /api/interview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.listReadinessEvidence.mockResolvedValue([]);
  });

  it("serves the evidence-backed readiness model with the authenticated session list", async () => {
    mocks.listRecentSessions.mockResolvedValue([
      session([question(1, "answered")], "complete"),
      session([question(1, "answered")]),
    ]);
    mocks.listReadinessEvidence.mockResolvedValue([
      {
        questionEvaluationId: "eval-1",
        sessionId: "session-1",
        recordedAt: "2026-08-29T10:00:00.000Z",
        score: 8,
        competencyId: "react-id",
        competencyName: "React architecture",
        category: "technical",
        relevance: 1,
        mode: "real",
        degraded: false,
        assistanceCount: 0,
      },
    ]);

    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.sessions).toHaveLength(2);
    expect(body.readiness.dimensions).toHaveLength(7);
    expect(body.readiness.overall).toBeGreaterThanOrEqual(0);
    expect(body).not.toHaveProperty("progress");
    expect(mocks.listRecentSessions).toHaveBeenCalledWith(expect.anything(), "user-1");
    expect(mocks.listReadinessEvidence).toHaveBeenCalledWith(expect.anything(), "user-1");
  });

  it("returns 401 without exposing readiness when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(401);
    expect(body).toEqual({ error: "Sign in to continue." });
    expect(mocks.listRecentSessions).not.toHaveBeenCalled();
    expect(mocks.listReadinessEvidence).not.toHaveBeenCalled();
  });
});

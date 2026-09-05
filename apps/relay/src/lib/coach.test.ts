import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  analyzeProfile,
  assessProfileReadiness,
  extractEngineeringEvidence,
  extractPdfText,
  evaluateAnswer,
  generateInterviewBlueprint,
  generatePracticeBlueprint,
  nextTurn,
  openingTurn,
  speakIntent,
} from "@/lib/coach";
import type { NextTurnInput } from "@/lib/coach";
import { modePolicyFor, roundFor } from "@/lib/interview-rounds";
import { deterministicLine } from "@/lib/interviewer-voice";
import type {
  BlueprintQuestion,
  CoverageTarget,
  Evaluation,
  EvidenceItem,
  GroundedEvaluation,
  Intent,
  InterviewBlueprint,
  InterviewSession,
  Opportunity,
  PlannedQuestion,
  PracticeBlueprintContext,
  PracticePlan,
  Profile,
  ProfileDraft,
} from "@/lib/types";

const planned = (overrides: Partial<PlannedQuestion>): PlannedQuestion => ({
  id: "question-1",
  sequence: 1,
  category: "technical",
  competencyId: "react-id",
  competencyName: "React architecture",
  difficulty: "senior",
  isFollowUp: false,
  prompt: "Generic prompt",
  answer: null,
  createdAt: "2026-08-29T10:00:00.000Z",
  askedIntent: null,
  assistance: [],
  nonAnswer: false,
  setAsideAt: null,
  setAsideReason: null,
  nonAnswers: [],
  ...overrides,
});

const dimensionKeys = [
  "correctness",
  "depth",
  "clarity",
  "structure",
  "practicalExperience",
  "tradeOffAwareness",
  "communication",
  "confidence",
  "relevance",
] as const;

const blueprintEvidence: EvidenceItem[] = [
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

const blueprintProfile: ProfileDraft = {
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

const practiceProfile: Profile = {
  userId: "user-1",
  role: "Frontend Engineer",
  seniority: "Senior",
  summary: "Frontend engineer focused on performance and delivery.",
  narrative: "Owns frontend platforms and reliability work.",
  expertise: ["React", "TypeScript", "Next.js"],
  characteristics: ["Pragmatic"],
  competencies: [
    {
      id: "react-architecture",
      name: "React architecture",
      relevance: 1,
      expectedLevel: "senior",
      estimatedLevel: "senior",
      confidence: "high",
      lastPracticedAt: null,
      questionCount: 0,
      averageScore: null,
      recentScore: null,
      strengths: [],
      weaknesses: [],
    },
  ],
  source: { cvText: "At Acme I led a React migration for checkout.", coverLetter: "" },
  createdAt: "2026-08-29T10:00:00.000Z",
  updatedAt: "2026-08-29T10:00:00.000Z",
};

const practicePlan = (overrides: Partial<PracticePlan> = {}): PracticePlan => ({
  id: "plan-1",
  userId: "user-1",
  status: "ready",
  primaryFocus: "React architecture trade-offs",
  secondaryFocus: null,
  rationale: "Upcoming onsite focuses on frontend architecture decisions.",
  format: "targeted_drill",
  estimatedMinutes: 20,
  successCriteria: ["Name a concrete trade-off with a measured outcome."],
  priorityScore: null,
  priorityFactors: {},
  generationError: null,
  completedAt: null,
  createdAt: "2026-08-29T10:00:00.000Z",
  updatedAt: "2026-08-29T10:00:00.000Z",
  opportunities: [],
  ...overrides,
});

const practiceContext: PracticeBlueprintContext = {
  primaryOpportunity: null,
  supportingOpportunities: [],
  observations: [],
  stories: [],
};

const groundedBlueprint = (question: PlannedQuestion, overrides: Partial<InterviewBlueprint["questions"][number]> = {}): InterviewBlueprint => ({
  status: "grounded",
  fallbackReason: null,
  maxFollowUps: 3,
  maxQuestions: 8,
  createdAt: "2026-08-29T10:00:00.000Z",
  questions: [
    {
      ...question,
      objective: "Probe the migration ownership and impact.",
      evidenceIds: ["evidence-1"],
      expectedSignals: ["ownership", "trade-off", "impact"],
      missingSignalPrompts: ["Name the trade-off you accepted."],
      followUpLimit: 1,
      sourceConfidence: 0.94,
      rubricCriteria: [
        "Name the project or work example.",
        "Describe the candidate's role and ownership.",
        "Explain the decision, trade-off, and outcome.",
      ],
      ...overrides,
    },
  ],
  roundId: "tech-lead",
  turnBudget: 8,
  targets: [],
});

/**
 * Stubs `fetch` to return the assessor response on the first Gemini call and
 * the interviewer-line response on every call after, mirroring the two-call
 * shape `nextTurn` makes (assessor, then `speakIntent`). Also stubs a Gemini
 * API key so `modelJson` actually reaches the stubbed `fetch` instead of
 * short-circuiting to its no-key fallback.
 */
function stubGemini(assessor: unknown, line: unknown) {
  vi.stubEnv("GEMINI_API_KEY", "test-key");
  const responses = [assessor, line];
  let call = 0;
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
    candidates: [{ content: { parts: [{ text: JSON.stringify(responses[Math.min(call++, 1)]) }] } }],
  }), { status: 200 })));
}

/** As `stubGemini`, but also records each request body onto `sink` in call order. */
function stubGeminiCapturing(sink: string[], assessor: unknown, line: unknown) {
  vi.stubEnv("GEMINI_API_KEY", "test-key");
  const responses = [assessor, line];
  let call = 0;
  vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
    sink.push(String(init.body));
    return new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: JSON.stringify(responses[Math.min(call++, 1)]) }] } }],
    }), { status: 200 });
  }));
}

const dimensionReasonSample = Object.fromEntries(dimensionKeys.map((key) => [key, "Reasoned."])) as GroundedEvaluation["dimensionReasons"];
const dimensionScoreSample = Object.fromEntries(dimensionKeys.map((key) => [key, 7])) as GroundedEvaluation["dimensions"];

/** A schema-valid assessor evaluation payload, matching `groundedEvaluationSchema`. */
function sampleGroundedEvaluation(overrides: Partial<GroundedEvaluation> = {}): GroundedEvaluation {
  return {
    score: 7,
    competencyId: null,
    competency: "Communication",
    relevance: 7,
    dimensions: dimensionScoreSample,
    strengths: ["Specific example"],
    needsWork: ["Add one trade-off"],
    missingPoints: ["Name the trade-off you accepted."],
    betterStructure: ["Start with the constraint, then the plan."],
    improvedAnswer: "I would start with the constraint, explain the plan, and close with the trade-off.",
    supportedClaims: ["I made a concrete decision and measured the result."],
    expectedSignalsPresent: ["ownership"],
    unsupportedClaims: [],
    dimensionReasons: dimensionReasonSample,
    answerTier: "strong",
    ...overrides,
  };
}

function coverageTarget(id: string, overrides: Partial<CoverageTarget> = {}): CoverageTarget {
  return {
    id,
    competencyId: `competency-${id}`,
    competencyName: `Competency ${id}`,
    category: "experience",
    evidenceIds: [],
    difficulty: "senior",
    objective: `Establish the candidate's ownership within Competency ${id}.`,
    expectedSignals: ["ownership", "trade-off", "impact"],
    rubricCriteria: [
      `Name a concrete example from Competency ${id}.`,
      "Describe the ownership or decision involved.",
      "Explain the outcome or trade-off.",
    ],
    required: id === "a",
    ...overrides,
  };
}

function nextTurnBlueprint(overrides: Partial<InterviewBlueprint> = {}): InterviewBlueprint {
  return {
    status: "grounded",
    fallbackReason: null,
    maxFollowUps: 3,
    maxQuestions: 8,
    createdAt: "2026-08-29T10:00:00.000Z",
    questions: [],
    roundId: "tech-lead",
    turnBudget: 8,
    targets: [coverageTarget("a"), coverageTarget("b")],
    ...overrides,
  };
}

/** A `PlannedQuestion` this pipeline authored: `askedIntent` carries the intent that produced it. */
function answeredQuestion(id: string, intent: Intent, overrides: Partial<PlannedQuestion> = {}): PlannedQuestion {
  const targetId = "targetId" in intent ? intent.targetId : null;
  return {
    id,
    sequence: 1,
    category: "experience",
    competencyId: targetId ? `competency-${targetId}` : null,
    competencyName: targetId ? `Competency ${targetId}` : null,
    difficulty: "senior",
    isFollowUp: false,
    prompt: "Prior interviewer prompt.",
    answer: null,
    createdAt: "2026-08-29T10:00:00.000Z",
    askedIntent: intent,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
    ...overrides,
  };
}

function nextTurnSession(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    status: "active",
    degraded: false,
    startedAt: "2026-08-29T10:00:00.000Z",
    completedAt: null,
    exercise: {},
    resultSummary: {},
    overallScore: null,
    questions: [],
    checkpoints: [],
    evaluations: [],
    messages: [],
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    practicePlanId: null,
    opportunityId: null,
    ...overrides,
  };
}

/** A full `NextTurnInput`, defaulting the session's questions to just the answered question. */
function nextTurnInput(overrides: Partial<NextTurnInput> = {}): NextTurnInput {
  const answered = overrides.answeredQuestion ?? answeredQuestion("q1", { kind: "open", targetId: "a" });
  return {
    profile: { role: "Frontend Engineer", seniority: "Senior", expertise: ["React"], narrative: "Owns frontend platforms." },
    session: nextTurnSession({ questions: [answered] }),
    answeredQuestion: answered,
    answer: "I led the checkout migration, split bundles by route, and measured a 28% drop in bundle size.",
    blueprint: nextTurnBlueprint(),
    evidence: blueprintEvidence,
    opportunity: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

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
    // A bare `prompt` string comparison would be tautological here: each call
    // stubs its own hard-coded `line`, and `prompt` is a direct passthrough of
    // that stub, so the assertion would pass even if `nextTurn` collapsed both
    // calls onto the same target internally. Assert instead on production
    // state `nextTurn` computes itself (`targetId`, `intent.kind`) and on the
    // `Subject:` line `speakIntent` builds from the real resolved target, via
    // the captured request bodies -- none of that is supplied by the stub.
    const capturedFirst: string[] = [];
    stubGeminiCapturing(
      capturedFirst,
      { read: "answered", evaluation: sampleGroundedEvaluation() },
      { line: "What decision did you personally make on Competency a?" },
    );
    const first = await nextTurn(nextTurnInput({
      answeredQuestion: answeredQuestion("q1", { kind: "open", targetId: "a" }),
    }));

    const capturedSecond: string[] = [];
    stubGeminiCapturing(
      capturedSecond,
      { read: "answered", evaluation: sampleGroundedEvaluation() },
      { line: "What decision did you personally make on Competency b?" },
    );
    const second = await nextTurn(nextTurnInput({
      answeredQuestion: answeredQuestion("q2", { kind: "open", targetId: "b" }),
    }));

    expect(first.targetId).toBe("a");
    expect(second.targetId).toBe("b");
    expect(first.targetId).not.toBe(second.targetId);
    expect(first.intent.kind).not.toBe("advance");
    expect(second.intent.kind).not.toBe("advance");

    const firstInterviewerCall = capturedFirst[capturedFirst.length - 1];
    const secondInterviewerCall = capturedSecond[capturedSecond.length - 1];
    expect(firstInterviewerCall).toContain("Subject: Competency a");
    expect(secondInterviewerCall).toContain("Subject: Competency b");

    expect(first.prompt).not.toBe(second.prompt);
  });
});

describe("nextTurn — director wiring", () => {
  it("keeps working the same target while it remains unsatisfied", async () => {
    const result = await nextTurn(nextTurnInput({
      answeredQuestion: answeredQuestion("q1", { kind: "open", targetId: "a" }),
    }));

    // A non-stuck read produces a real evaluation (controller ruling 3).
    expect(result.evaluation).not.toBeNull();
    expect(result.intent.kind).not.toBe("advance");
    expect(result.targetId).toBe("a");
  });

  it("advances to the next target once the current one is satisfied", async () => {
    const priorQuestion = answeredQuestion("q1", { kind: "open", targetId: "a" }, { answer: "I owned the migration end to end." });
    const currentQuestion = answeredQuestion("q2", { kind: "probe", targetId: "a", aspect: "specifics", basis: "I owned the migration end to end." }, { answer: "It cut load time by 20%." });
    const priorEvaluation: Evaluation = {
      score: 8,
      questionId: priorQuestion.id,
      competencyId: null,
      competency: "Competency a",
      dimensions: {},
      strengths: [],
      needsWork: [],
      missingPoints: [],
      betterStructure: [],
      improvedAnswer: "",
      expectedSignalsPresent: ["ownership", "trade-off", "impact"],
    };

    const result = await nextTurn(nextTurnInput({
      answeredQuestion: currentQuestion,
      session: nextTurnSession({ questions: [priorQuestion, currentQuestion], evaluations: [priorEvaluation] }),
    }));

    expect(result.intent.kind).toBe("advance");
    expect(result.targetId).toBe("b");
  });
});

describe("openingTurn", () => {
  it("opens on the blueprint's first coverage target", async () => {
    const result = await openingTurn({
      profile: { role: "Frontend Engineer", seniority: "Senior", expertise: ["React"], narrative: "Owns frontend platforms." },
      session: nextTurnSession(),
      blueprint: nextTurnBlueprint(),
      evidence: blueprintEvidence,
      opportunity: null,
    });

    expect(result.intent).toEqual({ kind: "open", targetId: "a" });
    expect(result.targetId).toBe("a");
    expect(result.prompt.length).toBeGreaterThan(0);
  });
});

describe("nextTurn / evaluateAnswer", () => {
  it("falls back to a backend-oriented software-engineering profile when Gemini is unavailable", async () => {
    vi.stubEnv("GEMINI_API_KEY", "");

    const profile = await analyzeProfile(
      "Built Node.js APIs for a payments platform. Owned Postgres migrations and improved production reliability.",
      "",
    );

    expect(profile.role).toBe("Backend Engineer");
    expect(profile.summary).toContain("software-engineering");
    expect(profile.expertise).toEqual(expect.arrayContaining(["Node.js", "Postgres", "Reliability"]));
    expect(profile.competencies).toEqual(expect.arrayContaining([
      expect.objectContaining({ name: "Node.js" }),
      expect.objectContaining({ name: "Postgres" }),
    ]));
  });

  it("scores a relevant answer higher than an unrelated answer of the same length", async () => {
    const blueprint = groundedBlueprint(planned({
      id: "question-1",
      sequence: 2,
      category: "experience",
      competencyId: "react-id",
      competencyName: "React architecture",
      difficulty: "senior",
      isFollowUp: false,
      prompt: "Tell me about the checkout migration.",
      answer: null,
      createdAt: "2026-08-29T10:00:00.000Z",
    }));
    const relevantAnswer = "I led the checkout migration, split bundles, and measured a 28% drop in bundle size.";
    const unrelatedAnswer = "I enjoy solving puzzles and learning new things every day.".padEnd(relevantAnswer.length, ".");

    const relevant = await evaluateAnswer(
      blueprint.questions[0],
      blueprint,
      blueprintProfile,
      relevantAnswer,
      "interviewer: Tell me about the checkout migration.",
    );
    const unrelated = await evaluateAnswer(
      blueprint.questions[0],
      blueprint,
      blueprintProfile,
      unrelatedAnswer,
      "interviewer: Tell me about the checkout migration.",
    );

    expect(relevantAnswer.length).toBe(unrelatedAnswer.length);
    expect(relevant.score).toBeGreaterThan(unrelated.score);
    expect(relevant.relevance).toBeGreaterThan(unrelated.relevance);
    expect(relevant.supportedClaims.length).toBeGreaterThan(0);
    expect(relevant.expectedSignalsPresent).toEqual(expect.arrayContaining(["ownership", "trade-off", "impact"]));
    expect(unrelated.expectedSignalsPresent).toEqual([]);
    expect(unrelated.unsupportedClaims.length).toBeGreaterThan(0);
    expect(relevant.dimensionReasons.relevance).toContain("checkout migration");
  });

  it("rejects schema-valid Gemini praise when the answer is unrelated to the exact question", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              question: "What is your favorite CSS property?",
              shouldFollowUp: false,
              evaluation: {
                score: 9.1,
                competency: "Ignored by normalization",
                relevance: 9.4,
                dimensions: {
                  correctness: 9,
                  depth: 9,
                  clarity: 9,
                  structure: 9,
                  practicalExperience: 9,
                  tradeOffAwareness: 9,
                  communication: 9,
                  confidence: 9,
                  relevance: 9,
                },
                strengths: ["Clear migration ownership"],
                needsWork: ["Add a metric"],
                missingPoints: ["Explain the rollback trigger."],
                betterStructure: ["Start with the constraint, then the decision."],
                improvedAnswer: "I led the checkout migration, accepted the rollout trade-off, and measured the impact after launch.",
                supportedClaims: ["I led the checkout migration and measured the rollout."],
                expectedSignalsPresent: ["ownership", "trade-off", "impact"],
                unsupportedClaims: [],
                dimensionReasons: {
                  correctness: "The answer directly addresses the migration objective.",
                  depth: "It includes detailed rollout evidence.",
                  clarity: "It is organized and specific.",
                  structure: "It flows from constraint to result.",
                  practicalExperience: "It is grounded in the candidate's shipped migration.",
                  tradeOffAwareness: "It names the trade-off explicitly.",
                  communication: "It is concise and clear.",
                  confidence: "It states the ownership directly.",
                  relevance: "It answers the checkout migration question.",
                },
                answerTier: "strong",
              },
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const question = groundedBlueprint(planned({
      id: "question-1",
      sequence: 2,
      category: "experience",
      competencyId: "react-id",
      competencyName: "React architecture",
      difficulty: "senior",
      isFollowUp: false,
      prompt: "Tell me about the checkout migration.",
      answer: null,
      createdAt: "2026-08-29T10:00:00.000Z",
    })).questions[0];

    const evaluation = await evaluateAnswer(
      question,
      { status: "grounded", fallbackReason: null, maxFollowUps: 3, maxQuestions: 8, createdAt: "2026-08-29T10:00:00.000Z", questions: [question], roundId: "tech-lead", turnBudget: 8, targets: [] },
      blueprintProfile,
      "I enjoy mentoring, reading docs, and learning new tools every week.",
      "interviewer: Tell me about the checkout migration.",
    );

    expect(evaluation.score).toBeLessThan(6.5);
    expect(evaluation.relevance).toBeLessThan(5);
    expect(evaluation.expectedSignalsPresent).toEqual([]);
    expect(evaluation.supportedClaims).toEqual([]);
    expect(evaluation.unsupportedClaims.length).toBeGreaterThan(0);
    expect(evaluation.dimensionReasons.relevance).toContain("does not directly answer");
  });

  // The follow-up decision (and the nextQuestion/followUp routing it drives)
  // moved to the director and is no longer part of this call's contract --
  // covered by the "nextTurn — director wiring" tests. This test's subject is
  // evaluation grounding, so it asserts only the evaluation, via the assessor
  // call directly (`evaluateAnswer`) rather than the full turn pipeline.
  it("preserves grounded coaching fields while stripping ungrounded model claims", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              read: "answered",
              evaluation: {
                score: 8.4,
                competency: "Ignored by normalization",
                relevance: 8.7,
                dimensions: {
                  correctness: 8,
                  depth: 8,
                  clarity: 8,
                  structure: 9,
                  practicalExperience: 8,
                  tradeOffAwareness: 8,
                  communication: 8,
                  confidence: 7,
                  relevance: 9,
                },
                strengths: ["Specific trade-off framing"],
                needsWork: ["Quantify the rollout risk"],
                missingPoints: ["Explain the rollback trigger."],
                betterStructure: ["Start with constraints, then explain the migration phases."],
                improvedAnswer: "I would start with the constraints, phase the migration, and define the rollback trigger before rollout.",
                supportedClaims: ["I phased the rollout carefully."],
                expectedSignalsPresent: ["trade-off", "impact"],
                unsupportedClaims: ["We shipped it perfectly."],
                dimensionReasons: {
                  correctness: "The answer matches the migration question.",
                  depth: "The answer mentions rollout phases and a metric.",
                  clarity: "It is organized and specific.",
                  structure: "It starts with the constraint and ends with the rollback trigger.",
                  practicalExperience: "It refers to an actual migration.",
                  tradeOffAwareness: "It names the rollout trade-off.",
                  communication: "It is concise and explainable.",
                  confidence: "It is stated directly.",
                  relevance: "It answers the checkout migration question.",
                },
                answerTier: "strong",
              },
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    // evidenceIds is non-empty so this question keeps a source-evidence
    // target: the model's unsupportedClaims must still pass through.
    const answeredQuestion = planned({ id: "question-1", sequence: 1, category: "experience", evidenceIds: ["evidence-1"] });

    const evaluation = await evaluateAnswer(
      answeredQuestion,
      null,
      { role: "Frontend Engineer", seniority: "Senior", expertise: ["React"], narrative: "Owns frontend platforms." },
      "I phased the rollout carefully, compared alternatives with the team, made the trade-off explicit, and measured the impact after each milestone. ".repeat(2),
      "",
    );

    expect(evaluation.competencyId).toBe("react-id");
    expect(evaluation.competency).toBe("React architecture");
    expect(evaluation.score).toBe(8.4);
    expect(evaluation.relevance).toBe(8.7);
    expect(evaluation.supportedClaims).toEqual(["I phased the rollout carefully."]);
    expect(evaluation.expectedSignalsPresent).toEqual(["trade-off", "impact"]);
    expect(evaluation.unsupportedClaims).toEqual(["We shipped it perfectly."]);
    expect(evaluation.improvedAnswer).toContain("rollback trigger");
    expect(evaluation.dimensionReasons).toBeDefined();
    expect(evaluation.dimensionReasons?.correctness).toContain("migration question");
    expect(evaluation.dimensionReasons?.relevance).toContain("checkout migration question");
  });

  it("classifies a correct-but-shallow answer separately from a senior one", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              read: "answered",
              evaluation: {
                ...sampleGroundedEvaluation(),
                answerTier: "shallow",
              },
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const question = planned({ id: "question-1", sequence: 1, category: "technical" });
    const evaluation = await evaluateAnswer(
      question,
      null,
      { role: "Senior Full Stack Engineer", seniority: "Senior", expertise: ["Node.js"], narrative: null },
      "REST is fine for this, I'd just add pagination.",
      "",
    );

    expect(evaluation.answerTier).toBe("shallow");
  });

  describe("backend/full-stack answer tiers", () => {
    const backendQuestion = planned({
      id: "backend-question-1",
      sequence: 1,
      category: "technical",
      competencyId: "backend-api-design",
      competencyName: "API design",
      difficulty: "senior",
      prompt: "How would you version a public REST API without breaking existing clients?",
    });

    it.each([
      ["incorrect", "You'd just delete the old endpoint and tell clients to update."],
      ["incomplete", "I'd add a new /v2 path for the breaking change."],
      ["shallow", "I'd version with a /v2 path, keep /v1 running, and pick a sunset date."],
      ["strong", "I'd version with a /v2 path, keep /v1 running behind a deprecation header, pick a sunset date communicated in advance, and add contract tests so a v1 regression fails CI before it reaches a client."],
    ] as const)("classifies a %s backend answer as answerTier %s", async (tier, answer) => {
      vi.stubEnv("GEMINI_API_KEY", "private-test-key");
      vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
      vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
        candidates: [{
          content: {
            parts: [{
              text: JSON.stringify({
                read: "answered",
                evaluation: { ...sampleGroundedEvaluation(), answerTier: tier },
              }),
            }],
          },
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

      const evaluation = await evaluateAnswer(
        backendQuestion,
        null,
        { role: "Senior Full Stack Engineer", seniority: "Senior", expertise: ["Node.js", "Postgres"], narrative: null },
        answer,
        "",
      );

      expect(evaluation.answerTier).toBe(tier);
    });
  });

  describe("discovery answers with no source evidence target", () => {
    const discoveryQuestion: BlueprintQuestion = {
      ...planned({
        id: "discovery-question-1",
        category: "experience",
        prompt: "Think of one piece of work you remember clearly. What part were you responsible for?",
      }),
      objective: "Discover a real work example and personal ownership.",
      evidenceIds: [],
      expectedSignals: ["ownership", "trade-off", "impact"],
      missingSignalPrompts: ["What changed because of your work?"],
      rubricCriteria: [
        "Name a real work example.",
        "Describe personal responsibility.",
        "Explain one action or decision and its result when remembered.",
      ],
      followUpLimit: 1,
      sourceConfidence: null,
    };
    const discoveryBlueprint: InterviewBlueprint = {
      status: "limited-grounding",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-08-29T10:00:00.000Z",
      questions: [discoveryQuestion],
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [],
    };

    it("scores a relevant discovery answer without treating newly supplied facts as unsupported", async () => {
      vi.stubEnv("GEMINI_API_KEY", "");

      const relevantAnswer = "At my previous company I rebuilt our questionnaire editor. I shaped the component architecture, coordinated the migration with the team, and measured a 20% drop in load time.";
      const unrelatedAnswer = "I enjoy long weekend hikes and trying new recipes with friends after busy work weeks.".padEnd(relevantAnswer.length, ".");

      const relevant = await evaluateAnswer(
        discoveryQuestion,
        discoveryBlueprint,
        blueprintProfile,
        relevantAnswer,
        "interviewer: Think of one piece of work you remember clearly.",
      );
      const unrelated = await evaluateAnswer(
        discoveryQuestion,
        discoveryBlueprint,
        blueprintProfile,
        unrelatedAnswer,
        "interviewer: Think of one piece of work you remember clearly.",
      );

      expect(relevant.relevance).toBeGreaterThan(5);
      expect(relevant.unsupportedClaims).toEqual([]);
      expect(relevant.supportedClaims.length).toBeGreaterThan(0);
      expect(relevant.relevance).toBeGreaterThan(unrelated.relevance);
    });

    it("never surfaces a model-flagged first-person discovery detail as unsupported", async () => {
      vi.stubEnv("GEMINI_API_KEY", "private-test-key");
      vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
      const mockResponseBody = JSON.stringify({
        candidates: [{
          content: {
            parts: [{
              text: JSON.stringify({
                question: "What was the outcome of that work?",
                shouldFollowUp: false,
                read: "answered",
                evaluation: {
                  score: 7.2,
                  competency: "Ignored by normalization",
                  relevance: 7.5,
                  dimensions: {
                    correctness: 7, depth: 7, clarity: 7, structure: 7,
                    practicalExperience: 7, tradeOffAwareness: 7, communication: 7,
                    confidence: 7, relevance: 7,
                  },
                  strengths: ["Names a concrete example"],
                  needsWork: ["Add the outcome"],
                  missingPoints: ["What changed because of your work?"],
                  betterStructure: ["Lead with the responsibility, then the outcome."],
                  improvedAnswer: "I rebuilt our questionnaire editor and shaped the component architecture.",
                  supportedClaims: ["I shaped the component architecture and coordinated the migration."],
                  expectedSignalsPresent: ["role"],
                  unsupportedClaims: ["I rebuilt our questionnaire editor."],
                  dimensionReasons: {
                    correctness: "The answer names a real example.",
                    depth: "It includes a concrete example.",
                    clarity: "It is concise.",
                    structure: "It follows a clear sequence.",
                    practicalExperience: "It refers to hands-on work.",
                    tradeOffAwareness: "It does not name a trade-off yet.",
                    communication: "It reads as a full answer.",
                    confidence: "It states the example directly.",
                    relevance: "It answers the discovery prompt.",
                  },
                  answerTier: "shallow",
                },
              }),
            }],
          },
        }],
      });
      // Two evaluateAnswer calls share this mock; each needs its own Response
      // instance because a Response body can only be read once.
      vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(mockResponseBody, { status: 200, headers: { "Content-Type": "application/json" } }));

      const discoveryAnswer = "At my previous company I rebuilt our questionnaire editor. I shaped the component architecture and coordinated the migration with the team.";

      const discoveryResult = await evaluateAnswer(
        discoveryQuestion,
        discoveryBlueprint,
        blueprintProfile,
        discoveryAnswer,
        "interviewer: Think of one piece of work you remember clearly.",
      );

      expect(discoveryResult.unsupportedClaims).toEqual([]);

      const groundedQuestion: BlueprintQuestion = { ...discoveryQuestion, evidenceIds: ["evidence-1"] };
      const groundedBlueprintWithEvidence: InterviewBlueprint = {
        ...discoveryBlueprint,
        status: "grounded",
        questions: [groundedQuestion],
      };

      const groundedResult = await evaluateAnswer(
        groundedQuestion,
        groundedBlueprintWithEvidence,
        blueprintProfile,
        discoveryAnswer,
        "interviewer: Think of one piece of work you remember clearly.",
      );

      expect(groundedResult.unsupportedClaims).toEqual(["I rebuilt our questionnaire editor."]);
    });

    // The post-Gemini normalization backstops `unsupportedClaims` alone if this prompt
    // rule silently reverts (see the two tests above), but nothing previously backstopped
    // `improvedAnswer`, `needsWork`, or `missingPoints` -- all of which the model could
    // still ground-check against the CV and accuse the candidate of inventing their own
    // career. This pins the prompt rule itself so a regression there is caught directly.
    it("includes the discovery grounding rule in the evaluator prompt only when the question has no evidence target", async () => {
      vi.stubEnv("GEMINI_API_KEY", "private-test-key");
      vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
      const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(JSON.stringify({
        candidates: [{
          content: {
            parts: [{
              text: JSON.stringify({
                question: "What was the outcome of that work?",
                shouldFollowUp: false,
                evaluation: {
                  score: 7, competency: "Ignored by normalization", relevance: 7,
                  dimensions: {
                    correctness: 7, depth: 7, clarity: 7, structure: 7,
                    practicalExperience: 7, tradeOffAwareness: 7, communication: 7,
                    confidence: 7, relevance: 7,
                  },
                  strengths: [], needsWork: [], missingPoints: [], betterStructure: [],
                  improvedAnswer: "An improved answer.",
                  supportedClaims: ["A supported claim."],
                  expectedSignalsPresent: ["ownership"],
                  unsupportedClaims: [],
                  dimensionReasons: {
                    correctness: "ok", depth: "ok", clarity: "ok", structure: "ok",
                    practicalExperience: "ok", tradeOffAwareness: "ok", communication: "ok",
                    confidence: "ok", relevance: "ok",
                  },
                  answerTier: "shallow",
                },
              }),
            }],
          },
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

      await evaluateAnswer(
        discoveryQuestion,
        discoveryBlueprint,
        blueprintProfile,
        "At my previous company I rebuilt our questionnaire editor.",
        "interviewer: Think of one piece of work you remember clearly.",
      );

      const discoveryPrompt = JSON.parse(String(fetchMock.mock.calls[0][1]?.body)).contents[0].parts[0].text as string;
      expect(discoveryPrompt).toContain("Grounding rule: question.evidenceIds is empty");
      expect(discoveryPrompt).toContain("Do not mark them unsupported merely because they were absent from the source profile");

      const groundedQuestion: BlueprintQuestion = { ...discoveryQuestion, evidenceIds: ["evidence-1"] };
      const groundedBlueprintWithEvidence: InterviewBlueprint = {
        ...discoveryBlueprint,
        status: "grounded",
        questions: [groundedQuestion],
      };

      await evaluateAnswer(
        groundedQuestion,
        groundedBlueprintWithEvidence,
        blueprintProfile,
        "At my previous company I rebuilt our questionnaire editor.",
        "interviewer: Think of one piece of work you remember clearly.",
      );

      const groundedPrompt = JSON.parse(String(fetchMock.mock.calls[1][1]?.body)).contents[0].parts[0].text as string;
      expect(groundedPrompt).not.toContain("Grounding rule:");
    });

  });

  it("calibrates schema-valid model praise against verified answer signals", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              question: "How would you phase the migration?",
              shouldFollowUp: false,
              read: "answered",
              evaluation: {
                score: 9.9,
                competency: "Ignored by normalization",
                relevance: 9.9,
                dimensions: {
                  correctness: 10,
                  depth: 10,
                  clarity: 10,
                  structure: 10,
                  practicalExperience: 10,
                  tradeOffAwareness: 10,
                  communication: 10,
                  confidence: 10,
                  relevance: 10,
                },
                strengths: ["Perfect migration answer"],
                needsWork: ["None"],
                missingPoints: ["None"],
                betterStructure: ["None"],
                improvedAnswer: "I would lead the checkout migration, explain the rollout trade-off, and measure the impact.",
                supportedClaims: ["I led the checkout migration and measured the rollout."],
                expectedSignalsPresent: ["ownership", "trade-off", "impact"],
                unsupportedClaims: [],
                dimensionReasons: {
                  correctness: "The answer directly addresses the migration objective.",
                  depth: "It includes detailed rollout evidence.",
                  clarity: "It is organized and specific.",
                  structure: "It flows from constraint to result.",
                  practicalExperience: "It is grounded in the candidate's shipped migration.",
                  tradeOffAwareness: "It names the trade-off explicitly.",
                  communication: "It is concise and clear.",
                  confidence: "It states the ownership directly.",
                  relevance: "It answers the checkout migration question.",
                },
                answerTier: "strong",
              },
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const question = groundedBlueprint(planned({
      id: "question-1",
      sequence: 2,
      category: "experience",
      competencyId: "react-id",
      competencyName: "React architecture",
      difficulty: "senior",
      isFollowUp: false,
      prompt: "Tell me about the checkout migration.",
      answer: null,
      createdAt: "2026-08-29T10:00:00.000Z",
    })).questions[0];

    const evaluation = await evaluateAnswer(
      question,
      { status: "grounded", fallbackReason: null, maxFollowUps: 3, maxQuestions: 8, createdAt: "2026-08-29T10:00:00.000Z", questions: [question], roundId: "tech-lead", turnBudget: 8, targets: [] },
      blueprintProfile,
      "I led the checkout migration, split bundles by route, accepted extra QA during rollout, and measured a 28% bundle-size drop.",
      "interviewer: Tell me about the checkout migration.",
    );

    expect(evaluation.score).toBe(9.9);
    expect(evaluation.relevance).toBe(9.9);
    expect(evaluation.supportedClaims).toEqual(["I led the checkout migration and measured the rollout."]);
    expect(evaluation.expectedSignalsPresent).toEqual(["ownership", "trade-off", "impact"]);
    expect(evaluation.improvedAnswer).toContain("checkout migration");
    expect(evaluation.dimensionReasons.relevance).toContain("checkout migration");
  });

  it("falls back to a deterministic evaluation when the model omits any required dimension", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              read: "answered",
              evaluation: {
                score: 8.4,
                competency: "Ignored by normalization",
                dimensions: { structure: 9, tradeOffAwareness: 8 },
                strengths: ["Specific trade-off framing"],
                needsWork: ["Quantify the rollout risk"],
                missingPoints: ["Explain the rollback trigger."],
                betterStructure: ["Start with constraints, then explain the migration phases."],
                improvedAnswer: "I would start with the constraints, phase the migration, and define the rollback trigger before rollout.",
              },
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const answeredQuestion = planned({ id: "question-1", sequence: 1, category: "experience" });

    const evaluation = await evaluateAnswer(
      answeredQuestion,
      null,
      { role: "Frontend Engineer", seniority: "Senior", expertise: ["React"], narrative: "Owns frontend platforms." },
      "I phased the rollout carefully, compared alternatives with the team, made the trade-off explicit, and measured the impact after each milestone. ".repeat(2),
      "",
    );

    expect(Object.keys(evaluation.dimensions).sort()).toEqual([...dimensionKeys].sort());
    expect(evaluation.dimensions.structure).not.toBe(9);
  });

  it("sends PDF input without unsupported sampling parameters", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: "Extracted CV text with enough detail to pass the minimum response length requirement for this test." }] } }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await extractPdfText(new File(["%PDF-1.7 test"], "cv.pdf", { type: "application/pdf" }));

    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/models/gemini-3.6-flash:generateContent");
    const payload = JSON.parse(String(init?.body));
    expect(payload.generationConfig).toBeUndefined();
  });

  it("surfaces a sanitized Gemini status and provider message", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      error: { code: 400, message: "Unknown name temperature. key=private-test-key" },
    }), { status: 400, headers: { "Content-Type": "application/json" } }));

    await expect(extractPdfText(
      new File(["%PDF-1.7 test"], "cv.pdf", { type: "application/pdf" }),
    )).rejects.toThrow("Gemini rejected the PDF (400): Unknown name temperature. key=[redacted]");
  });

  it("logs a safe operation label when PDF extraction is rate limited", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    const consoleWarn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      error: { code: 429, message: "Too many requests key=private-test-key" },
    }), { status: 429, headers: { "Content-Type": "application/json" } }));

    await expect(extractPdfText(
      new File(["%PDF-1.7 test"], "cv.pdf", { type: "application/pdf" }),
    )).rejects.toThrow("Gemini rate limit reached for the PDF (429)");
    expect(consoleWarn).toHaveBeenCalledWith("[gemini] request failed", expect.objectContaining({
      operation: "the PDF",
      state: "rate-limited",
      status: 429,
      model: "gemini-3.6-flash",
    }));
    expect(JSON.stringify(consoleWarn.mock.calls)).not.toContain("private-test-key");
    consoleWarn.mockRestore();
  });

  it("rejects PDFs above the safe Vercel request limit before calling Gemini", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const oversizedPdf = new File(
      [new Uint8Array(4 * 1024 * 1024 + 1)],
      "large-cv.pdf",
      { type: "application/pdf" },
    );

    await expect(extractPdfText(oversizedPdf)).rejects.toThrow("under 4 MB");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("extractEngineeringEvidence", () => {
  it("verifies Gemini evidence against the supplied text and nulls unsupported facts", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify([
              {
                id: "evidence-1",
                sourceKind: "cv",
                sourceExcerpt: "Led a React migration for the checkout flow.",
                projectOrEmployer: "Checkout Platform",
                ownership: "Owned the frontend migration end to end.",
                technologies: ["React", "TypeScript"],
                decision: "Split a large route into smaller bundles.",
                constraint: "Tight launch window.",
                outcome: "Cut bundle size by 28%.",
                recency: "2025-02",
                confidence: 0.94,
              },
            ]),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(extractEngineeringEvidence(
      "I led a React migration for checkout.",
      "",
    )).resolves.toEqual([expect.objectContaining({
      id: "evidence-1",
      sourceKind: "cv",
      sourceExcerpt: "I led a React migration for checkout.",
      projectOrEmployer: null,
      ownership: null,
      technologies: ["React"],
      decision: null,
      constraint: null,
      outcome: null,
      recency: null,
      confidence: 0.94,
    })]);
  });
});

describe("generateInterviewBlueprint", () => {
  it("returns a validated grounded blueprint that preserves evidence ids and objectives", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              status: "grounded",
              fallbackReason: null,
              maxFollowUps: 3,
              maxQuestions: 8,
              questions: [
                {
                  sequence: 1,
                  category: "introduction",
                  competencyName: null,
                  difficulty: "senior",
                  objective: "Establish recent engineering ownership.",
                  evidenceIds: [],
                  expectedSignals: ["role summary", "recent ownership"],
                  missingSignalPrompts: ["Name the most recent engineering area you owned."],
                  rubricCriteria: [
                    "Establish the candidate's recent engineering ownership.",
                    "Keep the summary grounded in the role context.",
                    "Do not drift into unrelated background details.",
                  ],
                  followUpLimit: 0,
                  prompt: "Give me a concise introduction to yourself and the frontend work you have owned recently.",
                  sourceConfidence: null,
                },
                {
                  sequence: 2,
                  category: "experience",
                  competencyId: "react",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "Probe the checkout migration ownership and impact.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["role", "trade-off", "outcome"],
                  missingSignalPrompts: ["Name the launch trade-off you accepted."],
                  rubricCriteria: [
                    "Name the project or work example in Checkout Platform.",
                    "Describe the candidate's role and ownership.",
                    "Explain the decision, trade-off, and outcome.",
                  ],
                  followUpLimit: 1,
                  prompt: "Tell me about the Checkout Platform migration.",
                  sourceConfidence: 0.94,
                },
                {
                  sequence: 3,
                  category: "technical",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "Probe the migration trade-off decision.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["decision", "constraint", "trade-off"],
                  missingSignalPrompts: ["What trade-off did you reject?"],
                  rubricCriteria: [
                    "Name the technical decision being discussed in React architecture.",
                    "Explain the constraint or rejected alternative.",
                    "Describe the trade-off and result.",
                  ],
                  followUpLimit: 1,
                  prompt: "Walk me through the route-splitting decision.",
                  sourceConfidence: 0.94,
                },
                {
                  sequence: 4,
                  category: "architecture",
                  competencyName: "System design",
                  difficulty: "senior",
                  objective: "Probe observability system design choices.",
                  evidenceIds: ["evidence-2"],
                  expectedSignals: ["requirements", "signal design", "constraint"],
                  missingSignalPrompts: ["Which alert trade-off mattered most?"],
                  rubricCriteria: [
                    "Explain the requirements or constraints that shaped System design.",
                    "Describe the system-level decision or architecture choice.",
                    "State the outcome or reliability impact.",
                  ],
                  followUpLimit: 1,
                  prompt: "How did you shape observability for API regressions?",
                  sourceConfidence: 0.91,
                },
                {
                  sequence: 5,
                  category: "behavioral",
                  competencyName: "System design",
                  difficulty: "senior",
                  objective: "Probe cross-functional delivery around observability work.",
                  evidenceIds: ["evidence-2"],
                  expectedSignals: ["collaboration", "decision", "impact"],
                  missingSignalPrompts: ["Who disagreed and how did you resolve it?"],
                  rubricCriteria: [
                    "Name the collaboration challenge around System design.",
                    "Describe how the team aligned on the decision.",
                    "State what changed because of the collaboration.",
                  ],
                  followUpLimit: 0,
                  prompt: "How did you align the team on the release-health dashboards?",
                  sourceConfidence: 0.91,
                },
              ],
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const blueprint = await generateInterviewBlueprint(blueprintProfile, blueprintEvidence, { roundId: "tech-lead", opportunity: null });

    expect(blueprint).toMatchObject({
      status: "grounded",
      roundId: "tech-lead",
      turnBudget: 8,
      questions: expect.arrayContaining([
        expect.objectContaining({
          sequence: 2,
          competencyId: "react",
          evidenceIds: ["evidence-1"],
          objective: "Probe the checkout migration ownership and impact.",
        }),
      ]),
    });
    // The coverage plan (spec §9.1) must land on the model-validated success
    // path too, not only on the discovery and fallback paths -- this is the
    // path most likely to omit it silently, since the model's own JSON never
    // includes targets.
    expect(blueprint.targets.length).toBeGreaterThan(0);
    expect(blueprint.targets.some((target) => target.required)).toBe(true);
    expect(fetchSpy).toHaveBeenCalled();
  });

  it("uses deterministic discovery planning when source readiness is incomplete", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    const fetchSpy = vi.spyOn(globalThis, "fetch");

    const result = await generateInterviewBlueprint(
      {
        ...blueprintProfile,
        competencies: [{ name: "React", relevance: 1 }],
      },
      [],
      { roundId: "tech-lead", opportunity: null },
    );

    expect(result.status).toBe("limited-grounding");
    expect(result.questions).toHaveLength(5);
    expect(result.roundId).toBe("tech-lead");
    expect(result.turnBudget).toBe(8);
    expect(result.targets.length).toBeGreaterThan(0);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("retries once when the first blueprint response fails validation", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(new Response(JSON.stringify({
        candidates: [{
          content: {
            parts: [{
              text: JSON.stringify({
                status: "grounded",
                fallbackReason: null,
                maxFollowUps: 3,
                maxQuestions: 8,
                questions: [{
                  sequence: 1,
                  category: "experience",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "",
                  evidenceIds: ["missing-evidence"],
                  expectedSignals: [],
                  missingSignalPrompts: [],
                  followUpLimit: 4,
                  prompt: "bad",
                  sourceConfidence: 0.9,
                }],
              }),
            }],
          },
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        candidates: [{
          content: {
            parts: [{
              text: JSON.stringify({
                status: "grounded",
                fallbackReason: null,
                maxFollowUps: 3,
                maxQuestions: 8,
                questions: [
                  {
                    sequence: 1,
                    category: "introduction",
                    competencyName: null,
                    difficulty: "senior",
                  objective: "Establish recent engineering ownership.",
                  evidenceIds: [],
                  expectedSignals: ["role summary"],
                  missingSignalPrompts: ["Name the area you owned."],
                  rubricCriteria: [
                    "Establish the candidate's recent engineering ownership.",
                    "Keep the summary grounded in the role context.",
                    "Do not drift into unrelated background details.",
                  ],
                  followUpLimit: 0,
                  prompt: "Give me a concise introduction to yourself and the frontend work you have owned recently.",
                  sourceConfidence: null,
                },
                  {
                    sequence: 2,
                    category: "experience",
                    competencyName: "React architecture",
                    difficulty: "senior",
                  objective: "Probe the checkout migration ownership and impact.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["role", "outcome"],
                  missingSignalPrompts: ["Name the trade-off."],
                  rubricCriteria: [
                    "Name the project or work example in Checkout Platform.",
                    "Describe the candidate's role and ownership.",
                    "Explain the decision, trade-off, and outcome.",
                  ],
                  followUpLimit: 1,
                  prompt: "Tell me about the Checkout Platform migration.",
                  sourceConfidence: 0.94,
                },
                  {
                    sequence: 3,
                    category: "technical",
                    competencyName: "React architecture",
                    difficulty: "senior",
                  objective: "Probe the route-splitting decision.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["decision", "constraint"],
                  missingSignalPrompts: ["What option did you reject?"],
                  rubricCriteria: [
                    "Name the technical decision being discussed in React architecture.",
                    "Explain the constraint or rejected alternative.",
                    "Describe the trade-off and result.",
                  ],
                  followUpLimit: 1,
                  prompt: "Walk me through the route-splitting decision.",
                  sourceConfidence: 0.94,
                },
                  {
                    sequence: 4,
                    category: "architecture",
                    competencyName: "System design",
                    difficulty: "senior",
                  objective: "Probe the observability system design choices.",
                  evidenceIds: ["evidence-2"],
                  expectedSignals: ["requirements", "signal design"],
                  missingSignalPrompts: ["How did you tune the alerts?"],
                  rubricCriteria: [
                    "Explain the requirements or constraints that shaped System design.",
                    "Describe the system-level decision or architecture choice.",
                    "State the outcome or reliability impact.",
                  ],
                  followUpLimit: 1,
                  prompt: "How did you shape observability for API regressions?",
                  sourceConfidence: 0.91,
                },
                  {
                    sequence: 5,
                    category: "behavioral",
                    competencyName: "System design",
                    difficulty: "senior",
                  objective: "Probe collaboration during observability delivery.",
                  evidenceIds: ["evidence-2"],
                  expectedSignals: ["collaboration", "impact"],
                  missingSignalPrompts: ["Who did you need alignment from?"],
                  rubricCriteria: [
                    "Name the collaboration challenge around System design.",
                    "Describe how the team aligned on the decision.",
                    "State what changed because of the collaboration.",
                  ],
                  followUpLimit: 0,
                  prompt: "How did you align the team on release health?",
                  sourceConfidence: 0.91,
                },
                ],
              }),
            }],
          },
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const blueprint = await generateInterviewBlueprint(blueprintProfile, blueprintEvidence, { roundId: "tech-lead", opportunity: null });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(blueprint.status).toBe("grounded");
    expect(blueprint.questions[1].evidenceIds).toEqual(["evidence-1"]);
  });

  it("falls back to a limited-grounding blueprint after two invalid model responses", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    vi.spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(new Response(JSON.stringify({ candidates: [{ content: { parts: [{ text: "{}" }] } }] }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ candidates: [{ content: { parts: [{ text: "{}" }] } }] }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const blueprint = await generateInterviewBlueprint(blueprintProfile, blueprintEvidence, { roundId: "tech-lead", opportunity: null });

    expect(blueprint.status).toBe("limited-grounding");
    expect(blueprint.fallbackReason).toContain("Gemini");
    expect(blueprint.questions).toHaveLength(5);
    expect(blueprint.questions[1].evidenceIds).toEqual(["evidence-1"]);
  });

  it.each([
    ["the discovery path", [] as EvidenceItem[]],
    ["the deterministic fallback path", blueprintEvidence],
  ])("refuses an empty coverage plan on %s", async (_label, evidence) => {
    // `buildCoverageTargets` returns nothing for a profile with no
    // competencies and no anchored gaps. Without validation here the empty
    // plan reached `createSessionWithBlueprint`, which validates
    // `blueprint.questions` rather than `targets`: it inserted an active
    // session with zero question rows and only then failed in `openingTurn`,
    // leaving the orphan behind.
    vi.stubEnv("GEMINI_API_KEY", "");

    await expect(generateInterviewBlueprint(
      { ...blueprintProfile, competencies: [] },
      evidence,
      { roundId: "tech-lead", opportunity: null },
    )).rejects.toThrow(/at least one coverage target/);
  });
});

describe("generatePracticeBlueprint", () => {
  it.each([
    ["targeted_drill", 3],
    ["story_work", 3],
    ["self_presentation", 2],
    ["behavioral", 3],
    ["technical_communication", 3],
    ["role_prep", 4],
    ["full_simulation", 5],
  ] as const)("generates %s with %d base questions", async (format, count) => {
    const blueprint = await generatePracticeBlueprint(
      practiceProfile,
      blueprintEvidence,
      practicePlan({ format }),
      practiceContext,
    );

    expect(blueprint.questions).toHaveLength(count);
    // Ruling R5: maxQuestions must always leave follow-up headroom above the
    // base question count -- the persisted floor in the Task 2 migration
    // would otherwise refuse every follow-up on a plan-driven session.
    expect(blueprint.maxQuestions).toBeGreaterThan(blueprint.questions.length);
  });

  it("rejects an AI response that exceeds the plan's base question count and falls back instead of silently expanding", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    // role_prep's base count is 4 (baseQuestionCountFor). This response
    // returns 5 -- schema-valid on its own (practiceBlueprintDraftSchema
    // allows up to 5 questions), but it must still be rejected by
    // validatePracticeBlueprint's over-count check and never silently
    // accepted as a 5-question role_prep blueprint.
    const overCountQuestion = (sequence: number) => ({
      sequence,
      category: "technical",
      competencyName: "React architecture",
      difficulty: "senior",
      objective: `Probe decision ${sequence}.`,
      evidenceIds: ["evidence-1"],
      expectedSignals: ["decision", "trade-off"],
      missingSignalPrompts: ["Name the trade-off."],
      rubricCriteria: ["Name the decision.", "Explain the trade-off."],
      followUpLimit: 1,
      prompt: `Walk me through decision ${sequence}.`,
      sourceConfidence: 0.9,
    });
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              status: "grounded",
              fallbackReason: null,
              maxFollowUps: 2,
              maxQuestions: 7,
              questions: [1, 2, 3, 4, 5].map(overCountQuestion),
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const blueprint = await generatePracticeBlueprint(
      practiceProfile,
      blueprintEvidence,
      practicePlan({ format: "role_prep" }),
      practiceContext,
    );

    expect(fetchSpy).toHaveBeenCalledTimes(2); // one attempt, one repair retry, both over-count
    expect(blueprint.questions).toHaveLength(4);
    expect(blueprint.status).toBe("limited-grounding");
  });

  it("accepts a conforming AI response sized to a second (non-role_prep) format's base count", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    // self_presentation's base count is 2 -- drives the AI path (not the
    // fallback) for a second format so the count matrix above is not
    // exclusively exercising buildFallbackPracticeBlueprint.
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              status: "grounded",
              fallbackReason: null,
              maxFollowUps: 1,
              maxQuestions: 3,
              questions: [
                {
                  sequence: 1,
                  category: "introduction",
                  competencyName: null,
                  difficulty: "senior",
                  objective: "Establish recent engineering ownership.",
                  evidenceIds: [],
                  expectedSignals: ["role summary", "recent ownership"],
                  missingSignalPrompts: ["Name the most recent engineering area you owned."],
                  rubricCriteria: ["Establish the candidate's recent engineering ownership."],
                  followUpLimit: 0,
                  prompt: "Give me a concise introduction to yourself.",
                  sourceConfidence: null,
                },
                {
                  sequence: 2,
                  category: "experience",
                  competencyId: "react",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "Probe the checkout migration ownership and impact.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["role", "trade-off", "outcome"],
                  missingSignalPrompts: ["Name the launch trade-off you accepted."],
                  rubricCriteria: ["Name the project or work example.", "Describe ownership.", "Explain the outcome."],
                  followUpLimit: 1,
                  prompt: "Tell me about the Checkout Platform migration.",
                  sourceConfidence: 0.94,
                },
              ],
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const blueprint = await generatePracticeBlueprint(
      practiceProfile,
      blueprintEvidence,
      practicePlan({ format: "self_presentation" }),
      practiceContext,
    );

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(blueprint.status).toBe("grounded");
    expect(blueprint.questions).toHaveLength(2);
    expect(blueprint.maxQuestions).toBeGreaterThan(2);
  });

  // Observed in a live self_presentation session: evidence extracted without a
  // `projectOrEmployer` collapsed the fallback subject onto `plan.primaryFocus`,
  // producing "Probe Build your self-presentation foundation using Build your
  // self-presentation foundation." and a prompt that named the focus twice.
  it("names an evidence anchor other than the focus when the fallback evidence has no project or employer", async () => {
    vi.stubEnv("GEMINI_API_KEY", "");
    const anchorlessEvidence: EvidenceItem[] = [{
      ...blueprintEvidence[0],
      projectOrEmployer: null,
      ownership: "Owned frontend architecture",
    }];

    const blueprint = await generatePracticeBlueprint(
      practiceProfile,
      anchorlessEvidence,
      practicePlan({ format: "self_presentation", primaryFocus: "Build your self-presentation foundation" }),
      practiceContext,
    );

    expect(blueprint.status).toBe("limited-grounding");
    const grounded = blueprint.questions.filter((question) => question.category !== "introduction");
    expect(grounded.length).toBeGreaterThan(0);
    for (const question of grounded) {
      expect(question.objective).toContain("Owned frontend architecture");
      expect(question.objective.match(/Build your self-presentation foundation/g) ?? []).toHaveLength(1);
      expect(question.prompt!.match(/Build your self-presentation foundation/g) ?? []).toHaveLength(1);
    }
  });

  it("constrains Gemini decoding with the blueprint schema so out-of-enum values cannot be returned", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    // Without a response schema Gemini honours only the JSON mime type and
    // invents plausible-but-invalid enum values (observed in production:
    // `difficulty: "medium"`), which fails schema.parse on both the first and
    // the repair attempt and silently degrades every session to the
    // deterministic fallback blueprint.
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: "{}" }] } }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await generatePracticeBlueprint(
      practiceProfile,
      blueprintEvidence,
      practicePlan({ format: "self_presentation" }),
      practiceContext,
    );

    const payload = JSON.parse(String(fetchSpy.mock.calls[0]?.[1]?.body));
    const responseJsonSchema = payload.generationConfig.responseJsonSchema;
    expect(payload.generationConfig.responseMimeType).toBe("application/json");
    expect(responseJsonSchema.$schema).toBeUndefined();
    expect(responseJsonSchema.properties.questions.items.properties.difficulty.enum)
      .toEqual(["foundational", "intermediate", "senior", "advanced"]);
    expect(responseJsonSchema.properties.questions.maxItems).toBe(5);
  });

  it("keeps evidence ids traceable to candidate evidence when job-description requirements shape the prompt", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    const opportunity: Opportunity = {
      id: "opportunity-1",
      userId: "user-1",
      company: "Northwind",
      role: "Senior Frontend Engineer",
      status: "interviewing",
      location: null,
      remote: null,
      jobUrl: null,
      jobDescription: "Requires deep experience leading large-scale React migrations and mentoring engineers on rollout risk.",
      sourceLabel: null,
      sourceSystem: null,
      sourceExternalId: null,
      matchScore: null,
      strengths: [],
      gaps: [],
      notes: null,
      appliedAt: null,
      nextInterviewAt: null,
      createdAt: "2026-08-29T10:00:00.000Z",
      updatedAt: "2026-08-29T10:00:00.000Z",
    };
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{
        content: {
          parts: [{
            text: JSON.stringify({
              status: "grounded",
              fallbackReason: null,
              maxFollowUps: 2,
              maxQuestions: 6,
              questions: [
                {
                  sequence: 1,
                  category: "introduction",
                  competencyName: null,
                  difficulty: "senior",
                  objective: "Establish recent engineering ownership relevant to the role.",
                  evidenceIds: [],
                  expectedSignals: ["role summary", "recent ownership"],
                  missingSignalPrompts: ["Name the most recent engineering area you owned."],
                  rubricCriteria: ["Establish the candidate's recent engineering ownership."],
                  followUpLimit: 0,
                  prompt: "Give me a concise introduction focused on leading React migrations.",
                  sourceConfidence: null,
                },
                {
                  sequence: 2,
                  category: "experience",
                  competencyId: "react",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "Probe the checkout migration ownership and impact.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["role", "trade-off", "outcome"],
                  missingSignalPrompts: ["Name the launch trade-off you accepted."],
                  rubricCriteria: ["Name the project or work example.", "Describe ownership.", "Explain the outcome."],
                  followUpLimit: 1,
                  prompt: "Tell me about the Checkout Platform migration.",
                  sourceConfidence: 0.94,
                },
                {
                  sequence: 3,
                  category: "technical",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "Probe the migration trade-off decision.",
                  evidenceIds: ["evidence-1"],
                  expectedSignals: ["decision", "constraint", "trade-off"],
                  missingSignalPrompts: ["What trade-off did you reject?"],
                  rubricCriteria: ["Name the technical decision.", "Explain the constraint.", "Describe the trade-off."],
                  followUpLimit: 1,
                  prompt: "Walk me through the route-splitting decision.",
                  sourceConfidence: 0.94,
                },
                {
                  sequence: 4,
                  category: "architecture",
                  competencyName: "React architecture",
                  difficulty: "senior",
                  objective: "Probe rollout mentoring readiness for large-scale migrations.",
                  evidenceIds: ["evidence-2"],
                  expectedSignals: ["requirements", "mentoring", "constraint"],
                  missingSignalPrompts: ["Who did you mentor through the rollout?"],
                  rubricCriteria: ["Explain the requirements.", "Describe the design choice.", "State the outcome."],
                  followUpLimit: 0,
                  prompt: "How would you mentor a team through a large-scale React migration rollout?",
                  sourceConfidence: 0.91,
                },
              ],
            }),
          }],
        },
      }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const blueprint = await generatePracticeBlueprint(
      practiceProfile,
      blueprintEvidence,
      practicePlan({ format: "role_prep" }),
      { ...practiceContext, primaryOpportunity: opportunity },
    );

    const requestBody = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
    expect(requestBody.contents[0].parts[0].text).toContain("leading large-scale React migrations");
    expect(requestBody.contents[0].parts[0].text).toContain("Job requirements are targets to probe, not candidate evidence.");
    expect(requestBody.contents[0].parts[0].text).toContain("Candidate factual claims must be grounded in supplied evidence or confirmed story facts.");
    expect(requestBody.contents[0].parts[0].text).toContain("Do not invent company interview-process facts.");

    const knownEvidenceIds = new Set(blueprintEvidence.map((item) => item.id));
    for (const question of blueprint.questions) {
      for (const evidenceId of question.evidenceIds) {
        expect(knownEvidenceIds.has(evidenceId)).toBe(true);
      }
    }
  });

  it("only grounds on confirmed or corrected observations and confirmed stories, preferring a user correction over the original claim", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
    vi.stubEnv("GEMINI_MODEL", "models/gemini-3.6-flash");
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: "{}" }] } }],
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const context: PracticeBlueprintContext = {
      primaryOpportunity: null,
      supportingOpportunities: [],
      observations: [
        {
          id: "observation-unreviewed",
          userId: "user-1",
          observationType: "weakness",
          claim: "Skips trade-offs entirely, never seen this candidate mention one.",
          confidence: 0.8,
          importance: 0.9,
          trend: "unresolved",
          reviewState: "unreviewed",
          userCorrection: null,
          firstSeenAt: null,
          lastSeenAt: null,
          confirmedAt: null,
          correctedAt: null,
          dismissedAt: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          updatedAt: "2026-08-29T10:00:00.000Z",
        },
        {
          id: "observation-corrected",
          userId: "user-1",
          observationType: "weakness",
          claim: "Never explains trade-offs.",
          confidence: 0.8,
          importance: 0.9,
          trend: "unresolved",
          reviewState: "corrected",
          userCorrection: "Explains trade-offs but rushes the outcome.",
          firstSeenAt: null,
          lastSeenAt: null,
          confirmedAt: null,
          correctedAt: "2026-08-29T10:00:00.000Z",
          dismissedAt: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          updatedAt: "2026-08-29T10:00:00.000Z",
        },
      ],
      stories: [
        {
          id: "story-draft",
          userId: "user-1",
          title: "Unfinished draft story, never seen elsewhere in this test.",
          situation: null,
          responsibility: null,
          problem: null,
          actions: null,
          alternatives: null,
          tradeoffs: null,
          ownership: null,
          outcome: null,
          lessons: null,
          tags: [],
          completeness: 0.2,
          reviewState: "draft",
          confirmedAt: null,
          createdAt: "2026-08-29T10:00:00.000Z",
          updatedAt: "2026-08-29T10:00:00.000Z",
        },
        {
          id: "story-confirmed",
          userId: "user-1",
          title: "Checkout migration rollback story",
          situation: "Rollout risk on the checkout migration.",
          responsibility: "Owned the rollback plan.",
          problem: null,
          actions: null,
          alternatives: null,
          tradeoffs: null,
          ownership: null,
          outcome: null,
          lessons: null,
          tags: [],
          completeness: 0.9,
          reviewState: "confirmed",
          confirmedAt: "2026-08-29T10:00:00.000Z",
          createdAt: "2026-08-29T10:00:00.000Z",
          updatedAt: "2026-08-29T10:00:00.000Z",
        },
      ],
    };

    await generatePracticeBlueprint(practiceProfile, blueprintEvidence, practicePlan(), context);

    const requestBody = JSON.parse(String(fetchSpy.mock.calls[0][1]?.body));
    const promptText = requestBody.contents[0].parts[0].text as string;
    expect(promptText).toContain("Explains trade-offs but rushes the outcome.");
    expect(promptText).not.toContain("Skips trade-offs entirely, never seen this candidate mention one.");
    expect(promptText).not.toContain("Never explains trade-offs.");
    expect(promptText).toContain("Checkout migration rollback story");
    expect(promptText).not.toContain("Unfinished draft story, never seen elsewhere in this test.");
  });
});

describe("assessProfileReadiness", () => {
  it("deduplicates equivalent evidence before counting readiness", () => {
    expect(assessProfileReadiness([
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
        sourceExcerpt: "Led a React migration for checkout.",
        projectOrEmployer: "Checkout Platform",
        ownership: "Owned the frontend migration end to end.",
        technologies: ["TypeScript", "React"],
        decision: "Split a large route into smaller bundles.",
        constraint: "Tight launch window.",
        outcome: "Cut bundle size by 28%.",
        recency: "2025-02",
        confidence: 0.94,
      },
    ])).toEqual({
      ready: false,
      missing: expect.arrayContaining([
        "two concrete engineering projects or work examples",
      ]),
    });
  });

  it("does not treat generic skill summaries as two concrete work examples", () => {
    expect(assessProfileReadiness([
      {
        id: "evidence-1",
        sourceKind: "cv",
        sourceExcerpt: "React and TypeScript developer.",
        projectOrEmployer: null,
        ownership: null,
        technologies: ["React", "TypeScript"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: null,
        confidence: 0.3,
      },
      {
        id: "evidence-2",
        sourceKind: "cv",
        sourceExcerpt: "Built user interfaces with JavaScript.",
        projectOrEmployer: null,
        ownership: null,
        technologies: ["JavaScript"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: null,
        confidence: 0.3,
      },
    ])).toEqual({
      ready: false,
      missing: expect.arrayContaining([
        "two concrete engineering projects or work examples",
        "responsibilities or outcomes",
      ]),
    });
  });

  it("rejects a generic profile with only one vague work example", () => {
    expect(assessProfileReadiness([{
      id: "evidence-1",
      sourceKind: null,
      sourceExcerpt: "Worked on a project.",
      projectOrEmployer: null,
      ownership: null,
      technologies: [],
      decision: null,
      constraint: null,
      outcome: null,
      recency: null,
      confidence: 0.2,
    }])).toEqual({
      ready: false,
      missing: expect.arrayContaining([
        "two concrete engineering projects or work examples",
        "identifiable technologies",
        "responsibilities or outcomes",
      ]),
    });
  });

  it("accepts concise evidence with an excerpt, technologies, and ownership without a duplicated project label", () => {
    expect(assessProfileReadiness([
      {
        id: "evidence-1",
        sourceKind: "cv",
        sourceExcerpt: "React checkout flow with route-splitting.",
        projectOrEmployer: null,
        ownership: "Owned the frontend migration end to end.",
        technologies: ["React", "TypeScript"],
        decision: null,
        constraint: null,
        outcome: "Cut bundle size by 28%.",
        recency: "2025-02",
        confidence: 0.94,
      },
      {
        id: "evidence-2",
        sourceKind: "cv",
        sourceExcerpt: "Observability flow for API regressions.",
        projectOrEmployer: null,
        ownership: "Designed the dashboard and alerting flow.",
        technologies: ["Next.js", "Postgres"],
        decision: null,
        constraint: null,
        outcome: "Reduced incident triage time by 35%.",
        recency: "2024-11",
        confidence: 0.91,
      },
    ])).toEqual({
      ready: true,
      missing: [],
    });
  });

  it("accepts concise bullets when the source excerpt itself carries the action and outcome", () => {
    expect(assessProfileReadiness([
      {
        id: "evidence-1",
        sourceKind: "cv",
        sourceExcerpt: "Created a React checkout flow and cut bundle size by 28%.",
        projectOrEmployer: null,
        ownership: null,
        technologies: ["React", "TypeScript"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: "2025-02",
        confidence: 0.94,
      },
      {
        id: "evidence-2",
        sourceKind: "cv",
        sourceExcerpt: "Refactored API observability dashboards and reduced triage time by 35%.",
        projectOrEmployer: null,
        ownership: null,
        technologies: ["Next.js", "Postgres"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: "2024-11",
        confidence: 0.91,
      },
    ])).toEqual({
      ready: true,
      missing: [],
    });
  });

  it("rejects employer-label-only evidence even when technologies are listed", () => {
    expect(assessProfileReadiness([
      {
        id: "evidence-1",
        sourceKind: "cv",
        sourceExcerpt: "Checkout Platform",
        projectOrEmployer: "Checkout Platform",
        ownership: null,
        technologies: ["React", "TypeScript"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: null,
        confidence: 0.3,
      },
      {
        id: "evidence-2",
        sourceKind: "cv",
        sourceExcerpt: "Reliability Tooling",
        projectOrEmployer: "Reliability Tooling",
        ownership: null,
        technologies: ["Next.js"],
        decision: null,
        constraint: null,
        outcome: null,
        recency: null,
        confidence: 0.35,
      },
    ])).toEqual({
      ready: false,
      missing: expect.arrayContaining([
        "two concrete engineering projects or work examples",
        "responsibilities or outcomes",
      ]),
    });
  });

  it("rejects labeled projects with metrics when no action or ownership semantics are present", () => {
    expect(assessProfileReadiness([
      {
        id: "evidence-1",
        sourceKind: "cv",
        sourceExcerpt: "Checkout Platform",
        projectOrEmployer: "Checkout Platform",
        ownership: null,
        technologies: ["React", "TypeScript"],
        decision: null,
        constraint: null,
        outcome: "28% faster.",
        recency: null,
        confidence: 0.35,
      },
      {
        id: "evidence-2",
        sourceKind: "cv",
        sourceExcerpt: "Reliability Tooling",
        projectOrEmployer: "Reliability Tooling",
        ownership: null,
        technologies: ["Next.js", "Postgres"],
        decision: null,
        constraint: null,
        outcome: "35% lower triage time.",
        recency: null,
        confidence: 0.35,
      },
    ])).toEqual({
      ready: false,
      missing: expect.arrayContaining([
        "two concrete engineering projects or work examples",
        "responsibilities or outcomes",
      ]),
    });
  });

  it("accepts two concrete engineering projects with technologies and outcomes", () => {
    expect(assessProfileReadiness([
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
    ])).toEqual({
      ready: true,
      missing: [],
    });
  });
});

describe("speakIntent", () => {
  it("never puts rubric text in the interviewer prompt", async () => {
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
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
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
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
    vi.stubEnv("GEMINI_API_KEY", "private-test-key");
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

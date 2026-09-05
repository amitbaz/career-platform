import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  InterviewBlueprint,
  InterviewSession,
  Opportunity,
  PracticePlan,
  PracticePlanStatus,
  PracticeRecommendation,
  Profile,
} from "@/lib/types";

const mocks = vi.hoisted(() => ({
  getProfile: vi.fn(),
  listOpportunities: vi.fn(),
  listCoachObservations: vi.fn(),
  listCareerStories: vi.fn(),
  listRecentSessions: vi.fn(),
  listReadinessEvidence: vi.fn(),
  listPracticePlans: vi.fn(),
  createPracticePlan: vi.fn(),
  getPracticePlan: vi.fn(),
  setPracticePlanOpportunities: vi.fn(),
  updatePracticePlan: vi.fn(),
  recommendPractice: vi.fn(),
  generatePracticeBlueprint: vi.fn(),
  handsOnExercise: vi.fn(),
  createSessionWithPracticeBlueprint: vi.fn(),
  createHandsOnPracticeSession: vi.fn(),
}));

vi.mock("@/lib/repositories/profile", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/profile")>("@/lib/repositories/profile");
  return { ...actual, getProfile: mocks.getProfile };
});
vi.mock("@/lib/repositories/opportunities", () => ({ listOpportunities: mocks.listOpportunities }));
vi.mock("@/lib/repositories/observations", () => ({ listCoachObservations: mocks.listCoachObservations }));
vi.mock("@/lib/repositories/stories", () => ({ listCareerStories: mocks.listCareerStories }));
vi.mock("@/lib/repositories/interviews", () => ({
  listRecentSessions: mocks.listRecentSessions,
  listReadinessEvidence: mocks.listReadinessEvidence,
  createSessionWithPracticeBlueprint: mocks.createSessionWithPracticeBlueprint,
  createHandsOnPracticeSession: mocks.createHandsOnPracticeSession,
}));
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
vi.mock("@/lib/practice-recommendation", () => ({ recommendPractice: mocks.recommendPractice }));
vi.mock("@/lib/coach", () => ({
  generatePracticeBlueprint: mocks.generatePracticeBlueprint,
  handsOnExercise: mocks.handsOnExercise,
}));

import {
  PracticeServiceError,
  completeLinkedPracticePlanBestEffort,
  loadPracticeInputs,
  loadPracticeOverview,
  startManualPractice,
  startRecommendedPractice,
} from "@/lib/practice-service";
import { RepositoryError } from "@/lib/repositories/profile";

const now = new Date("2026-08-31T09:00:00.000Z");

const profile: Profile = {
  userId: "user-1",
  role: "Frontend Engineer",
  seniority: "Senior",
  summary: "Frontend engineer",
  narrative: "Owns frontend platforms.",
  expertise: ["React"],
  characteristics: ["Pragmatic"],
  competencies: [],
  evidence: [
    {
      id: "evidence-1",
      sourceKind: "cv",
      sourceExcerpt: "Led the checkout migration.",
      projectOrEmployer: "Acme",
      ownership: "Led the migration",
      technologies: ["React"],
      decision: "Chose incremental rollout",
      constraint: "No downtime",
      outcome: "Cut checkout errors by 30%",
      recency: "2026",
      confidence: 0.9,
    },
  ],
  source: { cvText: "At Acme I led a React migration.", coverLetter: "" },
  readiness: { ready: true, missing: [] },
  createdAt: "2026-08-29T10:00:00.000Z",
  updatedAt: "2026-08-29T10:00:00.000Z",
};

function opportunity(id: string, overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id,
    userId: "user-1",
    company: `Company ${id}`,
    role: "Senior Frontend Engineer",
    status: "interviewing",
    location: null,
    remote: null,
    jobUrl: null,
    jobDescription: "Own the design system.",
    sourceLabel: null,
    sourceSystem: "manual",
    sourceExternalId: null,
    matchScore: null,
    strengths: [],
    gaps: [],
    notes: null,
    appliedAt: null,
    nextInterviewAt: "2026-09-02T09:00:00.000Z",
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

function plan(status: PracticePlanStatus, overrides: Partial<PracticePlan> = {}): PracticePlan {
  return {
    id: "plan-1",
    userId: "user-1",
    status,
    primaryFocus: "Prepare for the Company opp-1 interview",
    secondaryFocus: null,
    rationale: "Your interview is in two days.",
    format: "role_prep",
    estimatedMinutes: 18,
    successCriteria: ["Answer role-specific questions grounded in the job description."],
    priorityScore: null,
    priorityFactors: {},
    generationError: null,
    completedAt: null,
    createdAt: "2026-08-31T09:00:00.000Z",
    updatedAt: "2026-08-31T09:00:00.000Z",
    opportunities: [],
    ...overrides,
  };
}

const primaryLink = {
  userId: "user-1",
  practicePlanId: "plan-1",
  opportunityId: "opp-1",
  relevance: "primary" as const,
  createdAt: "2026-08-31T09:00:00.000Z",
};

const supportingLink = { ...primaryLink, opportunityId: "opp-2", relevance: "supporting" as const };

const recommendation: PracticeRecommendation = {
  format: "role_prep",
  primaryFocus: "Prepare for the Company opp-1 interview",
  secondaryFocus: null,
  rationale: "Your interview is in two days.",
  estimatedMinutes: 18,
  successCriteria: ["Answer role-specific questions grounded in the job description."],
  primaryOpportunityId: "opp-1",
  supportingOpportunityIds: ["opp-2"],
  signals: [{ kind: "upcoming_interview", label: "upcoming interview", detail: "Company opp-1 · in 2 days" }],
};

const blueprint: InterviewBlueprint = {
  status: "grounded",
  fallbackReason: null,
  maxFollowUps: 2,
  maxQuestions: 6,
  createdAt: "2026-08-31T09:00:00.000Z",
  questions: [],
  roundId: "tech-lead",
  turnBudget: 8,
  targets: [],
};

function session(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    degraded: false,
    status: "active",
    startedAt: "2026-08-31T09:00:00.000Z",
    completedAt: null,
    exercise: {},
    resultSummary: {},
    overallScore: null,
    questions: [],
    checkpoints: [],
    evaluations: [],
    messages: [],
    createdAt: "2026-08-31T09:00:00.000Z",
    updatedAt: "2026-08-31T09:00:00.000Z",
    practicePlanId: null,
    opportunityId: null,
    ...overrides,
  };
}

const supabase = { client: true };

describe("practice orchestration", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getProfile.mockResolvedValue(profile);
    mocks.listOpportunities.mockResolvedValue([opportunity("opp-1"), opportunity("opp-2")]);
    mocks.listCoachObservations.mockResolvedValue([]);
    mocks.listCareerStories.mockResolvedValue([]);
    mocks.listRecentSessions.mockResolvedValue([]);
    mocks.listPracticePlans.mockResolvedValue([]);
    mocks.listReadinessEvidence.mockResolvedValue([]);
    mocks.recommendPractice.mockReturnValue(recommendation);
    mocks.createPracticePlan.mockResolvedValue(plan("ready"));
    mocks.setPracticePlanOpportunities.mockImplementation(async () => plan("ready", {
      opportunities: [primaryLink, supportingLink],
    }));
    mocks.getPracticePlan.mockResolvedValue(plan("started", { opportunities: [primaryLink, supportingLink] }));
    mocks.updatePracticePlan.mockResolvedValue(plan("failed"));
    mocks.generatePracticeBlueprint.mockResolvedValue(blueprint);
    mocks.handsOnExercise.mockReturnValue({ title: "Accessible product search" });
    mocks.createSessionWithPracticeBlueprint.mockResolvedValue(session({
      practicePlanId: "plan-1",
      opportunityId: "opp-1",
    }));
    mocks.createHandsOnPracticeSession.mockResolvedValue(session({
      kind: "hands-on",
      practicePlanId: "plan-1",
      opportunityId: "opp-1",
    }));
  });

  it("recomputes recommended practice on the server", async () => {
    await startRecommendedPractice(supabase as never, "user-1", now);

    expect(mocks.recommendPractice).toHaveBeenCalledWith(expect.objectContaining({
      opportunities: [opportunity("opp-1"), opportunity("opp-2")],
      now,
    }));
    expect(mocks.createPracticePlan).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      expect.objectContaining({ status: "ready" }),
    );
  });

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

  it("persists the recommendation's opportunity links before starting the session", async () => {
    await startRecommendedPractice(supabase as never, "user-1", now);

    expect(mocks.setPracticePlanOpportunities).toHaveBeenCalledWith(expect.anything(), "user-1", "plan-1", [
      { opportunityId: "opp-1", relevance: "primary" },
      { opportunityId: "opp-2", relevance: "supporting" },
    ]);
    expect(mocks.setPracticePlanOpportunities.mock.invocationCallOrder[0])
      .toBeLessThan(mocks.createSessionWithPracticeBlueprint.mock.invocationCallOrder[0]);
  });

  it("passes the plan's primary opportunity into the transactional session context", async () => {
    const result = await startRecommendedPractice(supabase as never, "user-1", now);

    expect(mocks.generatePracticeBlueprint).toHaveBeenCalledWith(
      profile,
      profile.evidence,
      expect.objectContaining({ id: "plan-1" }),
      expect.objectContaining({
        primaryOpportunity: opportunity("opp-1"),
        supportingOpportunities: [opportunity("opp-2")],
      }),
    );
    expect(mocks.createSessionWithPracticeBlueprint).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      blueprint,
      { practicePlanId: "plan-1", opportunityId: "opp-1" },
    );
    expect(result.plan.status).toBe("started");
    expect(result.session.practicePlanId).toBe("plan-1");
  });

  it("dispatches hands-on through the transactional planned hands-on wrapper", async () => {
    mocks.createPracticePlan.mockResolvedValue(plan("ready", { format: "hands_on" }));
    mocks.getPracticePlan.mockResolvedValue(plan("started", { format: "hands_on" }));

    const result = await startManualPractice(supabase as never, "user-1", {
      format: "hands_on",
      primaryFocus: "React implementation",
    });

    expect(mocks.handsOnExercise).toHaveBeenCalledWith(profile);
    expect(mocks.createHandsOnPracticeSession).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      { title: "Accessible product search" },
      { practicePlanId: "plan-1", opportunityId: null },
    );
    expect(mocks.generatePracticeBlueprint).not.toHaveBeenCalled();
    expect(result.session.kind).toBe("hands-on");
  });

  it("never recomputes a recommendation for manual practice", async () => {
    mocks.createPracticePlan.mockResolvedValue(plan("ready", { format: "targeted_drill" }));

    await startManualPractice(supabase as never, "user-1", {
      format: "targeted_drill",
      primaryFocus: "Architecture decision framing",
      estimatedMinutes: 12,
      primaryOpportunityId: null,
    });

    expect(mocks.recommendPractice).not.toHaveBeenCalled();
    expect(mocks.createPracticePlan).toHaveBeenCalledWith(expect.anything(), "user-1", expect.objectContaining({
      status: "ready",
      format: "targeted_drill",
      primaryFocus: "Architecture decision framing",
      estimatedMinutes: 12,
    }));
    expect(mocks.setPracticePlanOpportunities).not.toHaveBeenCalled();
  });

  it("links a manually chosen owned opportunity as the plan's primary", async () => {
    mocks.setPracticePlanOpportunities.mockResolvedValue(plan("ready", { opportunities: [primaryLink] }));
    mocks.getPracticePlan.mockResolvedValue(plan("started", { opportunities: [primaryLink] }));

    await startManualPractice(supabase as never, "user-1", {
      format: "role_prep",
      primaryFocus: "Company opp-1 role prep",
      primaryOpportunityId: "opp-1",
    });

    expect(mocks.setPracticePlanOpportunities).toHaveBeenCalledWith(expect.anything(), "user-1", "plan-1", [
      { opportunityId: "opp-1", relevance: "primary" },
    ]);
    expect(mocks.createSessionWithPracticeBlueprint).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      blueprint,
      { practicePlanId: "plan-1", opportunityId: "opp-1" },
    );
  });

  it.each([
    ["an unknown format", { format: "chit_chat" as never, primaryFocus: "Anything" }],
    ["a blank focus", { format: "targeted_drill" as const, primaryFocus: "   " }],
    ["an out-of-range duration", { format: "targeted_drill" as const, primaryFocus: "Focus", estimatedMinutes: 500 }],
    ["a non-numeric duration", { format: "targeted_drill" as const, primaryFocus: "Focus", estimatedMinutes: "12" as never }],
    ["a non-string opportunity id", { format: "targeted_drill" as const, primaryFocus: "Focus", primaryOpportunityId: 7 as never }],
  ])("rejects manual practice with %s before creating a plan", async (_label, request) => {
    await expect(startManualPractice(supabase as never, "user-1", request))
      .rejects.toMatchObject({ code: "INVALID_PRACTICE_REQUEST" });
    expect(mocks.createPracticePlan).not.toHaveBeenCalled();
  });

  it("ignores malformed success criteria instead of failing the start", async () => {
    mocks.createPracticePlan.mockResolvedValue(plan("ready", { format: "targeted_drill" }));

    await startManualPractice(supabase as never, "user-1", {
      format: "targeted_drill",
      primaryFocus: "Architecture decision framing",
      successCriteria: "not a list" as never,
    });

    expect(mocks.createPracticePlan).toHaveBeenCalledWith(expect.anything(), "user-1", expect.objectContaining({
      successCriteria: [],
    }));
  });

  it("rejects a manual primary opportunity the user does not own", async () => {
    await expect(startManualPractice(supabase as never, "user-1", {
      format: "role_prep",
      primaryFocus: "Focus",
      primaryOpportunityId: "opp-someone-else",
    })).rejects.toMatchObject({ code: "OPPORTUNITY_NOT_FOUND" });
    expect(mocks.createPracticePlan).not.toHaveBeenCalled();
  });

  it("requires a profile before creating a practice plan", async () => {
    mocks.getProfile.mockResolvedValue(null);

    await expect(startRecommendedPractice(supabase as never, "user-1", now))
      .rejects.toBeInstanceOf(PracticeServiceError);
    expect(mocks.createPracticePlan).not.toHaveBeenCalled();
  });

  it("marks the ready plan failed with a user-safe error when generation fails before a session exists", async () => {
    mocks.generatePracticeBlueprint.mockRejectedValue(new Error("gemini exploded: key sk-live-123"));

    await expect(startRecommendedPractice(supabase as never, "user-1", now)).rejects.toThrow("gemini exploded");

    expect(mocks.updatePracticePlan).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      "plan-1",
      { status: "failed", generationError: expect.not.stringContaining("sk-live-123") },
      { expectedStatus: "ready" },
    );
    expect(mocks.createSessionWithPracticeBlueprint).not.toHaveBeenCalled();
  });

  it("keeps a repository failure's user-safe message as the plan's generation error", async () => {
    mocks.createSessionWithPracticeBlueprint
      .mockRejectedValue(new RepositoryError("Could not start the planned practice session.", "22023"));

    await expect(startRecommendedPractice(supabase as never, "user-1", now)).rejects.toBeInstanceOf(RepositoryError);

    expect(mocks.updatePracticePlan).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      "plan-1",
      { status: "failed", generationError: "Could not start the planned practice session." },
      { expectedStatus: "ready" },
    );
  });

  it("cannot mark a plan failed once the start RPC has already moved it to started", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    // A start call can fail on the way back from a transaction that already
    // committed. The compensating write is scoped to `ready`, so it matches no
    // row and the plan keeps the `started` status the RPC gave it.
    mocks.createSessionWithPracticeBlueprint.mockRejectedValue(new Error("socket hang up"));
    mocks.updatePracticePlan.mockRejectedValue(new RepositoryError("Could not update the practice plan.", "NO_OWNED_ROW"));

    await expect(startRecommendedPractice(supabase as never, "user-1", now)).rejects.toThrow("socket hang up");

    expect(mocks.updatePracticePlan).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      "plan-1",
      expect.objectContaining({ status: "failed" }),
      { expectedStatus: "ready" },
    );
    expect(consoleError).toHaveBeenCalledWith(
      "[practice-service] could not mark the practice plan failed",
      expect.objectContaining({ code: "NO_OWNED_ROW" }),
    );
    consoleError.mockRestore();
  });

  it("keeps the started session when the post-start plan reload fails", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.getPracticePlan.mockRejectedValue(new RepositoryError("Could not load the practice plan.", "500"));

    const result = await startRecommendedPractice(supabase as never, "user-1", now);

    expect(result.session.id).toBe("session-1");
    expect(result.plan.status).toBe("started");
    expect(mocks.updatePracticePlan).not.toHaveBeenCalled();
    consoleError.mockRestore();
  });
});

describe("loadPracticeOverview", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getProfile.mockResolvedValue(profile);
    mocks.listOpportunities.mockResolvedValue([]);
    mocks.listCoachObservations.mockResolvedValue([]);
    mocks.listCareerStories.mockResolvedValue([]);
    mocks.listRecentSessions.mockResolvedValue([]);
    mocks.listPracticePlans.mockResolvedValue([plan("completed")]);
    mocks.listReadinessEvidence.mockResolvedValue([]);
    mocks.recommendPractice.mockReturnValue(recommendation);
  });

  it("returns the recomputed recommendation with the caller's recent plans", async () => {
    const overview = await loadPracticeOverview(supabase as never, "user-1", now);

    expect(overview.recommendation).toEqual(recommendation);
    expect(overview.plans).toEqual([plan("completed")]);
    expect(mocks.recommendPractice).toHaveBeenCalledWith(expect.objectContaining({
      recentPlans: [plan("completed")],
      now,
    }));
  });

  it("recommends without a profile instead of failing the read", async () => {
    mocks.getProfile.mockResolvedValue(null);

    const overview = await loadPracticeOverview(supabase as never, "user-1", now);

    expect(overview.recommendation).toEqual(recommendation);
  });
});

describe("loadPracticeInputs", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.listOpportunities.mockResolvedValue([]);
    mocks.listCoachObservations.mockResolvedValue([]);
    mocks.listCareerStories.mockResolvedValue([]);
    mocks.listPracticePlans.mockResolvedValue([]);
  });

  /**
   * `calculateReadiness` (from `src/lib/readiness.ts`) is deliberately NOT
   * mocked in this file, so this drives the real composition at
   * `loadPracticeInputs`'s `readiness: calculateReadiness(evidence)` call
   * site -- the shared loader this dashboard test exists to cover, per the
   * design/plan's `PracticeInputs` contract. It must fail if the evidence
   * `listReadinessEvidence` returned was dropped, hardcoded, or not
   * actually reaching `calculateReadiness`: a competency-named evidence row
   * maps to exactly one dimension (`dimensionFor`, `src/lib/readiness-dimensions.ts`),
   * so a wrong or missing evidence argument leaves that dimension's
   * `evidenceCount`/`score` at their empty defaults.
   */
  it("threads the loaded evidence, and the caller's now, through the real calculateReadiness call", async () => {
    const now = new Date("2026-08-31T10:00:00.000Z");
    mocks.getProfile.mockResolvedValue(profile);
    mocks.listRecentSessions.mockResolvedValue([]);
    mocks.listReadinessEvidence.mockResolvedValue([
      {
        questionEvaluationId: "eval-1",
        sessionId: "session-1",
        recordedAt: now.toISOString(),
        score: 8,
        competencyId: "comp-1",
        competencyName: "React architecture",
        category: "technical",
        relevance: 1,
        mode: "real",
        degraded: false,
        assistanceCount: 0,
      },
    ]);

    const inputs = await loadPracticeInputs(supabase as never, "user-1", now);

    // Evidence recorded exactly at `now` decays to nothing, so this is exact
    // -- not just non-zero -- and would drift if `now` stopped reaching
    // `calculateReadiness` as `asOf` (e.g. it silently fell back to the
    // wall clock instead).
    const frontend = inputs.readiness.dimensions.find((dimension) => dimension.dimension === "frontend");
    expect(frontend).toMatchObject({ evidenceCount: 1, score: 80, confidence: "low" });
    expect(inputs.readiness.overall).toBe(80);
    expect(inputs.readiness.computedAt).toBe(now.toISOString());
    expect(mocks.listReadinessEvidence).toHaveBeenCalledWith(supabase, "user-1");
  });

  it("computes an empty-but-valid readiness model when there is no evidence", async () => {
    mocks.getProfile.mockResolvedValue(null);
    mocks.listRecentSessions.mockResolvedValue([]);
    mocks.listReadinessEvidence.mockResolvedValue([]);

    const inputs = await loadPracticeInputs(supabase as never, "user-1", new Date("2026-08-31T10:00:00.000Z"));

    expect(inputs.readiness.overall).toBeNull();
    expect(inputs.readiness.dimensions.every((dimension) => dimension.score === null)).toBe(true);
  });
});

describe("completeLinkedPracticePlanBestEffort", () => {
  const completedSession = session({
    status: "complete",
    completedAt: "2026-08-31T10:00:00.000Z",
    practicePlanId: "plan-1",
  });

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("marks the linked plan completed at the session's completion time", async () => {
    mocks.updatePracticePlan.mockResolvedValue(plan("completed"));

    const result = await completeLinkedPracticePlanBestEffort(supabase as never, "user-1", completedSession);

    expect(result).toEqual({ warning: null });
    expect(mocks.updatePracticePlan).toHaveBeenCalledWith(expect.anything(), "user-1", "plan-1", {
      status: "completed",
      completedAt: "2026-08-31T10:00:00.000Z",
    });
  });

  it("does nothing for a session that was never linked to a plan", async () => {
    const result = await completeLinkedPracticePlanBestEffort(supabase as never, "user-1", session({ status: "complete" }));

    expect(result).toEqual({ warning: null });
    expect(mocks.updatePracticePlan).not.toHaveBeenCalled();
  });

  it("keeps interview completion successful when plan completion fails", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.updatePracticePlan.mockRejectedValue(new Error("failed"));

    await expect(completeLinkedPracticePlanBestEffort(supabase as never, "user-1", completedSession))
      .resolves.toEqual({ warning: expect.any(String) });

    expect(consoleError).toHaveBeenCalledWith(
      "[practice-service] practice plan completion failed",
      expect.objectContaining({ message: "failed" }),
    );
    consoleError.mockRestore();
  });
});

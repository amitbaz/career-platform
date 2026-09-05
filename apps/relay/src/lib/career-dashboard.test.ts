import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CareerStory,
  CoachObservation,
  ObservationEvidence,
  Opportunity,
  PracticePlan,
  PracticeRecommendation,
  Profile,
  ReadinessModel,
} from "@/lib/types";
import type { PracticeInputs } from "@/lib/practice-service";

const mocks = vi.hoisted(() => ({
  loadPracticeInputs: vi.fn(),
  recommendPractice: vi.fn(),
  listObservationEvidence: vi.fn(),
  listCareerStoryEvidence: vi.fn(),
  resolveObservationEvidence: vi.fn(),
}));

vi.mock("@/lib/practice-service", () => ({ loadPracticeInputs: mocks.loadPracticeInputs }));
// `effectiveObservationText` is kept REAL (not mocked) here -- this suite's whole point is to
// verify `loadCareerDashboard` uses the single shared implementation, trimming included, rather
// than a second copy that could (and, before this fix, did) drift from it.
vi.mock("@/lib/practice-recommendation", async () => {
  const actual = await vi.importActual<typeof import("@/lib/practice-recommendation")>("@/lib/practice-recommendation");
  return { ...actual, recommendPractice: mocks.recommendPractice };
});
vi.mock("@/lib/repositories/observations", () => ({ listObservationEvidence: mocks.listObservationEvidence }));
vi.mock("@/lib/repositories/stories", () => ({ listCareerStoryEvidence: mocks.listCareerStoryEvidence }));
vi.mock("@/lib/coach-memory", () => ({ resolveObservationEvidence: mocks.resolveObservationEvidence }));

import { CareerDashboardError, loadCareerDashboard } from "@/lib/career-dashboard";

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
  evidence: [],
  source: { cvText: "", coverLetter: "" },
  readiness: { ready: true, missing: [] },
  createdAt: "2026-08-29T10:00:00.000Z",
  updatedAt: "2026-08-29T10:00:00.000Z",
};

const readiness: ReadinessModel = {
  overall: 62,
  overallConfidence: "medium",
  overallTrend: "stable",
  dimensions: [],
  unmappedEvidenceCount: 0,
  computedAt: "2026-08-31T09:00:00.000Z",
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
    jobDescription: null,
    sourceLabel: null,
    sourceSystem: "manual",
    sourceExternalId: null,
    matchScore: null,
    strengths: [],
    gaps: [],
    notes: null,
    appliedAt: null,
    nextInterviewAt: null,
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

function observation(overrides: Partial<CoachObservation> = {}): CoachObservation {
  return {
    id: "obs-1",
    userId: "user-1",
    observationType: "weakness",
    claim: "You skip tradeoffs.",
    confidence: 0.7,
    importance: 0.6,
    trend: "unresolved",
    reviewState: "unreviewed",
    userCorrection: null,
    firstSeenAt: null,
    lastSeenAt: null,
    confirmedAt: null,
    correctedAt: null,
    dismissedAt: null,
    createdAt: "2026-08-30T10:00:00.000Z",
    updatedAt: "2026-08-30T10:00:00.000Z",
    ...overrides,
  };
}

function story(overrides: Partial<CareerStory> = {}): CareerStory {
  return {
    id: "story-1",
    userId: "user-1",
    title: "Led the checkout migration",
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
    completeness: 0,
    reviewState: "draft",
    confirmedAt: null,
    createdAt: "2026-08-30T10:00:00.000Z",
    updatedAt: "2026-08-30T10:00:00.000Z",
    ...overrides,
  };
}

function plan(overrides: Partial<PracticePlan> = {}): PracticePlan {
  return {
    id: "plan-1",
    userId: "user-1",
    status: "completed",
    primaryFocus: "Prepare for the interview",
    secondaryFocus: null,
    rationale: "Because.",
    format: "role_prep",
    estimatedMinutes: 18,
    successCriteria: [],
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

function practiceInputs(overrides: Partial<PracticeInputs> = {}): PracticeInputs {
  return {
    profile,
    opportunities: [],
    observations: [],
    stories: [],
    sessions: [],
    plans: [],
    readiness,
    ...overrides,
  };
}

const recommendation: PracticeRecommendation = {
  format: "full_simulation",
  primaryFocus: "Run a full mock interview simulation",
  secondaryFocus: null,
  rationale: "No urgent signals right now.",
  estimatedMinutes: 30,
  successCriteria: ["Complete a full mock interview across the core competencies."],
  primaryOpportunityId: null,
  supportingOpportunityIds: [],
  signals: [{ kind: "fallback", label: "general readiness", detail: "no urgent signals right now" }],
};

const supabase = { client: true };

describe("loadCareerDashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs());
    mocks.recommendPractice.mockReturnValue(recommendation);
    mocks.listObservationEvidence.mockResolvedValue([]);
    mocks.listCareerStoryEvidence.mockResolvedValue([]);
    mocks.resolveObservationEvidence.mockResolvedValue([]);
  });

  it("loads inputs through the shared practice-service loader for the given user, threading now through", async () => {
    await loadCareerDashboard(supabase as never, "user-1", now, "demo");
    expect(mocks.loadPracticeInputs).toHaveBeenCalledWith(supabase, "user-1", now);
  });

  it("produces a valid dashboard for a brand-new account with every Career Brain table empty", async () => {
    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.profile).toEqual(profile);
    expect(dashboard.coachMode).toBe("demo");
    expect(dashboard.opportunities).toEqual([]);
    expect(dashboard.upcomingOpportunities).toEqual([]);
    expect(dashboard.observations).toEqual([]);
    expect(dashboard.stories).toEqual([]);
    expect(dashboard.recentPracticePlans).toEqual([]);
    expect(dashboard.recommendation).toBe(recommendation);
  });

  it("reports coachMode exactly as passed by the caller", async () => {
    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "live");
    expect(dashboard.coachMode).toBe("live");
  });

  it("rejects when the caller has no profile yet, without resolving any observation/story evidence", async () => {
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ profile: null }));

    await expect(loadCareerDashboard(supabase as never, "user-1", now, "demo")).rejects.toThrow(CareerDashboardError);
    await expect(loadCareerDashboard(supabase as never, "user-1", now, "demo")).rejects.toMatchObject({
      code: "PROFILE_REQUIRED",
    });
    expect(mocks.listObservationEvidence).not.toHaveBeenCalled();
    expect(mocks.listCareerStoryEvidence).not.toHaveBeenCalled();
  });

  it("excludes dismissed observations and never lets them drive evidence resolution", async () => {
    const kept = observation({ id: "obs-kept", reviewState: "confirmed" });
    const dismissed = observation({ id: "obs-dismissed", reviewState: "dismissed", dismissedAt: "2026-08-30T12:00:00.000Z" });
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ observations: [kept, dismissed] }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.observations.every((item) => item.reviewState !== "dismissed")).toBe(true);
    expect(dashboard.observations.map((item) => item.id)).toEqual(["obs-kept"]);
    expect(mocks.listObservationEvidence).toHaveBeenCalledWith(supabase, "user-1", "obs-kept");
    expect(mocks.listObservationEvidence).not.toHaveBeenCalledWith(supabase, "user-1", "obs-dismissed");
  });

  it("keeps unreviewed observations visible but still passes the full raw list to the recommendation selector", async () => {
    const unreviewed = observation({ id: "obs-unreviewed", reviewState: "unreviewed" });
    const dismissed = observation({ id: "obs-dismissed", reviewState: "dismissed" });
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ observations: [unreviewed, dismissed] }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.observations.map((item) => item.id)).toEqual(["obs-unreviewed"]);
    // Dismissal filtering for display never re-implements recommendPractice's own review-state
    // gate -- the raw, unfiltered list (including the dismissed row) is what the selector sees.
    expect(mocks.recommendPractice).toHaveBeenCalledWith(expect.objectContaining({
      observations: [unreviewed, dismissed],
    }));
  });

  it("exposes a corrected observation's trimmed user correction as effectiveText while keeping the original, untrimmed claim", async () => {
    // Regression test: `effectiveText` must come from the same
    // `effectiveObservationText` `recommendPractice` uses internally
    // (via `src/lib/practice-recommendation.ts`), including its trimming --
    // a previously duplicated, untrimmed copy in this module disagreed with
    // it for any claim/correction with leading or trailing whitespace.
    const corrected = observation({
      id: "obs-corrected",
      reviewState: "corrected",
      claim: "  You skip tradeoffs.  ",
      userCorrection: "  I do mention tradeoffs, just briefly.  ",
    });
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ observations: [corrected] }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.observations[0].claim).toBe("  You skip tradeoffs.  ");
    expect(dashboard.observations[0].effectiveText).toBe("I do mention tradeoffs, just briefly.");
  });

  it("falls back to the trimmed original claim as effectiveText for unreviewed/confirmed observations", async () => {
    const confirmed = observation({ id: "obs-confirmed", reviewState: "confirmed", claim: "  You skip tradeoffs.  " });
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ observations: [confirmed] }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.observations[0].effectiveText).toBe("You skip tradeoffs.");
  });

  it("attaches resolved evidence to each observation summary", async () => {
    const kept = observation({ id: "obs-kept" });
    const evidenceRows: ObservationEvidence[] = [{
      id: "evidence-1",
      userId: "user-1",
      observationId: "obs-kept",
      profileEvidenceId: "pe-1",
      questionEvaluationId: null,
      careerStoryId: null,
      opportunityEventId: null,
      evidenceRole: "supporting",
      weight: 1,
      reason: null,
      createdAt: "2026-08-30T10:00:00.000Z",
    }];
    const resolvedDisplay = { kind: "profile_evidence" as const, label: "Acme", summary: "Led the migration.", role: "supporting" as const, reason: null };
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ observations: [kept] }));
    mocks.listObservationEvidence.mockResolvedValue(evidenceRows);
    mocks.resolveObservationEvidence.mockResolvedValue([resolvedDisplay]);

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(mocks.resolveObservationEvidence).toHaveBeenCalledWith(supabase, "user-1", evidenceRows);
    expect(dashboard.observations[0].evidence).toEqual([resolvedDisplay]);
  });

  it("adds evidenceCount to each career story summary", async () => {
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ stories: [story({ id: "story-1" })] }));
    mocks.listCareerStoryEvidence.mockResolvedValue([{ id: "e1" }, { id: "e2" }]);

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.stories).toEqual([{ ...story({ id: "story-1" }), evidenceCount: 2 }]);
  });

  it("filters upcomingOpportunities to future-dated interviews, soonest first", async () => {
    const past = opportunity("opp-past", { nextInterviewAt: "2026-08-01T09:00:00.000Z" });
    const none = opportunity("opp-none", { nextInterviewAt: null });
    const soon = opportunity("opp-soon", { nextInterviewAt: "2026-09-02T09:00:00.000Z" });
    const later = opportunity("opp-later", { nextInterviewAt: "2026-09-10T09:00:00.000Z" });
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ opportunities: [later, past, none, soon] }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.opportunities).toEqual([later, past, none, soon]);
    expect(dashboard.upcomingOpportunities.map((item) => item.id)).toEqual(["opp-soon", "opp-later"]);
  });

  it("passes the readiness model from loadPracticeInputs straight through, uncomputed here", async () => {
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ readiness }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.readiness).toBe(readiness);
  });

  it("passes recentPracticePlans and recentSessions straight through, and forwards now to the recommendation selector", async () => {
    const plans = [plan({ id: "plan-1" })];
    mocks.loadPracticeInputs.mockResolvedValue(practiceInputs({ plans }));

    const dashboard = await loadCareerDashboard(supabase as never, "user-1", now, "demo");

    expect(dashboard.recentPracticePlans).toBe(plans);
    expect(mocks.recommendPractice).toHaveBeenCalledWith(expect.objectContaining({ now, recentPlans: plans }));
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, type Mock } from "vitest";
import type { CareerDashboard, InterviewSession, Opportunity, PracticePlan, PracticeRecommendation, Profile } from "@/lib/types";
import { PracticeView } from "@/app/views/practice-view";

type PracticeViewMocks = {
  onStartRecommended: Mock;
  onStartManual: Mock;
};

function recommendation(overrides: Partial<PracticeRecommendation> = {}): PracticeRecommendation {
  return {
    format: "targeted_drill",
    primaryFocus: "Tighten your tradeoff narration",
    secondaryFocus: null,
    rationale: "Your last reviewed observation flagged this as a recurring gap.",
    estimatedMinutes: 12,
    successCriteria: ["Name at least one alternative you rejected."],
    primaryOpportunityId: null,
    supportingOpportunityIds: [],
    signals: [{ kind: "reviewed_observation", label: "reviewed weakness", detail: "tradeoffs are not explicit enough" }],
    ...overrides,
  };
}

function profile(overrides: Partial<Profile> = {}): Profile {
  return {
    userId: "user-1",
    role: "Senior Frontend Engineer",
    seniority: "Senior",
    summary: null,
    narrative: "Leads complex React work.",
    expertise: ["React"],
    characteristics: [],
    competencies: [],
    evidence: [],
    readiness: { ready: true, missing: [] },
    source: { cvText: "", coverLetter: "" },
    createdAt: "2026-08-01T10:00:00.000Z",
    updatedAt: "2026-08-01T10:00:00.000Z",
    ...overrides,
  };
}

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "opp-1",
    userId: "user-1",
    company: "Northwind",
    role: "Staff Engineer",
    status: "interviewing",
    location: null,
    remote: true,
    jobUrl: null,
    jobDescription: null,
    sourceLabel: null,
    sourceSystem: null,
    sourceExternalId: null,
    matchScore: null,
    strengths: [],
    gaps: [],
    notes: null,
    appliedAt: null,
    nextInterviewAt: null,
    createdAt: "2026-08-10T10:00:00.000Z",
    updatedAt: "2026-08-10T10:00:00.000Z",
    ...overrides,
  };
}

function practicePlan(overrides: Partial<PracticePlan> = {}): PracticePlan {
  return {
    id: "plan-1",
    userId: "user-1",
    status: "completed",
    primaryFocus: "Role prep for Northwind",
    secondaryFocus: null,
    rationale: "Your Staff Engineer interview at Northwind is coming up.",
    format: "role_prep",
    estimatedMinutes: 18,
    successCriteria: [],
    priorityScore: null,
    priorityFactors: {},
    generationError: null,
    completedAt: "2026-08-20T10:00:00.000Z",
    createdAt: "2026-08-18T10:00:00.000Z",
    updatedAt: "2026-08-20T10:00:00.000Z",
    opportunities: [],
    ...overrides,
  };
}

function interviewSession(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    degraded: false,
    status: "complete",
    startedAt: "2026-08-20T10:00:00.000Z",
    completedAt: "2026-08-20T10:30:00.000Z",
    exercise: {},
    resultSummary: {},
    overallScore: 7.5,
    questions: [],
    checkpoints: [],
    evaluations: [],
    messages: [],
    createdAt: "2026-08-20T10:00:00.000Z",
    updatedAt: "2026-08-20T10:30:00.000Z",
    practicePlanId: "plan-1",
    opportunityId: null,
    ...overrides,
  };
}

function dashboard(overrides: Partial<CareerDashboard> = {}): CareerDashboard {
  return {
    profile: profile(),
    coachMode: "demo",
    readiness: { overall: null, overallConfidence: null, overallTrend: "unresolved", dimensions: [], unmappedEvidenceCount: 0, computedAt: "2026-08-29T10:00:00.000Z" },
    recentSessions: [],
    opportunities: [],
    upcomingOpportunities: [],
    observations: [],
    stories: [],
    recentPracticePlans: [],
    recommendation: recommendation(),
    ...overrides,
  };
}

function renderPractice(dashboardValue: CareerDashboard, overrides: Partial<PracticeViewMocks> = {}) {
  const defaults: PracticeViewMocks = {
    onStartRecommended: vi.fn().mockResolvedValue(undefined),
    onStartManual: vi.fn().mockResolvedValue(undefined),
  };
  const effective = { ...defaults, ...overrides };
  render(<PracticeView dashboard={dashboardValue} busy={false} {...effective} />);
  return effective;
}

describe("PracticeView", () => {
  it("shows the current recommendation summary", () => {
    renderPractice(dashboard({ recommendation: recommendation({ primaryFocus: "Tighten your tradeoff narration", rationale: "Your last reviewed observation flagged this as a recurring gap." }) }));

    expect(screen.getByText("Tighten your tradeoff narration")).toBeInTheDocument();
    expect(screen.getByText("Your last reviewed observation flagged this as a recurring gap.")).toBeInTheDocument();
  });

  it("starts recommended practice from its own button", async () => {
    const { onStartRecommended } = renderPractice(dashboard());

    fireEvent.click(screen.getByRole("button", { name: "Start recommended practice" }));

    await waitFor(() => expect(onStartRecommended).toHaveBeenCalledTimes(1));
  });

  it("keeps the recommended-practice action enabled and shows practice-first guidance when the profile readiness is not ready", async () => {
    const { onStartRecommended } = renderPractice(dashboard({ profile: profile({ readiness: { ready: false, missing: ["two concrete engineering projects or work examples"] } }) }));

    const cta = screen.getByRole("button", { name: "Start recommended practice" });
    expect(cta).toBeEnabled();
    expect(screen.getByText(/You can practice now\./)).toBeInTheDocument();
    expect(screen.getByText(/help you uncover stronger/i)).toBeInTheDocument();

    fireEvent.click(cta);
    await waitFor(() => expect(onStartRecommended).toHaveBeenCalledTimes(1));
  });

  it("submits the manual focus/format form", async () => {
    const { onStartManual } = renderPractice(dashboard({ opportunities: [opportunity()] }));

    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "System design tradeoffs" } });
    fireEvent.change(screen.getByLabelText("Secondary focus (optional)"), { target: { value: "Scaling reads" } });
    fireEvent.change(screen.getByLabelText("Format"), { target: { value: "story_work" } });
    fireEvent.change(screen.getByLabelText("Approximate minutes (optional)"), { target: { value: "20" } });
    fireEvent.change(screen.getByLabelText("Opportunity (optional)"), { target: { value: "opp-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));

    await waitFor(() => expect(onStartManual).toHaveBeenCalledTimes(1));
    expect(onStartManual.mock.calls[0][0]).toMatchObject({
      format: "story_work",
      primaryFocus: "System design tradeoffs",
      secondaryFocus: "Scaling reads",
      estimatedMinutes: 20,
      primaryOpportunityId: "opp-1",
    });
  });

  it("submits the manual form without an opportunity when none is chosen", async () => {
    const { onStartManual } = renderPractice(dashboard({ opportunities: [opportunity()] }));

    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "System design tradeoffs" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));

    await waitFor(() => expect(onStartManual).toHaveBeenCalledTimes(1));
    expect(onStartManual.mock.calls[0][0].primaryOpportunityId).toBeNull();
  });

  // Design section 7.4: selecting the `hands_on` format still goes through the
  // same manual-start pipeline as every other format ("existing hands-on
  // session flow" happens server-side) -- there must not be a second,
  // unrelated session-start architecture (design section 4.5/6.2). This
  // proves the one-click hands-on option reaches `onStartManual`, not some
  // other handler.
  it("starts hands-on practice directly through the same manual-start path", async () => {
    const { onStartManual } = renderPractice(dashboard());

    fireEvent.click(screen.getByRole("button", { name: "Start hands-on practice" }));

    await waitFor(() => expect(onStartManual).toHaveBeenCalledTimes(1));
    expect(onStartManual.mock.calls[0][0]).toMatchObject({ format: "hands_on" });
    expect(onStartManual.mock.calls[0][0].primaryFocus).toBeTruthy();
  });

  it("lists recent practice plans", () => {
    renderPractice(dashboard({ recentPracticePlans: [practicePlan({ primaryFocus: "Role prep for Northwind" })] }));

    expect(screen.getByText("Role prep for Northwind")).toBeInTheDocument();
  });

  it("lists recent sessions", () => {
    renderPractice(dashboard({ recentSessions: [interviewSession({ overallScore: 8.5 })] }));

    expect(screen.getByText("8.5/10")).toBeInTheDocument();
  });

  it("shows honest empty states when there is no practice history yet", () => {
    renderPractice(dashboard({ recentPracticePlans: [], recentSessions: [] }));

    expect(screen.getByText(/no practice plans yet/i)).toBeInTheDocument();
    expect(screen.getByText(/no completed sessions yet/i)).toBeInTheDocument();
  });
});

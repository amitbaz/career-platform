import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type {
  CareerDashboard,
  CareerStorySummary,
  CoachObservationSummary,
  Opportunity,
  PracticeRecommendation,
  Profile,
  ReadinessModel,
} from "@/lib/types";
import { HomeView } from "@/app/views/home-view";

function profile(overrides: Partial<Profile> = {}): Profile {
  return {
    userId: "user-1",
    role: "Senior Frontend Engineer",
    seniority: "Senior",
    summary: "Builds product UIs.",
    narrative: "Leads complex React work.",
    expertise: ["React", "TypeScript"],
    characteristics: [],
    competencies: [],
    evidence: [],
    readiness: { ready: true, missing: [] },
    source: { cvText: "Frontend engineer", coverLetter: "" },
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

function readinessModel(overrides: Partial<ReadinessModel> = {}): ReadinessModel {
  return {
    overall: null,
    overallConfidence: null,
    overallTrend: "unresolved",
    dimensions: [],
    unmappedEvidenceCount: 0,
    computedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

function recommendation(overrides: Partial<PracticeRecommendation> = {}): PracticeRecommendation {
  return {
    format: "targeted_drill",
    primaryFocus: "Strengthen: Performance",
    secondaryFocus: null,
    rationale: "Your coaching progress points to Performance as the area most worth drilling next.",
    estimatedMinutes: 12,
    successCriteria: [
      "Answer at least three questions on the target competency.",
      "Name one concrete change to apply in the next real interview.",
    ],
    primaryOpportunityId: null,
    supportingOpportunityIds: [],
    signals: [{ kind: "progress_weakness", label: "progress signal", detail: "Performance is currently weakest" }],
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
    remote: null,
    jobUrl: null,
    jobDescription: null,
    sourceLabel: null,
    sourceSystem: null,
    sourceExternalId: null,
    matchScore: null,
    strengths: [],
    gaps: [],
    notes: null,
    appliedAt: "2026-08-20T10:00:00.000Z",
    nextInterviewAt: null,
    createdAt: "2026-08-15T10:00:00.000Z",
    updatedAt: "2026-08-20T10:00:00.000Z",
    ...overrides,
  };
}

function story(overrides: Partial<CareerStorySummary> = {}): CareerStorySummary {
  return {
    id: "story-1",
    userId: "user-1",
    title: "Checkout migration",
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
    completeness: 0.5,
    reviewState: "confirmed",
    confirmedAt: "2026-08-20T10:00:00.000Z",
    createdAt: "2026-08-15T10:00:00.000Z",
    updatedAt: "2026-08-20T10:00:00.000Z",
    evidenceCount: 2,
    ...overrides,
  };
}

function observation(overrides: Partial<CoachObservationSummary> = {}): CoachObservationSummary {
  return {
    id: "obs-1",
    userId: "user-1",
    observationType: "story_gap",
    claim: "You skip tradeoffs when asked about architecture decisions.",
    confidence: 0.8,
    importance: 0.7,
    trend: "unresolved",
    reviewState: "confirmed",
    userCorrection: null,
    firstSeenAt: "2026-08-15T10:00:00.000Z",
    lastSeenAt: "2026-08-20T10:00:00.000Z",
    confirmedAt: "2026-08-20T10:00:00.000Z",
    correctedAt: null,
    dismissedAt: null,
    createdAt: "2026-08-15T10:00:00.000Z",
    updatedAt: "2026-08-20T10:00:00.000Z",
    effectiveText: "You skip tradeoffs when asked about architecture decisions.",
    evidence: [],
    ...overrides,
  };
}

function dashboard(overrides: Partial<CareerDashboard> = {}): CareerDashboard {
  return {
    profile: profile(),
    coachMode: "demo",
    readiness: readinessModel(),
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

function renderHome(overrides: Partial<CareerDashboard> = {}, propOverrides: Partial<Parameters<typeof HomeView>[0]> = {}) {
  const onStartRecommended = vi.fn().mockResolvedValue(undefined);
  const onOpenApplications = vi.fn();
  const onOpenStories = vi.fn();
  const onOpenCoach = vi.fn();
  const onOpenProgress = vi.fn();
  render(
    <HomeView
      dashboard={dashboard(overrides)}
      busy={false}
      onStartRecommended={onStartRecommended}
      onOpenApplications={onOpenApplications}
      onOpenStories={onOpenStories}
      onOpenCoach={onOpenCoach}
      onOpenProgress={onOpenProgress}
      {...propOverrides}
    />,
  );
  return { onStartRecommended, onOpenApplications, onOpenStories, onOpenCoach, onOpenProgress };
}

describe("HomeView", () => {
  it("renders a dominant Start recommended practice CTA with format, minutes, and rationale", () => {
    renderHome();

    const cta = screen.getByRole("button", { name: "Start recommended practice" });
    expect(cta).toBeEnabled();
    expect(screen.getByText("Strengthen: Performance")).toBeInTheDocument();
    expect(screen.getByText(/12 min/)).toBeInTheDocument();
    expect(screen.getByText("Your coaching progress points to Performance as the area most worth drilling next.")).toBeInTheDocument();
  });

  it("calls onStartRecommended when the CTA is clicked", () => {
    const { onStartRecommended } = renderHome();

    fireEvent.click(screen.getByRole("button", { name: "Start recommended practice" }));

    expect(onStartRecommended).toHaveBeenCalledTimes(1);
  });

  it("shows only the first three recommendation signals when more are present", () => {
    renderHome({
      recommendation: recommendation({
        signals: [
          { kind: "progress_weakness", label: "signal one", detail: "detail one" },
          { kind: "story_bank_gap", label: "signal two", detail: "detail two" },
          { kind: "first_practice", label: "signal three", detail: "detail three" },
          { kind: "fallback", label: "signal four", detail: "detail four" },
        ],
      }),
    });

    expect(screen.getByText("detail one")).toBeInTheDocument();
    expect(screen.getByText("detail two")).toBeInTheDocument();
    expect(screen.getByText("detail three")).toBeInTheDocument();
    expect(screen.queryByText("detail four")).not.toBeInTheDocument();
  });

  it("shows success criteria and the primary opportunity when the recommendation names one", () => {
    renderHome({
      opportunities: [opportunity({ id: "opp-1", company: "Northwind", role: "Staff Engineer", status: "offer" })],
      recommendation: recommendation({
        successCriteria: ["Answer at least three questions on the target competency."],
        primaryOpportunityId: "opp-1",
      }),
    });

    const recommendedSection = screen.getByRole("region", { name: "Recommended practice" });
    expect(within(recommendedSection).getByText("Answer at least three questions on the target competency.")).toBeInTheDocument();
    expect(within(recommendedSection).getByText(/Northwind/)).toBeInTheDocument();
    expect(within(recommendedSection).getByText(/Staff Engineer/)).toBeInTheDocument();
  });

  it("keeps the CTA enabled and shows practice-first guidance when the profile readiness is not ready", () => {
    const { onStartRecommended } = renderHome({
      profile: profile({ readiness: { ready: false, missing: ["two concrete engineering projects"] } }),
    });

    const cta = screen.getByRole("button", { name: "Start recommended practice" });
    expect(cta).toBeEnabled();
    expect(screen.getByText(/You can practice now\./)).toBeInTheDocument();
    expect(screen.getByText(/help you uncover stronger/i)).toBeInTheDocument();

    fireEvent.click(cta);
    expect(onStartRecommended).toHaveBeenCalledTimes(1);
  });

  it("shows applications needing attention with company and role", () => {
    renderHome({
      opportunities: [
        opportunity({ id: "opp-1", company: "Northwind", role: "Staff Engineer", status: "interviewing" }),
        opportunity({ id: "opp-2", company: "Acme", role: "Principal Engineer", status: "offer" }),
      ],
    });

    expect(screen.getByText("Northwind")).toBeInTheDocument();
    expect(screen.getByText("Staff Engineer")).toBeInTheDocument();
    // Terminal-status opportunities (offer/rejected/withdrawn/closed) don't need attention.
    expect(screen.queryByText("Acme")).not.toBeInTheDocument();
  });

  it("shows an honest empty state when no applications need attention", () => {
    renderHome({ opportunities: [] });

    expect(screen.getByText(/no applications/i)).toBeInTheDocument();
  });

  it("shows an honest empty state for the story bank when there are no stories", () => {
    renderHome({ stories: [] });

    expect(screen.getByText(/haven't added any career stories/i)).toBeInTheDocument();
  });

  it("shows a story bank summary when stories exist", () => {
    renderHome({ stories: [story({ reviewState: "confirmed" }), story({ id: "story-2", reviewState: "draft" })] });

    expect(screen.getByText(/1 confirmed/i)).toBeInTheDocument();
    expect(screen.getByText(/2 total/i)).toBeInTheDocument();
  });

  it("shows an honest empty state for what Relay is noticing when there are no observations", () => {
    renderHome({ observations: [] });

    expect(screen.getByText(/hasn't noticed/i)).toBeInTheDocument();
  });

  it("shows observation text when Relay has noticed something", () => {
    renderHome({ observations: [observation({ effectiveText: "You skip tradeoffs under pressure." })] });

    expect(screen.getByText("You skip tradeoffs under pressure.")).toBeInTheDocument();
  });

  it("shows an honest empty state for progress when there is no readiness score yet", () => {
    renderHome({ readiness: readinessModel({ overall: null }) });

    expect(screen.getByText(/complete your first practice session/i)).toBeInTheDocument();
  });

  it("shows the readiness score when readiness data exists", () => {
    renderHome({ readiness: readinessModel({ overall: 81, overallTrend: "improving" }) });

    expect(screen.getByText(/81/)).toBeInTheDocument();
  });

  it("wires the open-affordance buttons to their callbacks", () => {
    const { onOpenApplications, onOpenStories, onOpenCoach, onOpenProgress } = renderHome();

    fireEvent.click(screen.getByRole("button", { name: "Open applications" }));
    fireEvent.click(screen.getByRole("button", { name: "Open story bank" }));
    fireEvent.click(screen.getByRole("button", { name: "Open coach" }));
    fireEvent.click(screen.getByRole("button", { name: "Open progress" }));

    expect(onOpenApplications).toHaveBeenCalledTimes(1);
    expect(onOpenStories).toHaveBeenCalledTimes(1);
    expect(onOpenCoach).toHaveBeenCalledTimes(1);
    expect(onOpenProgress).toHaveBeenCalledTimes(1);
  });

  it("renders the five sections in order: recommended practice, applications, noticing, story bank, progress", () => {
    renderHome();

    const headings = screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent);
    expect(headings).toEqual([
      "Recommended practice",
      "Applications needing attention",
      "What Relay is noticing",
      "Story bank",
      "Progress",
    ]);
  });
});

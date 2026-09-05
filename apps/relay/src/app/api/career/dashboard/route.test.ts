import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  loadCareerDashboard: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/career-dashboard", async () => {
  const actual = await vi.importActual<typeof import("@/lib/career-dashboard")>("@/lib/career-dashboard");
  return { CareerDashboardError: actual.CareerDashboardError, loadCareerDashboard: mocks.loadCareerDashboard };
});

import { GET } from "@/app/api/career/dashboard/route";
import { CareerDashboardError } from "@/lib/career-dashboard";
import { RepositoryError } from "@/lib/repositories/profile";

const supabase = { client: true };
const user = { id: "user-1" };

const dashboard = {
  profile: { userId: "user-1" },
  coachMode: "demo",
  recentSessions: [],
  opportunities: [],
  upcomingOpportunities: [],
  observations: [],
  stories: [],
  recentPracticePlans: [],
  recommendation: {
    format: "full_simulation",
    primaryFocus: "Run a full mock interview simulation",
    secondaryFocus: null,
    rationale: "No urgent signals right now.",
    estimatedMinutes: 30,
    successCriteria: [],
    primaryOpportunityId: null,
    supportingOpportunityIds: [],
    signals: [],
  },
};

describe("GET /api/career/dashboard", () => {
  const originalKey = process.env.GEMINI_API_KEY;

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase, user });
    mocks.loadCareerDashboard.mockResolvedValue(dashboard);
  });

  afterEach(() => {
    if (originalKey === undefined) delete process.env.GEMINI_API_KEY;
    else process.env.GEMINI_API_KEY = originalKey;
  });

  it("returns the authenticated caller's dashboard", async () => {
    const response = await GET();
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual(dashboard);
    expect(mocks.loadCareerDashboard).toHaveBeenCalledWith(supabase, "user-1", expect.any(Date), "demo");
  });

  it("computes coachMode as live when GEMINI_API_KEY is configured", async () => {
    process.env.GEMINI_API_KEY = "test-key";
    await GET();
    expect(mocks.loadCareerDashboard).toHaveBeenCalledWith(supabase, "user-1", expect.any(Date), "live");
  });

  it("computes coachMode as demo when GEMINI_API_KEY is not configured", async () => {
    delete process.env.GEMINI_API_KEY;
    await GET();
    expect(mocks.loadCareerDashboard).toHaveBeenCalledWith(supabase, "user-1", expect.any(Date), "demo");
  });

  it("returns 401 when the caller is unauthenticated", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));
    const response = await GET();
    expect(response.status).toBe(401);
  });

  it("returns 400 when the caller has no profile yet", async () => {
    mocks.loadCareerDashboard.mockRejectedValue(new CareerDashboardError("Create your profile first.", "PROFILE_REQUIRED"));
    const response = await GET();
    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: "Create your profile first." });
  });

  it("returns 500 with a user-safe message for an unexpected repository failure", async () => {
    mocks.loadCareerDashboard.mockRejectedValue(new RepositoryError("boom", "UNKNOWN"));
    const response = await GET();
    expect(response.status).toBe(500);
    await expect(response.json()).resolves.toEqual({ error: "Could not load your dashboard." });
  });

  it("never mutates -- GET has no side-effecting calls beyond loading the dashboard", async () => {
    await GET();
    expect(mocks.loadCareerDashboard).toHaveBeenCalledTimes(1);
  });
});

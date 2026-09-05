import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  createOpportunity: vi.fn(),
  updateOpportunityDetails: vi.fn(),
  transitionOpportunity: vi.fn(),
  scheduleOpportunityInterview: vi.fn(),
  listOpportunities: vi.fn(),
  listOpportunityEvents: vi.fn(),
  addOpportunityNote: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/repositories/opportunities", async () => {
  const actual = await vi.importActual<typeof import("@/lib/repositories/opportunities")>(
    "@/lib/repositories/opportunities",
  );
  return {
    isOpportunityStatus: actual.isOpportunityStatus,
    createOpportunity: mocks.createOpportunity,
    updateOpportunityDetails: mocks.updateOpportunityDetails,
    transitionOpportunity: mocks.transitionOpportunity,
    scheduleOpportunityInterview: mocks.scheduleOpportunityInterview,
    listOpportunities: mocks.listOpportunities,
    listOpportunityEvents: mocks.listOpportunityEvents,
    addOpportunityNote: mocks.addOpportunityNote,
  };
});

import { GET, POST } from "@/app/api/opportunities/route";
import { RepositoryError } from "@/lib/repositories/profile";

const opportunity = {
  id: "opp-1",
  userId: "user-1",
  company: "Example",
  role: "Senior Frontend Engineer",
  status: "considering",
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
  appliedAt: null,
  nextInterviewAt: null,
  createdAt: "2026-08-30T10:00:00.000Z",
  updatedAt: "2026-08-30T10:00:00.000Z",
};

const event = {
  id: "event-1",
  userId: "user-1",
  opportunityId: "opp-1",
  eventType: "note",
  fromStatus: null,
  toStatus: null,
  occurredAt: "2026-08-30T10:00:00.000Z",
  note: "Recruiter call went well",
  metadata: {},
  createdAt: "2026-08-30T10:00:00.000Z",
};

function post(body: unknown) {
  return POST(new Request("http://localhost/api/opportunities", { method: "POST", body: JSON.stringify(body) }));
}

function get(query = "") {
  return GET(new Request(`http://localhost/api/opportunities${query}`));
}

describe("GET /api/opportunities", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
  });

  it("returns the caller's opportunities when no opportunityId is given", async () => {
    mocks.listOpportunities.mockResolvedValue([opportunity]);

    const response = await get();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({ opportunities: [opportunity] });
    expect(mocks.listOpportunities).toHaveBeenCalledWith(expect.anything(), "user-1");
    expect(mocks.listOpportunityEvents).not.toHaveBeenCalled();
  });

  it("returns 401 without loading opportunities when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await get();

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Sign in to continue." });
    expect(mocks.listOpportunities).not.toHaveBeenCalled();
  });

  describe("with an opportunityId query parameter", () => {
    it("returns that opportunity's append-only event history instead of the opportunity list", async () => {
      mocks.listOpportunityEvents.mockResolvedValue([event]);

      const response = await get("?opportunityId=opp-1");
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ events: [event] });
      expect(mocks.listOpportunityEvents).toHaveBeenCalledWith(expect.anything(), "user-1", "opp-1");
      expect(mocks.listOpportunities).not.toHaveBeenCalled();
    });

    it("scopes the query to the authenticated caller, ignoring any other id on the request", async () => {
      mocks.listOpportunityEvents.mockResolvedValue([]);

      const response = await get("?opportunityId=opp-1&userId=someone-elses-id");
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ events: [] });
      // The authenticated session's id ("user-1"), never a request-supplied one.
      expect(mocks.listOpportunityEvents).toHaveBeenCalledWith(expect.anything(), "user-1", "opp-1");
    });

    it("returns 401 without loading events when authentication is absent", async () => {
      mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

      const response = await get("?opportunityId=opp-1");

      expect(response.status).toBe(401);
      expect(mocks.listOpportunityEvents).not.toHaveBeenCalled();
    });
  });
});

describe("POST /api/opportunities", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.createOpportunity.mockResolvedValue(opportunity);
    mocks.updateOpportunityDetails.mockResolvedValue(opportunity);
    mocks.transitionOpportunity.mockResolvedValue({ ...opportunity, status: "applied" });
    mocks.scheduleOpportunityInterview.mockResolvedValue({ ...opportunity, status: "interviewing" });
    mocks.addOpportunityNote.mockResolvedValue(event);
  });

  it("returns 401 before reading a request body when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));
    const json = vi.fn();

    const response = await POST({ json } as unknown as Request);

    expect(response.status).toBe(401);
    expect(json).not.toHaveBeenCalled();
  });

  it("rejects a body that is not a JSON object", async () => {
    const response = await POST(new Request("http://localhost/api/opportunities", { method: "POST", body: "not json" }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "A valid opportunity request is required." });
  });

  it("rejects an unknown action", async () => {
    const response = await post({ action: "delete" });

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Unknown action." });
  });

  describe("create", () => {
    it("creates an opportunity from the validated fields and returns it", async () => {
      const response = await post({
        action: "create",
        company: "Example",
        role: "Senior Frontend Engineer",
        location: "Remote",
        matchScore: 82,
        strengths: ["React"],
      });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ opportunity });
      expect(mocks.createOpportunity).toHaveBeenCalledWith(expect.anything(), "user-1", expect.objectContaining({
        company: "Example",
        role: "Senior Frontend Engineer",
        location: "Remote",
        matchScore: 82,
        strengths: ["React"],
      }));
      expect(mocks.transitionOpportunity).not.toHaveBeenCalled();
    });

    it("rejects a missing company", async () => {
      const response = await post({ action: "create", role: "Senior Frontend Engineer" });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "Company is required." });
      expect(mocks.createOpportunity).not.toHaveBeenCalled();
    });

    it("rejects a blank role", async () => {
      const response = await post({ action: "create", company: "Example", role: "   " });

      expect(response.status).toBe(400);
      expect(mocks.createOpportunity).not.toHaveBeenCalled();
    });

    it("rejects a match score outside 0-100", async () => {
      const response = await post({
        action: "create",
        company: "Example",
        role: "Senior Frontend Engineer",
        matchScore: 142,
      });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "Match score must be a number between 0 and 100." });
      expect(mocks.createOpportunity).not.toHaveBeenCalled();
    });

    it(
      "calls createOpportunity then transitionOpportunity for a non-considering initialStatus, "
      + "never a direct lifecycle-column update",
      async () => {
        const response = await post({
          action: "create",
          company: "Example",
          role: "Senior Frontend Engineer",
          initialStatus: "applied",
        });
        const body = await response.json();

        expect(response.status).toBe(200);
        expect(mocks.createOpportunity).toHaveBeenCalledWith(expect.anything(), "user-1", expect.objectContaining({
          company: "Example",
          role: "Senior Frontend Engineer",
        }));
        expect(mocks.createOpportunity).not.toHaveBeenCalledWith(
          expect.anything(),
          "user-1",
          expect.objectContaining({ status: expect.anything() }),
        );
        expect(mocks.transitionOpportunity).toHaveBeenCalledWith(
          expect.anything(),
          "user-1",
          "opp-1",
          "applied",
        );
        expect(body).toEqual({ opportunity: { ...opportunity, status: "applied" } });
      },
    );

    it("does not transition when initialStatus is considering", async () => {
      await post({
        action: "create",
        company: "Example",
        role: "Senior Frontend Engineer",
        initialStatus: "considering",
      });

      expect(mocks.transitionOpportunity).not.toHaveBeenCalled();
    });

    it("rejects an invalid initialStatus", async () => {
      const response = await post({
        action: "create",
        company: "Example",
        role: "Senior Frontend Engineer",
        initialStatus: "hired",
      });

      expect(response.status).toBe(400);
      // Validated before creating anything, so an invalid initialStatus
      // never leaves behind an orphaned "considering" opportunity.
      expect(mocks.createOpportunity).not.toHaveBeenCalled();
      expect(mocks.transitionOpportunity).not.toHaveBeenCalled();
    });
  });

  describe("update", () => {
    it("updates only the fields provided, scoped to the caller", async () => {
      const response = await post({ action: "update", opportunityId: "opp-1", notes: "Updated notes" });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ opportunity });
      expect(mocks.updateOpportunityDetails).toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        "opp-1",
        expect.objectContaining({ notes: "Updated notes" }),
      );
    });

    it("leaves fields that were not provided undefined rather than nulling them", async () => {
      await post({ action: "update", opportunityId: "opp-1", notes: "Updated notes" });

      const call = mocks.updateOpportunityDetails.mock.calls[0][3];
      // `updateOpportunityDetails` only patches a field when it is
      // `!== undefined` (see `src/lib/repositories/opportunities.ts`), so an
      // omitted request field must stay `undefined`, not become `null`.
      expect(call.company).toBeUndefined();
      expect(call.role).toBeUndefined();
    });

    it("clears an optional field when explicitly set to null", async () => {
      await post({ action: "update", opportunityId: "opp-1", location: null });

      const call = mocks.updateOpportunityDetails.mock.calls[0][3];
      expect(call.location).toBeNull();
    });

    it("rejects a missing opportunityId", async () => {
      const response = await post({ action: "update", notes: "Updated notes" });

      expect(response.status).toBe(400);
      expect(mocks.updateOpportunityDetails).not.toHaveBeenCalled();
    });
  });

  describe("transition", () => {
    it("calls transitionOpportunity with the validated status", async () => {
      const response = await post({
        action: "transition",
        opportunityId: "opp-1",
        toStatus: "applied",
        note: "Applied from company site",
      });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ opportunity: { ...opportunity, status: "applied" } });
      expect(mocks.transitionOpportunity).toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        "opp-1",
        "applied",
        expect.objectContaining({ note: "Applied from company site" }),
      );
    });

    it("rejects an invalid toStatus", async () => {
      const response = await post({ action: "transition", opportunityId: "opp-1", toStatus: "hired" });

      expect(response.status).toBe(400);
      expect(mocks.transitionOpportunity).not.toHaveBeenCalled();
    });
  });

  describe("schedule_interview", () => {
    it("calls scheduleOpportunityInterview with a validated future timestamp", async () => {
      const interviewAt = new Date(Date.now() + 60_000).toISOString();

      const response = await post({ action: "schedule_interview", opportunityId: "opp-1", interviewAt });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ opportunity: { ...opportunity, status: "interviewing" } });
      expect(mocks.scheduleOpportunityInterview).toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        "opp-1",
        interviewAt,
        expect.anything(),
      );
    });

    it("rejects a non-ISO interview timestamp", async () => {
      const response = await post({ action: "schedule_interview", opportunityId: "opp-1", interviewAt: "not a date" });

      expect(response.status).toBe(400);
      expect(mocks.scheduleOpportunityInterview).not.toHaveBeenCalled();
    });

    it("rejects an interview timestamp in the past", async () => {
      const interviewAt = new Date(Date.now() - 60_000).toISOString();

      const response = await post({ action: "schedule_interview", opportunityId: "opp-1", interviewAt });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "interviewAt must be in the future." });
      expect(mocks.scheduleOpportunityInterview).not.toHaveBeenCalled();
    });
  });

  describe("add_note", () => {
    it("calls addOpportunityNote and returns the created event", async () => {
      const response = await post({ action: "add_note", opportunityId: "opp-1", note: "Recruiter call went well" });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ event });
      expect(mocks.addOpportunityNote).toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        "opp-1",
        "Recruiter call went well",
      );
    });

    it("rejects a blank note without calling the repository", async () => {
      const response = await post({ action: "add_note", opportunityId: "opp-1", note: "   " });

      expect(response.status).toBe(400);
      expect(mocks.addOpportunityNote).not.toHaveBeenCalled();
    });
  });

  it("returns 404 when the repository reports the row was not owned", async () => {
    mocks.updateOpportunityDetails.mockRejectedValue(new RepositoryError("Could not update the opportunity.", "NO_OWNED_ROW"));

    const response = await post({ action: "update", opportunityId: "opp-missing", notes: "x" });

    expect(response.status).toBe(404);
  });

  it("logs the underlying failure while keeping the public error generic", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
    mocks.listOpportunities.mockRejectedValue(new Error("db exploded"));

    const response = await get();

    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: "Could not complete your opportunity request." });
    expect(consoleError).toHaveBeenCalledWith("[api/opportunities] request failed", expect.objectContaining({
      message: "db exploded",
    }));
    consoleError.mockRestore();
  });
});

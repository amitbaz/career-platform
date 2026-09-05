import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  listCoachObservations: vi.fn(),
  listObservationEvidence: vi.fn(),
  reviewCoachObservation: vi.fn(),
  resolveObservationEvidence: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/repositories/observations", () => ({
  listCoachObservations: mocks.listCoachObservations,
  listObservationEvidence: mocks.listObservationEvidence,
  reviewCoachObservation: mocks.reviewCoachObservation,
}));
vi.mock("@/lib/coach-memory", () => ({ resolveObservationEvidence: mocks.resolveObservationEvidence }));

import { GET, POST } from "@/app/api/observations/route";
import { RepositoryError } from "@/lib/repositories/profile";
import type { CoachObservation } from "@/lib/types";

function observation(overrides: Partial<CoachObservation> = {}): CoachObservation {
  return {
    id: "observation-1",
    userId: "user-1",
    observationType: "knowledge_gap",
    claim: "Skips discussing trade-offs.",
    confidence: 0.7,
    importance: 0.7,
    trend: "unresolved",
    reviewState: "unreviewed",
    userCorrection: null,
    firstSeenAt: "2026-08-01T00:00:00.000Z",
    lastSeenAt: "2026-08-20T00:00:00.000Z",
    confirmedAt: null,
    correctedAt: null,
    dismissedAt: null,
    createdAt: "2026-08-01T00:00:00.000Z",
    updatedAt: "2026-08-20T00:00:00.000Z",
    ...overrides,
  };
}

const unreviewed = observation();
const corrected = observation({
  id: "observation-2",
  reviewState: "corrected",
  userCorrection: "Actually I do discuss trade-offs when asked.",
  correctedAt: "2026-08-25T00:00:00.000Z",
});
const dismissed = observation({ id: "observation-3", reviewState: "dismissed", dismissedAt: "2026-08-25T00:00:00.000Z" });

function post(body: unknown) {
  return POST(new Request("http://localhost/api/observations", { method: "POST", body: JSON.stringify(body) }));
}

describe("GET /api/observations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.listObservationEvidence.mockResolvedValue([]);
    mocks.resolveObservationEvidence.mockResolvedValue([]);
  });

  it("splits non-dismissed observations into active and dismissed ones into history", async () => {
    mocks.listCoachObservations.mockResolvedValue([unreviewed, corrected, dismissed]);

    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.active.map((item: { id: string }) => item.id)).toEqual(["observation-1", "observation-2"]);
    expect(body.history.map((item: { id: string }) => item.id)).toEqual(["observation-3"]);
  });

  it("attaches effectiveText using the user correction when corrected, otherwise the original claim", async () => {
    mocks.listCoachObservations.mockResolvedValue([unreviewed, corrected]);

    const response = await GET();
    const body = await response.json();

    expect(body.active[0]).toMatchObject({ effectiveText: "Skips discussing trade-offs." });
    expect(body.active[1]).toMatchObject({ effectiveText: "Actually I do discuss trade-offs when asked." });
  });

  it("resolves each observation's evidence through coach-memory.ts rather than re-resolving it itself", async () => {
    const evidenceRows = [{ id: "evidence-1" }];
    const resolvedDisplay = [{ kind: "profile_evidence", label: "Example Co", summary: "...", role: "supporting", reason: null }];
    mocks.listCoachObservations.mockResolvedValue([unreviewed]);
    mocks.listObservationEvidence.mockResolvedValue(evidenceRows);
    mocks.resolveObservationEvidence.mockResolvedValue(resolvedDisplay);

    const response = await GET();
    const body = await response.json();

    expect(mocks.listObservationEvidence).toHaveBeenCalledWith(expect.anything(), "user-1", "observation-1");
    expect(mocks.resolveObservationEvidence).toHaveBeenCalledWith(expect.anything(), "user-1", evidenceRows);
    expect(body.active[0].evidence).toEqual(resolvedDisplay);
  });

  it("returns 401 without loading observations when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await GET();

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Sign in to continue." });
    expect(mocks.listCoachObservations).not.toHaveBeenCalled();
  });
});

describe("POST /api/observations", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.reviewCoachObservation.mockResolvedValue(observation({ reviewState: "confirmed" }));
  });

  it("returns 401 before reading a request body when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));
    const json = vi.fn();

    const response = await POST({ json } as unknown as Request);

    expect(response.status).toBe(401);
    expect(json).not.toHaveBeenCalled();
  });

  it("rejects a body that is not a JSON object", async () => {
    const response = await POST(new Request("http://localhost/api/observations", { method: "POST", body: "not json" }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "A valid observation request is required." });
  });

  it("rejects an unknown action", async () => {
    const response = await post({ action: "create", observationId: "observation-1", claim: "New claim" });

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Unknown action." });
    expect(mocks.reviewCoachObservation).not.toHaveBeenCalled();
  });

  describe("confirm", () => {
    it("rejects a missing observationId", async () => {
      const response = await post({ action: "confirm" });

      expect(response.status).toBe(400);
      expect(mocks.reviewCoachObservation).not.toHaveBeenCalled();
    });

    it("reviews the observation as confirmed", async () => {
      const response = await post({ action: "confirm", observationId: "observation-1" });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ observation: observation({ reviewState: "confirmed" }) });
      expect(mocks.reviewCoachObservation).toHaveBeenCalledWith(expect.anything(), "user-1", "observation-1", {
        state: "confirmed",
      });
    });
  });

  describe("dismiss", () => {
    it("rejects a missing observationId", async () => {
      const response = await post({ action: "dismiss" });

      expect(response.status).toBe(400);
      expect(mocks.reviewCoachObservation).not.toHaveBeenCalled();
    });

    it("reviews the observation as dismissed", async () => {
      await post({ action: "dismiss", observationId: "observation-1" });

      expect(mocks.reviewCoachObservation).toHaveBeenCalledWith(expect.anything(), "user-1", "observation-1", {
        state: "dismissed",
      });
    });
  });

  describe("correct", () => {
    it("rejects a missing observationId", async () => {
      const response = await post({ action: "correct", correction: "Actually..." });

      expect(response.status).toBe(400);
      expect(mocks.reviewCoachObservation).not.toHaveBeenCalled();
    });

    it("rejects a missing correction", async () => {
      const response = await post({ action: "correct", observationId: "observation-1" });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "correction is required." });
      expect(mocks.reviewCoachObservation).not.toHaveBeenCalled();
    });

    it("rejects a blank/whitespace-only correction", async () => {
      const response = await post({ action: "correct", observationId: "observation-1", correction: "   " });

      expect(response.status).toBe(400);
      expect(mocks.reviewCoachObservation).not.toHaveBeenCalled();
    });

    it("reviews the observation as corrected with the replacement text", async () => {
      await post({ action: "correct", observationId: "observation-1", correction: "Actually I do discuss trade-offs." });

      expect(mocks.reviewCoachObservation).toHaveBeenCalledWith(expect.anything(), "user-1", "observation-1", {
        state: "corrected",
        correction: "Actually I do discuss trade-offs.",
      });
    });
  });

  it("returns 404 when the repository reports the row was not owned", async () => {
    mocks.reviewCoachObservation.mockRejectedValue(
      new RepositoryError("Could not review the coach observation.", "NO_OWNED_ROW"),
    );

    const response = await post({ action: "confirm", observationId: "observation-1" });

    expect(response.status).toBe(404);
  });
});

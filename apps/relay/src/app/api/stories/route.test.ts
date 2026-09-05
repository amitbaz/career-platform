import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  createCareerStory: vi.fn(),
  getCareerStory: vi.fn(),
  updateCareerStory: vi.fn(),
  listCareerStories: vi.fn(),
  listCareerStoryEvidence: vi.fn(),
  attachCareerStoryEvidence: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/repositories/stories", () => ({
  createCareerStory: mocks.createCareerStory,
  getCareerStory: mocks.getCareerStory,
  updateCareerStory: mocks.updateCareerStory,
  listCareerStories: mocks.listCareerStories,
  listCareerStoryEvidence: mocks.listCareerStoryEvidence,
  attachCareerStoryEvidence: mocks.attachCareerStoryEvidence,
}));

import { GET, POST } from "@/app/api/stories/route";
import { RepositoryError } from "@/lib/repositories/profile";
import type { CareerStory, CareerStoryEvidence } from "@/lib/types";

const draftStory: CareerStory = {
  id: "story-1",
  userId: "user-1",
  title: "Migrated the legacy payments service",
  situation: "The payments service was on an unsupported runtime.",
  responsibility: null,
  problem: null,
  actions: null,
  alternatives: null,
  tradeoffs: null,
  ownership: null,
  outcome: null,
  lessons: null,
  tags: [],
  completeness: 1 / 6,
  reviewState: "draft",
  confirmedAt: null,
  createdAt: "2026-08-30T10:00:00.000Z",
  updatedAt: "2026-08-30T10:00:00.000Z",
};

const evidence: CareerStoryEvidence = {
  id: "evidence-1",
  userId: "user-1",
  careerStoryId: "story-1",
  profileEvidenceId: "profile-evidence-1",
  interviewQuestionId: null,
  note: "From resume bullet",
  createdAt: "2026-08-30T10:00:00.000Z",
};

function post(body: unknown) {
  return POST(new Request("http://localhost/api/stories", { method: "POST", body: JSON.stringify(body) }));
}

describe("GET /api/stories", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
  });

  it("returns the caller's stories enriched with their evidence count", async () => {
    mocks.listCareerStories.mockResolvedValue([draftStory]);
    mocks.listCareerStoryEvidence.mockResolvedValue([evidence]);

    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({ stories: [{ ...draftStory, evidenceCount: 1 }] });
    expect(mocks.listCareerStories).toHaveBeenCalledWith(expect.anything(), "user-1");
    expect(mocks.listCareerStoryEvidence).toHaveBeenCalledWith(expect.anything(), "user-1", "story-1");
  });

  it("returns 401 without loading stories when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await GET();

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Sign in to continue." });
    expect(mocks.listCareerStories).not.toHaveBeenCalled();
  });
});

describe("POST /api/stories", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.createCareerStory.mockResolvedValue(draftStory);
    mocks.getCareerStory.mockResolvedValue(draftStory);
    mocks.updateCareerStory.mockResolvedValue(draftStory);
    mocks.attachCareerStoryEvidence.mockResolvedValue(evidence);
  });

  it("returns 401 before reading a request body when authentication is absent", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));
    const json = vi.fn();

    const response = await POST({ json } as unknown as Request);

    expect(response.status).toBe(401);
    expect(json).not.toHaveBeenCalled();
  });

  it("rejects a body that is not a JSON object", async () => {
    const response = await POST(new Request("http://localhost/api/stories", { method: "POST", body: "not json" }));

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "A valid story request is required." });
  });

  it("rejects an unknown action", async () => {
    const response = await post({ action: "delete" });

    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: "Unknown action." });
  });

  describe("create", () => {
    it("rejects a missing title", async () => {
      const response = await post({ action: "create" });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "Title is required." });
      expect(mocks.createCareerStory).not.toHaveBeenCalled();
    });

    it("computes completeness from the submitted draft fields, ignoring any browser-supplied value", async () => {
      const response = await post({
        action: "create",
        title: "Migrated the legacy payments service",
        situation: "The payments service was on an unsupported runtime.",
        completeness: 0.99,
      });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ story: draftStory });
      expect(mocks.createCareerStory).toHaveBeenCalledWith(expect.anything(), "user-1", expect.objectContaining({
        title: "Migrated the legacy payments service",
        situation: "The payments service was on an unsupported runtime.",
        completeness: 1 / 6,
      }));
    });

    it("scores a story with no factual fields filled in as 0 completeness", async () => {
      await post({ action: "create", title: "Untitled story" });

      expect(mocks.createCareerStory).toHaveBeenCalledWith(expect.anything(), "user-1", expect.objectContaining({
        completeness: 0,
      }));
    });

    it("ignores a browser-supplied reviewState -- new stories always start as a draft", async () => {
      await post({ action: "create", title: "Untitled story", reviewState: "confirmed" });

      expect(mocks.createCareerStory).not.toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        expect.objectContaining({ reviewState: expect.anything() }),
      );
    });
  });

  describe("update", () => {
    it("rejects a missing storyId", async () => {
      const response = await post({ action: "update", situation: "New context." });

      expect(response.status).toBe(400);
      expect(await response.json()).toEqual({ error: "storyId is required." });
      expect(mocks.getCareerStory).not.toHaveBeenCalled();
      expect(mocks.updateCareerStory).not.toHaveBeenCalled();
    });

    it("returns 404 when the story is not owned by the caller", async () => {
      mocks.getCareerStory.mockResolvedValue(null);

      const response = await post({ action: "update", storyId: "story-1", situation: "New context." });

      expect(response.status).toBe(404);
      expect(mocks.updateCareerStory).not.toHaveBeenCalled();
    });

    it("recomputes completeness from the merged draft fields, ignoring any browser-supplied value", async () => {
      // draftStory only has `situation` filled in (1/6); filling `outcome` too should bring it to 2/6.
      const response = await post({
        action: "update",
        storyId: "story-1",
        outcome: "Zero downtime cutover.",
        completeness: 0.01,
      });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ story: draftStory });
      expect(mocks.updateCareerStory).toHaveBeenCalledWith(expect.anything(), "user-1", "story-1", expect.objectContaining({
        outcome: "Zero downtime cutover.",
        completeness: 2 / 6,
      }));
    });

    it("keeps the current value of an omitted field when recomputing completeness", async () => {
      // draftStory already has `situation` filled in; patching only `tags` should keep completeness at 1/6.
      await post({ action: "update", storyId: "story-1", tags: ["ownership"] });

      expect(mocks.updateCareerStory).toHaveBeenCalledWith(expect.anything(), "user-1", "story-1", expect.objectContaining({
        completeness: 1 / 6,
      }));
    });

    it("treats an explicit null as clearing a field when recomputing completeness", async () => {
      // draftStory's only filled field is `situation`; explicitly nulling it should drop completeness to 0.
      await post({ action: "update", storyId: "story-1", situation: null });

      expect(mocks.updateCareerStory).toHaveBeenCalledWith(expect.anything(), "user-1", "story-1", expect.objectContaining({
        situation: null,
        completeness: 0,
      }));
    });

    it("ignores a browser-supplied reviewState -- update never changes review state", async () => {
      await post({ action: "update", storyId: "story-1", reviewState: "confirmed" });

      expect(mocks.updateCareerStory).not.toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        "story-1",
        expect.objectContaining({ reviewState: expect.anything() }),
      );
    });
  });

  describe("confirm", () => {
    it("rejects a missing storyId", async () => {
      const response = await post({ action: "confirm" });

      expect(response.status).toBe(400);
      expect(mocks.updateCareerStory).not.toHaveBeenCalled();
    });

    it("sets reviewState to confirmed with a server-generated timestamp", async () => {
      const before = Date.now();
      const response = await post({ action: "confirm", storyId: "story-1" });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ story: draftStory });
      expect(mocks.updateCareerStory).toHaveBeenCalledTimes(1);
      const [, , , patch] = mocks.updateCareerStory.mock.calls[0];
      expect(patch.reviewState).toBe("confirmed");
      expect(typeof patch.confirmedAt).toBe("string");
      expect(Date.parse(patch.confirmedAt)).toBeGreaterThanOrEqual(before);
    });

    it("ignores a browser-supplied confirmedAt -- the timestamp is always server-generated", async () => {
      await post({ action: "confirm", storyId: "story-1", confirmedAt: "2020-01-01T00:00:00.000Z" });

      const [, , , patch] = mocks.updateCareerStory.mock.calls[0];
      expect(patch.confirmedAt).not.toBe("2020-01-01T00:00:00.000Z");
    });
  });

  describe("retire", () => {
    it("rejects a missing storyId", async () => {
      const response = await post({ action: "retire" });

      expect(response.status).toBe(400);
      expect(mocks.updateCareerStory).not.toHaveBeenCalled();
    });

    it("sets reviewState to retired and touches nothing else", async () => {
      const response = await post({ action: "retire", storyId: "story-1" });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ story: draftStory });
      expect(mocks.updateCareerStory).toHaveBeenCalledWith(expect.anything(), "user-1", "story-1", { reviewState: "retired" });
    });
  });

  describe("attach_profile_evidence", () => {
    it("rejects a missing storyId", async () => {
      const response = await post({ action: "attach_profile_evidence", profileEvidenceId: "profile-evidence-1" });

      expect(response.status).toBe(400);
      expect(mocks.attachCareerStoryEvidence).not.toHaveBeenCalled();
    });

    it("rejects a missing profileEvidenceId", async () => {
      const response = await post({ action: "attach_profile_evidence", storyId: "story-1" });

      expect(response.status).toBe(400);
      expect(mocks.attachCareerStoryEvidence).not.toHaveBeenCalled();
    });

    it("attaches typed profile-evidence provenance and returns the created link", async () => {
      const response = await post({
        action: "attach_profile_evidence",
        storyId: "story-1",
        profileEvidenceId: "profile-evidence-1",
        note: "From resume bullet",
      });
      const body = await response.json();

      expect(response.status).toBe(200);
      expect(body).toEqual({ evidence });
      expect(mocks.attachCareerStoryEvidence).toHaveBeenCalledWith(
        expect.anything(),
        "user-1",
        "story-1",
        { kind: "profile_evidence", profileEvidenceId: "profile-evidence-1" },
        "From resume bullet",
      );
    });
  });

  it("returns 404 when the repository reports the row was not owned", async () => {
    mocks.updateCareerStory.mockRejectedValue(new RepositoryError("Could not update the career story.", "NO_OWNED_ROW"));

    const response = await post({ action: "retire", storyId: "story-1" });

    expect(response.status).toBe(404);
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";
import { GeminiRequestError } from "@/lib/gemini";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  analyzeProfile: vi.fn(),
  extractEngineeringEvidence: vi.fn(),
  assessProfileReadiness: vi.fn(),
  saveProfile: vi.fn(),
  extractPdfText: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/coach", () => ({
  analyzeProfile: mocks.analyzeProfile,
  extractEngineeringEvidence: mocks.extractEngineeringEvidence,
  assessProfileReadiness: mocks.assessProfileReadiness,
  extractPdfText: mocks.extractPdfText,
}));
vi.mock("@/lib/repositories/profile", () => ({ saveProfile: mocks.saveProfile, getProfile: vi.fn() }));

import { POST } from "@/app/api/profile/route";

describe("POST /api/profile", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
    mocks.analyzeProfile.mockResolvedValue({
      role: "Frontend Engineer",
      seniority: "Senior",
      summary: "Frontend engineer",
      narrative: "Owns frontend platforms.",
      expertise: ["React"],
      characteristics: ["Pragmatic"],
      competencies: [{ name: "React architecture", relevance: 1 }],
    });
    mocks.extractEngineeringEvidence.mockResolvedValue([]);
  });

  it("saves a sparse profile instead of gating on readiness", async () => {
    const readiness = {
      ready: false,
      missing: [
        "two concrete engineering projects or work examples",
        "identifiable technologies",
        "responsibilities or outcomes",
      ],
    };
    mocks.assessProfileReadiness.mockReturnValue(readiness);
    mocks.saveProfile.mockResolvedValue({
      userId: "user-1",
      role: "Frontend Engineer",
      seniority: "Senior",
      summary: "Frontend engineer",
      narrative: "Owns frontend platforms.",
      expertise: ["React"],
      characteristics: ["Pragmatic"],
      competencies: [{ name: "React architecture", relevance: 1 }],
      source: { cvText: "I am a developer.", coverLetter: "" },
      readiness,
      evidence: [],
      createdAt: "2026-08-29T10:00:00.000Z",
      updatedAt: "2026-08-29T10:00:00.000Z",
    });

    const response = await POST(new Request("http://localhost/api/profile", {
      method: "POST",
      body: JSON.stringify({ cvText: "I am a developer.", coverLetter: "" }),
    }));

    expect(response.status).toBe(200);
    expect(mocks.saveProfile).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      expect.objectContaining({ role: "Frontend Engineer" }),
      expect.objectContaining({ cvText: "I am a developer." }),
      [],
      readiness,
    );
  });

  it("saves a profile only after the evidence gate passes", async () => {
    const evidence = [{
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
    }];
    mocks.extractEngineeringEvidence.mockResolvedValue(evidence);
    mocks.assessProfileReadiness.mockReturnValue({ ready: true, missing: [] });
    mocks.saveProfile.mockResolvedValue({
      userId: "user-1",
      role: "Frontend Engineer",
      seniority: "Senior",
      summary: "Frontend engineer",
      narrative: "Owns frontend platforms.",
      expertise: ["React"],
      characteristics: ["Pragmatic"],
      competencies: [{ name: "React architecture", relevance: 1 }],
      source: { cvText: "CV text", coverLetter: "" },
      readiness: { ready: true, missing: [] },
      evidence,
      createdAt: "2026-08-29T10:00:00.000Z",
      updatedAt: "2026-08-29T10:00:00.000Z",
    });

    const response = await POST(new Request("http://localhost/api/profile", {
      method: "POST",
      body: JSON.stringify({ cvText: "I led a React migration.", coverLetter: "" }),
    }));

    expect(response.status).toBe(200);
    expect(mocks.saveProfile).toHaveBeenCalledWith(
      expect.anything(),
      "user-1",
      expect.objectContaining({ role: "Frontend Engineer" }),
      expect.objectContaining({ cvText: "I led a React migration." }),
      evidence,
      { ready: true, missing: [] },
    );
  });

  it("surfaces a provider-specific status when PDF extraction is rate limited", async () => {
    mocks.extractPdfText.mockRejectedValue(new GeminiRequestError(
      "the PDF",
      429,
      "rate-limited",
      "Gemini rate limit reached for the PDF (429). Wait briefly and try again.",
    ));

    const formData = new FormData();
    formData.append("cv", new File(["%PDF-1.7 test"], "cv.pdf", { type: "application/pdf" }));
    formData.append("coverLetter", "");

    const request = new Request("http://localhost/api/profile", {
      method: "POST",
      headers: { "content-type": "multipart/form-data; boundary=test" },
    });
    vi.spyOn(request, "formData").mockResolvedValue(formData);

    const response = await POST(request);

    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: "Gemini rate limit reached for the PDF (429). Wait briefly and try again.",
    });
    expect(mocks.analyzeProfile).not.toHaveBeenCalled();
    expect(mocks.extractEngineeringEvidence).not.toHaveBeenCalled();
  });
});

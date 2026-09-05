import { describe, expect, it } from "vitest";
import { BACKEND_SENIORITY_CALIBRATION, BACKEND_TOPIC_AREAS } from "@/lib/backend-topics";

describe("BACKEND_TOPIC_AREAS", () => {
  it("lists exactly the seven areas issue #21 scopes backend practice to", () => {
    expect(BACKEND_TOPIC_AREAS).toEqual([
      "API design",
      "persistence and database fundamentals",
      "authentication and authorization",
      "caching",
      "concurrency and reliability",
      "backend boundaries",
      "operational reasoning",
    ]);
  });

  it("has no duplicate or empty entries", () => {
    const unique = new Set(BACKEND_TOPIC_AREAS);
    expect(unique.size).toBe(BACKEND_TOPIC_AREAS.length);
    for (const topic of BACKEND_TOPIC_AREAS) expect(topic.trim().length).toBeGreaterThan(0);
  });
});

describe("BACKEND_SENIORITY_CALIBRATION", () => {
  it("names the frontend-leaning full-stack bar, not a backend-specialist bar", () => {
    expect(BACKEND_SENIORITY_CALIBRATION).toContain("frontend-leaning");
    expect(BACKEND_SENIORITY_CALIBRATION).not.toContain("DBA-level depth needed");
  });
});

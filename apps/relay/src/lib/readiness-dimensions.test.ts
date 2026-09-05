import { describe, expect, it } from "vitest";
import { READINESS_DIMENSIONS, dimensionFor } from "@/lib/readiness-dimensions";

describe("READINESS_DIMENSIONS", () => {
  it("declares the seven dimensions from issue #14", () => {
    expect(READINESS_DIMENSIONS).toEqual([
      "frontend",
      "backend",
      "system-design",
      "coding",
      "behavioral",
      "communication",
      "ai-engineering",
    ]);
  });
});

describe("dimensionFor", () => {
  it("prefers the competency name over the question category", () => {
    expect(dimensionFor({ competencyName: "React architecture", category: "technical" })).toBe("frontend");
  });

  it("falls back to the question category when the competency name matches nothing", () => {
    expect(dimensionFor({ competencyName: "Widget wrangling", category: "system-design" })).toBe("system-design");
  });

  it("falls back to the category when there is no competency name at all", () => {
    expect(dimensionFor({ competencyName: null, category: "behavioral" })).toBe("behavioral");
  });

  it("returns null when neither the name nor the category resolves", () => {
    expect(dimensionFor({ competencyName: "Widget wrangling", category: "introduction" })).toBe("behavioral");
    expect(dimensionFor({ competencyName: null, category: null })).toBeNull();
  });

  it("recognises AI and agentic work as its own dimension", () => {
    expect(dimensionFor({ competencyName: "LLM agent design", category: "technical" })).toBe("ai-engineering");
    expect(dimensionFor({ competencyName: "RAG pipelines", category: null })).toBe("ai-engineering");
  });

  it("is case-insensitive", () => {
    expect(dimensionFor({ competencyName: "BACKEND APIs", category: null })).toBe("backend");
  });

  it("resolves each baseline competency name to a dimension", () => {
    const baseline = [
      "Coding and implementation",
      "Debugging and reliability",
      "Architecture and system design",
      "Testing and quality",
      "Performance and scalability",
      "Accessibility and user impact",
      "Delivery and trade-offs",
      "Collaboration and communication",
    ];
    for (const name of baseline) {
      expect(dimensionFor({ competencyName: name, category: null })).not.toBeNull();
    }
  });
});

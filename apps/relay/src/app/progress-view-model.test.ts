import { describe, expect, it } from "vitest";
import type { ReadinessModel } from "@/lib/types";
import { readinessViewModel } from "@/app/progress-view-model";

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

describe("readinessViewModel", () => {
  it("exposes the overall readiness score and the weakest confident dimension", () => {
    const model = readinessModel({
      overall: 72,
      overallConfidence: "medium",
      dimensions: [
        {
          dimension: "backend",
          score: 88,
          confidence: "high",
          trend: "stable",
          evidenceCount: 4,
          totalWeight: 4,
          contributions: [],
        },
        {
          dimension: "system-design",
          score: 41,
          confidence: "medium",
          trend: "stable",
          evidenceCount: 2,
          totalWeight: 2,
          contributions: [],
        },
        // No evidence at all -- an unknown, not a weakness, so it must
        // never be picked over a lower-scoring but confident dimension.
        {
          dimension: "communication",
          score: null,
          confidence: null,
          trend: "unresolved",
          evidenceCount: 0,
          totalWeight: 0,
          contributions: [],
        },
      ],
    });

    const view = readinessViewModel(model);

    expect(view.hasEvidence).toBe(true);
    expect(view.readiness).toBe(72);
    expect(view.weakest?.dimension).toBe("system-design");
  });

  it("reports no evidence when nothing has been graded yet", () => {
    expect(readinessViewModel(readinessModel()).hasEvidence).toBe(false);
  });

  it("returns the empty-state model when no readiness model is available", () => {
    expect(readinessViewModel(null)).toEqual({
      hasEvidence: false,
      readiness: null,
      weakest: null,
    });
  });
});

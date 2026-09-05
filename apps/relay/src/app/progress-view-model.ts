import { weakestConfidentDimension } from "@/lib/readiness";
import type { ReadinessDimensionResult, ReadinessModel } from "@/lib/types";

export type ReadinessViewModel = {
  hasEvidence: boolean;
  readiness: number | null;
  weakest: ReadinessDimensionResult | null;
};

/**
 * Selects the server-calculated readiness fields that drive the UI. The
 * page deliberately consumes the model as-is instead of rebuilding
 * readiness or practice focus from profile competency aggregates.
 */
export function readinessViewModel(model: ReadinessModel | null): ReadinessViewModel {
  return {
    hasEvidence: model !== null && model.overall !== null,
    readiness: model?.overall ?? null,
    weakest: model ? weakestConfidentDimension(model) : null,
  };
}

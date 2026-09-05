import { describe, expect, it } from "vitest";
import { applyEvaluation } from "@/lib/competencies";
import type { Competency, Evaluation } from "@/lib/types";

const unassessedReact: Competency = {
  id: "react",
  name: "React",
  relevance: 1,
  expectedLevel: "senior",
  estimatedLevel: null,
  confidence: null,
  lastPracticedAt: null,
  questionCount: 0,
  averageScore: null,
  recentScore: null,
  strengths: [],
  weaknesses: [],
};

const evaluation = (score: number, strengths: string[] = [], needsWork: string[] = []): Evaluation => ({
  score,
  competencyId: "react",
  competency: "React",
  dimensions: {},
  strengths,
  needsWork,
  missingPoints: [],
  betterStructure: [],
  improvedAnswer: "",
});

describe("applyEvaluation", () => {
  it("turns an unassessed competency into low-confidence evidence", () => {
    const next = applyEvaluation(unassessedReact, evaluation(7), "2026-08-29T12:00:00.000Z");

    expect(next.questionCount).toBe(1);
    expect(next.averageScore).toBe(7);
    expect(next.confidence).toBe("low");
    expect(next.estimatedLevel).toBe("senior");
  });

  it("keeps a recent score and promotes confidence after three answers", () => {
    const afterThree = [6, 7, 8].reduce(
      (competency, score) => applyEvaluation(competency, evaluation(score), "2026-08-29T12:00:00.000Z"),
      unassessedReact,
    );

    expect(afterThree.recentScore).toBe(8);
    expect(afterThree.averageScore).toBe(7);
    expect(afterThree.confidence).toBe("medium");
  });

  it("keeps scores and averages within the valid 0-to-10 range", () => {
    const low = applyEvaluation(unassessedReact, evaluation(-1), "now");
    const high = applyEvaluation(unassessedReact, evaluation(11), "now");
    const invalid = applyEvaluation(unassessedReact, evaluation(Number.NaN), "now");
    const repaired = applyEvaluation({ ...unassessedReact, questionCount: 1, averageScore: Number.NaN }, evaluation(8), "now");

    expect(low.recentScore).toBe(0);
    expect(low.averageScore).toBe(0);
    expect(high.recentScore).toBe(10);
    expect(high.averageScore).toBe(10);
    expect(invalid.recentScore).toBe(0);
    expect(invalid.averageScore).toBe(0);
    expect(repaired.averageScore).toBe(4);
  });

  it("uses the specified confidence boundaries", () => {
    const afterTwo = [1, 2].reduce(
      (competency, score) => applyEvaluation(competency, evaluation(score), "now"),
      unassessedReact,
    );
    const afterFive = [1, 2, 3, 4, 5].reduce(
      (competency, score) => applyEvaluation(competency, evaluation(score), "now"),
      unassessedReact,
    );
    const afterSix = [1, 2, 3, 4, 5, 6].reduce(
      (competency, score) => applyEvaluation(competency, evaluation(score), "now"),
      unassessedReact,
    );

    expect(afterTwo.confidence).toBe("low");
    expect(afterFive.confidence).toBe("medium");
    expect(afterSix.confidence).toBe("high");
  });

  it("merges non-empty evidence uniquely and retains only the most recent five entries", () => {
    const competency = applyEvaluation(
      { ...unassessedReact, strengths: ["existing"] },
      evaluation(5, ["existing", "one", "", "two", "three"], ["gap", "", "other"]),
      "2026-08-29T12:00:00.000Z",
    );
    const next = applyEvaluation(
      competency,
      evaluation(6, ["two", "four", "five", "six"], ["gap", "new gap"]),
      "2026-08-30T12:00:00.000Z",
    );

    expect(next.strengths).toEqual(["three", "two", "four", "five", "six"]);
    expect(next.weaknesses).toEqual(["other", "gap", "new gap"]);
  });

  it("maps score thresholds and leaves no-evidence estimates null", () => {
    expect(applyEvaluation(unassessedReact, evaluation(5.5), "now").estimatedLevel).toBe("senior");
    expect(applyEvaluation(unassessedReact, evaluation(7.5), "now").estimatedLevel).toBe("advanced");
    expect(unassessedReact.estimatedLevel).toBeNull();
  });

  it("does not mutate the competency input", () => {
    const before = structuredClone(unassessedReact);

    applyEvaluation(unassessedReact, evaluation(7, ["hooks"], ["testing"]), "now");

    expect(unassessedReact).toEqual(before);
  });
});

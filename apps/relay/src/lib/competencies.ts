import type { Competency, Difficulty, Evaluation } from "@/lib/types";

const clampScore = (score: number): number => {
  if (!Number.isFinite(score)) return 0;
  return Math.min(10, Math.max(0, score));
};

const safeQuestionCount = (questionCount: number): number => {
  if (!Number.isFinite(questionCount) || questionCount < 0) return 0;
  return Math.floor(questionCount);
};

const estimatedLevelFor = (averageScore: number): Difficulty => {
  if (averageScore < 5.5) return "intermediate";
  if (averageScore < 7.5) return "senior";
  return "advanced";
};

const confidenceFor = (questionCount: number): Competency["confidence"] => {
  if (questionCount === 0) return null;
  if (questionCount < 3) return "low";
  if (questionCount < 6) return "medium";
  return "high";
};

const mergeRecent = (existing: string[], additions: string[]): string[] => {
  const values = [...existing, ...additions]
    .filter((value) => value.length > 0);
  const unique = values.filter((value, index) => values.lastIndexOf(value) === index);
  return unique.slice(-5);
};

export function applyEvaluation(
  competency: Competency,
  evaluation: Evaluation,
  practicedAt: string,
): Competency {
  const previousQuestionCount = safeQuestionCount(competency.questionCount);
  const questionCount = previousQuestionCount + 1;
  const previousAverage = clampScore(competency.averageScore ?? 0);
  const score = clampScore(evaluation.score);
  const previousTotal = previousAverage * previousQuestionCount;
  const averageScore = clampScore((previousTotal + score) / questionCount);

  return {
    ...competency,
    estimatedLevel: estimatedLevelFor(averageScore),
    confidence: confidenceFor(questionCount),
    lastPracticedAt: practicedAt,
    questionCount,
    averageScore,
    recentScore: score,
    strengths: mergeRecent(competency.strengths, evaluation.strengths),
    weaknesses: mergeRecent(competency.weaknesses, evaluation.needsWork),
  };
}

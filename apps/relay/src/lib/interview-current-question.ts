import type { PlannedQuestion } from "@/lib/types";

/**
 * Whether this row is still waiting on the candidate.
 *
 * Both halves are load-bearing. `answer` alone cannot decide it: a non-answer
 * is deliberately never scored, so a row the candidate blanked on keeps a null
 * `answer` forever and would stay current for the rest of the interview
 * (issue #10). The set-aside marker is the other way a row can be finished.
 */
export function isAwaitingAnswer(question: PlannedQuestion): boolean {
  return question.answer === null && question.setAsideAt === null;
}

/**
 * The one question the candidate is looking at, or null when the interview has
 * run out of rows. Callers must not re-derive this: the rule lives here so the
 * question picker, the transcript, and the completion check cannot drift apart.
 */
export function currentQuestion(questions: PlannedQuestion[]): PlannedQuestion | null {
  return questions.find(isAwaitingAnswer) ?? null;
}

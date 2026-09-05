import { canContinueOnAnsweredRow, targetIdOf } from "@/lib/interview-coverage";
import { questionIdForTarget } from "@/lib/repositories/interviews";
import type { FollowUpDraft, InterviewSession, PlannedQuestion } from "@/lib/types";

/**
 * The `NextTurnResult` fields the write resolution below actually reads. Kept
 * structural rather than importing `NextTurnResult` so this module does not
 * depend on `@/lib/coach` (which depends on the director, which this feeds).
 */
export type ResolvableTurn = {
  targetId: string | null;
  prompt: string;
  nonAnswer: boolean;
};

/** Where `record_conversation_turn` should put the next interviewer question. */
export type NextQuestionWrite = {
  nextQuestionId: string | null;
  followUp: FollowUpDraft | null;
};

/**
 * True when this question's prompt was written by a planner before the session
 * started rather than by the director's interviewer call.
 *
 * A data-shape discriminator, not a session-kind flag: the adaptive creator
 * (`create_conversation_session_with_blueprint`) inserts rows with a null
 * prompt and stamps an `askedIntent` on each one as it is asked, while the
 * planned-practice creator
 * (`create_planned_conversation_session_with_blueprint`) inserts rows that
 * already carry their prompt and never carry an intent. Legacy conversation
 * sessions created before this release have the same shape as the planned ones
 * and are correctly treated the same way.
 */
export function isPreWrittenQuestion(question: PlannedQuestion): boolean {
  return question.askedIntent === null && Boolean(question.prompt);
}

/**
 * Decides which row carries the prompt the interviewer just authored.
 *
 * `Intent.targetId` names a COVERAGE TARGET, not a row, and the two only
 * coincide for a target's own first row. Resolving one to the other is this
 * function's whole job, and it has three cases:
 *
 * 1. A pre-written question (planned practice, or a legacy session): the
 *    session is not driven by a coverage plan at all, so nothing is written.
 *    The next pre-written row already carries its own prompt and is revealed
 *    by `transcriptFor` as soon as this one is answered. Routing such a
 *    session through the follow-up branch instead would either overwrite a
 *    planned prompt with an adaptive line or -- when the row's persisted
 *    `follow_up_limit` is 0, as every practice introduction's is -- make the
 *    RPC raise "Conversation follow-up limit reached".
 * 2. A same-target continuation (probe/challenge/hypothetical after a real
 *    answer): the just-answered row cannot carry it, because
 *    `record_conversation_turn`'s next-question branch requires
 *    `answer is null` and the evidence step earlier in the same call already
 *    set that column. It goes onto a fresh follow-up row instead. The target
 *    is compared against the ANSWERED ROW'S `askedIntent.targetId`, never
 *    against the row's own id: a follow-up row has its own fresh id while
 *    still belonging to the parent target, so an id comparison would miss
 *    every continuation past the first and leave the next question blank.
 * 3. Anything else -- an advance, an open, or a rescue that re-asks the same
 *    still-unanswered row after a non-answer -- updates the target's own row
 *    in place.
 */
export function resolveNextQuestionWrite(
  session: InterviewSession,
  answeredQuestion: PlannedQuestion,
  turn: ResolvableTurn,
): NextQuestionWrite {
  if (isPreWrittenQuestion(answeredQuestion)) return { nextQuestionId: null, followUp: null };

  const answeredTargetId = answeredQuestion.askedIntent ? targetIdOf(answeredQuestion.askedIntent) : null;
  // A non-answer never sets the row's `answer` column, so a rescue on the same
  // target keeps re-asking that same row through case 3 -- which is required:
  // the RPC refuses a follow-up whose parent is itself a follow-up row, and
  // spending a continuation on a turn that never needed one would strand the
  // target when the candidate recovers.
  const isSameTargetContinuation = !turn.nonAnswer
    && turn.targetId !== null
    && turn.targetId === answeredTargetId;

  if (isSameTargetContinuation && canContinueOnAnsweredRow(session.questions, answeredQuestion, session.blueprint ?? null)) {
    return { nextQuestionId: null, followUp: followUpDraftForContinuation(answeredQuestion, turn.prompt) };
  }

  return {
    nextQuestionId: turn.targetId ? questionIdForTarget(session, turn.targetId) : null,
    followUp: null,
  };
}

/**
 * Builds the follow-up row payload for a same-target continuation.
 *
 * Takes the just-answered `PlannedQuestion` rather than its `CoverageTarget`:
 * both carry the same objective/evidence/signal data (both are read off the
 * same persisted row), but the question is always available -- unlike
 * `session.blueprint.targets`, which a legacy conversation session predating
 * coverage targets may not carry. Copying the rubric fields forward is what
 * keeps the new row scoreable against the target it continues:
 * `groundedQuestion` reads them off the answered row, not off the blueprint.
 */
function followUpDraftForContinuation(question: PlannedQuestion, prompt: string): FollowUpDraft {
  return {
    category: question.category,
    competencyId: question.competencyId,
    competencyName: question.competencyName,
    difficulty: question.difficulty,
    isFollowUp: true,
    prompt,
    objective: question.objective ?? "",
    evidenceIds: question.evidenceIds ?? [],
    expectedSignals: question.expectedSignals ?? [],
    missingSignalPrompts: [],
    rubricCriteria: question.rubricCriteria ?? [],
    followUpLimit: 1,
    sourceConfidence: null,
  };
}

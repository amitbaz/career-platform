import type { InterviewSession } from "@/lib/types";

/**
 * Whether the user may end a conversation early, from the Complete action.
 *
 * A planned practice conversation is shorter than the generic five-question
 * backbone, so the five-answer rule does not apply to it: it may finish once
 * every currently persisted question is answered. That deliberately includes
 * follow-ups added mid-conversation -- an unanswered persisted follow-up
 * blocks explicit completion, because the follow-up exists precisely because
 * the plan's objective was not yet met. Generic/manual conversations keep the
 * existing five-answer rule.
 *
 * This module carries no `server-only` marker on purpose: the interview view
 * must gate its Finish control on exactly the rule the API enforces, and a
 * divergent client copy previously left Finish permanently disabled for every
 * planned format with fewer than five base questions.
 */
export function canExplicitlyCompleteConversation(session: InterviewSession): boolean {
  if (!session.practicePlanId) {
    return session.questions.filter((question) => question.answer).length >= 5;
  }
  return session.questions.every((question) => Boolean(question.answer));
}

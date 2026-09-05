import { NextResponse } from "next/server";
import { completeSession as summarizeSession, EVALUATION_DIMENSIONS, evaluateHandsOn, generateInterviewBlueprint, handsOnCheckpoint, handsOnExercise, nextTurn, openingTurn } from "@/lib/coach";
import { canExplicitlyCompleteConversation } from "@/lib/conversation-completion";
import { deriveCoverageState, uncoveredTargets, type UncoveredTarget } from "@/lib/interview-coverage";
import { currentQuestion, isAwaitingAnswer } from "@/lib/interview-current-question";
import { IMPLEMENTED_ROUNDS } from "@/lib/interview-rounds";
import { isPreWrittenQuestion, resolveNextQuestionWrite } from "@/lib/interview-turn-write";
import { completeLinkedPracticePlanBestEffort } from "@/lib/practice-service";
import { calculateReadiness } from "@/lib/readiness";
import {
  completeHandsOnSession,
  completeSession,
  createHandsOnSession,
  createSessionWithBlueprint,
  getSession,
  listReadinessEvidence,
  listRecentSessions,
  recordConversationTurn,
  revealFirstQuestion,
  saveHandsOnCheckpoint,
} from "@/lib/repositories/interviews";
import { getOpportunity } from "@/lib/repositories/opportunities";
import { getProfile } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";
import type { Evaluation, InterviewMode, InterviewSession, PlannedQuestion, Profile, RoundId } from "@/lib/types";

export const runtime = "nodejs";

/** Lists only interview sessions owned by the authenticated caller, plus their evidence-backed readiness model. */
export async function GET() {
  try {
    const { supabase, user } = await requireUser();
    const now = new Date();
    const [sessions, evidence] = await Promise.all([
      listRecentSessions(supabase, user.id),
      listReadinessEvidence(supabase, user.id),
    ]);
    return NextResponse.json({
      sessions,
      readiness: calculateReadiness(evidence, now),
    });
  } catch (error) {
    return errorResponse(error);
  }
}

/** Runs authenticated start, turn, checkpoint, and completion interview actions. */
export async function POST(request: Request) {
  let supabase;
  let user;
  try {
    ({ supabase, user } = await requireUser());
  } catch (error) {
    return errorResponse(error);
  }

  let body: Record<string, unknown>;
  try {
    const parsed = await request.json();
    if (!parsed || typeof parsed !== "object") throw new Error("INVALID_BODY");
    body = parsed as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "A valid interview request is required." }, { status: 400 });
  }

  try {
    const profile = await getProfile(supabase, user.id);
    if (!profile) return NextResponse.json({ error: "Create your profile first." }, { status: 400 });

    if (body.action === "start") {
      if (body.mode === "hands-on") {
        const session = await createHandsOnSession(supabase, user.id, handsOnExercise(profile));
        return NextResponse.json({ session });
      }

      const roundId = (typeof body.roundId === "string" ? body.roundId : "tech-lead") as RoundId;
      if (!IMPLEMENTED_ROUNDS.includes(roundId)) {
        return NextResponse.json({ error: "That interview round is not available yet." }, { status: 400 });
      }
      const mode: InterviewMode = body.mode === "coach" ? "coach" : "real";
      const opportunityId = typeof body.opportunityId === "string" ? body.opportunityId : null;
      const opportunity = opportunityId ? await getOpportunity(supabase, user.id, opportunityId) : null;

      const blueprint = await generateInterviewBlueprint(profile, profile.evidence ?? [], { roundId, opportunity });
      // `createSessionWithBlueprint` deliberately does not take `opportunityId`
      // (Ruling A) -- only `linkSessionCareerContext` ever writes it.
      const session = await createSessionWithBlueprint(supabase, user.id, blueprint, { roundId, mode });

      // `blueprint: session.blueprint!`, NOT the pre-persistence `blueprint`
      // above: `session.blueprint.targets` carry the row ids `openingTurn`'s
      // resulting `targetId` must persist as, so later turns (which reload
      // the session and reconstruct targets from those same row ids) can
      // still match it. The original `blueprint.targets` use transient
      // `gap-0`/`competency-0` ids that never round-trip through the
      // database, which would otherwise break coverage tracking from the
      // second turn onward.
      const opening = await openingTurn({
        profile,
        session,
        blueprint: session.blueprint!,
        evidence: profile.evidence ?? [],
        opportunity,
      });
      const revealed = await revealFirstQuestion(supabase, user.id, session, opening);
      return NextResponse.json({ session: visibleConversation(revealed) });
    }

    const sessionId = typeof body.sessionId === "string" ? body.sessionId : "";
    const session = sessionId ? await getSession(supabase, user.id, sessionId) : null;
    if (!session) return NextResponse.json({ error: "Interview not found." }, { status: 404 });
    if (session.status === "complete") return NextResponse.json({ error: "This interview is already complete." }, { status: 400 });

    if (body.action === "respond") {
      if (session.kind === "hands-on") return NextResponse.json({ error: "Use a coding checkpoint for a hands-on session." }, { status: 400 });
      const answer = typeof body.answer === "string" ? body.answer.trim() : "";
      if (!answer) return NextResponse.json({ error: "Write an answer before sending." }, { status: 400 });
      const question = currentQuestion(session.questions);
      if (!question) return finishConversation(supabase, user.id, profile, session);

      const hydratedQuestion = hydratePlannedQuestion(session, question);
      const turn = await nextTurn({
        profile,
        session: visibleConversation(session),
        answeredQuestion: hydratedQuestion,
        answer,
        blueprint: session.blueprint!,
        evidence: profile.evidence ?? [],
        opportunity: session.opportunityId ? await getOpportunity(supabase, user.id, session.opportunityId) : null,
      });

      // Resolving the director's coverage-target id to the row that will carry
      // the next prompt lives in one place, shared with the flow tests -- see
      // `resolveNextQuestionWrite`'s doc comment for the three cases.
      const write = resolveNextQuestionWrite(session, hydratedQuestion, turn);
      // A pre-written session (planned practice, or a legacy conversation) is
      // not driven by the coverage plan, so it also keeps the pre-adaptive
      // "every turn advances" behaviour: treating a blank answer as a
      // non-answer there would leave the row unanswered with its planned
      // prompt unchanged, silently discarding what the candidate wrote.
      const nonAnswer = turn.nonAnswer && !isPreWrittenQuestion(hydratedQuestion);
      // Gated on `nonAnswer` for the same reason it is: a pre-written session
      // (planned practice, or a legacy conversation) is not driven by the
      // coverage plan, so its rows always advance by being answered and must
      // never be set aside.
      const setAsideReason = nonAnswer ? turn.setAside : null;

      const updated = await recordConversationTurn(
        supabase,
        user.id,
        question.id,
        answer,
        turn.evaluation ?? emptyEvaluationFor(hydratedQuestion),
        {
          nextQuestionId: write.nextQuestionId,
          nextPrompt: turn.prompt,
          followUp: write.followUp,
          askedIntent: turn.intent,
          // Accumulates onto the row's own persisted history, not just this
          // turn's grant: a non-answer continuation re-asks the SAME row
          // (see above), so a rescue budget check like `rescuesSpentInSession`
          // (which sums `question.assistance.length`) would silently lose
          // every earlier rescue on that row if this replaced instead.
          assistance: [...question.assistance, ...(turn.assistance ? [turn.assistance] : [])],
          nonAnswer,
          degraded: turn.degraded,
          setAsideReason,
        },
      );
      if (!currentQuestion(updated.questions)) {
        return finishConversation(supabase, user.id, profile, updated);
      }
      return NextResponse.json({ session: visibleConversation(updated) });
    }
    if (body.action === "checkpoint") {
      if (session.kind !== "hands-on") return NextResponse.json({ error: "Coding checkpoints are available in hands-on interviews." }, { status: 400 });
      const code = typeof body.code === "string" ? body.code : "";
      const note = typeof body.note === "string" ? body.note.trim() : "";
      if (!code.trim() || !note) return NextResponse.json({ error: "Save your current code and a short think-aloud note." }, { status: 400 });
      const prompt = await handsOnCheckpoint(profile, session, code, note);
      const updated = await saveHandsOnCheckpoint(supabase, user.id, session.id, code, note, prompt);
      return NextResponse.json({ session: updated });
    }
    if (body.action === "complete") {
      if (session.kind === "conversation" && !canExplicitlyCompleteConversation(session)) {
        return NextResponse.json({ error: incompleteConversationMessage(session) }, { status: 400 });
      }
      if (session.kind === "hands-on" && !session.checkpoints.length) {
        return NextResponse.json({ error: "Save a coding checkpoint before completing this interview." }, { status: 400 });
      }
      const result = session.kind === "hands-on" ? evaluateHandsOn(session) : summarizeSession(session);
      const completed = session.kind === "hands-on"
        ? await completeHandsOnSession(supabase, user.id, session.id, result as ReturnType<typeof evaluateHandsOn>)
        : await completeSession(supabase, user.id, session.id, result);
      // The session and its evidence are already saved; plan bookkeeping runs
      // afterwards and can only add a warning, never fail this response.
      const { warning } = await completeLinkedPracticePlanBestEffort(supabase, user.id, completed);
      return NextResponse.json({
        session: completed.kind === "conversation" ? visibleConversation(completed) : completed,
        profile: await getProfile(supabase, user.id),
        // Always present, `null` when there is nothing to warn about: clients
        // read this as a nullable field, never as an optional key.
        practicePlanWarning: warning,
        coverage: coverageFor(completed),
      });
    }
    return NextResponse.json({ error: "Unknown action." }, { status: 400 });
  } catch (error) {
    return errorResponse(error);
  }
}

async function finishConversation(
  supabase: Parameters<typeof completeSession>[0],
  userId: string,
  profile: Profile,
  session: InterviewSession,
) {
  const completed = await completeSession(supabase, userId, session.id, summarizeSession(session));
  const { warning } = await completeLinkedPracticePlanBestEffort(supabase, userId, completed);
  const refreshedProfile = await getProfile(supabase, userId);
  return NextResponse.json({
    session: visibleConversation(completed),
    profile: refreshedProfile ?? profile,
    practicePlanWarning: warning,
    coverage: coverageFor(completed),
  });
}

/**
 * Why an explicit Complete was refused. Planned practice and generic
 * conversations have different completion rules (see
 * `canExplicitlyCompleteConversation`), so they need different guidance: a
 * planned session must answer every persisted question, including a
 * follow-up, while a generic one needs five answers.
 */
function incompleteConversationMessage(session: InterviewSession): string {
  return session.practicePlanId
    ? "Answer every question in this practice before completing it."
    : "Answer at least five questions before completing this interview.";
}

/**
 * The coverage report attached to a finished interview. Empty for a hands-on
 * or legacy session, which have no coverage plan to report against.
 */
function coverageFor(session: InterviewSession): UncoveredTarget[] {
  if (session.kind !== "conversation" || !session.blueprint) return [];
  return uncoveredTargets(deriveCoverageState(session.blueprint.targets, session.questions, session.evaluations));
}

function visibleConversation(session: InterviewSession): InterviewSession {
  if (!session.questions) return session;
  const visibleQuestionIds = new Set(
    session.questions.filter((question) => !isAwaitingAnswer(question)).map((question) => question.id),
  );
  const nextQuestion = currentQuestion(session.questions);
  if (nextQuestion) visibleQuestionIds.add(nextQuestion.id);
  return {
    ...session,
    messages: session.messages.filter((message) => visibleQuestionIds.has(message.id.split(":")[0])),
  };
}

/**
 * Placeholder evaluation values for a turn the assessor could not score. The
 * RPC skips evidence recording when `p_non_answer` is true, so on the adaptive
 * path these only satisfy the RPC's non-null parameters (spec §11.3). On a
 * pre-written session, where a turn always advances, they ARE persisted -- and
 * "Not attempted." is then the honest record of what happened.
 */
function emptyEvaluationFor(question: PlannedQuestion): Evaluation {
  return {
    questionId: question.id,
    competencyId: question.competencyId,
    competency: question.competencyName ?? "Communication",
    score: 0,
    relevance: 0,
    dimensions: Object.fromEntries(EVALUATION_DIMENSIONS.map((key) => [key, 0])) as Evaluation["dimensions"],
    strengths: [],
    needsWork: [],
    missingPoints: ["Not attempted."],
    betterStructure: ["Not attempted."],
    improvedAnswer: "Not attempted.",
    supportedClaims: [],
    expectedSignalsPresent: [],
    unsupportedClaims: [],
    dimensionReasons: Object.fromEntries(
      EVALUATION_DIMENSIONS.map((key) => [key, "Not attempted."]),
    ) as Evaluation["dimensionReasons"],
  } as Evaluation;
}

function hydratePlannedQuestion(
  session: InterviewSession,
  question: InterviewSession["questions"][number],
): InterviewSession["questions"][number] {
  return session.blueprint?.questions.find((item) => item.id === question.id) ?? question;
}

function errorResponse(error: unknown) {
  if (error instanceof Error) {
    const code = typeof error === "object" && error !== null && "code" in error
      ? (error as { code?: unknown }).code
      : undefined;
    console.error("[api/interview] request failed", {
      name: error.name,
      message: error.message,
      code,
    });
  } else {
    console.error("[api/interview] request failed", error);
  }
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof Error && "code" in error && error.code === "NO_OWNED_ROW") {
    return NextResponse.json({ error: "Interview not found." }, { status: 404 });
  }
  return NextResponse.json({ error: "Could not complete your interview request." }, { status: 500 });
}

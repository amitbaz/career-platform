import { afterEach, describe, expect, it, vi } from "vitest";
import { EVALUATION_DIMENSIONS, generateInterviewBlueprint, nextTurn, openingTurn } from "@/lib/coach";
import { currentQuestion } from "@/lib/interview-current-question";
import { deriveCoverageState } from "@/lib/interview-coverage";
import { resolveNextQuestionWrite } from "@/lib/interview-turn-write";
import {
  createSessionWithBlueprint,
  recordConversationTurn,
  revealFirstQuestion,
} from "@/lib/repositories/interviews";
import type {
  CompetencyScope,
  Evaluation,
  EvidenceItem,
  InterviewMode,
  InterviewSession,
  Opportunity,
  PlannedQuestion,
  Profile,
  ProfileDraft,
  RoundId,
} from "@/lib/types";

/**
 * End-to-end coverage for the full adaptive-interviewer pipeline (spec §16):
 * blueprint generation (Task 8) -> round-based session creation (Task 10) ->
 * the opening turn (Task 7) -> repeated response turns (Task 7 + Task 10),
 * entirely in-process. This is the sibling to `release2-flow.test.ts`, but
 * drives `createSessionWithBlueprint` (the ROUND-based creator) instead of
 * `createSessionWithPracticeBlueprint` (the PLANNED-PRACTICE creator that
 * file exercises), so it is written from scratch rather than copied.
 *
 * Unlike `release2-flow.test.ts` (which mocks `@/lib/repositories/interviews`
 * entirely and hand-rolls a persistence stand-in), this file mocks NOTHING
 * at the repository-function level. `createSessionWithBlueprint`,
 * `revealFirstQuestion`, `recordConversationTurn`, and the `getSession`
 * reload path it all funnels through run for REAL, unmocked, against a fake
 * Supabase CLIENT (`rpc`/`from`) backed by in-memory tables. The fake RPCs
 * (`create_conversation_session_with_blueprint`, `record_conversation_turn`)
 * reproduce the exact field semantics of the live Postgres functions in
 * `supabase/migrations/202609010001_adaptive_interviewer.sql` and
 * `supabase/migrations/202609020001_coverage_target_required.sql` -- see the
 * fake RPC implementations below for a running commentary tying each branch
 * back to its SQL counterpart.
 *
 * Gemini is stubbed via a queue this file fills in call order: one
 * "interviewer line" response per `openingTurn`/`speakIntent` call, and one
 * "answer evaluation" (assessor) response immediately before each `nextTurn`
 * call's matching "interviewer line" response, mirroring the two-call shape
 * `nextTurn` makes every turn (assessor, then the interviewer line).
 * `GEMINI_API_KEY` is deliberately EMPTY during blueprint generation (so
 * `generateInterviewBlueprint` takes its deterministic fallback path with
 * zero network calls, the same technique `release2-flow.test.ts` uses) and
 * only set once the turn loop starts, where a controllable `read` per
 * scripted answer and a distinct line per call are actually needed.
 */

type Row = Record<string, unknown>;

const NOW = new Date("2026-09-01T09:00:00.000Z");

const evidence: EvidenceItem[] = [
  {
    id: "evidence-1",
    sourceKind: "cv",
    sourceExcerpt: "Led the Acme checkout platform migration end to end.",
    projectOrEmployer: "Acme Checkout Platform",
    ownership: "Owned the migration end to end.",
    technologies: ["React", "TypeScript"],
    decision: "Split the migration into two phases.",
    constraint: "Tight launch window.",
    outcome: "Cut checkout errors by 30%.",
    recency: "2025-06",
    confidence: 0.92,
  },
  {
    id: "evidence-2",
    sourceKind: "cv",
    sourceExcerpt: "Built reliability tooling for the platform team.",
    projectOrEmployer: "Reliability Tooling",
    ownership: "Designed the alerting flow.",
    technologies: ["Next.js", "Postgres"],
    decision: "Added release health dashboards.",
    constraint: "Small team bandwidth.",
    outcome: "Reduced triage time by 35%.",
    recency: "2025-01",
    confidence: 0.88,
  },
];

const competencies: CompetencyScope[] = [
  { name: "React architecture", relevance: 1 },
  { name: "System design", relevance: 0.8 },
];

/** The `Pick<ProfileDraft, ...>` subset `generateInterviewBlueprint`/`nextTurn`/`openingTurn` actually consume. */
const profileDraft: Pick<ProfileDraft, "role" | "seniority" | "summary" | "narrative" | "expertise" | "characteristics" | "competencies"> = {
  role: "Staff Frontend Engineer",
  seniority: "Staff",
  summary: "Frontend engineer focused on platform reliability and delivery.",
  narrative: "Owns frontend platforms end to end.",
  expertise: ["React", "TypeScript"],
  characteristics: ["Pragmatic"],
  competencies,
};

/** Unused by this file directly, but documents the full-shaped fixture the draft above is carved from. */
const profile: Profile = {
  userId: "user-1",
  role: profileDraft.role,
  seniority: profileDraft.seniority,
  summary: profileDraft.summary,
  narrative: profileDraft.narrative,
  expertise: profileDraft.expertise,
  characteristics: profileDraft.characteristics,
  competencies: [],
  evidence,
  readiness: { ready: true, missing: [] },
  source: { cvText: "At Acme I led a checkout platform migration.", coverLetter: "" },
  createdAt: NOW.toISOString(),
  updatedAt: NOW.toISOString(),
};
void profile;

const USER_ID = "user-1";

/** Distinct, guardrail-safe interviewer lines (single sentence, exactly one trailing "?", no shared long words). */
const LINE_TEMPLATES = [
  "What surprised you most about that outcome?",
  "Which constraint shaped that decision the most?",
  "How did the team respond once that shipped?",
  "What would you change if you tackled it again?",
  "Who else weighed in before you committed?",
  "How did you confirm the fix actually worked?",
  "What made that trade-off worth making?",
  "Where did that effort head next?",
  "What was the hardest part to get right?",
  "How did priorities shift once that landed?",
  "What feedback came back afterward?",
  "Which option did you rule out, and why?",
  "What broke first when load increased?",
  "How did you convince the rest of the team?",
  "What data told you it was working?",
  "Which part took longer than expected?",
  "What would a skeptic have pushed back on?",
  "How did you decide it was finally done?",
  "What changed about your approach midway?",
  "Who owned the rollback plan if it failed?",
];

/** True for the two brief-specified "candidate is blanking" scripted answers. */
function isStuckAnswer(answer: string): boolean {
  return /i don't know|i dont know|blackout/i.test(answer);
}

/** A schema-valid placeholder `dimensionReasons`/`dimensions` payload, one entry per scoring dimension. */
function dimensionRecord<T>(value: T): Record<(typeof EVALUATION_DIMENSIONS)[number], T> {
  return Object.fromEntries(EVALUATION_DIMENSIONS.map((key) => [key, value])) as Record<(typeof EVALUATION_DIMENSIONS)[number], T>;
}

/**
 * Builds the stubbed assessor ("answer evaluation") response body for one
 * turn. `expectedSignalsPresent` is set to the CURRENT question's own
 * `expectedSignals` (read off the real, persisted `session.blueprint.targets`
 * entry for the question actually being answered) whenever the scripted read
 * is "answered" and the turn is not scripted as partial -- this is what lets
 * `deriveCoverageState` mark a target `satisfied` deterministically, without depending on the fallback
 * text-matching heuristics `groundedEvaluationFor` uses for un-stubbed runs.
 * Every strong scripted answer also contains explicit first-person ownership
 * language ("I owned/led/built ..."), which independently satisfies
 * `signalMatches(answer, "ownership")` in `coach.ts` -- so even though this
 * evaluation's `supportedClaims` do not literally reappear in the answer
 * text (and so get filtered out by `claimMatchesAnswer`), the answer is
 * never treated as "materially ungrounded" and downgraded to the
 * deterministic fallback evaluation.
 */
function assessorResponse(read: "answered" | "stuck", expectedSignals: string[]) {
  const answered = read === "answered";
  return {
    read,
    evaluation: {
      score: answered ? 8 : 0,
      competency: "Communication",
      relevance: answered ? 8 : 0,
      dimensions: dimensionRecord(answered ? 8 : 0),
      strengths: answered ? ["Concrete first-person ownership with a measurable outcome."] : [],
      needsWork: answered ? [] : ["No attempt was made."],
      missingPoints: ["Add one more concrete detail next time."],
      betterStructure: ["Lead with the decision, then the outcome."],
      improvedAnswer: "Keep the same example but state the outcome earlier.",
      supportedClaims: answered ? ["Owned the work end to end and measured the result."] : [],
      expectedSignalsPresent: answered ? expectedSignals : [],
      unsupportedClaims: [],
      dimensionReasons: dimensionRecord("Reasoned against the rubric."),
      answerTier: answered ? "strong" : "incorrect",
    },
  };
}

/** Placeholder evaluation for a non-answer turn, mirroring `route.ts`'s `emptyEvaluationFor` (the RPC never persists it). */
function emptyEvaluationFor(question: PlannedQuestion): Evaluation {
  return {
    questionId: question.id,
    competencyId: question.competencyId,
    competency: question.competencyName ?? "Communication",
    score: 0,
    relevance: 0,
    dimensions: dimensionRecord(0),
    strengths: [],
    needsWork: [],
    missingPoints: ["Not attempted."],
    betterStructure: ["Not attempted."],
    improvedAnswer: "Not attempted.",
    supportedClaims: [],
    expectedSignalsPresent: [],
    unsupportedClaims: [],
    dimensionReasons: dimensionRecord("Not attempted."),
  } as Evaluation;
}

// ---------------------------------------------------------------------------
// Fake Supabase client: in-memory tables + RPCs that reproduce the exact
// field semantics of `supabase/migrations/202609010001_adaptive_interviewer.sql`
// and `supabase/migrations/202609020001_coverage_target_required.sql`.
// ---------------------------------------------------------------------------

function matchesFilters(row: Row, filters: Array<{ field: string; op: "eq" | "in"; value: unknown }>): boolean {
  return filters.every((filter) => (
    filter.op === "eq" ? row[filter.field] === filter.value : (filter.value as unknown[]).includes(row[filter.field])
  ));
}

/**
 * A minimal thenable query-builder standing in for the subset of the
 * Supabase JS client's fluent `.from(table).select()/.update()` surface that
 * `src/lib/repositories/interviews.ts` actually calls: `.eq()`, `.in()`,
 * `.order()` (a no-op here -- every caller that depends on order re-sorts
 * client-side, e.g. `mapSession`'s `sequence` sort), and a terminal
 * `.maybeSingle()`/`.single()` or bare `await`.
 */
class FakeQueryBuilder implements PromiseLike<{ data: unknown; error: unknown }> {
  private filters: Array<{ field: string; op: "eq" | "in"; value: unknown }> = [];
  private singleMode: "none" | "single" | "maybeSingle" = "none";

  constructor(
    private readonly rows: Row[],
    private readonly mode: "select" | "update",
    private readonly patch?: Row,
  ) {}

  select(): this { return this; }
  eq(field: string, value: unknown): this { this.filters.push({ field, op: "eq", value }); return this; }
  in(field: string, value: unknown[]): this { this.filters.push({ field, op: "in", value }); return this; }
  order(): this { return this; }
  maybeSingle(): this { this.singleMode = "maybeSingle"; return this; }
  single(): this { this.singleMode = "single"; return this; }

  then<TResult1, TResult2 = never>(
    onfulfilled?: ((value: { data: unknown; error: unknown }) => TResult1 | PromiseLike<TResult1>) | null,
    onrejected?: ((reason: unknown) => TResult2 | PromiseLike<TResult2>) | null,
  ): PromiseLike<TResult1 | TResult2> {
    return Promise.resolve(this.run()).then(onfulfilled, onrejected);
  }

  private run(): { data: unknown; error: unknown } {
    if (this.mode === "update") {
      const updated = this.rows.filter((row) => matchesFilters(row, this.filters));
      for (const row of updated) Object.assign(row, this.patch);
      if (this.singleMode === "maybeSingle") return { data: updated[0] ?? null, error: null };
      if (this.singleMode === "single") {
        return updated[0] ? { data: updated[0], error: null } : { data: null, error: { code: "NOT_FOUND" } };
      }
      return { data: updated, error: null };
    }
    const matched = this.rows.filter((row) => matchesFilters(row, this.filters));
    if (this.singleMode === "maybeSingle") return { data: matched[0] ?? null, error: null };
    if (this.singleMode === "single") {
      return matched[0] ? { data: matched[0], error: null } : { data: null, error: { code: "NOT_FOUND" } };
    }
    return { data: matched, error: null };
  }
}

function makeFakeSupabase() {
  let sessionCounter = 0;
  let questionCounter = 0;
  const sessions = new Map<string, Row>();
  const questions = new Map<string, Row>();
  const questionEvaluations: Row[] = [];
  const emptyTable: Row[] = [];

  function nowIso(): string {
    return new Date().toISOString();
  }

  function tableRows(name: string): Row[] {
    if (name === "interview_sessions") return [...sessions.values()];
    if (name === "interview_questions") return [...questions.values()];
    if (name === "question_evaluations") return questionEvaluations;
    // hands_on_checkpoints, session_evaluations, competencies: never populated by this flow
    // (competency_id is always null -- see `buildCoverageTargets`'s `CompetencyScope` has no
    // stable id -- so `competencyNamesFor` never even queries "competencies").
    return emptyTable;
  }

  /**
   * `create_conversation_session_with_blueprint` (202609010001, amended by
   * 202609020001 to add `required`): inserts one session row and one
   * `interview_questions` row per blueprint target, `prompt: null`,
   * `is_follow_up: false`, with the same clamps the SQL applies
   * (`max_follow_ups` in [0,3], `max_questions` in [5,8], `follow_up_limit`
   * per-target in [0,3], `required` defaulted to `true`).
   */
  function createSessionRpc(args: { p_blueprint: Row }): { data: unknown; error: unknown } {
    const blueprint = args.p_blueprint;
    if (!blueprint || !Array.isArray(blueprint.targets)) {
      return { data: null, error: { code: "22023", message: "Interview blueprint must contain a targets array" } };
    }
    sessionCounter += 1;
    const sessionId = `session-${sessionCounter}`;
    const roundId = (typeof blueprint.roundId === "string" && blueprint.roundId.trim()) || "tech-lead";
    const mode = (typeof blueprint.mode === "string" && blueprint.mode.trim()) || "real";
    const status = (typeof blueprint.status === "string" && blueprint.status.trim()) || "grounded";
    const fallbackReason = (typeof blueprint.fallback_reason === "string" && blueprint.fallback_reason.trim()) || null;
    const maxFollowUps = Math.max(0, Math.min(3, Number(blueprint.max_follow_ups ?? 3)));
    const maxQuestions = Math.max(5, Math.min(8, Number(blueprint.max_questions ?? 8)));
    const now = nowIso();

    sessions.set(sessionId, {
      id: sessionId,
      user_id: USER_ID,
      kind: "conversation",
      status: "active",
      round_id: roundId,
      mode,
      degraded: false,
      blueprint_status: status,
      blueprint_fallback_reason: fallbackReason,
      blueprint_max_follow_ups: maxFollowUps,
      blueprint_max_questions: maxQuestions,
      started_at: now,
      completed_at: null,
      exercise: {},
      result_summary: {},
      overall_score: null,
      practice_plan_id: null,
      opportunity_id: null,
      created_at: now,
      updated_at: now,
    });

    for (const target of blueprint.targets as Row[]) {
      questionCounter += 1;
      const id = `question-${questionCounter}`;
      const competencyName = typeof target.competency_name === "string" ? target.competency_name.trim() : "";
      questions.set(id, {
        id,
        user_id: USER_ID,
        session_id: sessionId,
        sequence: Number(target.sequence ?? 0),
        category: target.category,
        competency_id: target.competency_id ?? null,
        competency_name: competencyName || null,
        difficulty: target.difficulty,
        is_follow_up: false,
        prompt: null,
        objective: typeof target.objective === "string" ? target.objective.trim() : "",
        evidence_ids: target.evidence_ids ?? [],
        expected_signals: target.expected_signals ?? [],
        missing_signal_prompts: target.missing_signal_prompts ?? [],
        rubric_criteria: target.rubric_criteria ?? [],
        follow_up_limit: Math.max(0, Math.min(3, Number(target.follow_up_limit ?? 0))),
        source_confidence: target.source_confidence ?? null,
        required: target.required === undefined ? true : Boolean(target.required),
        asked_intent: null,
        assistance: [],
        non_answer: false,
        set_aside_at: null,
        set_aside_reason: null,
        non_answers: [],
        answer: null,
        answered_at: null,
        asked_at: null,
        parent_question_id: null,
        created_at: now,
        updated_at: now,
      });
    }

    return { data: [{ session_id: sessionId }], error: null };
  }

  /**
   * `record_conversation_turn` (202609010001): locks the answered question,
   * (when not a non-answer) runs `record_interview_evidence`'s effects
   * inline -- sets `answer`/`answered_at` and inserts a `question_evaluations`
   * row -- then always marks `non_answer`/`assistance` on the answered row,
   * then EITHER inserts a new follow-up row (`p_follow_up`) OR, when
   * `p_next_question_id` is given, updates THAT row's `prompt`/`asked_intent`,
   * guarded by `q.answer is null` -- exactly like the live SQL's `elsif`
   * branch, including the case where `p_next_question_id` equals
   * `p_question_id` itself (a same-target continuation after a non-answer,
   * where `answer` is still null): because the answer-recording step above
   * already ran first, that guard can fail for a self-targeting continuation
   * after a real answer exactly as it would in production -- which is why
   * `route.ts` now routes that case through `p_follow_up` instead.
   */
  function recordTurnRpc(args: Row): { data: unknown; error: unknown } {
    const questionId = args.p_question_id as string;
    const question = questions.get(questionId);
    if (!question) return { data: null, error: { code: "P0002", message: "Active owned question was not found" } };
    const session = sessions.get(question.session_id as string);
    if (!session || session.kind !== "conversation" || session.status !== "active") {
      return { data: null, error: { code: "P0002", message: "Active owned question was not found" } };
    }

    session.degraded = Boolean(session.degraded) || Boolean(args.p_degraded);
    session.updated_at = nowIso();

    const nonAnswer = Boolean(args.p_non_answer);
    if (!nonAnswer) {
      if (question.answer !== null) {
        return { data: null, error: { code: "23505", message: "Question already has evidence" } };
      }
      question.answer = args.p_answer;
      question.answered_at = nowIso();
      question.updated_at = nowIso();
      questionEvaluations.push({
        user_id: USER_ID,
        question_id: questionId,
        overall_score: args.p_score,
        dimensions: args.p_dimensions,
        strengths: args.p_strengths,
        weaknesses: args.p_needs_work,
        missing_points: args.p_missing_points,
        better_structure: args.p_better_structure,
        improved_answer: args.p_improved_answer,
        relevance: args.p_relevance,
        supported_claims: args.p_supported_claims,
        expected_signals_present: args.p_expected_signals_present,
        unsupported_claims: args.p_unsupported_claims,
        dimension_reasons: args.p_dimension_reasons,
        updated_at: nowIso(),
      });
    }

    question.non_answer = nonAnswer;
    question.assistance = args.p_assistance ?? [];
    // Mirrors 202609040001: append the unscored exchange, and set (never
    // clear) the set-aside marker on the answered row.
    if (nonAnswer) {
      question.non_answers = [
        ...(Array.isArray(question.non_answers) ? question.non_answers : []),
        { prompt: (question.prompt as string) ?? "", answer: args.p_answer, at: nowIso() },
      ];
    }
    if (args.p_set_aside_reason != null) {
      question.set_aside_at = nowIso();
      question.set_aside_reason = args.p_set_aside_reason;
    }
    question.updated_at = nowIso();

    if (args.p_follow_up != null) {
      const followUp = args.p_follow_up as Row;
      const sessionQuestions = [...questions.values()].filter(
        (row) => row.session_id === question.session_id && row.user_id === USER_ID,
      );
      const total = sessionQuestions.length;
      const followUpsCount = sessionQuestions.filter((row) => row.is_follow_up).length;
      const parentFollowUps = sessionQuestions.filter((row) => row.parent_question_id === question.id).length;
      const followUpLimit = Math.max(0, Math.min(3, Number(
        followUp.followUpLimit ?? followUp.follow_up_limit ?? question.follow_up_limit ?? 0,
      )));
      const sessionMaxFollowUps = Number(session.blueprint_max_follow_ups ?? 3);
      const sessionMaxQuestions = Number(session.blueprint_max_questions ?? 8);
      if (
        total >= sessionMaxQuestions
        || followUpsCount >= sessionMaxFollowUps
        || parentFollowUps >= followUpLimit
        || question.is_follow_up
      ) {
        return { data: null, error: { code: "22023", message: "Conversation follow-up limit reached" } };
      }

      for (const row of sessionQuestions) {
        if (Number(row.sequence) > Number(question.sequence)) {
          row.sequence = Number(row.sequence) + 1;
          row.updated_at = nowIso();
        }
      }

      questionCounter += 1;
      const id = `question-${questionCounter}`;
      const competencyName = String(
        followUp.competencyName ?? followUp.competency_name ?? question.competency_name ?? "",
      ).trim();
      const objective = String(followUp.objective ?? question.objective ?? "").trim();
      questions.set(id, {
        id,
        user_id: USER_ID,
        session_id: question.session_id,
        sequence: Number(question.sequence) + 1,
        category: question.category,
        competency_id: question.competency_id,
        competency_name: competencyName || null,
        difficulty: question.difficulty,
        is_follow_up: true,
        prompt: typeof followUp.prompt === "string" ? followUp.prompt.trim() : null,
        objective: objective || null,
        evidence_ids: followUp.evidenceIds ?? followUp.evidence_ids ?? question.evidence_ids ?? [],
        expected_signals: followUp.expectedSignals ?? followUp.expected_signals ?? question.expected_signals ?? [],
        missing_signal_prompts: followUp.missingSignalPrompts ?? followUp.missing_signal_prompts ?? question.missing_signal_prompts ?? [],
        rubric_criteria: followUp.rubricCriteria ?? followUp.rubric_criteria ?? question.rubric_criteria ?? [],
        follow_up_limit: followUpLimit,
        source_confidence: followUp.sourceConfidence ?? followUp.source_confidence ?? question.source_confidence ?? null,
        required: true,
        parent_question_id: question.id,
        asked_intent: args.p_asked_intent ?? null,
        assistance: [],
        non_answer: false,
        set_aside_at: null,
        set_aside_reason: null,
        non_answers: [],
        answer: null,
        answered_at: null,
        asked_at: nowIso(),
        created_at: nowIso(),
        updated_at: nowIso(),
      });

      return { data: [{ session_id: question.session_id }], error: null };
    }
    if (args.p_next_question_id != null) {
      const next = questions.get(args.p_next_question_id as string);
      const eligible = next
        && next.session_id === question.session_id
        && next.user_id === USER_ID
        && next.answer === null;
      if (!eligible) return { data: null, error: { code: "P0002", message: "Owned next question was not found" } };
      next!.prompt = typeof args.p_next_prompt === "string" ? args.p_next_prompt.trim() : null;
      next!.asked_intent = args.p_asked_intent ?? null;
      // Mirrors 202609040001: asking a row again makes it current again.
      next!.set_aside_at = null;
      next!.set_aside_reason = null;
      next!.asked_at = nowIso();
      next!.updated_at = nowIso();
    }

    return { data: [{ session_id: question.session_id }], error: null };
  }

  return {
    from(table: string) {
      return {
        select: () => new FakeQueryBuilder(tableRows(table), "select"),
        update: (patch: Row) => new FakeQueryBuilder(tableRows(table), "update", patch),
      };
    },
    rpc(name: string, args: Row) {
      if (name === "create_conversation_session_with_blueprint") return Promise.resolve(createSessionRpc(args as { p_blueprint: Row }));
      if (name === "record_conversation_turn") return Promise.resolve(recordTurnRpc(args));
      throw new Error(`adaptive-interviewer-flow.test.ts: unhandled fake RPC "${name}"`);
    },
  };
}

// ---------------------------------------------------------------------------
// Gemini stub: a queue this file fills in exact call order.
// ---------------------------------------------------------------------------

function installGeminiQueue(): { queue: unknown[] } {
  const state = { queue: [] as unknown[] };
  vi.stubGlobal("fetch", vi.fn(async () => {
    if (state.queue.length === 0) {
      throw new Error("adaptive-interviewer-flow.test.ts: unexpected extra Gemini call (queue exhausted)");
    }
    const payload = state.queue.shift();
    return new Response(JSON.stringify({
      candidates: [{ content: { parts: [{ text: JSON.stringify(payload) }] } }],
    }), { status: 200 });
  }));
  return state;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

/** Ten plausible, evidence-grounded, single-sentence answers with explicit first-person ownership language. */
function strongAnswers(): string[] {
  return [
    "I owned the Acme checkout platform migration end to end, split the rollout into two phases, and cut checkout errors by 30%.",
    "I led the decision to split the migration because the launch window was tight, and we shipped both phases on schedule.",
    "I designed the alerting flow for our reliability tooling and reduced incident triage time by 35% within a quarter.",
    "I built the release health dashboards myself after we kept missing regressions, and adoption across the team was immediate.",
    "I chose Postgres over the existing queue because it gave us transactional guarantees the team actually needed.",
    "I coordinated with the platform team to roll the migration out gradually and rolled back one phase safely when metrics dipped.",
    "I measured the impact with weekly dashboards and used that data to justify extending the reliability work another quarter.",
    "I mentored two engineers through the second phase of the migration and documented the rollout so it could repeat cleanly.",
  ];
}

async function runScriptedSession(options: {
  mode: InterviewMode;
  answers: string[];
  /**
   * Zero-based indices of answers the assessor reports as covering only the
   * FIRST of the target's expected signals. A target only leaves `open` when
   * every signal is present, so such a turn leaves the thread open and forces
   * the director to take a second turn on the same target -- the follow-up-row
   * path, which a session of uniformly complete coverage never reaches.
   */
  partialCoverageTurns?: number[];
  opportunity?: Pick<Opportunity, "company" | "role" | "jobDescription" | "gaps">;
}): Promise<InterviewSession> {
  const supabase = makeFakeSupabase();
  const roundId: RoundId = "tech-lead";
  const opportunity = options.opportunity ?? null;

  // No key -> `modelJson` short-circuits to null with zero network calls, so
  // `generateInterviewBlueprint` takes its deterministic fallback path (same
  // technique `release2-flow.test.ts` uses). `targets` (the coverage plan)
  // is computed by `buildCoverageTargets` independently of that choice, so
  // this has no bearing on the coverage-target assertions below.
  vi.stubEnv("GEMINI_API_KEY", "");
  const blueprint = await generateInterviewBlueprint(profileDraft, evidence, { roundId, opportunity });

  vi.stubEnv("GEMINI_API_KEY", "test-key");
  const gemini = installGeminiQueue();
  let lineCalls = 0;
  function pushLine(): void {
    const template = LINE_TEMPLATES[lineCalls % LINE_TEMPLATES.length];
    lineCalls += 1;
    gemini.queue.push({ line: template });
  }
  function pushAssessor(read: "answered" | "stuck", expectedSignals: string[]): void {
    gemini.queue.push(assessorResponse(read, expectedSignals));
  }

  let session = await createSessionWithBlueprint(supabase as never, USER_ID, blueprint, { roundId, mode: options.mode });

  pushLine();
  const opening = await openingTurn({
    profile: profileDraft,
    session,
    blueprint: session.blueprint!,
    evidence,
    opportunity,
  });
  session = await revealFirstQuestion(supabase as never, USER_ID, session, opening);

  const partialTurns = new Set(options.partialCoverageTurns ?? []);
  for (const [index, answer] of options.answers.entries()) {
    const question = currentQuestion(session.questions);
    if (!question) break;
    // A follow-up row carries its parent's signals, so read them off the row
    // being answered rather than only off `targets` (which holds base rows).
    const signals = session.blueprint!.targets.find((item) => item.id === question.id)?.expectedSignals
      ?? question.expectedSignals
      ?? [];
    const read: "answered" | "stuck" = isStuckAnswer(answer) ? "stuck" : "answered";
    pushAssessor(read, partialTurns.has(index) ? signals.slice(0, 1) : signals);
    pushLine();

    const turn = await nextTurn({
      profile: profileDraft,
      session,
      answeredQuestion: question,
      answer,
      blueprint: session.blueprint!,
      evidence,
      opportunity,
    });

    // The route's own resolution, not a re-implementation of it: which row
    // carries the next prompt is exactly the seam this flow is here to cover,
    // and a local copy is what let it drift out of step with `route.ts`.
    const write = resolveNextQuestionWrite(session, question, turn);
    // Gated on `turn.nonAnswer` for the same reason `route.ts` gates on its
    // own `nonAnswer`: a pre-written session must never be set aside. This
    // harness never drives one, so the gate is always a no-op here, but it
    // stays in step with the route rather than assuming that away.
    const setAsideReason = turn.nonAnswer ? turn.setAside : null;

    session = await recordConversationTurn(
      supabase as never,
      USER_ID,
      question.id,
      answer,
      turn.evaluation ?? emptyEvaluationFor(question),
      {
        nextQuestionId: write.nextQuestionId,
        nextPrompt: turn.prompt,
        followUp: write.followUp,
        askedIntent: turn.intent,
        // Accumulates onto the row's own persisted history, as `route.ts`
        // does: a rescue re-asks the SAME row, so replacing would lose every
        // earlier rescue recorded on it.
        assistance: [...question.assistance, ...(turn.assistance ? [turn.assistance] : [])],
        nonAnswer: turn.nonAnswer,
        degraded: turn.degraded,
        setAsideReason,
      },
    );
  }

  return session;
}

function coverageAtEnd(session: InterviewSession) {
  return deriveCoverageState(session.blueprint!.targets, session.questions, session.evaluations);
}

/**
 * Every question the candidate was actually shown had something in it. A row
 * answered with a null prompt means the turn that should have authored it
 * resolved to no write at all, and `transcriptFor` rendered an empty
 * interviewer bubble the candidate then "answered".
 */
function answeredWithNoPrompt(session: InterviewSession): PlannedQuestion[] {
  return session.questions.filter((question) => question.answer !== null && !question.prompt);
}

describe("adaptive interviewer flow", () => {
  it("completes a real-mode session covering every required target", async () => {
    const session = await runScriptedSession({ mode: "real", answers: strongAnswers() });
    const states = coverageAtEnd(session);
    expect(states.filter((state) => state.target.required).every((state) => state.status === "satisfied")).toBe(true);
    expect(session.questions.every((question) => question.assistance.length === 0)).toBe(true);
    expect(answeredWithNoPrompt(session)).toEqual([]);
  });

  it("follows a partially covered target onto a second turn without ever blanking a question", async () => {
    // The first answer leaves the opening target open, so the director probes
    // the SAME target again. That continuation cannot go back onto the row it
    // just answered, so it lands on a follow-up row -- and that follow-up row
    // then carries a different id from the target it belongs to, which is
    // where an id-based same-target check silently stopped writing a next
    // question at all and left the candidate an empty bubble.
    const session = await runScriptedSession({
      mode: "real",
      answers: strongAnswers(),
      partialCoverageTurns: [0, 1],
    });

    expect(session.questions.some((question) => question.isFollowUp)).toBe(true);
    expect(answeredWithNoPrompt(session)).toEqual([]);
    // The thread stayed on its own target: the follow-up row is attributed
    // through `askedIntent.targetId`, not through the row that carries it.
    const followUp = session.questions.find((question) => question.isFollowUp);
    const openingTargetId = session.blueprint!.targets[0].id;
    expect(followUp?.askedIntent).toMatchObject({ targetId: openingTargetId });
  });

  // A park issues `{ kind: "rescue", targetId: <the same target>, style:
  // "park" }`, and the set-aside marker (not `answer`) is what lets the
  // parked row stop being the current row -- see the inner comment below for
  // what this test actually asserts about where the conversation lands.
  it("rescues a blanking candidate in coach mode and records the rescue on the question", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "ok — I owned the design system migration at Acme."],
    });
    const rescues = session.questions.flatMap((question) => question.assistance);
    expect(rescues.length).toBeGreaterThan(0);
    expect(rescues.map((rescue) => rescue.style)).toContain("park");
    // The rescued row is the one that was PARKED, so it correctly stays
    // unanswered: park sets it aside and moves the interview to a different
    // target, and this three-answer script has no turns left to come back to
    // it (spec §8.2, "return later if turns remain"). The candidate's recovery
    // therefore lands on the destination row, not this one. Before issue #10
    // was fixed this test asserted the opposite -- that a parked row kept
    // receiving the candidate's next answer -- which was the blackout bug
    // itself, not intended behaviour.
    const rescuedQuestion = session.questions.find((question) => question.assistance.length > 0);
    expect(rescuedQuestion?.setAsideReason).toBe("parked");
    expect(rescuedQuestion?.answer).toBeNull();
    const answered = session.questions.filter((question) => question.answer);
    expect(answered.some((question) => question.id !== rescuedQuestion?.id)).toBe(true);
  });

  // Issue #10. Four consecutive blackouts in coach mode: narrow rescue, park,
  // then the rescue budget is spent. The director advances to a second target
  // and writes its question down -- but the candidate is shown the first row
  // on every single turn, because a row with no answer is still "the current
  // question". Asserted on the ROWS the candidate blanked on, not on the text
  // of the prompts: once unscored attempts are replayed in the transcript, a
  // re-ask of the SAME row produces fresh phrasing, so distinct message text
  // proves nothing about which question the candidate was actually served.
  it("moves the candidate onto a different question after a blackout", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "i don't know", "i am having a blackout"],
    });
    const blanked = session.questions.filter((question) => question.nonAnswers.length > 0);
    expect(blanked.length).toBeGreaterThan(1);
  });

  it("never scores a non-answer", async () => {
    const session = await runScriptedSession({ mode: "coach", answers: ["i am having a blackout"] });
    const blanks = session.questions.filter((question) => question.nonAnswer).map((question) => question.id);
    expect(session.evaluations.some((item) => blanks.includes(item.questionId ?? ""))).toBe(false);
  });

  it("asks a different question every turn", async () => {
    const session = await runScriptedSession({ mode: "real", answers: strongAnswers() });
    const prompts = session.questions.map((question) => question.prompt).filter(Boolean);
    expect(new Set(prompts).size).toBe(prompts.length);
  });

  it("grounds questions in the job when anchored to an opportunity", async () => {
    const session = await runScriptedSession({
      mode: "real",
      answers: strongAnswers(),
      opportunity: { company: "Acme", role: "Senior Frontend Engineer", jobDescription: "React, testing strategy, observability", gaps: ["Observability"] },
    });
    const targets = session.blueprint!.targets.filter((target) => target.required).map((target) => target.competencyName);
    expect(targets).toContain("Observability");
  });

  it("keeps the candidate's unanswered attempts in the transcript", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "ok — I owned the design system migration at Acme."],
    });
    const said = session.messages.filter((message) => message.role === "candidate").map((message) => message.content);
    expect(said).toContain("i don't know");
    expect(said).toContain("i am having a blackout");
  });

  it("sets a parked target aside as parked and a budget-spent one as skipped", async () => {
    const coach = await runScriptedSession({
      mode: "coach",
      answers: ["i don't know", "i am having a blackout", "i don't know", "i am having a blackout"],
    });
    const coachReasons = coach.questions.map((question) => question.setAsideReason).filter(Boolean);
    expect(coachReasons).toContain("parked");

    const real = await runScriptedSession({
      mode: "real",
      answers: ["i don't know", "i am having a blackout", "i don't know"],
    });
    const realReasons = real.questions.map((question) => question.setAsideReason).filter(Boolean);
    expect(realReasons).toContain("rescue-budget-spent");
    expect(realReasons).not.toContain("parked");
  });

  it("comes back to a parked target once nothing required is unasked", async () => {
    const session = await runScriptedSession({
      mode: "coach",
      answers: [
        "i don't know",
        "i am having a blackout",
        ...strongAnswers(),
      ],
    });
    // The parked row was returned to: its marker was cleared and it carries a
    // real answer by the end.
    const parked = session.questions.find((question) => question.nonAnswers.length > 0);
    expect(parked?.setAsideAt).toBeNull();
    expect(parked?.answer).toBeTruthy();
  });
});

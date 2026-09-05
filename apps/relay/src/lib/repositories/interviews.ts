import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import type {
  AssistanceRecord,
  BlueprintQuestion,
  CoverageTarget,
  Evaluation,
  FollowUpDraft,
  HandsOnCheckpoint,
  HandsOnExercise,
  Intent,
  InterviewBlueprint,
  InterviewMode,
  InterviewSession,
  Message,
  NonAnswerRecord,
  PlannedQuestion,
  PracticeSessionContext,
  QuestionCategory,
  ReadinessEvidence,
  RoundId,
  SessionCareerContext,
  SetAsideReason,
} from "@/lib/types";
import { isAwaitingAnswer } from "@/lib/interview-current-question";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;

const backboneCategories: PlannedQuestion["category"][] = [
  "introduction", "experience", "technical", "architecture", "behavioral",
];
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

const stringValue = (value: unknown): string => typeof value === "string" ? value : "";
const stringArray = (value: unknown): string[] => Array.isArray(value)
  ? value.filter((item): item is string => typeof item === "string")
  : [];
const jsonRecord = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value)
  ? value as Record<string, unknown>
  : {};

function numericValue(value: unknown): number | null {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function evaluationGroundingRecord(evaluation: Evaluation): Record<string, unknown> {
  return {
    relevance: evaluation.relevance ?? null,
    supported_claims: evaluation.supportedClaims ?? [],
    expected_signals_present: evaluation.expectedSignalsPresent ?? [],
    unsupported_claims: evaluation.unsupportedClaims ?? [],
    dimension_reasons: evaluation.dimensionReasons ?? {},
  };
}

function evaluationGroundingRpcArgs(evaluation: Evaluation): Record<string, unknown> {
  const record = evaluationGroundingRecord(evaluation);
  return {
    p_relevance: record.relevance,
    p_supported_claims: record.supported_claims,
    p_expected_signals_present: record.expected_signals_present,
    p_unsupported_claims: record.unsupported_claims,
    p_dimension_reasons: record.dimension_reasons,
  };
}

function persistableCompetencyId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return uuidPattern.test(trimmed) ? trimmed : null;
}

function mapQuestion(row: Row, competencyNames: Map<string, string>): PlannedQuestion {
  const competencyId = typeof row.competency_id === "string" ? row.competency_id : null;
  const competencyName = competencyId
    ? competencyNames.get(competencyId) ?? stringValue(row.competency_name) ?? null
    : stringValue(row.competency_name) || null;
  return {
    id: stringValue(row.id),
    sequence: Number(row.sequence ?? 0),
    category: row.category as PlannedQuestion["category"],
    competencyId,
    competencyName,
    difficulty: row.difficulty as PlannedQuestion["difficulty"],
    isFollowUp: Boolean(row.is_follow_up),
    /** Null until the interviewer authors it at reveal time (spec §9.1). */
    prompt: stringValue(row.prompt) || null,
    answer: typeof row.answer === "string" ? row.answer : null,
    createdAt: stringValue(row.created_at),
    askedIntent: (row.asked_intent as Intent | null) ?? null,
    assistance: Array.isArray(row.assistance) ? (row.assistance as AssistanceRecord[]) : [],
    nonAnswer: row.non_answer === true,
    setAsideAt: typeof row.set_aside_at === "string" ? row.set_aside_at : null,
    setAsideReason: (row.set_aside_reason as PlannedQuestion["setAsideReason"]) ?? null,
    nonAnswers: Array.isArray(row.non_answers) ? (row.non_answers as NonAnswerRecord[]) : [],
    objective: typeof row.objective === "string" && row.objective.trim() ? row.objective.trim() : undefined,
    evidenceIds: stringArray(row.evidence_ids),
    expectedSignals: stringArray(row.expected_signals),
    missingSignalPrompts: stringArray(row.missing_signal_prompts),
    rubricCriteria: stringArray(row.rubric_criteria),
    followUpLimit: row.follow_up_limit === null || row.follow_up_limit === undefined ? undefined : Number(row.follow_up_limit),
    sourceConfidence: row.source_confidence === null || row.source_confidence === undefined ? undefined : Number(row.source_confidence),
    parentQuestionId: typeof row.parent_question_id === "string" ? row.parent_question_id : null,
  };
}

function mapBlueprintQuestion(row: Row, competencyNames: Map<string, string>): BlueprintQuestion | null {
  if (typeof row.objective !== "string" || !row.objective.trim()) return null;
  const question = mapQuestion(row, competencyNames);
  return {
    ...question,
    objective: row.objective.trim(),
    evidenceIds: question.evidenceIds ?? stringArray(row.evidence_ids),
    expectedSignals: question.expectedSignals ?? stringArray(row.expected_signals),
    missingSignalPrompts: question.missingSignalPrompts ?? stringArray(row.missing_signal_prompts),
    rubricCriteria: question.rubricCriteria ?? stringArray(row.rubric_criteria),
    followUpLimit: Number(row.follow_up_limit ?? question.followUpLimit ?? 0),
    sourceConfidence: row.source_confidence === null || row.source_confidence === undefined
      ? question.sourceConfidence ?? null
      : Number(row.source_confidence),
  };
}

function mapEvaluation(row: Row, question: PlannedQuestion): Evaluation {
  const evaluation: Evaluation = {
    score: Number(row.overall_score ?? 0),
    questionId: stringValue(row.question_id) || null,
    competencyId: question.competencyId,
    competency: question.competencyName ?? "Communication",
    dimensions: jsonRecord(row.dimensions) as Evaluation["dimensions"],
    strengths: stringArray(row.strengths),
    needsWork: stringArray(row.weaknesses),
    missingPoints: stringArray(row.missing_points),
    betterStructure: stringArray(row.better_structure),
    improvedAnswer: stringValue(row.improved_answer),
  };
  const relevance = numericValue(row.relevance);
  if (relevance !== null) evaluation.relevance = relevance;
  evaluation.supportedClaims = stringArray(row.supported_claims);
  evaluation.expectedSignalsPresent = stringArray(row.expected_signals_present);
  evaluation.unsupportedClaims = stringArray(row.unsupported_claims);
  evaluation.dimensionReasons = jsonRecord(row.dimension_reasons) as Evaluation["dimensionReasons"];
  return evaluation;
}

function mapSessionEvaluation(row: Row, competencyNames: Map<string, string>): Evaluation {
  const competencyId = typeof row.competency_id === "string" ? row.competency_id : null;
  const evaluation: Evaluation = {
    score: Number(row.overall_score ?? 0),
    questionId: null,
    competencyId,
    competency: competencyId
      ? competencyNames.get(competencyId) ?? stringValue(row.competency_name)
      : stringValue(row.competency_name) || "Communication",
    dimensions: jsonRecord(row.dimensions) as Evaluation["dimensions"],
    strengths: stringArray(row.strengths),
    needsWork: stringArray(row.weaknesses),
    missingPoints: stringArray(row.missing_points),
    betterStructure: stringArray(row.better_structure),
    improvedAnswer: stringValue(row.improved_answer),
  };
  const relevance = numericValue(row.relevance);
  if (relevance !== null) evaluation.relevance = relevance;
  evaluation.supportedClaims = stringArray(row.supported_claims);
  evaluation.expectedSignalsPresent = stringArray(row.expected_signals_present);
  evaluation.unsupportedClaims = stringArray(row.unsupported_claims);
  evaluation.dimensionReasons = jsonRecord(row.dimension_reasons) as Evaluation["dimensionReasons"];
  return evaluation;
}

function mapCheckpoint(row: Row): HandsOnCheckpoint {
  return {
    id: stringValue(row.id),
    code: stringValue(row.code),
    note: stringValue(row.note),
    interviewerPrompt: stringValue(row.interviewer_prompt),
    createdAt: stringValue(row.created_at),
  };
}

/**
 * Renders the planned questions as a conversation transcript, stopping after
 * the question the candidate is currently on. The whole plan is persisted when
 * the session starts, but revealing it at once would show the candidate every
 * upcoming question (and, through the blueprint panel, its expected signals)
 * before they answer the current one.
 *
 * A row can carry more than one exchange: each unscored attempt overwrites the
 * row's `prompt`, so the prompts and answers of those attempts are replayed
 * from `nonAnswers` before the row's current prompt. Without that, a candidate
 * who blanked twice and then recovered would see a transcript in which neither
 * blank ever happened.
 */
function transcriptFor(questions: PlannedQuestion[], answerTimes: Map<string, string>): Message[] {
  const current = questions.findIndex(isAwaitingAnswer);
  const revealed = current === -1 ? questions : questions.slice(0, current + 1);
  return revealed.flatMap((question) => {
    const attempts: Message[] = question.nonAnswers.flatMap((record, index) => [
      {
        id: `${question.id}:attempt-${index}:question`,
        role: "interviewer" as const,
        content: record.prompt,
        createdAt: record.at,
      },
      {
        id: `${question.id}:attempt-${index}:answer`,
        role: "candidate" as const,
        content: record.answer,
        createdAt: record.at,
      },
    ]);
    const interviewer: Message = {
      id: `${question.id}:question`,
      role: "interviewer",
      // Null until the interviewer authors it (revealFirstQuestion or a
      // later turn); render an empty bubble rather than widen Message.
      content: question.prompt ?? "",
      createdAt: question.createdAt,
    };
    // A set-aside row's current prompt is already on screen: it was copied
    // into the last `nonAnswers` entry above as that attempt's question, so
    // emitting the row's own bubble here would show the candidate the exact
    // same question twice in a row, right after they blanked on it (issue
    // #10). Guarded on `attempts.length > 0` rather than dropping the row
    // outright: today a set-aside row always has at least one attempt
    // (`route.ts` only sends a set-aside reason when `nonAnswer` is true, and
    // the SQL appends to `non_answers` on exactly that condition), but if
    // that ever stopped holding, the bare form would silently erase the row
    // from the transcript instead of just losing this de-dup.
    if (question.setAsideAt !== null && attempts.length > 0) return attempts;
    if (!question.answer) return [...attempts, interviewer];
    return [...attempts, interviewer, {
      id: `${question.id}:answer`,
      role: "candidate" as const,
      content: question.answer,
      createdAt: answerTimes.get(question.id) ?? question.createdAt,
    }];
  });
}

function handsOnTranscript(row: Row, checkpoints: HandsOnCheckpoint[]): Message[] {
  const exercise = jsonRecord(row.exercise);
  const opening = typeof exercise.interviewerOpening === "string" ? exercise.interviewerOpening : "";
  const messages: Message[] = opening ? [{
    id: `${stringValue(row.id)}:opening`,
    role: "interviewer",
    content: opening,
    createdAt: stringValue(row.created_at),
  }] : [];
  for (const checkpoint of checkpoints) {
    messages.push({
      id: `${checkpoint.id}:candidate`,
      role: "candidate",
      content: `Checkpoint: ${checkpoint.note}`,
      createdAt: checkpoint.createdAt,
    });
    if (checkpoint.interviewerPrompt) {
      messages.push({
        id: `${checkpoint.id}:interviewer`,
        role: "interviewer",
        content: checkpoint.interviewerPrompt,
        createdAt: checkpoint.createdAt,
      });
    }
  }
  return messages;
}

export function mapSession(
  row: Row,
  questionRows: Row[],
  evaluationRows: Row[],
  checkpointRows: Row[],
  competencyNames: Map<string, string>,
  sessionEvaluationRows: Row[] = [],
): InterviewSession {
  const questions = [...questionRows]
    .sort((left, right) => Number(left.sequence ?? 0) - Number(right.sequence ?? 0))
    .map((question) => mapQuestion(question, competencyNames));
  const blueprintQuestions = [...questionRows]
    .sort((left, right) => Number(left.sequence ?? 0) - Number(right.sequence ?? 0))
    .map((question) => mapBlueprintQuestion(question, competencyNames))
    .filter((question): question is BlueprintQuestion => question !== null);
  /**
   * Reconstructs each coverage target directly from its own
   * `interview_questions` row, using the row's database `id` as the
   * target's `id`. This is the SAME id at creation time and on every later
   * reload (a row's primary key never changes), which is what lets
   * `deriveCoverageState` keep matching a persisted `askedIntent.targetId`
   * against this array's targets after the session round-trips through the
   * database on every stateless turn (spec §9.2). Follow-up rows never
   * become targets -- they carry a `parent_question_id` and are inserted by
   * `record_conversation_turn`, not by the target-creating blueprint RPC.
   */
  const targets: CoverageTarget[] = [...questionRows]
    .sort((left, right) => Number(left.sequence ?? 0) - Number(right.sequence ?? 0))
    .filter((questionRow) => !questionRow.is_follow_up)
    .map((questionRow) => {
      const question = mapQuestion(questionRow, competencyNames);
      return {
        id: question.id,
        competencyId: question.competencyId,
        competencyName: question.competencyName,
        category: question.category,
        evidenceIds: question.evidenceIds ?? [],
        difficulty: question.difficulty,
        objective: question.objective ?? "",
        expectedSignals: question.expectedSignals ?? [],
        rubricCriteria: question.rubricCriteria ?? [],
        required: questionRow.required === true,
      };
    });
  const answerTimes = new Map(questionRows.map((question) => [
    stringValue(question.id),
    typeof question.answered_at === "string" ? question.answered_at : stringValue(question.created_at),
  ]));
  const questionsById = new Map(questions.map((question) => [question.id, question]));
  const questionEvaluations = [...evaluationRows]
    .sort((left, right) => {
      const leftQuestion = questionsById.get(stringValue(left.question_id));
      const rightQuestion = questionsById.get(stringValue(right.question_id));
      return (leftQuestion?.sequence ?? Number.MAX_SAFE_INTEGER)
        - (rightQuestion?.sequence ?? Number.MAX_SAFE_INTEGER);
    })
    .map((evaluation) => {
    const question = questionsById.get(stringValue(evaluation.question_id));
    return mapEvaluation(evaluation, question ?? {
      id: "", sequence: 0, category: "communication", competencyId: null, competencyName: null,
      difficulty: "foundational", isFollowUp: false, prompt: "", answer: null, createdAt: "",
      askedIntent: null, assistance: [], nonAnswer: false,
      setAsideAt: null, setAsideReason: null, nonAnswers: [],
    });
    });
  const checkpoints = [...checkpointRows]
    .sort((left, right) => stringValue(left.created_at).localeCompare(stringValue(right.created_at)))
    .map(mapCheckpoint);
  const evaluations = [
    ...questionEvaluations,
    ...sessionEvaluationRows.map((evaluation) => mapSessionEvaluation(evaluation, competencyNames)),
  ];
  const kind = row.kind === "hands-on" ? "hands-on" : "conversation";
  const roundId = (stringValue(row.round_id) || "tech-lead") as RoundId;
  const mode = (stringValue(row.mode) || "real") as InterviewMode;
  const blueprintMaxFollowUps = row.blueprint_max_follow_ups === null || row.blueprint_max_follow_ups === undefined
    ? 3
    : Number(row.blueprint_max_follow_ups);
  const blueprintMaxQuestions = row.blueprint_max_questions === null || row.blueprint_max_questions === undefined
    ? 8
    : Number(row.blueprint_max_questions);
  const hasPersistedBlueprint = kind === "conversation" && (
    typeof row.blueprint_status === "string"
    || typeof row.blueprint_fallback_reason === "string"
    || blueprintQuestions.length > 0
  );
  const blueprint = kind === "conversation"
    ? {
      status: !hasPersistedBlueprint || row.blueprint_status === "limited-grounding"
        ? "limited-grounding"
        : "grounded",
      fallbackReason: typeof row.blueprint_fallback_reason === "string"
        ? row.blueprint_fallback_reason
        : !hasPersistedBlueprint
          ? "Legacy session created before grounded blueprints were persisted."
          : null,
      maxFollowUps: Number.isFinite(blueprintMaxFollowUps) ? blueprintMaxFollowUps : 3,
      maxQuestions: Number.isFinite(blueprintMaxQuestions) ? blueprintMaxQuestions : 8,
      createdAt: stringValue(row.created_at),
      questions: blueprintQuestions,
      roundId,
      // Fixed by `generateInterviewBlueprint` (spec §9.1) and never persisted
      // as its own column -- every blueprint this release creates uses the
      // same turn budget, so reconstructing it as a constant is exact, not
      // a guess.
      turnBudget: 8,
      targets,
    } satisfies InterviewBlueprint
    : null;

  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    kind,
    roundId,
    mode,
    status: row.status === "complete" ? "complete" : "active",
    degraded: row.degraded === true,
    startedAt: stringValue(row.started_at),
    completedAt: typeof row.completed_at === "string" ? row.completed_at : null,
    exercise: jsonRecord(row.exercise),
    resultSummary: jsonRecord(row.result_summary),
    overallScore: row.overall_score === null || row.overall_score === undefined ? null : Number(row.overall_score),
    questions,
    blueprint,
    checkpoints,
    evaluations,
    messages: kind === "hands-on" ? handsOnTranscript(row, checkpoints) : transcriptFor(questions, answerTimes),
    createdAt: stringValue(row.created_at),
    updatedAt: stringValue(row.updated_at),
    practicePlanId: typeof row.practice_plan_id === "string" ? row.practice_plan_id : null,
    opportunityId: typeof row.opportunity_id === "string" ? row.opportunity_id : null,
  };
}

async function competencyNamesFor(
  supabase: SupabaseClient,
  userId: string,
  questions: Row[],
  sessionEvaluations: Row[],
): Promise<Map<string, string>> {
  const ids = [...new Set([
    ...questions.map((question) => question.competency_id),
    ...sessionEvaluations.map((evaluation) => evaluation.competency_id),
  ].filter((id): id is string => typeof id === "string"))];
  if (!ids.length) return new Map();
  const { data, error } = await supabase.from("competencies").select("id, name").eq("user_id", userId).in("id", ids);
  if (error) throw new RepositoryError("Could not load session competencies.", error.code);
  return new Map(((data ?? []) as Row[]).map((competency) => [stringValue(competency.id), stringValue(competency.name)]));
}

async function hydrateSession(supabase: SupabaseClient, userId: string, row: Row): Promise<InterviewSession> {
  const [
    { data: questions, error: questionsError },
    { data: checkpoints, error: checkpointsError },
    { data: sessionEvaluations, error: sessionEvaluationsError },
  ] = await Promise.all([
    supabase.from("interview_questions").select("*").eq("user_id", userId).eq("session_id", row.id).order("sequence"),
    supabase.from("hands_on_checkpoints").select("*").eq("user_id", userId).eq("session_id", row.id).order("created_at"),
    supabase.from("session_evaluations").select("*").eq("user_id", userId).eq("session_id", row.id).order("created_at"),
  ]);
  if (questionsError || checkpointsError || sessionEvaluationsError) {
    throw new RepositoryError(
      "Could not load the interview session.",
      questionsError?.code ?? checkpointsError?.code ?? sessionEvaluationsError?.code,
    );
  }
  const questionRows = (questions ?? []) as Row[];
  const sessionEvaluationRows = (sessionEvaluations ?? []) as Row[];
  const questionIds = questionRows.map((question) => stringValue(question.id)).filter(Boolean);
  const { data: evaluations, error: evaluationsError } = questionIds.length
    ? await supabase.from("question_evaluations").select("*").eq("user_id", userId).in("question_id", questionIds)
    : { data: [], error: null };
  if (evaluationsError) throw new RepositoryError("Could not load the interview session.", evaluationsError.code);
  return mapSession(
    row,
    questionRows,
    (evaluations ?? []) as Row[],
    (checkpoints ?? []) as Row[],
    await competencyNamesFor(supabase, userId, questionRows, sessionEvaluationRows),
    sessionEvaluationRows,
  );
}

export function assertConversationPlan(plan: PlannedQuestion[]): void {
  const isBackbone = plan.length === backboneCategories.length
    && plan.every((question, index) => question.sequence === index + 1
      && question.category === backboneCategories[index]
      && !question.isFollowUp);
  if (!isBackbone) throw new RepositoryError("A conversation must use the exact five-question backbone.", "INVALID_PLAN");
}

/**
 * The sibling validator to `assertConversationPlan` for planned practice
 * sessions -- a SEPARATE contract, not a relaxation of the generic
 * five-question backbone. A practice blueprint must contain 1-5 base
 * questions with contiguous sequences starting at one, and none may be a
 * follow-up (follow-ups are only ever added later, during the conversation,
 * via `recordConversationTurn`). Deliberately does not check `category`
 * against `backboneCategories` -- unlike the generic backbone, planned
 * practice categories are not fixed to a specific five-slot sequence.
 */
export function assertPracticeConversationBlueprint(blueprint: InterviewBlueprint): void {
  if (blueprint.questions.length < 1 || blueprint.questions.length > 5) {
    throw new RepositoryError("Planned practice must contain between one and five base questions.", "INVALID_PLAN");
  }
  blueprint.questions.forEach((question, index) => {
    if (question.sequence !== index + 1 || question.isFollowUp) {
      throw new RepositoryError("Planned practice questions must be contiguous base questions.", "INVALID_PLAN");
    }
  });
}

export async function getSession(supabase: SupabaseClient, userId: string, sessionId: string): Promise<InterviewSession | null> {
  const { data, error } = await supabase
    .from("interview_sessions")
    .select("*")
    .eq("id", sessionId)
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw new RepositoryError("Could not load the interview session.", error.code);
  return data ? hydrateSession(supabase, userId, data as Row) : null;
}

export async function listRecentSessions(supabase: SupabaseClient, userId: string): Promise<InterviewSession[]> {
  const { data, error } = await supabase
    .from("interview_sessions")
    .select("*")
    .eq("user_id", userId)
    .order("created_at", { ascending: false })
    .limit(20);
  if (error) throw new RepositoryError("Could not load recent interviews.", error.code);
  return Promise.all(((data ?? []) as Row[]).map((session) => hydrateSession(supabase, userId, session)));
}

/** The base owned-table query `selectAllPages` refines: an explicit column list scoped to the caller's rows. */
type EvidenceQuery = ReturnType<ReturnType<SupabaseClient["from"]>["select"]>;

const PAGE_SIZE = 1000;

/**
 * Reads an entire owned table in blocks. PostgREST caps a single response, and
 * readiness must see all history, so silently truncating here would quietly
 * corrupt every downstream score.
 *
 * The `.order("id", ...)` is load-bearing, not cosmetic: `.range()` pages by
 * asking Postgres for rows N..M of "the" result set, but without a stable
 * unique sort key there is no guarantee that ordering is identical across the
 * separate requests backing each page, so a row can be skipped or duplicated
 * right at a page boundary -- the exact failure this unbounded read exists to
 * avoid.
 *
 * `columns` is required rather than defaulting to `*`: these reads are
 * unbounded over a user's whole history, and the wide columns on these tables
 * (`result_summary`/`exercise` jsonb, every question prompt and answer, every
 * evaluation's dimensions/strengths/weaknesses) are megabytes the readiness
 * model never looks at.
 *
 * The cast on `.select` is what a runtime-chosen column list costs: postgrest-js
 * parses the select string in the type system, and handed a plain `string` it
 * gives up with "type instantiation is excessively deep". Rows are read back
 * through `stringValue`/`Number(...)` anyway, so nothing downstream relies on
 * the inference this discards.
 */
async function selectAllPages(
  supabase: SupabaseClient,
  table: string,
  userId: string,
  columns: string,
  refine: (query: EvidenceQuery) => EvidenceQuery,
): Promise<Row[]> {
  const rows: Row[] = [];
  for (let page = 0; ; page += 1) {
    const from = supabase.from(table) as unknown as { select: (columns: string) => EvidenceQuery };
    const { data, error } = await refine(
      from.select(columns).eq("user_id", userId).order("id", { ascending: true }),
    ).range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE - 1);
    if (error) throw new RepositoryError("Could not load readiness evidence.", error.code);
    const batch = (data ?? []) as Row[];
    rows.push(...batch);
    if (batch.length < PAGE_SIZE) return rows;
  }
}

/**
 * Every graded answer the user has ever produced, flattened with the session
 * conditions the readiness model needs to weight it.
 *
 * Deliberately unbounded, unlike `listRecentSessions`, which caps at 20: a
 * readiness model that forgets the user's history cannot apply recency
 * weighting, because it has nothing old to weigh the recent evidence against.
 * Rows are paged in blocks so a long history does not hit PostgREST's limit.
 *
 * Skipped on purpose:
 * - sessions that are not `complete` -- partial grades are not yet evidence.
 *   `complete` (not `completed`) is the literal every completion RPC writes and
 *   `hydrateSession` reads, and `InterviewSession["status"]` admits no other
 *   finished value;
 * - questions flagged `non_answer` -- never scored, so never proof of anything.
 *
 * The questions and evaluations reads are NOT narrowed by an id list. Putting
 * every session or question id into an `.in(...)` puts them all in a URL query
 * string -- a thousand uuids is roughly 38KB, past what proxies and PostgREST
 * accept -- and those filters were redundant anyway: every table is scoped to
 * `user_id`, and the join loop below drops any row that does not resolve back
 * to a kept question and a completed session, which is what actually enforces
 * the narrowing.
 */
export async function listReadinessEvidence(
  supabase: SupabaseClient,
  userId: string,
): Promise<ReadinessEvidence[]> {
  const sessions = await selectAllPages(
    supabase,
    "interview_sessions",
    userId,
    "id, status, mode, degraded",
    (query) => query.eq("status", "complete"),
  );
  if (sessions.length === 0) return [];

  const sessionById = new Map(sessions.map((row) => [stringValue(row.id), row]));
  // `.not("non_answer", "is", true)` rather than `.neq("non_answer", true)`:
  // `neq` compiles to `<> true`, which is NULL -- and so excluded -- for rows
  // where the flag was never written, whereas `is not true` keeps them,
  // matching the `row.non_answer !== true` check this replaced.
  const questions = await selectAllPages(
    supabase,
    "interview_questions",
    userId,
    "id, session_id, category, competency_id, assistance, non_answer",
    (query) => query.not("non_answer", "is", true),
  );
  if (questions.length === 0) return [];

  const questionById = new Map(questions.map((row) => [stringValue(row.id), row]));
  const evaluations = await selectAllPages(
    supabase,
    "question_evaluations",
    userId,
    "id, question_id, overall_score, created_at",
    (query) => query,
  );

  // Narrowed to questions that actually join back to a completed session, since
  // the questions read above is no longer session-scoped.
  const competencyIds = [
    ...new Set(
      questions
        .filter((row) => sessionById.has(stringValue(row.session_id)))
        .map((row) => stringValue(row.competency_id))
        .filter(Boolean),
    ),
  ];
  // Bounded by the user's distinct competency count -- tens, not thousands --
  // so this id list is safe in a query string where the two above were not.
  const competencies = competencyIds.length
    ? await selectAllPages(supabase, "competencies", userId, "id, name, relevance", (query) =>
        query.in("id", competencyIds),
      )
    : [];
  const competencyById = new Map(competencies.map((row) => [stringValue(row.id), row]));

  const evidence: ReadinessEvidence[] = [];
  for (const evaluation of evaluations) {
    const question = questionById.get(stringValue(evaluation.question_id));
    if (!question) continue;
    const session = sessionById.get(stringValue(question.session_id));
    if (!session) continue;
    const competency = competencyById.get(stringValue(question.competency_id));
    const assistance = Array.isArray(question.assistance) ? question.assistance : [];
    evidence.push({
      questionEvaluationId: stringValue(evaluation.id),
      sessionId: stringValue(session.id),
      recordedAt: stringValue(evaluation.created_at),
      score: Number(evaluation.overall_score ?? 0),
      competencyId: competency ? stringValue(competency.id) : null,
      competencyName: competency ? stringValue(competency.name) : null,
      category: (question.category as QuestionCategory | undefined) ?? null,
      relevance: competency ? Number(competency.relevance ?? 1) : 1,
      mode: session.mode === "coach" ? "coach" : "real",
      degraded: session.degraded === true,
      assistanceCount: assistance.length,
    });
  }
  return evidence;
}

export async function createSessionWithPlan(
  supabase: SupabaseClient,
  userId: string,
  plan: PlannedQuestion[],
): Promise<InterviewSession> {
  assertConversationPlan(plan);
  const { data, error } = await supabase.rpc("create_conversation_session_with_plan", {
    p_plan: plan.map((question) => ({
      sequence: question.sequence,
      category: question.category,
      competency_id: persistableCompetencyId(question.competencyId),
      competency_name: question.competencyName ?? null,
      difficulty: question.difficulty,
      is_follow_up: question.isFollowUp,
      prompt: question.prompt,
    })),
  });
  if (error || !data) throw new RepositoryError("Could not start the interview.", error?.code ?? "NO_OWNED_ROW");
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const sessionId = result && stringValue(result.session_id);
  if (!sessionId) throw new RepositoryError("Could not find the created interview session.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the created interview session.", "NO_OWNED_ROW");
  return session;
}

/**
 * Persists a round's coverage targets before the interview starts, one
 * `interview_questions` row per target (via `p_blueprint.targets`), so later
 * turns can reuse the exact objective and evidence targets and so a reload
 * can reconstruct `blueprint.targets` with stable ids (see `mapSession`).
 *
 * `options` carries only `roundId`/`mode` -- deliberately not
 * `opportunityId`. Setting Career Brain context is `linkSessionCareerContext`'s
 * job alone (see its doc comment): no session-creation function ever writes
 * `opportunity_id`, so this one does not gain that parameter either.
 *
 * `assertConversationPlan(blueprint.questions)` still runs first as a cheap
 * legacy sanity check -- every blueprint this release produces still carries
 * the same five-entry backbone shape in `questions` (now with `prompt: null`)
 * alongside the real `targets` payload below.
 *
 * `follow_up_limit: 1`, not 0: a same-target continuation (probe/challenge/
 * hypothetical/non-park rescue) is persisted via `record_conversation_turn`'s
 * follow-up-row branch (see `recordConversationTurn`'s call site in
 * `route.ts`), because the just-answered row can never satisfy that branch's
 * sibling `answer is null` guard. Each row -- a target row here, or a
 * follow-up row created later -- is the parent of at most one further
 * continuation in this turn-by-turn model, so 1 is exact, not a placeholder.
 */
export async function createSessionWithBlueprint(
  supabase: SupabaseClient,
  userId: string,
  blueprint: InterviewBlueprint,
  options: { roundId: RoundId; mode: InterviewMode },
): Promise<InterviewSession> {
  assertConversationPlan(blueprint.questions);
  const { data, error } = await supabase.rpc("create_conversation_session_with_blueprint", {
    p_blueprint: {
      roundId: options.roundId,
      mode: options.mode,
      status: blueprint.status,
      fallback_reason: blueprint.fallbackReason,
      max_follow_ups: blueprint.maxFollowUps,
      max_questions: blueprint.maxQuestions,
      targets: blueprint.targets.map((target, index) => ({
        sequence: index + 1,
        category: target.category,
        competency_id: persistableCompetencyId(target.competencyId),
        competency_name: target.competencyName ?? null,
        difficulty: target.difficulty,
        objective: target.objective,
        evidence_ids: target.evidenceIds,
        expected_signals: target.expectedSignals,
        missing_signal_prompts: [],
        rubric_criteria: target.rubricCriteria,
        follow_up_limit: 1,
        source_confidence: null,
        required: target.required,
      })),
    },
  });
  if (error || !data) throw new RepositoryError("Could not start the interview.", error?.code ?? "NO_OWNED_ROW");
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const sessionId = result && stringValue(result.session_id);
  if (!sessionId) throw new RepositoryError("Could not find the created interview session.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the created interview session.", "NO_OWNED_ROW");
  return session;
}

/**
 * Starts a planned practice conversation and its owning practice plan in one
 * transaction, via `create_planned_conversation_session_with_blueprint`.
 * Persists the same blueprint fields as `createSessionWithBlueprint`
 * (objective, evidence targets, rubric criteria, follow-up limits), but for
 * 1-5 base questions instead of the fixed five-question backbone, and links
 * the session to `context.practicePlanId`/`context.opportunityId`. The RPC
 * atomically locks and transitions the plan from `ready` to `started`, so a
 * plan can only be started once; a second call fails server-side.
 */
export async function createSessionWithPracticeBlueprint(
  supabase: SupabaseClient,
  userId: string,
  blueprint: InterviewBlueprint,
  context: PracticeSessionContext,
): Promise<InterviewSession> {
  assertPracticeConversationBlueprint(blueprint);
  const { data, error } = await supabase.rpc("create_planned_conversation_session_with_blueprint", {
    p_blueprint: {
      status: blueprint.status,
      fallback_reason: blueprint.fallbackReason,
      max_follow_ups: blueprint.maxFollowUps,
      max_questions: blueprint.maxQuestions,
      questions: blueprint.questions.map((question) => ({
        sequence: question.sequence,
        category: question.category,
        competency_id: persistableCompetencyId(question.competencyId),
        competency_name: question.competencyName ?? null,
        difficulty: question.difficulty,
        prompt: question.prompt,
        objective: question.objective,
        evidence_ids: question.evidenceIds,
        expected_signals: question.expectedSignals,
        missing_signal_prompts: question.missingSignalPrompts,
        rubric_criteria: question.rubricCriteria ?? [],
        follow_up_limit: question.followUpLimit,
        source_confidence: question.sourceConfidence,
      })),
    },
    p_practice_plan_id: context.practicePlanId,
    p_opportunity_id: context.opportunityId,
  });
  if (error || !data) throw new RepositoryError("Could not start the planned practice session.", error?.code ?? "NO_OWNED_ROW");
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const sessionId = result && stringValue(result.session_id);
  if (!sessionId) throw new RepositoryError("Could not find the created practice session.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the created practice session.", "NO_OWNED_ROW");
  return session;
}

export async function createHandsOnSession(
  supabase: SupabaseClient,
  userId: string,
  exercise: HandsOnExercise,
): Promise<InterviewSession> {
  const { data, error } = await supabase
    .from("interview_sessions")
    .insert({ user_id: userId, kind: "hands-on", status: "active", exercise })
    .select("*")
    .single();
  if (error || !data) throw new RepositoryError("Could not start the hands-on interview.", error?.code);
  return hydrateSession(supabase, userId, data as Row);
}

/**
 * Starts a planned hands-on practice session and its owning practice plan in
 * one transaction, via `start_hands_on_practice_session`. Unlike
 * `createHandsOnSession` (a plain table insert), this must be an RPC because
 * it also has to atomically transition the plan from `ready` to `started`
 * and validate the plan/opportunity relationship server-side; `exercise` is
 * stored unchanged, matching the plain-insert shape of the generic path.
 */
export async function createHandsOnPracticeSession(
  supabase: SupabaseClient,
  userId: string,
  exercise: HandsOnExercise,
  context: PracticeSessionContext,
): Promise<InterviewSession> {
  const { data, error } = await supabase.rpc("start_hands_on_practice_session", {
    p_practice_plan_id: context.practicePlanId,
    p_opportunity_id: context.opportunityId,
    p_exercise: exercise,
  });
  if (error || !data) throw new RepositoryError("Could not start the hands-on practice session.", error?.code ?? "NO_OWNED_ROW");
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const sessionId = result && stringValue(result.session_id);
  if (!sessionId) throw new RepositoryError("Could not find the created hands-on practice session.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the created hands-on practice session.", "NO_OWNED_ROW");
  return session;
}

export async function recordAnswerAndEvaluation(
  supabase: SupabaseClient,
  userId: string,
  questionId: string,
  answer: string,
  evaluation: Evaluation,
): Promise<InterviewSession> {
  const { data, error } = await supabase.rpc("record_interview_evidence", {
    p_question_id: questionId,
    p_answer: answer,
    p_score: evaluation.score,
    p_dimensions: evaluation.dimensions,
    p_strengths: evaluation.strengths,
    p_needs_work: evaluation.needsWork,
    p_missing_points: evaluation.missingPoints,
    p_better_structure: evaluation.betterStructure,
    p_improved_answer: evaluation.improvedAnswer,
    ...evaluationGroundingRpcArgs(evaluation),
  });
  if (error || !data) throw new RepositoryError("Could not record your interview answer.", error?.code ?? "NO_OWNED_ROW");
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const sessionId = result && stringValue(result.session_id);
  if (!sessionId) throw new RepositoryError("Could not find the updated interview session.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the updated interview session.", "NO_OWNED_ROW");
  return session;
}

export type ConversationTurnPersistence = {
  nextQuestionId: string | null;
  nextPrompt: string | null;
  followUp: FollowUpDraft | null;
  /** The director intent the just-answered question was asked under. */
  askedIntent: Intent | null;
  /** Every rescue actually granted on the just-answered question this turn. */
  assistance: AssistanceRecord[];
  /** True when the candidate did not attempt the question; skips scoring. */
  nonAnswer: boolean;
  /** True when this turn fell back to a deterministic evaluation or line. */
  degraded: boolean;
  /** Set when this turn finishes the answered row without an answer; null otherwise. */
  setAsideReason: SetAsideReason | null;
};

/** Atomically records answer evidence and persists the exact next interviewer question. */
export async function recordConversationTurn(
  supabase: SupabaseClient,
  userId: string,
  questionId: string,
  answer: string,
  evaluation: Evaluation,
  next: ConversationTurnPersistence,
): Promise<InterviewSession> {
  const { data, error } = await supabase.rpc("record_conversation_turn", {
    p_question_id: questionId,
    p_answer: answer,
    p_score: evaluation.score,
    p_dimensions: evaluation.dimensions,
    p_strengths: evaluation.strengths,
    p_needs_work: evaluation.needsWork,
    p_missing_points: evaluation.missingPoints,
    p_better_structure: evaluation.betterStructure,
    p_improved_answer: evaluation.improvedAnswer,
    ...evaluationGroundingRpcArgs(evaluation),
    p_next_question_id: next.nextQuestionId,
    p_next_prompt: next.nextPrompt,
    p_follow_up: next.followUp,
    p_asked_intent: next.askedIntent,
    p_assistance: next.assistance,
    p_non_answer: next.nonAnswer,
    p_degraded: next.degraded,
    p_set_aside_reason: next.setAsideReason,
  });
  if (error || !data) throw new RepositoryError("Could not record your interview turn.", error?.code ?? "NO_OWNED_ROW");
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const sessionId = result && stringValue(result.session_id);
  if (!sessionId) throw new RepositoryError("Could not find the updated interview session.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the updated interview session.", "NO_OWNED_ROW");
  return session;
}

/**
 * Writes the interviewer's opening line onto the first question row. The
 * blueprint creates rows with a null prompt (spec §9.1), so a session is not
 * showable until this runs. A direct table write scoped by both `id` and
 * `user_id` -- the same pattern `completeSession` and
 * `linkSessionCareerContext` already use for owned-row writes elsewhere in
 * this file -- backed by `interview_questions`' `update_own` RLS policy.
 */
export async function revealFirstQuestion(
  supabase: SupabaseClient,
  userId: string,
  session: InterviewSession,
  opening: { intent: Intent; prompt: string; targetId: string },
): Promise<InterviewSession> {
  const first = session.questions.find((question) => question.answer === null);
  if (!first) throw new RepositoryError("The new interview has no question to reveal.", "NO_OWNED_ROW");
  const { error } = await supabase
    .from("interview_questions")
    .update({ prompt: opening.prompt, asked_intent: opening.intent, asked_at: new Date().toISOString() })
    .eq("id", first.id)
    .eq("user_id", userId);
  if (error) throw new RepositoryError("Could not start your interview.", error.code);
  const refreshed = await getSession(supabase, userId, session.id);
  if (!refreshed) throw new RepositoryError("Could not reload the new interview session.", "NO_OWNED_ROW");
  return refreshed;
}

/**
 * Maps a coverage target to the question row that will carry it.
 *
 * Every target reconstructed by `mapSession` uses its own question row's
 * database id as `target.id` (see that function's doc comment), and every
 * `Intent.targetId` the director produces comes from that same reconstructed
 * target list. So for any `targetId` this is actually called with, it IS a
 * question row id already -- the direct lookup below is exact, not a guess,
 * and returns null (rather than misrouting to a different row) whenever
 * that row does not exist or is no longer unanswered.
 *
 * This deliberately does not fall back to matching by `competencyName`: a
 * round can and does probe the same competency through more than one
 * target (`buildCoverageTargets` in `interview-planner.ts` assigns several
 * targets the same `competencyName` whenever a candidate has more relevant
 * competencies than backbone slots), so a name-based fallback could silently
 * hand the authored prompt to the wrong unanswered row instead of failing
 * loudly.
 */
export function questionIdForTarget(session: InterviewSession, targetId: string): string | null {
  return session.questions.find((question) => question.id === targetId && question.answer === null)?.id ?? null;
}

export async function saveHandsOnCheckpoint(
  supabase: SupabaseClient,
  userId: string,
  sessionId: string,
  code: string,
  note: string,
  interviewerPrompt: string,
): Promise<InterviewSession> {
  const session = await getSession(supabase, userId, sessionId);
  if (!session || session.kind !== "hands-on" || session.status !== "active") {
    throw new RepositoryError("The active hands-on interview was not found.", "NO_OWNED_ROW");
  }
  const { error } = await supabase.from("hands_on_checkpoints").insert({
    user_id: userId,
    session_id: sessionId,
    code,
    note,
    interviewer_prompt: interviewerPrompt,
  });
  if (error) throw new RepositoryError("Could not save your hands-on checkpoint.", error.code);
  const refreshed = await getSession(supabase, userId, sessionId);
  if (!refreshed) throw new RepositoryError("Could not reload your hands-on interview.", "NO_OWNED_ROW");
  return refreshed;
}

/** Atomically persists hands-on evaluations, competency evidence, and session completion. */
export async function completeHandsOnSession(
  supabase: SupabaseClient,
  userId: string,
  sessionId: string,
  result: { overallScore: number; summary: string; evaluations: Evaluation[] },
): Promise<InterviewSession> {
  const { data, error } = await supabase.rpc("complete_hands_on_session", {
    p_session_id: sessionId,
    p_overall_score: result.overallScore,
    p_summary: result.summary,
      p_evaluations: result.evaluations.map((evaluation) => ({
        competency_id: evaluation.competencyId,
        competency: evaluation.competency,
        score: evaluation.score,
        dimensions: evaluation.dimensions,
        strengths: evaluation.strengths,
        needs_work: evaluation.needsWork,
        missing_points: evaluation.missingPoints,
        better_structure: evaluation.betterStructure,
        improved_answer: evaluation.improvedAnswer,
        ...evaluationGroundingRecord(evaluation),
      })),
  });
  if (error || !data) throw new RepositoryError("Could not complete the hands-on interview.", error?.code ?? "NO_OWNED_ROW");
  const rpcRow = Array.isArray(data) ? data[0] as Row | undefined : data as Row;
  const completedSessionId = rpcRow && stringValue(rpcRow.session_id);
  if (!completedSessionId) throw new RepositoryError("Could not find the completed hands-on interview.", "NO_OWNED_ROW");
  const session = await getSession(supabase, userId, completedSessionId);
  if (!session) throw new RepositoryError("Could not reload the completed hands-on interview.", "NO_OWNED_ROW");
  return session;
}

export async function completeSession(
  supabase: SupabaseClient,
  userId: string,
  sessionId: string,
  result: { overallScore: number; summary: string },
): Promise<InterviewSession> {
  const { data, error } = await supabase
    .from("interview_sessions")
    .update({
      status: "complete",
      completed_at: new Date().toISOString(),
      overall_score: result.overallScore,
      result_summary: { summary: result.summary },
      updated_at: new Date().toISOString(),
    })
    .eq("id", sessionId)
    .eq("user_id", userId)
    .eq("status", "active")
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not complete the interview.", error?.code ?? "NO_OWNED_ROW");
  return hydrateSession(supabase, userId, data as Row);
}

/**
 * Links (or clears) an owned interview session's Career Brain context --
 * which practice plan explains why the session existed, and which real
 * opportunity it primarily prepared for. Either field may be null; passing
 * both null clears existing links. Never adds a required parameter to any
 * session-creation function, and never assigns context automatically --
 * this is the only place `practice_plan_id`/`opportunity_id` are written.
 *
 * When both `practicePlanId` and `opportunityId` are supplied, the
 * opportunity must be one the plan actually serves (a row in
 * `practice_plan_opportunities` for that exact user/plan/opportunity), and
 * if the plan has a `primary` opportunity, the requested `opportunityId`
 * must equal it -- see design doc section 10. A mismatch throws
 * `RepositoryError` with the stable code `INVALID_PLAN_CONTEXT` rather
 * than a raw constraint-violation error, since this relationship is
 * enforced here, not by a cross-table SQL constraint.
 */
export async function linkSessionCareerContext(
  supabase: SupabaseClient,
  userId: string,
  sessionId: string,
  context: SessionCareerContext,
): Promise<InterviewSession> {
  const { data: sessionRow, error: sessionError } = await supabase
    .from("interview_sessions")
    .select("id")
    .eq("id", sessionId)
    .eq("user_id", userId)
    .maybeSingle();
  if (sessionError || !sessionRow) {
    throw new RepositoryError("The interview session was not found.", sessionError?.code ?? "NO_OWNED_ROW");
  }

  if (context.practicePlanId !== null) {
    const { data: planRow, error: planError } = await supabase
      .from("practice_plans")
      .select("id")
      .eq("id", context.practicePlanId)
      .eq("user_id", userId)
      .maybeSingle();
    if (planError || !planRow) {
      throw new RepositoryError("The practice plan was not found.", planError?.code ?? "NO_OWNED_ROW");
    }
  }

  if (context.opportunityId !== null) {
    const { data: opportunityRow, error: opportunityError } = await supabase
      .from("opportunities")
      .select("id")
      .eq("id", context.opportunityId)
      .eq("user_id", userId)
      .maybeSingle();
    if (opportunityError || !opportunityRow) {
      throw new RepositoryError("The opportunity was not found.", opportunityError?.code ?? "NO_OWNED_ROW");
    }
  }

  if (context.practicePlanId !== null && context.opportunityId !== null) {
    const { data: linkRows, error: linkError } = await supabase
      .from("practice_plan_opportunities")
      .select("opportunity_id, relevance")
      .eq("user_id", userId)
      .eq("practice_plan_id", context.practicePlanId);
    if (linkError) {
      throw new RepositoryError("Could not verify the practice plan's linked opportunities.", linkError.code);
    }
    const links = (linkRows ?? []) as Row[];
    const associated = links.some((link) => stringValue(link.opportunity_id) === context.opportunityId);
    if (!associated) {
      throw new RepositoryError("The practice plan and opportunity do not match.", "INVALID_PLAN_CONTEXT");
    }
    const primaryLink = links.find((link) => link.relevance === "primary");
    if (primaryLink && stringValue(primaryLink.opportunity_id) !== context.opportunityId) {
      throw new RepositoryError("The practice plan and opportunity do not match.", "INVALID_PLAN_CONTEXT");
    }
  }

  const { error: updateError } = await supabase
    .from("interview_sessions")
    .update({
      practice_plan_id: context.practicePlanId,
      opportunity_id: context.opportunityId,
      updated_at: new Date().toISOString(),
    })
    .eq("id", sessionId)
    .eq("user_id", userId);
  if (updateError) {
    throw new RepositoryError("Could not link the interview session to its career context.", updateError.code);
  }

  const session = await getSession(supabase, userId, sessionId);
  if (!session) throw new RepositoryError("Could not reload the linked interview session.", "NO_OWNED_ROW");
  return session;
}

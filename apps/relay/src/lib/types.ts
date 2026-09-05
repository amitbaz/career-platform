import type { ReadinessDimension } from "@/lib/readiness-dimensions";

export type QuestionCategory =
  | "introduction"
  | "experience"
  | "technical"
  | "practical"
  | "architecture"
  | "system-design"
  | "behavioral"
  | "communication";

export type Difficulty = "foundational" | "intermediate" | "senior" | "advanced";

export type RoundId = "recruiter" | "tech-lead" | "hr" | "founder" | "code-review";

export type InterviewMode = "coach" | "real";

export type ProbeAspect =
  | "specifics"
  | "ownership"
  | "tradeoff"
  | "outcome"
  | "collaboration"
  | "hindsight";

export type RescueStyle = "narrow" | "hook" | "reframe" | "park";

/**
 * Why a question stopped being the candidate's current one without ever being
 * answered. `parked` is recoverable -- the interview may come back to it while
 * turns remain (spec §9.3 rule 3); `rescue-budget-spent` is not.
 */
export type SetAsideReason = "parked" | "rescue-budget-spent";

/**
 * One exchange the assessor refused to score, kept verbatim. The prompt is
 * copied in because the row's own `prompt` is overwritten by the next re-ask.
 */
export type NonAnswerRecord = {
  prompt: string;
  answer: string;
  at: string;
};

export type AdvanceReason =
  | "satisfied"
  | "line-exhausted"
  | "rescue-budget-spent"
  | "turn-budget";

/**
 * A single decided interviewer move. The director produces exactly one per
 * turn; the interviewer model authors speech for it and decides nothing.
 *
 * `basis` and `claim` carry the candidate's own words, so the interviewer can
 * react to what was actually said without ever receiving the rubric.
 */
export type Intent =
  | { kind: "open"; targetId: string }
  | { kind: "probe"; targetId: string; aspect: ProbeAspect; basis: string }
  | { kind: "challenge"; targetId: string; claim: string }
  /**
   * `targetId` is the target the interviewer's line is ABOUT. For every style
   * but `park` that is the target being rescued. A `park` moves the interview
   * elsewhere, so its `targetId` is the destination and `parkedTargetId` names
   * the target being set aside. Absent on intents persisted before this change.
   */
  | { kind: "rescue"; targetId: string; style: RescueStyle; hook: string | null; parkedTargetId?: string | null }
  | { kind: "advance"; targetId: string; reason: AdvanceReason }
  | { kind: "hypothetical"; targetId: string; basis: string }
  | { kind: "candidate-questions" }
  | { kind: "close" };

export type IntentKind = Intent["kind"];

/** One rescue actually applied to a question, for results display and Release 3. */
export type AssistanceRecord = {
  style: RescueStyle;
  at: string;
};

export type ModePolicy = {
  rescuesPerQuestion: number;
  rescuesPerSession: number;
  rescueStyles: RescueStyle[];
  pushback: "light" | "firm";
  parkAndReturn: boolean;
  acknowledgeStruggle: boolean;
};

/**
 * A competency the round must cover. Replaces pre-written prompt text: the
 * blueprint now says what to find out, not what to say.
 *
 * `objective`, `expectedSignals` and `rubricCriteria` are assessor-only and
 * must never reach the interviewer call (spec §11.1).
 */
export type CoverageTarget = {
  id: string;
  competencyId: string | null;
  competencyName: string | null;
  category: QuestionCategory;
  evidenceIds: string[];
  difficulty: Difficulty;
  objective: string;
  expectedSignals: string[];
  rubricCriteria: string[];
  required: boolean;
};

export type TargetStatus = "unasked" | "open" | "satisfied" | "parked" | "skipped";

export type TargetState = {
  target: CoverageTarget;
  status: TargetStatus;
  turnsSpent: number;
  rescuesSpent: number;
  askedIntents: Intent[];
};

/** The assessor's coarse read of the answer, separate from its rubric scoring. */
export type AssessmentRead = "answered" | "partial" | "evasive" | "stuck";

export type Competency = {
  id: string;
  name: string;
  relevance: number;
  expectedLevel: Difficulty;
  estimatedLevel: Difficulty | null;
  confidence: "low" | "medium" | "high" | null;
  lastPracticedAt: string | null;
  questionCount: number;
  averageScore: number | null;
  recentScore: number | null;
  strengths: string[];
  weaknesses: string[];
};

/** A source-backed engineering fact that can justify question planning and feedback. */
export type EvidenceItem = {
  id: string;
  sourceKind: "cv" | "cover_letter" | "summary" | null;
  sourceExcerpt: string;
  projectOrEmployer: string | null;
  ownership: string | null;
  technologies: string[];
  decision: string | null;
  constraint: string | null;
  outcome: string | null;
  recency: string | null;
  confidence: number;
};

/**
 * Deterministic assessment of how much source-backed detail is available for
 * interview grounding. `ready === false` is advisory: it selects broader
 * discovery-oriented practice and must not by itself block a session start.
 */
export type ProfileReadiness = {
  ready: boolean;
  missing: string[];
};

export type BlueprintStatus = "grounded" | "limited-grounding";

export type PlannedQuestion = {
  id: string;
  sequence: number;
  category: QuestionCategory;
  competencyId: string | null;
  competencyName: string | null;
  difficulty: Difficulty;
  isFollowUp: boolean;
  /** Null until the interviewer authors it at reveal time. */
  prompt: string | null;
  answer: string | null;
  createdAt: string;
  /** The director intent that produced this question's prompt. */
  askedIntent: Intent | null;
  assistance: AssistanceRecord[];
  /** True when the candidate did not attempt the question; never scored. */
  nonAnswer: boolean;
  /** Set when this row was finished without an answer; null while it is still open. */
  setAsideAt: string | null;
  setAsideReason: SetAsideReason | null;
  /** Every unscored exchange on this row, oldest first. */
  nonAnswers: NonAnswerRecord[];
  objective?: string;
  evidenceIds?: string[];
  expectedSignals?: string[];
  missingSignalPrompts?: string[];
  rubricCriteria?: string[];
  followUpLimit?: number;
  sourceConfidence?: number | null;
  parentQuestionId?: string | null;
};

/**
 * A persisted interview question contract whose objective, evidence targets,
 * rubric criteria, and evidence targets must remain stable across the session.
 */
export type BlueprintQuestion = PlannedQuestion & {
  objective: string;
  evidenceIds: string[];
  expectedSignals: string[];
  missingSignalPrompts: string[];
  rubricCriteria?: string[];
  followUpLimit: number;
  sourceConfidence: number | null;
};

/**
 * The interview plan generated before the first answer is collected.
 * Reused by two separate contracts, not renamed for either: generic
 * interviews carry the exact five-question backbone (enforced by
 * `assertConversationPlan` in `src/lib/repositories/interviews.ts`), while
 * planned practice sessions carry 1-5 base questions (enforced by
 * `assertPracticeConversationBlueprint`, the sibling validator used by the
 * `PracticeSessionContext`-driven start RPCs).
 */
export type InterviewBlueprint = {
  status: BlueprintStatus;
  fallbackReason: string | null;
  maxFollowUps: number;
  maxQuestions: number;
  createdAt: string;
  /** Legacy pre-written questions. Present on sessions created before this release. */
  questions: BlueprintQuestion[];
  roundId: RoundId;
  turnBudget: number;
  targets: CoverageTarget[];
};

/**
 * Server-generated follow-up content; the follow-up keeps the original rubric
 * contract so later persistence and hydration can preserve the exact objective.
 */
export type FollowUpDraft = {
  category: PlannedQuestion["category"];
  competencyId: string | null;
  competencyName: string | null;
  difficulty: Difficulty;
  isFollowUp: true;
  prompt: string;
  objective: string;
  evidenceIds: string[];
  expectedSignals: string[];
  missingSignalPrompts: string[];
  rubricCriteria: string[];
  followUpLimit: number;
  sourceConfidence: number | null;
  parentQuestionId?: string | null;
};

/**
 * The assessor's depth/seniority read on one answer, separate from both the
 * 0-10 `dimensions` scores and `AssessmentRead` (which is about whether an
 * attempt happened at all, not how good it was). Named after issue #21's own
 * four categories so a fixture written against the issue reads directly
 * against this type: "strong" is that issue's "strong", not `Difficulty`'s
 * "senior" question-difficulty level, which is a different axis entirely.
 */
export type AnswerTier = "incorrect" | "incomplete" | "shallow" | "strong";

export type Evaluation = {
  score: number;
  questionId?: string | null;
  competencyId: string | null;
  competency: string;
  dimensions: Partial<Record<
    | "correctness"
    | "depth"
    | "clarity"
    | "structure"
    | "practicalExperience"
    | "tradeOffAwareness"
    | "communication"
    | "confidence"
    | "relevance",
    number
  >>;
  strengths: string[];
  needsWork: string[];
  /** Specific content the candidate omitted and should add next time. */
  missingPoints: string[];
  /** Recommended sequencing changes that make the answer easier to follow. */
  betterStructure: string[];
  /** A concise example of how the answer could be improved without inventing facts. */
  improvedAnswer: string;
  /** Optional grounded-answer metadata persisted for richer coaching history. */
  relevance?: number | null;
  /** Optional exact claims the answer supports. */
  supportedClaims?: string[];
  /** Optional expected rubric signals the answer actually covered. */
  expectedSignalsPresent?: string[];
  /** Optional unsupported or contradictory claims the answer introduced. */
  unsupportedClaims?: string[];
  /** Optional dimension-level justifications for the grounded evaluation. */
  dimensionReasons?: Partial<Record<
    | "correctness"
    | "depth"
    | "clarity"
    | "structure"
    | "practicalExperience"
    | "tradeOffAwareness"
    | "communication"
    | "confidence"
    | "relevance",
    string
  >>;
  /** Optional depth/seniority read — present on every `GroundedEvaluation`, optional here for evaluations built before this field existed. */
  answerTier?: AnswerTier;
};

/** A fully grounded interview evaluation with answer-relevance and claim evidence. */
export type GroundedEvaluation = Evaluation & {
  relevance: number;
  supportedClaims: string[];
  expectedSignalsPresent: string[];
  unsupportedClaims: string[];
  answerTier: AnswerTier;
  dimensionReasons: Record<
    | "correctness"
    | "depth"
    | "clarity"
    | "structure"
    | "practicalExperience"
    | "tradeOffAwareness"
    | "communication"
    | "confidence"
    | "relevance",
    string
  >;
};

export type CompetencyScope = {
  name: string;
  relevance: number;
};

export type ProfileDraft = {
  role: string | null;
  seniority: string | null;
  summary: string | null;
  narrative: string | null;
  expertise: string[];
  characteristics: string[];
  competencies: CompetencyScope[];
};

export type ProfileSource = {
  cvText: string;
  coverLetter: string;
  cvFileName?: string | null;
  coverLetterFileName?: string | null;
};

export type Profile = {
  userId: string;
  role: string | null;
  seniority: string | null;
  summary: string | null;
  narrative: string | null;
  expertise: string[];
  characteristics: string[];
  competencies: Competency[];
  /** Persisted source-backed evidence. Legacy rows may hydrate without it. */
  evidence?: EvidenceItem[];
  /** Latest deterministic readiness decision. Legacy rows may hydrate without it. */
  readiness?: ProfileReadiness;
  source: ProfileSource;
  createdAt: string;
  updatedAt: string;
};

export type Message = {
  id: string;
  role: "interviewer" | "candidate";
  content: string;
  createdAt: string;
};

export type HandsOnExercise = {
  title: string;
  durationMinutes: number;
  briefing: string;
  requirements: string[];
  starterCode: string;
  interviewerOpening: string;
};

export type HandsOnCheckpoint = {
  id: string;
  code: string;
  note: string;
  interviewerPrompt: string;
  createdAt: string;
};

export type InterviewSession = {
  id: string;
  userId: string;
  kind: "conversation" | "hands-on";
  roundId: RoundId;
  mode: InterviewMode;
  status: "active" | "complete";
  /** True once any turn fell back to a deterministic evaluation or line (spec §13.2). */
  degraded: boolean;
  startedAt: string;
  completedAt: string | null;
  exercise: Record<string, unknown>;
  resultSummary: Record<string, unknown>;
  overallScore: number | null;
  questions: PlannedQuestion[];
  /** The persisted planning contract for grounded sessions; legacy rows may omit it. */
  blueprint?: InterviewBlueprint | null;
  checkpoints: HandsOnCheckpoint[];
  evaluations: Evaluation[];
  messages: Message[];
  createdAt: string;
  updatedAt: string;
  /** Why this practice existed; null for legacy sessions and any session never linked to a plan. */
  practicePlanId: string | null;
  /** The primary real job/interview this session prepared for; null for legacy sessions and any unlinked session. */
  opportunityId: string | null;
};

/**
 * The Career Brain context to attach to an existing interview session via
 * `linkSessionCareerContext` in `src/lib/repositories/interviews.ts`. Either
 * or both fields may be null; passing both null clears existing links.
 */
export type SessionCareerContext = {
  practicePlanId: string | null;
  opportunityId: string | null;
};

/**
 * The plan (and optional opportunity) context passed to a planned practice
 * session's atomic start RPC -- `createSessionWithPracticeBlueprint` and
 * `createHandsOnPracticeSession` in `src/lib/repositories/interviews.ts`.
 * Unlike `SessionCareerContext` (used to link an already-existing session,
 * where both fields may be null), `practicePlanId` here is always the plan
 * being started and can never be null.
 */
export type PracticeSessionContext = {
  practicePlanId: string;
  opportunityId: string | null;
};

/**
 * Why one dimension scored what it did. Carried on the result rather than
 * stored, so a consumer can explain a score without a second query. Ordered
 * heaviest-contribution first.
 */
export type ReadinessContribution = {
  questionEvaluationId: string;
  sessionId: string;
  recordedAt: string;
  score: number;
  /** strength x recency x relevance, the product actually used in the mean. */
  weight: number;
};

export type ReadinessTrend = "improving" | "stable" | "worsening" | "unresolved";

export type ReadinessDimensionResult = {
  dimension: ReadinessDimension;
  /** 0-100, or null when this dimension has no evidence at all. */
  score: number | null;
  confidence: "low" | "medium" | "high" | null;
  trend: ReadinessTrend;
  evidenceCount: number;
  /** Total accumulated weight. This, not the raw count, drives confidence. */
  totalWeight: number;
  contributions: ReadinessContribution[];
};

/**
 * Relay's single evidence-backed view of interview preparedness. It replaced
 * the lifetime-average score Relay used to keep alongside it; there is
 * deliberately no second competing readiness number anywhere in the app.
 *
 * `overall` and `overallTrend` are both DERIVED from `dimensions` -- never
 * computed independently and never generated by a model (issue #14 acceptance
 * criteria).
 */
export type ReadinessModel = {
  overall: number | null;
  overallConfidence: "low" | "medium" | "high" | null;
  overallTrend: ReadinessTrend;
  dimensions: ReadinessDimensionResult[];
  /** Graded answers that matched no dimension. Non-zero means a mapping gap. */
  unmappedEvidenceCount: number;
  /** When the model was computed, so a consumer can show its age. */
  computedAt: string;
};

/**
 * One graded answer, flattened with the session conditions needed to weight it.
 * This is the only input the readiness aggregation reads.
 */
export type ReadinessEvidence = {
  questionEvaluationId: string;
  sessionId: string;
  recordedAt: string;
  /** The rubric score on the existing 0-10 scale. */
  score: number;
  competencyId: string | null;
  competencyName: string | null;
  category: QuestionCategory | null;
  /** 0-1 role relevance from the competency row; 1 when there is no competency. */
  relevance: number;
  mode: InterviewMode;
  degraded: boolean;
  assistanceCount: number;
};

/**
 * The full job lifecycle, from a saved/shortlisted role through its outcome.
 * `opportunities` is the single canonical record for both roles a user is
 * only considering and roles they have actually applied to or interviewed
 * for; there is no separate "applications" entity.
 */
export type OpportunityStatus =
  | "considering"
  | "applied"
  | "interviewing"
  | "offer"
  | "rejected"
  | "withdrawn"
  | "closed";

/** The kinds of facts recorded in an opportunity's append-only history. */
export type OpportunityEventType =
  | "created"
  | "status_changed"
  | "interview_scheduled"
  | "interview_completed"
  | "note"
  | "source_updated";

export type Opportunity = {
  id: string;
  userId: string;
  company: string;
  role: string;
  status: OpportunityStatus;
  location: string | null;
  remote: boolean | null;
  jobUrl: string | null;
  jobDescription: string | null;
  /** Human-readable source such as an employer or ATS name. */
  sourceLabel: string | null;
  /** Stable integration namespace, e.g. "manual", "job-hunter", "tracker-import". */
  sourceSystem: string | null;
  /** Stable source-owned identity used to prevent duplicate imports of the same listing. */
  sourceExternalId: string | null;
  /** 0-100 fit score when available. */
  matchScore: number | null;
  strengths: string[];
  gaps: string[];
  notes: string | null;
  appliedAt: string | null;
  nextInterviewAt: string | null;
  createdAt: string;
  updatedAt: string;
};

/** One append-only fact in an opportunity's lifecycle history. Never mutated after creation. */
export type OpportunityEvent = {
  id: string;
  userId: string;
  opportunityId: string;
  eventType: OpportunityEventType;
  fromStatus: OpportunityStatus | null;
  toStatus: OpportunityStatus | null;
  occurredAt: string;
  note: string | null;
  metadata: Record<string, unknown>;
  createdAt: string;
};

/**
 * Every opportunity is created in `considering` status via `create_opportunity`;
 * status is deliberately not settable here so the summary row and its history
 * cannot disagree at birth. Use `transitionOpportunity` to move it onward.
 */
export type CreateOpportunityInput = {
  company: string;
  role: string;
  location?: string | null;
  remote?: boolean | null;
  jobUrl?: string | null;
  jobDescription?: string | null;
  sourceLabel?: string | null;
  sourceSystem?: string | null;
  sourceExternalId?: string | null;
  matchScore?: number | null;
  strengths?: string[];
  gaps?: string[];
  notes?: string | null;
};

/**
 * Ordinary descriptive-field updates. Deliberately excludes `status`,
 * `appliedAt`, and `nextInterviewAt` — those lifecycle fields only change
 * together with their history, through `transitionOpportunity` and
 * `scheduleOpportunityInterview`.
 */
export type UpdateOpportunityDetailsInput = {
  company?: string;
  role?: string;
  location?: string | null;
  remote?: boolean | null;
  jobUrl?: string | null;
  jobDescription?: string | null;
  sourceLabel?: string | null;
  sourceSystem?: string | null;
  sourceExternalId?: string | null;
  matchScore?: number | null;
  strengths?: string[];
  gaps?: string[];
  notes?: string | null;
};

/**
 * A career story is a real, reusable professional experience the user can
 * tell in an interview -- not a coach inference. `completeness` describes
 * whether enough factual fields exist to use the story, not how well the
 * user delivers it, and Release 1 never computes it automatically.
 */
export type CareerStoryReviewState = "draft" | "confirmed" | "retired";

export type CareerStory = {
  id: string;
  userId: string;
  title: string;
  situation: string | null;
  responsibility: string | null;
  problem: string | null;
  actions: string | null;
  alternatives: string | null;
  tradeoffs: string | null;
  ownership: string | null;
  outcome: string | null;
  lessons: string | null;
  tags: string[];
  /** 0-1; caller-provided or the database default, never computed by Release 1. */
  completeness: number;
  reviewState: CareerStoryReviewState;
  confirmedAt: string | null;
  createdAt: string;
  updatedAt: string;
};

/**
 * The nine free-text fields a career story is drafted with. Passed to
 * `careerStoryCompleteness` (`src/lib/career-story.ts`), which maps them
 * onto six factual dimensions to score how much of the story is filled in
 * -- see that function's doc comment for the exact mapping.
 */
export type CareerStoryDraftFields = Pick<
  CareerStory,
  | "situation"
  | "responsibility"
  | "problem"
  | "actions"
  | "alternatives"
  | "tradeoffs"
  | "ownership"
  | "outcome"
  | "lessons"
>;

/**
 * Typed provenance link from a career story to the durable evidence that
 * backs it. Exactly one source is set, enforced in the database with
 * `check (num_nonnulls(profile_evidence_id, interview_question_id) = 1)`.
 */
export type CareerStoryEvidence = {
  id: string;
  userId: string;
  careerStoryId: string;
  profileEvidenceId: string | null;
  interviewQuestionId: string | null;
  note: string | null;
  createdAt: string;
};

/**
 * A discriminated union so callers cannot supply two source IDs when
 * attaching career story evidence. See `storyEvidenceColumns` in
 * `src/lib/repositories/stories.ts` for the one place this is converted to
 * nullable database columns.
 */
export type CareerStoryEvidenceSource =
  | { kind: "profile_evidence"; profileEvidenceId: string }
  | { kind: "interview_question"; interviewQuestionId: string };

export type CreateCareerStoryInput = {
  title: string;
  situation?: string | null;
  responsibility?: string | null;
  problem?: string | null;
  actions?: string | null;
  alternatives?: string | null;
  tradeoffs?: string | null;
  ownership?: string | null;
  outcome?: string | null;
  lessons?: string | null;
  tags?: string[];
  /** Omit to use the database default of 0. Release 1 never computes this automatically. */
  completeness?: number;
  reviewState?: CareerStoryReviewState;
};

export type UpdateCareerStoryInput = {
  title?: string;
  situation?: string | null;
  responsibility?: string | null;
  problem?: string | null;
  actions?: string | null;
  alternatives?: string | null;
  tradeoffs?: string | null;
  ownership?: string | null;
  outcome?: string | null;
  lessons?: string | null;
  tags?: string[];
  completeness?: number;
  reviewState?: CareerStoryReviewState;
  confirmedAt?: string | null;
};

/** A `CareerStory` enriched for display with the count of its attached provenance rows. */
export type CareerStorySummary = CareerStory & {
  evidenceCount: number;
};

export type CoachObservationType =
  | "strength"
  | "weakness"
  | "answer_habit"
  | "knowledge_gap"
  | "story_gap"
  | "story_strength"
  | "delivery_pattern"
  | "other";

export type CoachObservationTrend = "unresolved" | "improving" | "stable" | "worsening";

export type CoachObservationReviewState = "unreviewed" | "confirmed" | "corrected" | "dismissed";

/**
 * A persistent, inspectable coach inference about the user (e.g. "you skip
 * tradeoffs"). Release 1 only stores and lets the user review observations
 * -- it never generates, infers, or reconciles them. `claim` is the
 * original AI/system wording and is never overwritten; a user's review
 * (confirmation, correction, or dismissal) is recorded alongside it via
 * `reviewState` and the `*_at` timestamps, never over it. See
 * `reviewCoachObservation` in `src/lib/repositories/observations.ts` for
 * the exact timestamp rules per review state.
 */
export type CoachObservation = {
  id: string;
  userId: string;
  observationType: CoachObservationType;
  claim: string;
  /** 0-1; caller-provided or the database default. Release 1 never computes this automatically. */
  confidence: number;
  /** 0-1; caller-provided or the database default. Release 1 never computes this automatically. */
  importance: number;
  trend: CoachObservationTrend;
  reviewState: CoachObservationReviewState;
  /** User's corrected wording/context, preserved separately from `claim`. Set only when `reviewState` is `"corrected"`. */
  userCorrection: string | null;
  firstSeenAt: string | null;
  lastSeenAt: string | null;
  confirmedAt: string | null;
  correctedAt: string | null;
  dismissedAt: string | null;
  createdAt: string;
  updatedAt: string;
};

export type CreateCoachObservationInput = {
  observationType: CoachObservationType;
  claim: string;
  confidence?: number;
  importance?: number;
  trend?: CoachObservationTrend;
  firstSeenAt?: string | null;
  lastSeenAt?: string | null;
};

/**
 * A user's review of a coach observation. `corrected` is the only state
 * that carries a `correction` -- it is stored in `userCorrection`, the
 * original `claim` is never overwritten. Excluding a `correction` field
 * from `confirmed`/`dismissed` at the type level prevents a caller from
 * supplying a correction that would silently be discarded.
 */
export type CoachObservationReview =
  | { state: "confirmed" }
  | { state: "dismissed" }
  | { state: "corrected"; correction: string };

export type ObservationEvidenceRole = "supporting" | "contradicting" | "context";

/**
 * Typed provenance link from a coach observation to the durable evidence
 * that backs it. Exactly one source is set, enforced in the database with
 * `check (num_nonnulls(profile_evidence_id, question_evaluation_id, career_story_id, opportunity_event_id) = 1)`.
 */
export type ObservationEvidence = {
  id: string;
  userId: string;
  observationId: string;
  profileEvidenceId: string | null;
  questionEvaluationId: string | null;
  careerStoryId: string | null;
  opportunityEventId: string | null;
  evidenceRole: ObservationEvidenceRole;
  /** 0-1; caller-provided or the database default of 1. Release 1 never computes this automatically. */
  weight: number;
  reason: string | null;
  createdAt: string;
};

/**
 * A discriminated union so callers cannot supply more than one source ID
 * when attaching observation evidence. See `observationEvidenceColumns` in
 * `src/lib/repositories/observations.ts` for the one place this is
 * converted to nullable database columns.
 */
export type ObservationEvidenceSource =
  | { kind: "profile_evidence"; profileEvidenceId: string }
  | { kind: "question_evaluation"; questionEvaluationId: string }
  | { kind: "career_story"; careerStoryId: string }
  | { kind: "opportunity_event"; opportunityEventId: string };

export type AttachObservationEvidenceOptions = {
  role?: ObservationEvidenceRole;
  weight?: number;
  reason?: string | null;
};

/**
 * A user-safe display item resolved from one typed `ObservationEvidence` row
 * by `resolveObservationEvidence` in `src/lib/coach-memory.ts`. The browser
 * never sees the raw evidence-source id or joins the underlying table
 * itself -- `label`/`summary` are always human-readable, per source kind:
 * profile evidence -> project/employer plus a source-excerpt summary;
 * question evaluation -> the question prompt plus a concise strength/
 * weakness summary; career story -> its title; opportunity event ->
 * company/role plus an event description.
 */
export type CoachEvidenceDisplay = {
  kind: "profile_evidence" | "question_evaluation" | "career_story" | "opportunity_event";
  label: string;
  summary: string;
  role: ObservationEvidenceRole;
  reason: string | null;
};

/**
 * A `CoachObservation` enriched for display by `loadCareerDashboard` in
 * `src/lib/career-dashboard.ts`. `effectiveText` is the corrected wording
 * when the user has reviewed and corrected the observation, otherwise the
 * original `claim` -- `claim` itself always remains available separately,
 * unmodified.
 */
export type CoachObservationSummary = CoachObservation & {
  effectiveText: string;
  evidence: CoachEvidenceDisplay[];
};

export type PracticePlanStatus = "draft" | "ready" | "started" | "completed" | "cancelled" | "failed";

export type PracticeFormat =
  | "targeted_drill"
  | "story_work"
  | "self_presentation"
  | "behavioral"
  | "technical_communication"
  | "role_prep"
  | "full_simulation"
  | "hands_on";

export type PracticePlanOpportunityRelevance = "primary" | "supporting";

/**
 * One row linking a practice plan to an opportunity it serves. A plan may
 * serve several opportunities, but at most one link per plan may be
 * `"primary"` -- enforced by the database's partial unique index
 * (`practice_plan_one_primary_opportunity_idx`) and, before any write, by
 * `setPracticePlanOpportunities` in `src/lib/repositories/practice-plans.ts`.
 */
export type PracticePlanOpportunity = {
  userId: string;
  practicePlanId: string;
  opportunityId: string;
  relevance: PracticePlanOpportunityRelevance;
  createdAt: string;
};

/**
 * One link to include when replacing a practice plan's opportunity set via
 * `setPracticePlanOpportunities`. `relevance` defaults to `"supporting"`
 * when omitted, matching the database column default.
 */
export type PracticePlanOpportunityLink = {
  opportunityId: string;
  relevance?: PracticePlanOpportunityRelevance;
};

/**
 * A practice plan is the explicit persisted contract explaining what a
 * future practice session is trying to improve and why (see
 * `docs/superpowers/specs/2026-08-30-career-brain-release-1-foundation-design.md`
 * section 9). `opportunities` hydrates the plan's current links from
 * `practice_plan_opportunities`, so later callers (Release 2) get the full
 * plan-and-links shape from one repository call instead of a
 * table-specific query.
 *
 * Release 1 does not define the prioritization formula -- `priorityScore`
 * and `priorityFactors` are nullable/default placeholders reserved for
 * Release 3's deterministic recommendation snapshot and are never computed
 * here.
 */
export type PracticePlan = {
  id: string;
  userId: string;
  status: PracticePlanStatus;
  primaryFocus: string;
  secondaryFocus: string | null;
  rationale: string;
  format: PracticeFormat;
  /** Minutes; null when not estimated, otherwise constrained to 1-180. */
  estimatedMinutes: number | null;
  successCriteria: unknown[];
  priorityScore: number | null;
  priorityFactors: Record<string, unknown>;
  /** Set when a later AI-generation step fails, so the plan persists safely instead of being lost. */
  generationError: string | null;
  completedAt: string | null;
  createdAt: string;
  updatedAt: string;
  opportunities: PracticePlanOpportunity[];
};

export type CreatePracticePlanInput = {
  status?: PracticePlanStatus;
  primaryFocus: string;
  secondaryFocus?: string | null;
  rationale?: string;
  format: PracticeFormat;
  estimatedMinutes?: number | null;
  successCriteria?: unknown[];
  priorityScore?: number | null;
  priorityFactors?: Record<string, unknown>;
  generationError?: string | null;
  completedAt?: string | null;
};

/**
 * One user-displayable fact behind the current baseline practice
 * recommendation, produced by `recommendPractice` in
 * `src/lib/practice-recommendation.ts`. Rendered on Home as an explanation
 * chip under "Why this?" -- `detail` must always be human-readable and must
 * never contain a raw id.
 */
export type PracticeRecommendationSignal = {
  kind:
    | "upcoming_interview"
    | "interviewing_opportunity"
    | "reviewed_observation"
    | "story_bank_gap"
    // Named for the Progress view it originally explained, and rendered under a
    // "readiness signal" label since the readiness model replaced that view. The
    // mismatch is deliberate: this is a value in a persisted shape, and rows
    // already carry the string, so renaming it would strand existing data.
    | "progress_weakness"
    | "applied_opportunity"
    | "first_practice"
    | "coverage_gap"
    | "fallback";
  label: string;
  detail: string;
};

/**
 * The deterministic Release 2 baseline practice recommendation preview. Not
 * yet a persisted `PracticePlan` -- it becomes one only when the user starts
 * it (see
 * `docs/superpowers/specs/2026-08-31-career-brain-release-2-relay-rework-design.md`
 * section 6). Release 2 deliberately does not compute a Release 3-style
 * weighted priority score here.
 */
export type PracticeRecommendation = {
  format: PracticeFormat;
  primaryFocus: string;
  secondaryFocus: string | null;
  rationale: string;
  estimatedMinutes: number;
  successCriteria: string[];
  primaryOpportunityId: string | null;
  supportingOpportunityIds: string[];
  signals: PracticeRecommendationSignal[];
};

/**
 * Inputs to the deterministic baseline recommendation selector
 * (`recommendPractice`). `now` is always caller-supplied -- the selector
 * never reads the clock itself -- so selection stays deterministic and
 * testable.
 */
export type PracticeRecommendationInput = {
  opportunities: Opportunity[];
  observations: CoachObservation[];
  stories: CareerStory[];
  readiness: ReadinessModel;
  /** The profile's current competencies, used to map a weak readiness dimension back to a competency to practise. */
  competencies: Competency[];
  recentSessions: InterviewSession[];
  recentPlans: PracticePlan[];
  now: Date;
};

/**
 * Career Brain grounding inputs for `generatePracticeBlueprint` in
 * `src/lib/coach.ts` -- distinct from `PracticeSessionContext`, which links
 * an already-generated blueprint's session to its plan/opportunity, not the
 * material used to generate it. `primaryOpportunity`/`supportingOpportunities`
 * shape what the blueprint probes (job requirements are targets to probe,
 * never candidate evidence). `observations` and `stories` may include
 * unreviewed/draft rows -- `generatePracticeBlueprint` itself filters to the
 * reviewed subset it is allowed to ground on (see that function's
 * documentation for the exact filter).
 */
export type PracticeBlueprintContext = {
  primaryOpportunity: Opportunity | null;
  supportingOpportunities: Opportunity[];
  observations: CoachObservation[];
  stories: CareerStory[];
};

/**
 * The single canonical read model for the Career Brain Home command center,
 * built by `loadCareerDashboard` in `src/lib/career-dashboard.ts`. The
 * client shell fetches this once after auth and never joins the underlying
 * Career Brain sources itself.
 *
 * `observations` excludes dismissed rows -- unreviewed observations may
 * still appear since Coach displays them, but never drive `recommendation`
 * (that filtering lives entirely inside `recommendPractice`).
 * `upcomingOpportunities` is `opportunities` filtered to future-dated
 * interviews, soonest first. `coachMode` reflects whether AI-backed
 * features are configured (`GEMINI_API_KEY` present) or running in demo
 * mode; it is computed by the route, not this read model, since it depends
 * on process configuration rather than persisted data.
 */
export type CareerDashboard = {
  profile: Profile;
  coachMode: "demo" | "live";
  readiness: ReadinessModel;
  recentSessions: InterviewSession[];
  opportunities: Opportunity[];
  upcomingOpportunities: Opportunity[];
  observations: CoachObservationSummary[];
  stories: CareerStorySummary[];
  recentPracticePlans: PracticePlan[];
  recommendation: PracticeRecommendation;
};

import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import { generatePracticeBlueprint, handsOnExercise } from "@/lib/coach";
import { recommendPractice } from "@/lib/practice-recommendation";
import { calculateReadiness } from "@/lib/readiness";
import {
  createHandsOnPracticeSession,
  createSessionWithPracticeBlueprint,
  listReadinessEvidence,
  listRecentSessions,
} from "@/lib/repositories/interviews";
import { listCoachObservations } from "@/lib/repositories/observations";
import { listOpportunities } from "@/lib/repositories/opportunities";
import {
  createPracticePlan,
  getPracticePlan,
  isPracticeFormat,
  listPracticePlans,
  setPracticePlanOpportunities,
  updatePracticePlan,
} from "@/lib/repositories/practice-plans";
import { RepositoryError, getProfile } from "@/lib/repositories/profile";
import { listCareerStories } from "@/lib/repositories/stories";
import type {
  CareerStory,
  CoachObservation,
  InterviewSession,
  Opportunity,
  PracticeFormat,
  PracticePlan,
  PracticePlanOpportunityLink,
  PracticeRecommendation,
  PracticeSessionContext,
  Profile,
  ReadinessModel,
} from "@/lib/types";

/** Longest a practice plan may claim to take; matches the `practice_plans.estimated_minutes` check constraint. */
const MAX_ESTIMATED_MINUTES = 180;

/** Rationale stored for user-chosen practice, where no deterministic selector produced one. */
const MANUAL_PRACTICE_RATIONALE = "You chose this practice focus yourself.";

/**
 * Why an orchestration request could not be served, distinguished from
 * `RepositoryError` (a storage/RPC failure) so `/api/practice` can map each
 * case to the right status: `PROFILE_REQUIRED`/`INVALID_PRACTICE_REQUEST` are
 * 400s, `OPPORTUNITY_NOT_FOUND` is a 404.
 */
export type PracticeServiceErrorCode =
  | "PROFILE_REQUIRED"
  | "INVALID_PRACTICE_REQUEST"
  | "OPPORTUNITY_NOT_FOUND";

/** Wraps orchestration-level rejections in user-safe messages plus a stable code. */
export class PracticeServiceError extends Error {
  constructor(message: string, public readonly code: PracticeServiceErrorCode) {
    super(message);
    this.name = "PracticeServiceError";
  }
}

/**
 * The validated practice choices a user may supply themselves. Every field is
 * re-validated server-side by `startManualPractice` -- the browser never
 * supplies a rationale, a priority, or a recommendation object.
 */
export type ManualPracticeRequest = {
  format: PracticeFormat;
  primaryFocus: string;
  secondaryFocus?: string | null;
  estimatedMinutes?: number | null;
  successCriteria?: string[];
  primaryOpportunityId?: string | null;
};

/** A persisted plan and the session that was started from it, in one transaction each. */
export type StartedPractice = {
  plan: PracticePlan;
  session: InterviewSession;
};

/** The Practice view's read model: what to practise next, plus the caller's plan history. */
export type PracticeOverview = {
  recommendation: PracticeRecommendation;
  plans: PracticePlan[];
};

/**
 * Everything the deterministic selector and the blueprint generator need,
 * loaded once per request. Exported so other Career Brain read models that
 * need the same seven repository calls plus the same `calculateReadiness`
 * call -- currently `loadCareerDashboard` in `src/lib/career-dashboard.ts`
 * -- reuse `loadPracticeInputs` instead of maintaining a second copy of the
 * loading logic that could silently drift from this one.
 */
export type PracticeInputs = {
  profile: Profile | null;
  opportunities: Opportunity[];
  observations: CoachObservation[];
  stories: CareerStory[];
  sessions: InterviewSession[];
  /** The caller's most recent plans only -- `listPracticePlans` is always bounded. */
  plans: PracticePlan[];
  readiness: ReadinessModel;
};

/** The plan fields plus opportunity links a start request resolves to, before anything is persisted. */
type PracticeDraft = {
  format: PracticeFormat;
  primaryFocus: string;
  secondaryFocus: string | null;
  rationale: string;
  estimatedMinutes: number | null;
  successCriteria: string[];
  primaryOpportunityId: string | null;
  supportingOpportunityIds: string[];
};

function describeError(error: unknown): Record<string, unknown> {
  if (!(error instanceof Error)) return { error };
  return {
    name: error.name,
    message: error.message,
    code: "code" in error ? (error as { code?: unknown }).code : undefined,
  };
}

/**
 * The text stored in `practice_plans.generation_error` and shown to the user.
 * `RepositoryError` messages are user-safe by contract, so they are kept;
 * anything else (an AI SDK failure, a thrown provider payload) is replaced,
 * because such messages can carry internal detail.
 */
function userSafeGenerationFailure(error: unknown): string {
  return error instanceof RepositoryError
    ? error.message
    : "Could not prepare this practice session. Start it again to retry.";
}

/**
 * Loads the profile, opportunities, coach observations, career stories,
 * recent sessions, recent practice plans, and readiness evidence a request
 * needs, in one call, plus the readiness model computed from that evidence
 * (a separate seventh read, not derived from the other six). Every other
 * Career Brain read model that needs this same combination should call this
 * rather than re-issuing the seven repository calls itself -- see the note
 * on {@link PracticeInputs}.
 *
 * `now` is caller-supplied and threaded straight into `calculateReadiness`
 * as `asOf` -- this function never reads the clock itself, matching that
 * function's own determinism contract (see `src/lib/readiness.ts`).
 */
export async function loadPracticeInputs(supabase: SupabaseClient, userId: string, now: Date): Promise<PracticeInputs> {
  const [profile, opportunities, observations, stories, sessions, plans, evidence] = await Promise.all([
    getProfile(supabase, userId),
    listOpportunities(supabase, userId),
    listCoachObservations(supabase, userId),
    listCareerStories(supabase, userId),
    listRecentSessions(supabase, userId),
    listPracticePlans(supabase, userId),
    listReadinessEvidence(supabase, userId),
  ]);
  return {
    profile,
    opportunities,
    observations,
    stories,
    sessions,
    plans,
    readiness: calculateReadiness(evidence, now),
  };
}

function recommendFrom(inputs: PracticeInputs, now: Date): PracticeRecommendation {
  return recommendPractice({
    opportunities: inputs.opportunities,
    observations: inputs.observations,
    stories: inputs.stories,
    readiness: inputs.readiness,
    competencies: inputs.profile?.competencies ?? [],
    recentSessions: inputs.sessions,
    recentPlans: inputs.plans,
    now,
  });
}

/**
 * Recomputes the current baseline recommendation and returns it with the
 * caller's practice plans. Read-only: a recommendation becomes a persisted
 * `PracticePlan` only when the user starts it (design section 6), so this
 * never writes.
 *
 * A missing profile is not an error here -- the deterministic selector reads
 * opportunities, observations, stories, and progress, none of which require
 * one. Starting practice does require a profile; see `startRecommendedPractice`.
 */
export async function loadPracticeOverview(
  supabase: SupabaseClient,
  userId: string,
  now: Date,
): Promise<PracticeOverview> {
  const inputs = await loadPracticeInputs(supabase, userId, now);
  return { recommendation: recommendFrom(inputs, now), plans: inputs.plans };
}

/**
 * Starts the practice the server currently recommends.
 *
 * The recommendation is RECOMPUTED here from freshly loaded Career Brain
 * inputs -- callers never pass one in, so a browser cannot choose its own
 * plan focus, format, or opportunity links.
 *
 * Side effects, in order: creates one `ready` `PracticePlan`, links the
 * recommendation's primary/supporting opportunities, generates the delivery
 * artifact, and starts the session through the transactional RPC that flips
 * the plan to `started`. Throws `PracticeServiceError("PROFILE_REQUIRED")`
 * when no profile exists, and marks the plan `failed` before rethrowing if
 * anything fails before a session exists (see `startPractice`).
 */
export async function startRecommendedPractice(
  supabase: SupabaseClient,
  userId: string,
  now: Date,
): Promise<StartedPractice> {
  const inputs = await loadPracticeInputs(supabase, userId, now);
  const profile = requireProfile(inputs.profile);
  const recommendation = recommendFrom(inputs, now);
  return startPractice(supabase, userId, profile, inputs, {
    format: recommendation.format,
    primaryFocus: recommendation.primaryFocus,
    secondaryFocus: recommendation.secondaryFocus,
    rationale: recommendation.rationale,
    estimatedMinutes: recommendation.estimatedMinutes,
    successCriteria: recommendation.successCriteria,
    primaryOpportunityId: recommendation.primaryOpportunityId,
    supportingOpportunityIds: recommendation.supportingOpportunityIds,
  });
}

/**
 * Starts practice the user chose themselves, through the same plan creation,
 * delivery, and failure handling as `startRecommendedPractice`.
 *
 * Every field of `request` is validated here rather than at the route, so the
 * rules hold for any caller: the format must be a known `PracticeFormat`, the
 * focus must be non-empty, `estimatedMinutes` (when given) must be a whole
 * number of 1-180 minutes matching the database's check constraint, and
 * `primaryOpportunityId` (when given) must be an opportunity this user owns --
 * otherwise `PracticeServiceError` with `INVALID_PRACTICE_REQUEST` or
 * `OPPORTUNITY_NOT_FOUND`. Manual practice never links supporting
 * opportunities; only the one the user picked.
 */
export async function startManualPractice(
  supabase: SupabaseClient,
  userId: string,
  request: ManualPracticeRequest,
): Promise<StartedPractice> {
  const draft = validateManualRequest(request);
  // No `now` is supplied by the caller here (manual practice never computes
  // a recommendation, so the readiness model `loadPracticeInputs` loads
  // alongside everything else is otherwise unused) -- read the clock once,
  // right here at the top of the request, same as every `now`-needing
  // service function in this file gets it from ITS caller reading the clock
  // once (see `startRecommendedPractice`/`loadPracticeOverview` above).
  const inputs = await loadPracticeInputs(supabase, userId, new Date());
  const profile = requireProfile(inputs.profile);
  if (draft.primaryOpportunityId && !inputs.opportunities.some((item) => item.id === draft.primaryOpportunityId)) {
    throw new PracticeServiceError("That opportunity was not found.", "OPPORTUNITY_NOT_FOUND");
  }
  return startPractice(supabase, userId, profile, inputs, draft);
}

/**
 * Marks the practice plan behind a just-completed session `completed`.
 *
 * BEST-EFFORT BY DESIGN: the interview evidence is already durably saved by
 * the time this runs, and design section 7.5 requires that evidence to
 * survive plan bookkeeping failures. So a failure here is logged, converted
 * to a user-safe `warning`, and never thrown -- the caller still returns the
 * completed session. Returns `{ warning: null }` for sessions that were never
 * linked to a plan. Call it only after the session's completion has been
 * persisted.
 */
export async function completeLinkedPracticePlanBestEffort(
  supabase: SupabaseClient,
  userId: string,
  session: InterviewSession,
): Promise<{ warning: string | null }> {
  if (!session.practicePlanId) return { warning: null };
  try {
    await updatePracticePlan(supabase, userId, session.practicePlanId, {
      status: "completed",
      completedAt: session.completedAt ?? new Date().toISOString(),
    });
    return { warning: null };
  } catch (error) {
    console.error("[practice-service] practice plan completion failed", describeError(error));
    return { warning: "Your session was saved, but its practice plan could not be marked complete." };
  }
}

function requireProfile(profile: Profile | null): Profile {
  if (!profile) throw new PracticeServiceError("Create your profile first.", "PROFILE_REQUIRED");
  return profile;
}

function validateManualRequest(request: ManualPracticeRequest): PracticeDraft {
  if (!isPracticeFormat(request.format)) {
    throw new PracticeServiceError("Choose a supported practice format.", "INVALID_PRACTICE_REQUEST");
  }
  const primaryFocus = typeof request.primaryFocus === "string" ? request.primaryFocus.trim() : "";
  if (!primaryFocus) {
    throw new PracticeServiceError("Describe what you want to practise.", "INVALID_PRACTICE_REQUEST");
  }
  const estimatedMinutes = request.estimatedMinutes ?? null;
  if (estimatedMinutes !== null
    && (!Number.isInteger(estimatedMinutes) || estimatedMinutes < 1 || estimatedMinutes > MAX_ESTIMATED_MINUTES)) {
    throw new PracticeServiceError(
      `Choose a duration between 1 and ${MAX_ESTIMATED_MINUTES} minutes.`,
      "INVALID_PRACTICE_REQUEST",
    );
  }
  const primaryOpportunityId = request.primaryOpportunityId ?? null;
  if (primaryOpportunityId !== null && (typeof primaryOpportunityId !== "string" || !primaryOpportunityId.trim())) {
    throw new PracticeServiceError("Choose a valid opportunity, or none at all.", "INVALID_PRACTICE_REQUEST");
  }
  const secondaryFocus = typeof request.secondaryFocus === "string" ? request.secondaryFocus.trim() : "";
  const successCriteria = (Array.isArray(request.successCriteria) ? request.successCriteria : [])
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    format: request.format,
    primaryFocus,
    secondaryFocus: secondaryFocus || null,
    rationale: MANUAL_PRACTICE_RATIONALE,
    estimatedMinutes,
    successCriteria,
    primaryOpportunityId,
    supportingOpportunityIds: [],
  };
}

function opportunityLinks(draft: PracticeDraft): PracticePlanOpportunityLink[] {
  const links: PracticePlanOpportunityLink[] = draft.primaryOpportunityId
    ? [{ opportunityId: draft.primaryOpportunityId, relevance: "primary" }]
    : [];
  return links.concat(draft.supportingOpportunityIds
    .filter((id) => id !== draft.primaryOpportunityId)
    .map((opportunityId) => ({ opportunityId, relevance: "supporting" as const })));
}

/**
 * Creates the `ready` plan, links its opportunities, and starts the session.
 *
 * The plan is created BEFORE generation so a generation failure leaves a
 * durable, explainable `failed` plan rather than nothing. The plan is never
 * moved to `started` here: the transactional start RPCs own that transition,
 * which is what makes a plan startable exactly once.
 */
async function startPractice(
  supabase: SupabaseClient,
  userId: string,
  profile: Profile,
  inputs: PracticeInputs,
  draft: PracticeDraft,
): Promise<StartedPractice> {
  const created = await createPracticePlan(supabase, userId, {
    status: "ready",
    primaryFocus: draft.primaryFocus,
    secondaryFocus: draft.secondaryFocus,
    rationale: draft.rationale,
    format: draft.format,
    estimatedMinutes: draft.estimatedMinutes,
    successCriteria: draft.successCriteria,
  });

  let session: InterviewSession;
  let plan = created;
  try {
    const links = opportunityLinks(draft);
    if (links.length > 0) plan = await setPracticePlanOpportunities(supabase, userId, created.id, links);
    session = await deliverPractice(supabase, userId, profile, inputs, plan);
  } catch (error) {
    await markPlanFailed(supabase, userId, created.id, error);
    throw error;
  }

  // The start RPC flipped the plan to `started` in the same transaction that
  // created the session, so this refresh is a convenience, not the source of
  // truth -- a failed reload must never discard a session that already exists.
  try {
    const started = await getPracticePlan(supabase, userId, created.id);
    if (started) return { plan: started, session };
  } catch (error) {
    console.error("[practice-service] could not reload the started practice plan", describeError(error));
  }
  return { plan: { ...plan, status: "started" }, session };
}

/**
 * Generates the delivery artifact for `plan` and starts its session through
 * the matching transactional RPC. `hands_on` uses the deterministic exercise
 * builder instead of a generated blueprint (design section 7.4).
 *
 * The session context carries the plan's PRIMARY opportunity link: both start
 * RPCs validate the plan/opportunity relationship only when an opportunity id
 * is supplied, so passing null for a plan that has a primary link would
 * silently create a session with no role context.
 */
async function deliverPractice(
  supabase: SupabaseClient,
  userId: string,
  profile: Profile,
  inputs: PracticeInputs,
  plan: PracticePlan,
): Promise<InterviewSession> {
  const primaryOpportunityId = plan.opportunities.find((link) => link.relevance === "primary")?.opportunityId ?? null;
  const context: PracticeSessionContext = { practicePlanId: plan.id, opportunityId: primaryOpportunityId };

  if (plan.format === "hands_on") {
    return createHandsOnPracticeSession(supabase, userId, handsOnExercise(profile), context);
  }

  const supportingIds = new Set(plan.opportunities
    .filter((link) => link.relevance === "supporting")
    .map((link) => link.opportunityId));
  const blueprint = await generatePracticeBlueprint(profile, profile.evidence ?? [], plan, {
    primaryOpportunity: inputs.opportunities.find((item) => item.id === primaryOpportunityId) ?? null,
    supportingOpportunities: inputs.opportunities.filter((item) => supportingIds.has(item.id)),
    observations: inputs.observations,
    stories: inputs.stories,
  });
  return createSessionWithPracticeBlueprint(supabase, userId, blueprint, context);
}

/**
 * Records why a plan never produced a session, so the plan stays explainable
 * instead of sitting in `ready` forever.
 *
 * CONDITIONAL ON `ready`: a start call can fail AFTER its RPC transaction
 * committed -- a transport or response-read failure on the way back -- and in
 * that case a session exists and the plan is legitimately `started`. Writing
 * `failed` unconditionally would orphan that live session behind a `failed`
 * plan, so the update is scoped to plans still in `ready`. When the RPC won
 * the race, no row matches and the resulting `RepositoryError` is swallowed
 * here along with any other bookkeeping failure: the original error is what
 * the caller must see.
 */
async function markPlanFailed(
  supabase: SupabaseClient,
  userId: string,
  planId: string,
  error: unknown,
): Promise<void> {
  try {
    await updatePracticePlan(
      supabase,
      userId,
      planId,
      { status: "failed", generationError: userSafeGenerationFailure(error) },
      { expectedStatus: "ready" },
    );
  } catch (bookkeepingError) {
    console.error("[practice-service] could not mark the practice plan failed", describeError(bookkeepingError));
  }
}

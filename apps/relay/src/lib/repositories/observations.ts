import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import type {
  AttachObservationEvidenceOptions,
  CoachObservation,
  CoachObservationReview,
  CoachObservationReviewState,
  CoachObservationTrend,
  CoachObservationType,
  CreateCoachObservationInput,
  ObservationEvidence,
  ObservationEvidenceRole,
  ObservationEvidenceSource,
} from "@/lib/types";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;

const coachObservationTypes: CoachObservationType[] = [
  "strength",
  "weakness",
  "answer_habit",
  "knowledge_gap",
  "story_gap",
  "story_strength",
  "delivery_pattern",
  "other",
];

const coachObservationTrends: CoachObservationTrend[] = ["unresolved", "improving", "stable", "worsening"];

const coachObservationReviewStates: CoachObservationReviewState[] = [
  "unreviewed",
  "confirmed",
  "corrected",
  "dismissed",
];

const observationEvidenceRoles: ObservationEvidenceRole[] = ["supporting", "contradicting", "context"];

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function nullableStringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberValue(value: unknown): number {
  return value === null || value === undefined ? 0 : Number(value);
}

function coachObservationType(value: unknown): CoachObservationType {
  return coachObservationTypes.includes(value as CoachObservationType) ? value as CoachObservationType : "other";
}

function coachObservationTrend(value: unknown): CoachObservationTrend {
  return coachObservationTrends.includes(value as CoachObservationTrend) ? value as CoachObservationTrend : "unresolved";
}

function coachObservationReviewState(value: unknown): CoachObservationReviewState {
  return coachObservationReviewStates.includes(value as CoachObservationReviewState)
    ? value as CoachObservationReviewState
    : "unreviewed";
}

function observationEvidenceRole(value: unknown): ObservationEvidenceRole {
  return observationEvidenceRoles.includes(value as ObservationEvidenceRole)
    ? value as ObservationEvidenceRole
    : "supporting";
}

function mapCoachObservation(row: Row): CoachObservation {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    observationType: coachObservationType(row.observation_type),
    claim: stringValue(row.claim),
    confidence: numberValue(row.confidence),
    importance: numberValue(row.importance),
    trend: coachObservationTrend(row.trend),
    reviewState: coachObservationReviewState(row.review_state),
    userCorrection: nullableStringValue(row.user_correction),
    firstSeenAt: nullableStringValue(row.first_seen_at),
    lastSeenAt: nullableStringValue(row.last_seen_at),
    confirmedAt: nullableStringValue(row.confirmed_at),
    correctedAt: nullableStringValue(row.corrected_at),
    dismissedAt: nullableStringValue(row.dismissed_at),
    createdAt: stringValue(row.created_at),
    updatedAt: stringValue(row.updated_at),
  };
}

function mapObservationEvidence(row: Row): ObservationEvidence {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    observationId: stringValue(row.observation_id),
    profileEvidenceId: nullableStringValue(row.profile_evidence_id),
    questionEvaluationId: nullableStringValue(row.question_evaluation_id),
    careerStoryId: nullableStringValue(row.career_story_id),
    opportunityEventId: nullableStringValue(row.opportunity_event_id),
    evidenceRole: observationEvidenceRole(row.evidence_role),
    weight: numberValue(row.weight),
    reason: nullableStringValue(row.reason),
    createdAt: stringValue(row.created_at),
  };
}

/**
 * Converts the discriminated evidence-source union to nullable database
 * columns. This is the one place that happens, so `observation_evidence`
 * inserts can never end up with zero or two sources set from application
 * code -- the database's `num_nonnulls(...) = 1` check is the final guard.
 */
function observationEvidenceColumns(source: ObservationEvidenceSource): Row {
  const columns: Row = {
    profile_evidence_id: null,
    question_evaluation_id: null,
    career_story_id: null,
    opportunity_event_id: null,
  };
  if (source.kind === "profile_evidence") columns.profile_evidence_id = source.profileEvidenceId;
  else if (source.kind === "question_evaluation") columns.question_evaluation_id = source.questionEvaluationId;
  else if (source.kind === "career_story") columns.career_story_id = source.careerStoryId;
  else if (source.kind === "opportunity_event") columns.opportunity_event_id = source.opportunityEventId;
  else {
    // Exhaustiveness guard: if `ObservationEvidenceSource` (spec §8.2
    // anticipates further evidence-source kinds) ever grows a fifth variant
    // without a branch here, this assignment fails to compile instead of
    // silently writing `opportunity_event_id` for the wrong source.
    const exhaustive: never = source;
    throw new RepositoryError(`Unknown observation evidence source kind: ${JSON.stringify(exhaustive)}`, "INVALID_EVIDENCE_SOURCE");
  }
  return columns;
}

/**
 * Converts a user's review decision to the row patch that records it.
 * Only one of `confirmed_at`/`corrected_at`/`dismissed_at` is ever set at a
 * time -- moving to a new review state clears the other two so the
 * timestamps always describe the current `review_state`, never a stale
 * prior one. The original `claim` column is never included in this patch:
 * a correction is recorded separately in `user_correction`, and `claim`
 * stays untouched for every review state, including `corrected`.
 *
 * INTENTIONAL DATA LOSS: the `confirmed` and `dismissed` branches also set
 * `user_correction: null`, clearing the correction TEXT itself, not just
 * `corrected_at`. This is deliberate, not a bug -- a correction is scoped
 * to the `corrected` review state, so once the user moves an observation to
 * `confirmed` or `dismissed`, the prior correction text is discarded and
 * unrecoverable through this table. Retaining it alongside a `confirmed`/
 * `dismissed` state would leave correction text attached with nothing
 * indicating whether it still applies. Durable correction history (e.g. for
 * a user who corrects, then reconsiders) belongs in an append-only review
 * log, which spec §8.1 leaves to Release 3's reconciliation rules.
 */
function reviewColumns(review: CoachObservationReview): Row {
  const now = new Date().toISOString();
  if (review.state === "confirmed") {
    return {
      review_state: "confirmed",
      confirmed_at: now,
      corrected_at: null,
      dismissed_at: null,
      user_correction: null,
    };
  }
  if (review.state === "corrected") {
    return {
      review_state: "corrected",
      corrected_at: now,
      user_correction: review.correction,
      confirmed_at: null,
      dismissed_at: null,
    };
  }
  return {
    review_state: "dismissed",
    dismissed_at: now,
    confirmed_at: null,
    corrected_at: null,
    user_correction: null,
  };
}

/**
 * Creates a coach observation owned by `userId`. Release 1 never generates,
 * infers, or reconciles observations automatically -- this persists
 * whatever `input` supplies, or the database defaults for the fields it
 * omits.
 */
export async function createCoachObservation(
  supabase: SupabaseClient,
  userId: string,
  input: CreateCoachObservationInput,
): Promise<CoachObservation> {
  const row: Row = {
    user_id: userId,
    observation_type: input.observationType,
    claim: input.claim,
    first_seen_at: input.firstSeenAt ?? null,
    last_seen_at: input.lastSeenAt ?? null,
  };
  if (input.confidence !== undefined) row.confidence = input.confidence;
  if (input.importance !== undefined) row.importance = input.importance;
  if (input.trend !== undefined) row.trend = input.trend;

  const { data, error } = await supabase
    .from("coach_observations")
    .insert(row)
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not create the coach observation.", error?.code ?? "NO_OWNED_ROW");
  return mapCoachObservation(data as Row);
}

/** Loads one coach observation owned by `userId`, or null if it does not exist/isn't owned by them. */
export async function getCoachObservation(
  supabase: SupabaseClient,
  userId: string,
  observationId: string,
): Promise<CoachObservation | null> {
  const { data, error } = await supabase
    .from("coach_observations")
    .select("*")
    .eq("id", observationId)
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw new RepositoryError("Could not load the coach observation.", error.code);
  return data ? mapCoachObservation(data as Row) : null;
}

/** Lists all coach observations owned by `userId`, most recently updated first. */
export async function listCoachObservations(supabase: SupabaseClient, userId: string): Promise<CoachObservation[]> {
  const { data, error } = await supabase
    .from("coach_observations")
    .select("*")
    .eq("user_id", userId)
    .order("updated_at", { ascending: false });
  if (error) throw new RepositoryError("Could not load your coach observations.", error.code);
  return ((data ?? []) as Row[]).map(mapCoachObservation);
}

/**
 * Records the user's review of an owned coach observation. The original
 * `claim` is never modified by this function; see `reviewColumns` for the
 * exact timestamp/clearing rules per review state.
 */
export async function reviewCoachObservation(
  supabase: SupabaseClient,
  userId: string,
  observationId: string,
  review: CoachObservationReview,
): Promise<CoachObservation> {
  const patch: Row = { ...reviewColumns(review), updated_at: new Date().toISOString() };

  const { data, error } = await supabase
    .from("coach_observations")
    .update(patch)
    .eq("id", observationId)
    .eq("user_id", userId)
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not review the coach observation.", error?.code ?? "NO_OWNED_ROW");
  return mapCoachObservation(data as Row);
}

/**
 * Attaches one typed provenance link to a coach observation. `source` must
 * carry exactly one of the four evidence IDs by construction; the
 * database's `num_nonnulls(...) = 1` check enforces the same invariant
 * against any future caller that bypasses this function. An inactive
 * `profile_evidence` row remains a valid source -- inactive means
 * historical, not invalid -- so this never filters on `is_active`.
 */
export async function attachObservationEvidence(
  supabase: SupabaseClient,
  userId: string,
  observationId: string,
  source: ObservationEvidenceSource,
  options?: AttachObservationEvidenceOptions,
): Promise<ObservationEvidence> {
  const row: Row = {
    user_id: userId,
    observation_id: observationId,
    evidence_role: options?.role ?? "supporting",
    reason: options?.reason ?? null,
    ...observationEvidenceColumns(source),
  };
  if (options?.weight !== undefined) row.weight = options.weight;

  const { data, error } = await supabase
    .from("observation_evidence")
    .insert(row)
    .select("*")
    .maybeSingle();
  if (error || !data) {
    throw new RepositoryError("Could not attach evidence to the coach observation.", error?.code ?? "NO_OWNED_ROW");
  }
  return mapObservationEvidence(data as Row);
}

/** Lists a coach observation's provenance links, most recently attached first. */
export async function listObservationEvidence(
  supabase: SupabaseClient,
  userId: string,
  observationId: string,
): Promise<ObservationEvidence[]> {
  const { data, error } = await supabase
    .from("observation_evidence")
    .select("*")
    .eq("user_id", userId)
    .eq("observation_id", observationId)
    .order("created_at", { ascending: false });
  if (error) throw new RepositoryError("Could not load the coach observation's evidence.", error.code);
  return ((data ?? []) as Row[]).map(mapObservationEvidence);
}

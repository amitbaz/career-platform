import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import type {
  CreatePracticePlanInput,
  PracticeFormat,
  PracticePlan,
  PracticePlanOpportunity,
  PracticePlanOpportunityLink,
  PracticePlanOpportunityRelevance,
  PracticePlanStatus,
} from "@/lib/types";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;

/** How many plans `listPracticePlans` returns when the caller does not choose. */
const DEFAULT_PRACTICE_PLAN_LIMIT = 20;

const practicePlanStatuses: PracticePlanStatus[] = [
  "draft", "ready", "started", "completed", "cancelled", "failed",
];

const practiceFormats: PracticeFormat[] = [
  "targeted_drill",
  "story_work",
  "self_presentation",
  "behavioral",
  "technical_communication",
  "role_prep",
  "full_simulation",
  "hands_on",
];

const practicePlanOpportunityRelevances: PracticePlanOpportunityRelevance[] = ["primary", "supporting"];

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function nullableStringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function jsonArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function practicePlanStatus(value: unknown): PracticePlanStatus {
  return practicePlanStatuses.includes(value as PracticePlanStatus) ? value as PracticePlanStatus : "draft";
}

/**
 * Whether `value` is one of the practice formats the `practice_plans.format`
 * check constraint accepts. Exported so callers that accept a format from
 * outside the server -- `startManualPractice` in `src/lib/practice-service.ts`
 * -- validate against this single list instead of duplicating it and drifting
 * from the database constraint.
 */
export function isPracticeFormat(value: unknown): value is PracticeFormat {
  return practiceFormats.includes(value as PracticeFormat);
}

function practiceFormat(value: unknown): PracticeFormat {
  return isPracticeFormat(value) ? value : "targeted_drill";
}

function practicePlanOpportunityRelevance(value: unknown): PracticePlanOpportunityRelevance {
  return practicePlanOpportunityRelevances.includes(value as PracticePlanOpportunityRelevance)
    ? value as PracticePlanOpportunityRelevance
    : "supporting";
}

function mapPracticePlanOpportunity(row: Row): PracticePlanOpportunity {
  return {
    userId: stringValue(row.user_id),
    practicePlanId: stringValue(row.practice_plan_id),
    opportunityId: stringValue(row.opportunity_id),
    relevance: practicePlanOpportunityRelevance(row.relevance),
    createdAt: stringValue(row.created_at),
  };
}

function mapPracticePlan(row: Row, opportunities: PracticePlanOpportunity[]): PracticePlan {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    status: practicePlanStatus(row.status),
    primaryFocus: stringValue(row.primary_focus),
    secondaryFocus: nullableStringValue(row.secondary_focus),
    rationale: stringValue(row.rationale),
    format: practiceFormat(row.format),
    estimatedMinutes: row.estimated_minutes === null || row.estimated_minutes === undefined
      ? null
      : Number(row.estimated_minutes),
    successCriteria: jsonArray(row.success_criteria),
    priorityScore: row.priority_score === null || row.priority_score === undefined ? null : Number(row.priority_score),
    priorityFactors: jsonRecord(row.priority_factors),
    generationError: nullableStringValue(row.generation_error),
    completedAt: nullableStringValue(row.completed_at),
    createdAt: stringValue(row.created_at),
    updatedAt: stringValue(row.updated_at),
    opportunities,
  };
}

/**
 * Loads the opportunities currently linked to an owned practice plan, in
 * the order they were linked. Used to hydrate a `PracticePlan` so Release 2
 * callers do not need a separate table-specific query to know what a plan
 * serves.
 */
async function loadPracticePlanOpportunities(
  supabase: SupabaseClient,
  userId: string,
  planId: string,
): Promise<PracticePlanOpportunity[]> {
  const { data, error } = await supabase
    .from("practice_plan_opportunities")
    .select("*")
    .eq("user_id", userId)
    .eq("practice_plan_id", planId)
    .order("created_at", { ascending: true });
  if (error) throw new RepositoryError("Could not load the practice plan's linked opportunities.", error.code);
  return ((data ?? []) as Row[]).map(mapPracticePlanOpportunity);
}

/**
 * Creates a practice plan owned by `userId`. Release 1 never generates a
 * plan or computes `priorityScore`/`priorityFactors` automatically -- this
 * persists whatever `input` supplies, or the database defaults for the
 * fields it omits (status `"draft"`, `rationale` `""`, `successCriteria`
 * `[]`, `priorityFactors` `{}`). A newly created plan always starts with no
 * linked opportunities; use `setPracticePlanOpportunities` to attach them.
 */
export async function createPracticePlan(
  supabase: SupabaseClient,
  userId: string,
  input: CreatePracticePlanInput,
): Promise<PracticePlan> {
  const row: Row = {
    user_id: userId,
    primary_focus: input.primaryFocus,
    secondary_focus: input.secondaryFocus ?? null,
    format: input.format,
    estimated_minutes: input.estimatedMinutes ?? null,
    generation_error: input.generationError ?? null,
    completed_at: input.completedAt ?? null,
  };
  if (input.status !== undefined) row.status = input.status;
  if (input.rationale !== undefined) row.rationale = input.rationale;
  if (input.successCriteria !== undefined) row.success_criteria = input.successCriteria;
  if (input.priorityScore !== undefined) row.priority_score = input.priorityScore;
  if (input.priorityFactors !== undefined) row.priority_factors = input.priorityFactors;

  const { data, error } = await supabase
    .from("practice_plans")
    .insert(row)
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not create the practice plan.", error?.code ?? "NO_OWNED_ROW");
  return mapPracticePlan(data as Row, []);
}

/**
 * Loads one practice plan owned by `userId`, hydrated with its linked
 * opportunities, or null if it does not exist/isn't owned by them.
 */
export async function getPracticePlan(
  supabase: SupabaseClient,
  userId: string,
  planId: string,
): Promise<PracticePlan | null> {
  const { data, error } = await supabase
    .from("practice_plans")
    .select("*")
    .eq("id", planId)
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw new RepositoryError("Could not load the practice plan.", error.code);
  if (!data) return null;
  const opportunities = await loadPracticePlanOpportunities(supabase, userId, planId);
  return mapPracticePlan(data as Row, opportunities);
}

/**
 * Lists the practice plans owned by `userId`, most recently updated first,
 * each hydrated with its linked opportunities.
 *
 * ALWAYS BOUNDED: each returned plan costs one extra query for its
 * opportunity links, so this returns at most `options.limit` plans
 * (`DEFAULT_PRACTICE_PLAN_LIMIT` when omitted) rather than every plan a
 * long-lived account has accumulated. Mirrors `listRecentSessions` in
 * `src/lib/repositories/interviews.ts`. Callers that need older plans must
 * ask for them explicitly.
 */
export async function listPracticePlans(
  supabase: SupabaseClient,
  userId: string,
  options: { limit?: number } = {},
): Promise<PracticePlan[]> {
  const { data, error } = await supabase
    .from("practice_plans")
    .select("*")
    .eq("user_id", userId)
    .order("updated_at", { ascending: false })
    .limit(options.limit ?? DEFAULT_PRACTICE_PLAN_LIMIT);
  if (error) throw new RepositoryError("Could not load your practice plans.", error.code);
  const rows = (data ?? []) as Row[];
  return Promise.all(rows.map(async (row) => {
    const opportunities = await loadPracticePlanOpportunities(supabase, userId, stringValue(row.id));
    return mapPracticePlan(row, opportunities);
  }));
}

/**
 * Updates the provided fields of an owned practice plan. Only fields
 * present on `input` are patched; omitted fields are left untouched. This
 * never touches the plan's opportunity links -- use
 * `setPracticePlanOpportunities` for that.
 *
 * `options.expectedStatus` makes the write CONDITIONAL: the row is patched
 * only while it still holds that status, so a compensating update can never
 * overwrite a status something else already advanced. That matters because
 * the planned-practice start RPCs move a plan `ready` -> `started` inside the
 * transaction that creates its session: an unguarded "mark it failed" written
 * after that commit would leave a `failed` plan with a live session pointing
 * at it (see `markPlanFailed` in `src/lib/practice-service.ts`). When the
 * status no longer matches, no row is returned and this throws
 * `RepositoryError` with code `NO_OWNED_ROW`, exactly as for a plan the
 * caller does not own.
 */
export async function updatePracticePlan(
  supabase: SupabaseClient,
  userId: string,
  planId: string,
  input: Partial<CreatePracticePlanInput>,
  options: { expectedStatus?: PracticePlanStatus } = {},
): Promise<PracticePlan> {
  const patch: Row = { updated_at: new Date().toISOString() };
  if (input.status !== undefined) patch.status = input.status;
  if (input.primaryFocus !== undefined) patch.primary_focus = input.primaryFocus;
  if (input.secondaryFocus !== undefined) patch.secondary_focus = input.secondaryFocus;
  if (input.rationale !== undefined) patch.rationale = input.rationale;
  if (input.format !== undefined) patch.format = input.format;
  if (input.estimatedMinutes !== undefined) patch.estimated_minutes = input.estimatedMinutes;
  if (input.successCriteria !== undefined) patch.success_criteria = input.successCriteria;
  if (input.priorityScore !== undefined) patch.priority_score = input.priorityScore;
  if (input.priorityFactors !== undefined) patch.priority_factors = input.priorityFactors;
  if (input.generationError !== undefined) patch.generation_error = input.generationError;
  if (input.completedAt !== undefined) patch.completed_at = input.completedAt;

  let query = supabase
    .from("practice_plans")
    .update(patch)
    .eq("id", planId)
    .eq("user_id", userId);
  if (options.expectedStatus !== undefined) query = query.eq("status", options.expectedStatus);
  const { data, error } = await query.select("*").maybeSingle();
  if (error || !data) throw new RepositoryError("Could not update the practice plan.", error?.code ?? "NO_OWNED_ROW");
  const opportunities = await loadPracticePlanOpportunities(supabase, userId, planId);
  return mapPracticePlan(data as Row, opportunities);
}

/**
 * Replaces the full set of opportunities an owned practice plan serves.
 * `practice_plan_opportunities` is deliberately REPLACEABLE, not
 * append-only: existing links are deleted and the requested set is
 * inserted in their place.
 *
 * At most one link may be `"primary"`. This is enforced twice, and both
 * layers are intentional: the database's partial unique index
 * (`practice_plan_one_primary_opportunity_idx`) is the actual guarantee --
 * it also protects against races and any future caller that bypasses this
 * function -- while this upfront check gives a typed `RepositoryError`
 * with code `INVALID_PLAN_CONTEXT` instead of a raw constraint-violation
 * error, and fails before anything is written.
 *
 * Existing links are deleted scoped to BOTH `user_id` and `practice_plan_id`
 * -- never by plan id alone -- so this can never delete another user's
 * rows, or another plan's rows, as a side effect.
 *
 * NOT ATOMIC: the delete and the insert are two separate round trips, with
 * no wrapping transaction or compensating rollback. If the insert fails
 * after the delete has already succeeded -- e.g. one `opportunityId` in
 * `links` fails the `(opportunity_id, user_id)` composite foreign key
 * because it belongs to another user or no longer exists -- this throws
 * `RepositoryError`, but the plan is left with zero opportunity links; the
 * previously-valid links are not restored. Callers must validate every
 * `opportunityId` (same user, still existing) before calling, rather than
 * relying on this function to roll back a partial failure.
 *
 * SESSION HAZARD -- NOT RE-VALIDATED: `linkSessionCareerContext` (see
 * `src/lib/repositories/interviews.ts`) validates, at link time, that a
 * session's opportunity is one the plan serves and, if the plan has a
 * primary opportunity, that it matches that primary (design doc section
 * 10). This function replaces a plan's link set with no awareness of
 * sessions already pointing at it, and does not re-run that check. A
 * session linked while its link set was valid can therefore be left
 * holding `(practice_plan_id, opportunity_id)` where the opportunity is no
 * longer associated with the plan, or where a different opportunity is now
 * primary -- exactly the state `linkSessionCareerContext` refuses to
 * create at link time. Nothing else re-validates existing sessions when
 * this function runs, and the database does not catch it either
 * (deliberately, per spec section 10).
 */
export async function setPracticePlanOpportunities(
  supabase: SupabaseClient,
  userId: string,
  planId: string,
  links: PracticePlanOpportunityLink[],
): Promise<PracticePlan> {
  if (links.filter((link) => link.relevance === "primary").length > 1) {
    throw new RepositoryError("A practice plan can have only one primary opportunity.", "INVALID_PLAN_CONTEXT");
  }

  const { error: deleteError } = await supabase
    .from("practice_plan_opportunities")
    .delete()
    .eq("user_id", userId)
    .eq("practice_plan_id", planId);
  if (deleteError) {
    throw new RepositoryError("Could not replace the practice plan's linked opportunities.", deleteError.code);
  }

  let opportunities: PracticePlanOpportunity[] = [];
  if (links.length > 0) {
    const rows: Row[] = links.map((link) => ({
      user_id: userId,
      practice_plan_id: planId,
      opportunity_id: link.opportunityId,
      relevance: link.relevance ?? "supporting",
    }));
    const { data, error: insertError } = await supabase
      .from("practice_plan_opportunities")
      .insert(rows)
      .select("*");
    if (insertError) {
      throw new RepositoryError("Could not save the practice plan's linked opportunities.", insertError.code);
    }
    opportunities = ((data ?? []) as Row[]).map(mapPracticePlanOpportunity);
  }

  const { data: planRow, error: planError } = await supabase
    .from("practice_plans")
    .select("*")
    .eq("id", planId)
    .eq("user_id", userId)
    .maybeSingle();
  if (planError || !planRow) {
    throw new RepositoryError(
      "Could not reload the practice plan after updating its links.",
      planError?.code ?? "NO_OWNED_ROW",
    );
  }
  return mapPracticePlan(planRow as Row, opportunities);
}

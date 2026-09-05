import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import type {
  CareerStory,
  CareerStoryEvidence,
  CareerStoryEvidenceSource,
  CareerStoryReviewState,
  CreateCareerStoryInput,
  UpdateCareerStoryInput,
} from "@/lib/types";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;

const careerStoryReviewStates: CareerStoryReviewState[] = ["draft", "confirmed", "retired"];

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function nullableStringValue(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function careerStoryReviewState(value: unknown): CareerStoryReviewState {
  return careerStoryReviewStates.includes(value as CareerStoryReviewState) ? value as CareerStoryReviewState : "draft";
}

function mapCareerStory(row: Row): CareerStory {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    title: stringValue(row.title),
    situation: nullableStringValue(row.situation),
    responsibility: nullableStringValue(row.responsibility),
    problem: nullableStringValue(row.problem),
    actions: nullableStringValue(row.actions),
    alternatives: nullableStringValue(row.alternatives),
    tradeoffs: nullableStringValue(row.tradeoffs),
    ownership: nullableStringValue(row.ownership),
    outcome: nullableStringValue(row.outcome),
    lessons: nullableStringValue(row.lessons),
    tags: stringArray(row.tags),
    completeness: row.completeness === null || row.completeness === undefined ? 0 : Number(row.completeness),
    reviewState: careerStoryReviewState(row.review_state),
    confirmedAt: nullableStringValue(row.confirmed_at),
    createdAt: stringValue(row.created_at),
    updatedAt: stringValue(row.updated_at),
  };
}

function mapCareerStoryEvidence(row: Row): CareerStoryEvidence {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    careerStoryId: stringValue(row.career_story_id),
    profileEvidenceId: nullableStringValue(row.profile_evidence_id),
    interviewQuestionId: nullableStringValue(row.interview_question_id),
    note: nullableStringValue(row.note),
    createdAt: stringValue(row.created_at),
  };
}

/**
 * Converts the discriminated evidence-source union to nullable database
 * columns. This is the one place that happens, so `career_story_evidence`
 * inserts can never end up with zero or two sources set from application
 * code -- the database's `num_nonnulls(...) = 1` check is the final guard.
 */
function storyEvidenceColumns(source: CareerStoryEvidenceSource): Row {
  return source.kind === "profile_evidence"
    ? { profile_evidence_id: source.profileEvidenceId, interview_question_id: null }
    : { profile_evidence_id: null, interview_question_id: source.interviewQuestionId };
}

/**
 * Creates a career story owned by `userId`. Release 1 never computes
 * `completeness` or `reviewState` automatically -- it persists whatever the
 * caller supplied, or the database defaults (`0` and `"draft"`).
 */
export async function createCareerStory(
  supabase: SupabaseClient,
  userId: string,
  input: CreateCareerStoryInput,
): Promise<CareerStory> {
  const row: Row = {
    user_id: userId,
    title: input.title,
    situation: input.situation ?? null,
    responsibility: input.responsibility ?? null,
    problem: input.problem ?? null,
    actions: input.actions ?? null,
    alternatives: input.alternatives ?? null,
    tradeoffs: input.tradeoffs ?? null,
    ownership: input.ownership ?? null,
    outcome: input.outcome ?? null,
    lessons: input.lessons ?? null,
    tags: input.tags ?? [],
  };
  if (input.completeness !== undefined) row.completeness = input.completeness;
  if (input.reviewState !== undefined) row.review_state = input.reviewState;

  const { data, error } = await supabase
    .from("career_stories")
    .insert(row)
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not create the career story.", error?.code ?? "NO_OWNED_ROW");
  return mapCareerStory(data as Row);
}

/** Loads one career story owned by `userId`, or null if it does not exist/isn't owned by them. */
export async function getCareerStory(
  supabase: SupabaseClient,
  userId: string,
  storyId: string,
): Promise<CareerStory | null> {
  const { data, error } = await supabase
    .from("career_stories")
    .select("*")
    .eq("id", storyId)
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw new RepositoryError("Could not load the career story.", error.code);
  return data ? mapCareerStory(data as Row) : null;
}

/** Lists all career stories owned by `userId`, most recently updated first. */
export async function listCareerStories(supabase: SupabaseClient, userId: string): Promise<CareerStory[]> {
  const { data, error } = await supabase
    .from("career_stories")
    .select("*")
    .eq("user_id", userId)
    .order("updated_at", { ascending: false });
  if (error) throw new RepositoryError("Could not load your career stories.", error.code);
  return ((data ?? []) as Row[]).map(mapCareerStory);
}

/**
 * Updates the provided fields of an owned career story. Only fields present
 * on `input` are patched; omitted fields are left untouched. This includes
 * `completeness` and `reviewState` -- Release 1 persists whatever the
 * caller supplies rather than deriving either automatically.
 */
export async function updateCareerStory(
  supabase: SupabaseClient,
  userId: string,
  storyId: string,
  input: UpdateCareerStoryInput,
): Promise<CareerStory> {
  const patch: Row = { updated_at: new Date().toISOString() };
  if (input.title !== undefined) patch.title = input.title;
  if (input.situation !== undefined) patch.situation = input.situation;
  if (input.responsibility !== undefined) patch.responsibility = input.responsibility;
  if (input.problem !== undefined) patch.problem = input.problem;
  if (input.actions !== undefined) patch.actions = input.actions;
  if (input.alternatives !== undefined) patch.alternatives = input.alternatives;
  if (input.tradeoffs !== undefined) patch.tradeoffs = input.tradeoffs;
  if (input.ownership !== undefined) patch.ownership = input.ownership;
  if (input.outcome !== undefined) patch.outcome = input.outcome;
  if (input.lessons !== undefined) patch.lessons = input.lessons;
  if (input.tags !== undefined) patch.tags = input.tags;
  if (input.completeness !== undefined) patch.completeness = input.completeness;
  if (input.reviewState !== undefined) patch.review_state = input.reviewState;
  if (input.confirmedAt !== undefined) patch.confirmed_at = input.confirmedAt;

  const { data, error } = await supabase
    .from("career_stories")
    .update(patch)
    .eq("id", storyId)
    .eq("user_id", userId)
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not update the career story.", error?.code ?? "NO_OWNED_ROW");
  return mapCareerStory(data as Row);
}

/**
 * Attaches one typed provenance link to a career story. `source` must carry
 * exactly one of `profileEvidenceId`/`interviewQuestionId` by construction;
 * the database's `num_nonnulls(...) = 1` check enforces the same invariant
 * against any future caller that bypasses this function.
 */
export async function attachCareerStoryEvidence(
  supabase: SupabaseClient,
  userId: string,
  storyId: string,
  source: CareerStoryEvidenceSource,
  note?: string | null,
): Promise<CareerStoryEvidence> {
  const row: Row = {
    user_id: userId,
    career_story_id: storyId,
    note: note ?? null,
    ...storyEvidenceColumns(source),
  };

  const { data, error } = await supabase
    .from("career_story_evidence")
    .insert(row)
    .select("*")
    .maybeSingle();
  if (error || !data) {
    throw new RepositoryError("Could not attach evidence to the career story.", error?.code ?? "NO_OWNED_ROW");
  }
  return mapCareerStoryEvidence(data as Row);
}

/** Lists a career story's provenance links, most recently attached first. */
export async function listCareerStoryEvidence(
  supabase: SupabaseClient,
  userId: string,
  storyId: string,
): Promise<CareerStoryEvidence[]> {
  const { data, error } = await supabase
    .from("career_story_evidence")
    .select("*")
    .eq("user_id", userId)
    .eq("career_story_id", storyId)
    .order("created_at", { ascending: false });
  if (error) throw new RepositoryError("Could not load the career story's evidence.", error.code);
  return ((data ?? []) as Row[]).map(mapCareerStoryEvidence);
}

import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";
import { careerStoryCompleteness } from "@/lib/career-story";
import {
  attachCareerStoryEvidence,
  createCareerStory,
  getCareerStory,
  listCareerStories,
  listCareerStoryEvidence,
  updateCareerStory,
} from "@/lib/repositories/stories";
import { RepositoryError } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";
import type {
  CareerStory,
  CareerStoryDraftFields,
  CareerStoryEvidence,
  CareerStorySummary,
  CreateCareerStoryInput,
  UpdateCareerStoryInput,
} from "@/lib/types";

export const runtime = "nodejs";

const DRAFT_FIELDS: (keyof CareerStoryDraftFields)[] = [
  "situation",
  "responsibility",
  "problem",
  "actions",
  "alternatives",
  "tradeoffs",
  "ownership",
  "outcome",
  "lessons",
];

/** Loads `story`'s evidence count and returns it enriched for display, per `CareerStorySummary`. */
async function summarizeStory(supabase: SupabaseClient, userId: string, story: CareerStory): Promise<CareerStorySummary> {
  const evidence = await listCareerStoryEvidence(supabase, userId, story.id);
  return { ...story, evidenceCount: evidence.length };
}

/**
 * Returns the authenticated caller's career stories (the Stories view's
 * list; see design section 8.1), each enriched with its provenance count,
 * most recently updated first. Read-only.
 */
export async function GET() {
  try {
    const { supabase, user } = await requireUser();
    const stories = await listCareerStories(supabase, user.id);
    const summaries = await Promise.all(stories.map((story) => summarizeStory(supabase, user.id, story)));
    return NextResponse.json({ stories: summaries });
  } catch (error) {
    return errorResponse(error);
  }
}

/**
 * Applies one story-lifecycle action for the authenticated caller.
 *
 * Actions: `create`, `update`, `confirm`, `retire`, `attach_profile_evidence`.
 * Every field is parsed and validated HERE from the untrusted request body
 * -- raw JSON is never cast directly to a repository input type -- matching
 * `src/app/api/opportunities/route.ts`.
 *
 * `create` and `update` always compute `completeness` themselves from the
 * resulting draft fields via `careerStoryCompleteness`; any browser-supplied
 * `completeness` is ignored. `update` never accepts a `reviewState` --
 * review-state transitions only ever happen through `confirm`/`retire`, so
 * this route reads the CURRENT story first (also the ownership check that
 * turns a missing/foreign story into a 404) to recompute completeness
 * against fields the caller left untouched, not just the ones in the patch.
 *
 * `confirm` sets `reviewState: "confirmed"` plus a server-generated
 * `confirmedAt` -- never a client-supplied one. `retire` only ever sets
 * `reviewState: "retired"`; it is a state change, not a delete, so the row
 * and its attached evidence are left exactly as they were.
 *
 * Responses: `{ story }` for `create`/`update`/`confirm`/`retire`;
 * `{ evidence }` for `attach_profile_evidence`, since that action creates a
 * provenance link rather than changing the story row.
 */
export async function POST(request: Request) {
  let supabase: SupabaseClient;
  let userId: string;
  try {
    const session = await requireUser();
    supabase = session.supabase;
    userId = session.user.id;
  } catch (error) {
    return errorResponse(error);
  }

  let body: Record<string, unknown>;
  try {
    const parsed = await request.json();
    if (!parsed || typeof parsed !== "object") throw new Error("INVALID_BODY");
    body = parsed as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "A valid story request is required." }, { status: 400 });
  }

  try {
    switch (body.action) {
      case "create":
        return NextResponse.json({ story: await handleCreate(supabase, userId, body) });
      case "update":
        return NextResponse.json({ story: await handleUpdate(supabase, userId, body) });
      case "confirm":
        return NextResponse.json({ story: await handleConfirm(supabase, userId, body) });
      case "retire":
        return NextResponse.json({ story: await handleRetire(supabase, userId, body) });
      case "attach_profile_evidence":
        return NextResponse.json({ evidence: await handleAttachProfileEvidence(supabase, userId, body) });
      default:
        return NextResponse.json({ error: "Unknown action." }, { status: 400 });
    }
  } catch (error) {
    return errorResponse(error);
  }
}

async function handleCreate(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<CareerStory> {
  const title = requireString(body.title, "Title");
  const draft = draftFieldsFrom(body);
  const input: CreateCareerStoryInput = {
    title,
    ...draft,
    tags: optionalStringArray(body.tags),
    completeness: careerStoryCompleteness(draft),
  };
  return createCareerStory(supabase, userId, input);
}

async function handleUpdate(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<CareerStory> {
  const storyId = requireString(body.storyId, "storyId");
  const current = await getCareerStory(supabase, userId, storyId);
  if (!current) throw new RepositoryError("Could not find that career story.", "NO_OWNED_ROW");

  const patch = draftPatchFrom(body);
  const merged: CareerStoryDraftFields = {} as CareerStoryDraftFields;
  for (const field of DRAFT_FIELDS) merged[field] = patch[field] !== undefined ? (patch[field] as string | null) : current[field];

  const input: UpdateCareerStoryInput = {
    title: body.title === undefined ? undefined : requireString(body.title, "Title"),
    ...patch,
    tags: optionalStringArray(body.tags),
    completeness: careerStoryCompleteness(merged),
  };
  return updateCareerStory(supabase, userId, storyId, input);
}

async function handleConfirm(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<CareerStory> {
  const storyId = requireString(body.storyId, "storyId");
  return updateCareerStory(supabase, userId, storyId, {
    reviewState: "confirmed",
    confirmedAt: new Date().toISOString(),
  });
}

/** Retiring preserves the row and its provenance -- a state change, never a delete. */
async function handleRetire(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<CareerStory> {
  const storyId = requireString(body.storyId, "storyId");
  return updateCareerStory(supabase, userId, storyId, { reviewState: "retired" });
}

async function handleAttachProfileEvidence(
  supabase: SupabaseClient,
  userId: string,
  body: Record<string, unknown>,
): Promise<CareerStoryEvidence> {
  const storyId = requireString(body.storyId, "storyId");
  const profileEvidenceId = requireString(body.profileEvidenceId, "profileEvidenceId");
  const note = optionalStoryText(body.note);
  return attachCareerStoryEvidence(supabase, userId, storyId, { kind: "profile_evidence", profileEvidenceId }, note);
}

/** Reads all nine draft fields off `body`, coercing an omitted or `null` field to `null` (never `undefined`) -- the shape `careerStoryCompleteness` expects. */
function draftFieldsFrom(body: Record<string, unknown>): CareerStoryDraftFields {
  const draft = {} as CareerStoryDraftFields;
  for (const field of DRAFT_FIELDS) draft[field] = optionalStoryText(body[field]) ?? null;
  return draft;
}

/** Reads the nine draft fields off `body` preserving the three-way distinction `updateCareerStory` depends on: `undefined` (omitted, leave alone) vs `null` (explicit clear) vs a trimmed string. */
function draftPatchFrom(body: Record<string, unknown>): Partial<CareerStoryDraftFields> {
  const patch: Partial<CareerStoryDraftFields> = {};
  for (const field of DRAFT_FIELDS) patch[field] = optionalStoryText(body[field]);
  return patch;
}

/** Raised by request parsing below; always a 400 -- see `errorResponse`. */
class StoryRequestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "StoryRequestError";
  }
}

function requireString(value: unknown, label: string): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) throw new StoryRequestError(`${label} is required.`);
  return text;
}

/**
 * Parses an optional free-text field, preserving the three-way distinction
 * `updateCareerStory` (and completeness recomputation) depends on:
 * `undefined` means "field omitted, leave it alone," while `null` means
 * "clear it." An empty/whitespace-only string is treated the same as an
 * explicit `null`.
 */
function optionalStoryText(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (typeof value !== "string") throw new StoryRequestError("Expected a text value.");
  return value.trim() || null;
}

function optionalStringArray(value: unknown): string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new StoryRequestError("Expected a list of text values.");
  }
  return value.map((item) => item.trim()).filter(Boolean);
}

function errorResponse(error: unknown) {
  if (error instanceof Error) {
    const code = "code" in error ? (error as { code?: unknown }).code : undefined;
    console.error("[api/stories] request failed", { name: error.name, message: error.message, code });
  } else {
    console.error("[api/stories] request failed", error);
  }
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof StoryRequestError) {
    return NextResponse.json({ error: error.message }, { status: 400 });
  }
  if (error instanceof RepositoryError && error.code === "NO_OWNED_ROW") {
    return NextResponse.json({ error: error.message }, { status: 404 });
  }
  return NextResponse.json({ error: "Could not complete your story request." }, { status: 500 });
}

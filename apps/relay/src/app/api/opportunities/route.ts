import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";
import {
  addOpportunityNote,
  createOpportunity,
  isOpportunityStatus,
  listOpportunities,
  listOpportunityEvents,
  scheduleOpportunityInterview,
  transitionOpportunity,
  updateOpportunityDetails,
} from "@/lib/repositories/opportunities";
import { RepositoryError } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";
import type { CreateOpportunityInput, Opportunity, OpportunityEvent, OpportunityStatus, UpdateOpportunityDetailsInput } from "@/lib/types";

export const runtime = "nodejs";

/**
 * Two reads in one handler, distinguished by the `opportunityId` query
 * parameter -- distinct response shapes, never conflated:
 *
 * - No `opportunityId`: the authenticated caller's opportunities (the UI's
 *   "Applications" list; see design section 8.1), most recently updated
 *   first -- `{ opportunities }`.
 * - `opportunityId` present: that one opportunity's append-only event
 *   history (its "timeline"), most recent first -- `{ events }`. Scoped to
 *   the authenticated caller's id from `requireUser()`, never a
 *   request-supplied one, so a foreign `opportunityId` resolves to an empty
 *   history rather than leaking another user's events (see
 *   `listOpportunityEvents` in `src/lib/repositories/opportunities.ts`).
 *   Deliberately per-opportunity rather than joined onto the list response:
 *   the opportunity list is unbounded, and fetching every opportunity's full
 *   history on every list load would be an N+1 across the whole board.
 *
 * Both are read-only.
 */
export async function GET(request: Request) {
  try {
    const { supabase, user } = await requireUser();
    const opportunityId = new URL(request.url).searchParams.get("opportunityId");
    if (opportunityId) {
      const events = await listOpportunityEvents(supabase, user.id, opportunityId);
      return NextResponse.json({ events });
    }
    const opportunities = await listOpportunities(supabase, user.id);
    return NextResponse.json({ opportunities });
  } catch (error) {
    return errorResponse(error);
  }
}

/**
 * Applies one application-lifecycle action for the authenticated caller.
 *
 * Actions: `create`, `update`, `transition`, `schedule_interview`, and
 * `add_note`. Every field is parsed and validated HERE from the untrusted
 * request body -- raw JSON is never cast directly to a repository input
 * type, so a malformed field (a non-string company, an out-of-range match
 * score, a non-future interview time, an unrecognized status) is rejected
 * with a 400 before any repository call runs.
 *
 * `create` with an `initialStatus` other than `"considering"` (e.g. logging
 * an application that was already submitted) is a two-step operation:
 * `createOpportunity` always creates in `considering`, then
 * `transitionOpportunity` moves it on. The route never writes `status` or
 * `next_interview_at` directly -- both only ever change through the
 * Release 1 lifecycle repository functions, which keep the summary row and
 * its history in sync.
 *
 * Responses: `{ opportunity }` for `create`/`update`/`transition`/
 * `schedule_interview`; `{ event }` for `add_note`, since that action
 * appends a history event rather than changing the opportunity row.
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
    return NextResponse.json({ error: "A valid opportunity request is required." }, { status: 400 });
  }

  try {
    switch (body.action) {
      case "create":
        return NextResponse.json({ opportunity: await handleCreate(supabase, userId, body) });
      case "update":
        return NextResponse.json({ opportunity: await handleUpdate(supabase, userId, body) });
      case "transition":
        return NextResponse.json({ opportunity: await handleTransition(supabase, userId, body) });
      case "schedule_interview":
        return NextResponse.json({ opportunity: await handleScheduleInterview(supabase, userId, body) });
      case "add_note":
        return NextResponse.json({ event: await handleAddNote(supabase, userId, body) });
      default:
        return NextResponse.json({ error: "Unknown action." }, { status: 400 });
    }
  } catch (error) {
    return errorResponse(error);
  }
}

async function handleCreate(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<Opportunity> {
  const input: CreateOpportunityInput = {
    company: requireString(body.company, "Company"),
    role: requireString(body.role, "Role"),
    location: optionalString(body.location),
    remote: optionalBoolean(body.remote),
    jobUrl: optionalString(body.jobUrl),
    jobDescription: optionalString(body.jobDescription),
    sourceLabel: optionalString(body.sourceLabel),
    sourceSystem: optionalString(body.sourceSystem),
    sourceExternalId: optionalString(body.sourceExternalId),
    matchScore: optionalMatchScore(body.matchScore),
    strengths: optionalStringArray(body.strengths),
    gaps: optionalStringArray(body.gaps),
    notes: optionalString(body.notes),
  };
  // Validate a given `initialStatus` BEFORE creating anything, so an invalid
  // one never leaves behind an orphaned `considering` opportunity.
  const initialStatus = body.initialStatus === undefined
    ? undefined
    : opportunityStatusValue(body.initialStatus, "Initial status");

  const opportunity = await createOpportunity(supabase, userId, input);
  if (initialStatus === undefined || initialStatus === "considering") return opportunity;
  return transitionOpportunity(supabase, userId, opportunity.id, initialStatus);
}

async function handleUpdate(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<Opportunity> {
  const opportunityId = requireString(body.opportunityId, "opportunityId");
  const input: UpdateOpportunityDetailsInput = {
    company: body.company === undefined ? undefined : requireString(body.company, "Company"),
    role: body.role === undefined ? undefined : requireString(body.role, "Role"),
    location: optionalString(body.location),
    remote: optionalBoolean(body.remote),
    jobUrl: optionalString(body.jobUrl),
    jobDescription: optionalString(body.jobDescription),
    sourceLabel: optionalString(body.sourceLabel),
    sourceSystem: optionalString(body.sourceSystem),
    sourceExternalId: optionalString(body.sourceExternalId),
    matchScore: optionalMatchScore(body.matchScore),
    strengths: optionalStringArray(body.strengths),
    gaps: optionalStringArray(body.gaps),
    notes: optionalString(body.notes),
  };
  return updateOpportunityDetails(supabase, userId, opportunityId, input);
}

async function handleTransition(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<Opportunity> {
  const opportunityId = requireString(body.opportunityId, "opportunityId");
  const toStatus = opportunityStatusValue(body.toStatus, "toStatus");
  return transitionOpportunity(supabase, userId, opportunityId, toStatus, {
    occurredAt: optionalIsoTimestamp(body.occurredAt, "occurredAt") ?? undefined,
    note: optionalString(body.note) ?? undefined,
    metadata: optionalMetadata(body.metadata),
  });
}

async function handleScheduleInterview(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<Opportunity> {
  const opportunityId = requireString(body.opportunityId, "opportunityId");
  const interviewAt = requireFutureIsoTimestamp(body.interviewAt, "interviewAt");
  return scheduleOpportunityInterview(supabase, userId, opportunityId, interviewAt, {
    note: optionalString(body.note) ?? undefined,
    metadata: optionalMetadata(body.metadata),
  });
}

async function handleAddNote(supabase: SupabaseClient, userId: string, body: Record<string, unknown>): Promise<OpportunityEvent> {
  const opportunityId = requireString(body.opportunityId, "opportunityId");
  const note = requireString(body.note, "note");
  return addOpportunityNote(supabase, userId, opportunityId, note);
}

/** Raised by request parsing below; always a 400 -- see `errorResponse`. */
class OpportunityRequestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "OpportunityRequestError";
  }
}

function requireString(value: unknown, label: string): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) throw new OpportunityRequestError(`${label} is required.`);
  return text;
}

/**
 * Parses an optional free-text field, preserving the three-way distinction
 * `updateOpportunityDetails` depends on: `undefined` means "field omitted,
 * leave it alone," while `null` means "clear it." An empty/whitespace-only
 * string is treated the same as an explicit `null`.
 */
function optionalString(value: unknown): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (typeof value !== "string") throw new OpportunityRequestError("Expected a text value.");
  return value.trim() || null;
}

function optionalBoolean(value: unknown): boolean | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (typeof value !== "boolean") throw new OpportunityRequestError("Expected true or false.");
  return value;
}

function optionalStringArray(value: unknown): string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw new OpportunityRequestError("Expected a list of text values.");
  }
  return value.map((item) => item.trim()).filter(Boolean);
}

function optionalMatchScore(value: unknown): number | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 100) {
    throw new OpportunityRequestError("Match score must be a number between 0 and 100.");
  }
  return value;
}

function optionalMetadata(value: unknown): Record<string, unknown> | undefined {
  if (value === undefined) return undefined;
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new OpportunityRequestError("Metadata must be an object.");
  }
  return value as Record<string, unknown>;
}

function opportunityStatusValue(value: unknown, label: string): OpportunityStatus {
  if (!isOpportunityStatus(value)) throw new OpportunityRequestError(`${label} must be a valid opportunity status.`);
  return value;
}

function optionalIsoTimestamp(value: unknown, label: string): string | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (typeof value !== "string" || Number.isNaN(Date.parse(value))) {
    throw new OpportunityRequestError(`${label} must be a valid date/time.`);
  }
  return value;
}

function requireFutureIsoTimestamp(value: unknown, label: string): string {
  const parsed = typeof value === "string" ? Date.parse(value) : NaN;
  if (Number.isNaN(parsed)) throw new OpportunityRequestError(`${label} must be a valid date/time.`);
  if (parsed <= Date.now()) throw new OpportunityRequestError(`${label} must be in the future.`);
  return value as string;
}

function errorResponse(error: unknown) {
  if (error instanceof Error) {
    const code = "code" in error ? (error as { code?: unknown }).code : undefined;
    console.error("[api/opportunities] request failed", { name: error.name, message: error.message, code });
  } else {
    console.error("[api/opportunities] request failed", error);
  }
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof OpportunityRequestError) {
    return NextResponse.json({ error: error.message }, { status: 400 });
  }
  if (error instanceof RepositoryError && error.code === "NO_OWNED_ROW") {
    return NextResponse.json({ error: error.message }, { status: 404 });
  }
  return NextResponse.json({ error: "Could not complete your opportunity request." }, { status: 500 });
}

import { NextResponse } from "next/server";
import {
  PracticeServiceError,
  loadPracticeOverview,
  startManualPractice,
  startRecommendedPractice,
  type ManualPracticeRequest,
} from "@/lib/practice-service";
import { RepositoryError } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";
import type { PracticeFormat } from "@/lib/types";

export const runtime = "nodejs";

/**
 * Returns the authenticated caller's current practice read model: the
 * recomputed baseline recommendation plus their practice plans. Read-only --
 * a recommendation is persisted only by POST, so refreshing this view never
 * creates plan rows.
 */
export async function GET() {
  try {
    const { supabase, user } = await requireUser();
    const { recommendation, plans } = await loadPracticeOverview(supabase, user.id, new Date());
    return NextResponse.json({ recommendation, plans });
  } catch (error) {
    return errorResponse(error);
  }
}

/**
 * Starts practice for the authenticated caller and returns
 * `{ plan, session }`.
 *
 * Actions: `start_recommended`, which recomputes the recommendation
 * server-side and ignores anything recommendation-shaped in the body; and
 * `start_manual`, which forwards ONLY the user-choosable fields
 * (`format`, `primaryFocus`, `secondaryFocus`, `estimatedMinutes`,
 * `successCriteria`, `primaryOpportunityId`) to the service. Plan status,
 * rationale, and priority are server-owned and can never be set from a
 * request body.
 *
 * Values are validated by `src/lib/practice-service.ts`, not here, so the
 * same rules apply to every caller. Failures map to 400 (invalid
 * input/state), 401 (unauthenticated), 404 (owned resource not found), 409
 * (the plan is no longer startable), and 500 (unexpected persistence/AI
 * failure); see `errorResponse`.
 */
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
    return NextResponse.json({ error: "A valid practice request is required." }, { status: 400 });
  }

  try {
    if (body.action === "start_recommended") {
      return NextResponse.json(await startRecommendedPractice(supabase, user.id, new Date()));
    }
    if (body.action === "start_manual") {
      return NextResponse.json(await startManualPractice(supabase, user.id, manualPracticeRequest(body)));
    }
    return NextResponse.json({ error: "Unknown action." }, { status: 400 });
  } catch (error) {
    return errorResponse(error);
  }
}

/**
 * Picks the user-choosable fields out of an untrusted body. Types are
 * asserted rather than checked here because `startManualPractice` validates
 * every value it receives -- an `estimatedMinutes` of `"12"` must reach the
 * service and be rejected as invalid input, not be silently dropped.
 */
function manualPracticeRequest(body: Record<string, unknown>): ManualPracticeRequest {
  return {
    format: body.format as PracticeFormat,
    primaryFocus: body.primaryFocus as string,
    secondaryFocus: (body.secondaryFocus ?? null) as string | null,
    estimatedMinutes: (body.estimatedMinutes ?? null) as number | null,
    successCriteria: (body.successCriteria ?? []) as string[],
    primaryOpportunityId: (body.primaryOpportunityId ?? null) as string | null,
  };
}

function errorResponse(error: unknown) {
  if (error instanceof Error) {
    const code = "code" in error ? (error as { code?: unknown }).code : undefined;
    console.error("[api/practice] request failed", { name: error.name, message: error.message, code });
  } else {
    console.error("[api/practice] request failed", error);
  }
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof PracticeServiceError) {
    const status = error.code === "OPPORTUNITY_NOT_FOUND" ? 404 : 400;
    return NextResponse.json({ error: error.message }, { status });
  }
  if (error instanceof RepositoryError) {
    // `P0002` (no_data_found) and `22023` (invalid_parameter_value) are the
    // SQLSTATEs the planned-practice start functions raise for "this plan or
    // opportunity is not yours" and "this plan is no longer startable".
    if (error.code === "NO_OWNED_ROW" || error.code === "P0002") {
      return NextResponse.json({ error: error.message }, { status: 404 });
    }
    if (error.code === "INVALID_PLAN_CONTEXT" || error.code === "22023") {
      return NextResponse.json({ error: error.message }, { status: 409 });
    }
  }
  return NextResponse.json({ error: "Could not complete your practice request." }, { status: 500 });
}

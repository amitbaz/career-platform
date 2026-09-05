import { NextResponse } from "next/server";
import { CareerDashboardError, loadCareerDashboard } from "@/lib/career-dashboard";
import { RepositoryError } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";

export const runtime = "nodejs";

/**
 * Returns the authenticated caller's canonical Career Brain dashboard: their
 * profile, progress, opportunities, coach observations (with typed evidence
 * resolved to user-safe display items), career stories, recent practice
 * plans, and the current deterministic practice recommendation. Read-only --
 * `loadCareerDashboard` only aggregates existing data and never creates or
 * reconciles a Career Brain row.
 *
 * `coachMode` is computed HERE, not inside the dashboard service, because it
 * reflects process configuration (`GEMINI_API_KEY`) rather than persisted
 * data -- the service stays a pure function of its repository inputs.
 *
 * Failures: 401 when unauthenticated, 400 when the caller has not finished
 * profile onboarding yet (`CareerDashboardError`), 500 for any other
 * unexpected persistence failure. See `errorResponse`.
 */
export async function GET() {
  try {
    const { supabase, user } = await requireUser();
    const coachMode = process.env.GEMINI_API_KEY ? "live" : "demo";
    const dashboard = await loadCareerDashboard(supabase, user.id, new Date(), coachMode);
    return NextResponse.json(dashboard);
  } catch (error) {
    return errorResponse(error);
  }
}

function errorResponse(error: unknown) {
  if (error instanceof Error) {
    const code = "code" in error ? (error as { code?: unknown }).code : undefined;
    console.error("[api/career/dashboard] request failed", { name: error.name, message: error.message, code });
  } else {
    console.error("[api/career/dashboard] request failed", error);
  }
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof CareerDashboardError) {
    return NextResponse.json({ error: error.message }, { status: 400 });
  }
  if (error instanceof RepositoryError && error.code === "NO_OWNED_ROW") {
    return NextResponse.json({ error: error.message }, { status: 404 });
  }
  return NextResponse.json({ error: "Could not load your dashboard." }, { status: 500 });
}

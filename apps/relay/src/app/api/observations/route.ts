import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";
import { resolveObservationEvidence } from "@/lib/coach-memory";
import { effectiveObservationText } from "@/lib/practice-recommendation";
import { listCoachObservations, listObservationEvidence, reviewCoachObservation } from "@/lib/repositories/observations";
import { RepositoryError } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";
import type { CoachObservation, CoachObservationReview, CoachObservationSummary } from "@/lib/types";

export const runtime = "nodejs";

/** Loads `observation`'s evidence and resolves it through `coach-memory.ts` into safe display items, per `CoachObservationSummary`. */
async function summarizeObservation(
  supabase: SupabaseClient,
  userId: string,
  observation: CoachObservation,
): Promise<CoachObservationSummary> {
  const evidenceRows = await listObservationEvidence(supabase, userId, observation.id);
  const evidence = await resolveObservationEvidence(supabase, userId, evidenceRows);
  return { ...observation, effectiveText: effectiveObservationText(observation), evidence };
}

/**
 * Returns the authenticated caller's coach observations (the Coach view's
 * review queue; see design section 5.2) split into `active` and `history`.
 * Per design section 5.2, "dismissed observations are hidden from the
 * default active list but can be shown under history" -- so `history` holds
 * exactly the dismissed observations, `active` everything else, and every
 * observation appears in exactly one of the two. Each item is enriched with
 * `effectiveText` (the user's correction when corrected, otherwise the
 * original `claim`) and its evidence resolved via `resolveObservationEvidence`
 * from `coach-memory.ts` -- this route never re-resolves evidence itself.
 * Read-only.
 */
export async function GET() {
  try {
    const { supabase, user } = await requireUser();
    const observations = await listCoachObservations(supabase, user.id);
    const summaries = await Promise.all(
      observations.map((observation) => summarizeObservation(supabase, user.id, observation)),
    );
    const active = summaries.filter((summary) => summary.reviewState !== "dismissed");
    const history = summaries.filter((summary) => summary.reviewState === "dismissed");
    return NextResponse.json({ active, history });
  } catch (error) {
    return errorResponse(error);
  }
}

/**
 * Records the authenticated caller's review of one coach observation.
 *
 * Actions: `confirm`, `correct`, `dismiss` -- there is no normal create
 * action here. Release 2 does not automatically create or reconcile coach
 * observations (design section 5.2); the caller only ever confirms,
 * corrects, or dismisses an observation the system already produced.
 * `correct` requires non-empty replacement text; the other two actions take
 * no body fields beyond `observationId`. Every action delegates to
 * `reviewCoachObservation`'s typed `CoachObservationReview`, which is the
 * one place the resulting timestamp/`user_correction` columns are set (see
 * that repository's `reviewColumns`) -- this route never writes those
 * columns directly.
 *
 * Response: `{ observation }`, the reviewed `CoachObservation` row.
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
    return NextResponse.json({ error: "A valid observation request is required." }, { status: 400 });
  }

  if (body.action !== "confirm" && body.action !== "dismiss" && body.action !== "correct") {
    return NextResponse.json({ error: "Unknown action." }, { status: 400 });
  }

  try {
    const observationId = requireString(body.observationId, "observationId");
    const review: CoachObservationReview =
      body.action === "confirm"
        ? { state: "confirmed" }
        : body.action === "dismiss"
          ? { state: "dismissed" }
          : { state: "corrected", correction: requireString(body.correction, "correction") };
    const observation = await reviewCoachObservation(supabase, userId, observationId, review);
    return NextResponse.json({ observation });
  } catch (error) {
    return errorResponse(error);
  }
}

/** Raised by request parsing below; always a 400 -- see `errorResponse`. */
class ObservationRequestError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ObservationRequestError";
  }
}

function requireString(value: unknown, label: string): string {
  const text = typeof value === "string" ? value.trim() : "";
  if (!text) throw new ObservationRequestError(`${label} is required.`);
  return text;
}

function errorResponse(error: unknown) {
  if (error instanceof Error) {
    const code = "code" in error ? (error as { code?: unknown }).code : undefined;
    console.error("[api/observations] request failed", { name: error.name, message: error.message, code });
  } else {
    console.error("[api/observations] request failed", error);
  }
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof ObservationRequestError) {
    return NextResponse.json({ error: error.message }, { status: 400 });
  }
  if (error instanceof RepositoryError && error.code === "NO_OWNED_ROW") {
    return NextResponse.json({ error: error.message }, { status: 404 });
  }
  return NextResponse.json({ error: "Could not complete your observation request." }, { status: 500 });
}

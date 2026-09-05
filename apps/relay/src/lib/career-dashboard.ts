import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import { resolveObservationEvidence } from "@/lib/coach-memory";
import { effectiveObservationText, recommendPractice } from "@/lib/practice-recommendation";
import { loadPracticeInputs } from "@/lib/practice-service";
import { listObservationEvidence } from "@/lib/repositories/observations";
import { listCareerStoryEvidence } from "@/lib/repositories/stories";
import type {
  CareerDashboard,
  CareerStory,
  CareerStorySummary,
  CoachObservation,
  CoachObservationSummary,
  Opportunity,
} from "@/lib/types";

/**
 * Raised when the dashboard cannot be built because the caller has not
 * finished profile onboarding yet. `CareerDashboard.profile` is
 * non-nullable by contract -- the client shell only ever routes to Home
 * once a profile exists, sending brand-new accounts to onboarding instead
 * (see `src/app/relay-shell.tsx`'s `setView(coachData.profile ? "home" : "onboarding")`)
 * -- so a missing profile here is a genuine invalid-state error, not a
 * normal empty-account case like zero opportunities or stories.
 */
export class CareerDashboardError extends Error {
  constructor(message: string, public readonly code: "PROFILE_REQUIRED") {
    super(message);
    this.name = "CareerDashboardError";
  }
}

/** Dismissed observations are archived, not surfaced -- everything else (including unreviewed) is still shown. */
function isDisplayableObservation(observation: CoachObservation): boolean {
  return observation.reviewState !== "dismissed";
}

async function summarizeObservation(
  supabase: SupabaseClient,
  userId: string,
  observation: CoachObservation,
): Promise<CoachObservationSummary> {
  const evidenceRows = await listObservationEvidence(supabase, userId, observation.id);
  const evidence = await resolveObservationEvidence(supabase, userId, evidenceRows);
  return { ...observation, effectiveText: effectiveObservationText(observation), evidence };
}

async function summarizeStory(
  supabase: SupabaseClient,
  userId: string,
  story: CareerStory,
): Promise<CareerStorySummary> {
  const evidence = await listCareerStoryEvidence(supabase, userId, story.id);
  return { ...story, evidenceCount: evidence.length };
}

/** `opportunities` filtered to future-dated interviews and sorted soonest-first. */
function upcomingOpportunities(opportunities: Opportunity[], now: Date): Opportunity[] {
  return opportunities
    .filter((opportunity) => opportunity.nextInterviewAt !== null && Date.parse(opportunity.nextInterviewAt) > now.getTime())
    .sort((left, right) => Date.parse(left.nextInterviewAt as string) - Date.parse(right.nextInterviewAt as string));
}

/**
 * Builds the single canonical Career Brain dashboard read model for
 * `userId`. Composes `loadPracticeInputs` (the same profile/opportunities/
 * observations/stories/sessions/plans loader `src/lib/practice-service.ts`
 * uses for the Practice view, so both views load and compute readiness
 * identically) with `recommendPractice` -- AGGREGATION ONLY, never
 * persistence: this issues no writes, so it is safe to call on every
 * `GET /api/career/dashboard` request without creating or reconciling any
 * Career Brain row (Release 2 does not auto-reconcile coach observations).
 *
 * Resolves each non-dismissed observation's evidence and each story's
 * evidence count in parallel, after `loadPracticeInputs` resolves (each of
 * those needs its rows first). `now` is never read from the clock here -- it
 * is threaded straight through to both `loadPracticeInputs` (as the
 * readiness model's `asOf`) and `recommendPractice`, so the readiness that
 * feeds a recommendation and the `now` beside it are always computed
 * against the same clock reading.
 *
 * Throws {@link CareerDashboardError} with code `PROFILE_REQUIRED` when the
 * caller has no profile yet; every other empty Career Brain table (no
 * opportunities, observations, stories, plans, or sessions) is a valid
 * dashboard, not an error.
 */
export async function loadCareerDashboard(
  supabase: SupabaseClient,
  userId: string,
  now: Date,
  coachMode: "demo" | "live",
): Promise<CareerDashboard> {
  const inputs = await loadPracticeInputs(supabase, userId, now);
  if (!inputs.profile) throw new CareerDashboardError("Create your profile first.", "PROFILE_REQUIRED");

  const [observationSummaries, storySummaries] = await Promise.all([
    Promise.all(
      inputs.observations
        .filter(isDisplayableObservation)
        .map((observation) => summarizeObservation(supabase, userId, observation)),
    ),
    Promise.all(inputs.stories.map((story) => summarizeStory(supabase, userId, story))),
  ]);

  const recommendation = recommendPractice({
    opportunities: inputs.opportunities,
    observations: inputs.observations,
    stories: inputs.stories,
    readiness: inputs.readiness,
    competencies: inputs.profile.competencies,
    recentSessions: inputs.sessions,
    recentPlans: inputs.plans,
    now,
  });

  return {
    profile: inputs.profile,
    coachMode,
    readiness: inputs.readiness,
    recentSessions: inputs.sessions,
    opportunities: inputs.opportunities,
    upcomingOpportunities: upcomingOpportunities(inputs.opportunities, now),
    observations: observationSummaries,
    stories: storySummaries,
    recentPracticePlans: inputs.plans,
    recommendation,
  };
}

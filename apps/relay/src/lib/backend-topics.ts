/**
 * The seven backend/full-stack topic areas issue #21 scopes practice to,
 * calibrated to a frontend-leaning Senior Full Stack Engineer rather than a
 * backend-specialist or DBA. `recommendPractice` (practice-recommendation.ts)
 * names one of these when no backend competency has been tracked yet, so the
 * practice-blueprint prompt (coach.ts) has something concrete to write
 * questions about instead of the bare word "backend".
 */
export const BACKEND_TOPIC_AREAS = [
  "API design",
  "persistence and database fundamentals",
  "authentication and authorization",
  "caching",
  "concurrency and reliability",
  "backend boundaries",
  "operational reasoning",
] as const;

/**
 * Threaded into a practice plan's free-text `secondaryFocus`, which
 * `practiceBlueprintPrompt` (coach.ts) passes straight to the question-writing
 * model — this is what keeps generated backend questions at a senior
 * full-stack generalist's depth rather than drifting into DBA-level minutiae.
 */
export const BACKEND_SENIORITY_CALIBRATION =
  "Calibrate to a frontend-leaning Senior Full Stack Engineer bar: practical judgment and trade-offs, not backend-specialist or DBA-level depth.";

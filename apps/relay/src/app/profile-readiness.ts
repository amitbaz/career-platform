import type { Profile } from "@/lib/types";

/**
 * Practice-first readiness copy: a `ready` profile is confirmed as
 * evidence-grounded for personalized practice; a sparse one is never a
 * blocker -- Relay simply starts broader and surfaces stronger project,
 * ownership, and outcome examples as the user answers.
 */
export function profileReadinessCopy(readiness: Profile["readiness"]): string | null {
  if (!readiness) return null;
  return readiness.ready
    ? "Your source profile has enough detail for evidence-grounded practice."
    : "You can practice now. Relay will start broader and help you uncover stronger project, ownership, and outcome examples as you answer.";
}

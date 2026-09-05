import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import { RepositoryError } from "@/lib/repositories/profile";
import type { CoachEvidenceDisplay, ObservationEvidence, OpportunityEventType } from "@/lib/types";

type Row = Record<string, unknown>;

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** The label/summary pair a resolved evidence source contributes; `CoachEvidenceDisplay.kind`/`role`/`reason` are attached by the caller. */
type ResolvedSource = { label: string; summary: string };

function uniqueIds(values: Array<string | null>): string[] {
  return [...new Set(values.filter((id): id is string => id !== null))];
}

/**
 * Loads profile-evidence rows owned by `userId` and maps each to its display
 * pair: the project/employer as the label, and the source excerpt as the
 * summary. Deliberately does not filter on `is_active` -- an inactive row is
 * historical evidence, not an invalid one, and remains a valid citation (see
 * the same rule documented on `attachObservationEvidence` in
 * `src/lib/repositories/observations.ts`).
 */
async function loadProfileEvidenceSources(
  supabase: SupabaseClient,
  userId: string,
  ids: string[],
): Promise<Map<string, ResolvedSource>> {
  const map = new Map<string, ResolvedSource>();
  if (!ids.length) return map;
  const { data, error } = await supabase.from("profile_evidence").select("*").eq("user_id", userId).in("id", ids);
  if (error) throw new RepositoryError("Could not load profile evidence for coach memory.", error.code);
  for (const row of (data ?? []) as Row[]) {
    const label = stringValue(row.project_or_employer) || "Profile evidence";
    map.set(stringValue(row.id), { label, summary: stringValue(row.source_excerpt) });
  }
  return map;
}

function evaluationSummary(strengths: string[], weaknesses: string[]): string {
  const parts: string[] = [];
  if (strengths.length) parts.push(`Strengths: ${strengths.join(", ")}.`);
  if (weaknesses.length) parts.push(`Needs work: ${weaknesses.join(", ")}.`);
  return parts.length ? parts.join(" ") : "No strengths or improvement areas were recorded for this answer.";
}

/**
 * Loads question-evaluation rows owned by `userId`, joined to their
 * question's prompt (a second owned, id-scoped query -- `question_evaluations`
 * has no prompt of its own), and maps each to its display pair: the question
 * prompt as the label, a concise strengths/weaknesses summary as the summary.
 */
async function loadQuestionEvaluationSources(
  supabase: SupabaseClient,
  userId: string,
  ids: string[],
): Promise<Map<string, ResolvedSource>> {
  const map = new Map<string, ResolvedSource>();
  if (!ids.length) return map;
  const { data, error } = await supabase.from("question_evaluations").select("*").eq("user_id", userId).in("id", ids);
  if (error) throw new RepositoryError("Could not load question evaluations for coach memory.", error.code);
  const evaluationRows = (data ?? []) as Row[];

  const questionIds = uniqueIds(evaluationRows.map((row) => stringValue(row.question_id) || null));
  const prompts = new Map<string, string>();
  if (questionIds.length) {
    const { data: questionRows, error: questionError } = await supabase
      .from("interview_questions")
      .select("id, prompt")
      .eq("user_id", userId)
      .in("id", questionIds);
    if (questionError) throw new RepositoryError("Could not load interview questions for coach memory.", questionError.code);
    for (const row of (questionRows ?? []) as Row[]) prompts.set(stringValue(row.id), stringValue(row.prompt));
  }

  for (const row of evaluationRows) {
    const prompt = prompts.get(stringValue(row.question_id));
    if (!prompt) continue; // the owned question could not be resolved -- never surface a raw id as a fallback label
    map.set(stringValue(row.id), {
      label: prompt,
      summary: evaluationSummary(stringArray(row.strengths), stringArray(row.weaknesses)),
    });
  }
  return map;
}

/** Loads career-story rows owned by `userId` and maps each to its display pair: the title, for both label and summary. */
async function loadCareerStorySources(
  supabase: SupabaseClient,
  userId: string,
  ids: string[],
): Promise<Map<string, ResolvedSource>> {
  const map = new Map<string, ResolvedSource>();
  if (!ids.length) return map;
  const { data, error } = await supabase.from("career_stories").select("id, title").eq("user_id", userId).in("id", ids);
  if (error) throw new RepositoryError("Could not load career stories for coach memory.", error.code);
  for (const row of (data ?? []) as Row[]) {
    const title = stringValue(row.title) || "Career story";
    map.set(stringValue(row.id), { label: title, summary: title });
  }
  return map;
}

function eventDescription(row: Row): string {
  const note = stringValue(row.note);
  if (note) return note;
  const fromStatus = stringValue(row.from_status);
  const toStatus = stringValue(row.to_status);
  switch (row.event_type as OpportunityEventType) {
    case "created":
      return "Opportunity created.";
    case "status_changed":
      return fromStatus && toStatus ? `Status changed from ${fromStatus} to ${toStatus}.` : "Status changed.";
    case "interview_scheduled":
      return "Interview scheduled.";
    case "interview_completed":
      return "Interview completed.";
    case "source_updated":
      return "Source details updated.";
    case "note":
    default:
      return "Note added.";
  }
}

/**
 * Loads opportunity-event rows owned by `userId`, joined to their
 * opportunity's company/role (a second owned, id-scoped query), and maps
 * each to its display pair: `company · role` as the label, a deterministic
 * event description as the summary (the event's own `note` when present,
 * otherwise a fixed phrase per `event_type`).
 */
async function loadOpportunityEventSources(
  supabase: SupabaseClient,
  userId: string,
  ids: string[],
): Promise<Map<string, ResolvedSource>> {
  const map = new Map<string, ResolvedSource>();
  if (!ids.length) return map;
  const { data, error } = await supabase.from("opportunity_events").select("*").eq("user_id", userId).in("id", ids);
  if (error) throw new RepositoryError("Could not load opportunity events for coach memory.", error.code);
  const eventRows = (data ?? []) as Row[];

  const opportunityIds = uniqueIds(eventRows.map((row) => stringValue(row.opportunity_id) || null));
  const opportunities = new Map<string, string>();
  if (opportunityIds.length) {
    const { data: opportunityRows, error: opportunityError } = await supabase
      .from("opportunities")
      .select("id, company, role")
      .eq("user_id", userId)
      .in("id", opportunityIds);
    if (opportunityError) throw new RepositoryError("Could not load opportunities for coach memory.", opportunityError.code);
    for (const row of (opportunityRows ?? []) as Row[]) {
      opportunities.set(stringValue(row.id), `${stringValue(row.company)} · ${stringValue(row.role)}`);
    }
  }

  for (const row of eventRows) {
    const label = opportunities.get(stringValue(row.opportunity_id));
    if (!label) continue; // the owned opportunity could not be resolved -- never surface a raw id as a fallback label
    map.set(stringValue(row.id), { label, summary: eventDescription(row) });
  }
  return map;
}

/**
 * Resolves a coach observation's typed provenance links into user-safe
 * display items -- see `CoachEvidenceDisplay` in `src/lib/types.ts` for the
 * exact per-kind label/summary contract. Every underlying lookup is scoped
 * to `userId`, so `evidence` rows pointing at a source the caller does not
 * own (or that no longer exists) resolve to nothing rather than a raw id;
 * such rows are silently omitted from the result instead of appearing as an
 * unreadable placeholder.
 *
 * Batches one query per evidence-source table (plus one join query each for
 * question evaluations and opportunity events) regardless of how many
 * `evidence` rows are passed, so this scales with the number of *distinct*
 * source kinds involved, not the number of evidence rows.
 */
export async function resolveObservationEvidence(
  supabase: SupabaseClient,
  userId: string,
  evidence: ObservationEvidence[],
): Promise<CoachEvidenceDisplay[]> {
  if (!evidence.length) return [];

  const [profileEvidence, questionEvaluations, careerStories, opportunityEvents] = await Promise.all([
    loadProfileEvidenceSources(supabase, userId, uniqueIds(evidence.map((item) => item.profileEvidenceId))),
    loadQuestionEvaluationSources(supabase, userId, uniqueIds(evidence.map((item) => item.questionEvaluationId))),
    loadCareerStorySources(supabase, userId, uniqueIds(evidence.map((item) => item.careerStoryId))),
    loadOpportunityEventSources(supabase, userId, uniqueIds(evidence.map((item) => item.opportunityEventId))),
  ]);

  const displays: CoachEvidenceDisplay[] = [];
  for (const item of evidence) {
    const resolved = item.profileEvidenceId
      ? { kind: "profile_evidence" as const, source: profileEvidence.get(item.profileEvidenceId) }
      : item.questionEvaluationId
        ? { kind: "question_evaluation" as const, source: questionEvaluations.get(item.questionEvaluationId) }
        : item.careerStoryId
          ? { kind: "career_story" as const, source: careerStories.get(item.careerStoryId) }
          : item.opportunityEventId
            ? { kind: "opportunity_event" as const, source: opportunityEvents.get(item.opportunityEventId) }
            : null;
    if (!resolved?.source) continue;
    displays.push({
      kind: resolved.kind,
      label: resolved.source.label,
      summary: resolved.source.summary,
      role: item.evidenceRole,
      reason: item.reason,
    });
  }
  return displays;
}

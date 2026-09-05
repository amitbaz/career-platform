import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import type {
  CreateOpportunityInput,
  Opportunity,
  OpportunityEvent,
  OpportunityEventType,
  OpportunityStatus,
  UpdateOpportunityDetailsInput,
} from "@/lib/types";
import { RepositoryError } from "@/lib/repositories/profile";

type Row = Record<string, unknown>;

const opportunityStatuses: OpportunityStatus[] = [
  "considering", "applied", "interviewing", "offer", "rejected", "withdrawn", "closed",
];

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function jsonRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

/**
 * Whether `value` is one of the seven statuses the `opportunities.status`
 * check constraint allows. Exported so callers outside this module (the
 * `/api/opportunities` route) can validate a `toStatus`/`initialStatus`
 * from an untrusted request body without duplicating this list -- mirrors
 * `isPracticeFormat` in `src/lib/repositories/practice-plans.ts`.
 */
export function isOpportunityStatus(value: unknown): value is OpportunityStatus {
  return opportunityStatuses.includes(value as OpportunityStatus);
}

function opportunityStatus(value: unknown): OpportunityStatus {
  return isOpportunityStatus(value) ? value : "considering";
}

function nullableOpportunityStatus(value: unknown): OpportunityStatus | null {
  return isOpportunityStatus(value) ? value : null;
}

function mapOpportunity(row: Row): Opportunity {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    company: stringValue(row.company),
    role: stringValue(row.role),
    status: opportunityStatus(row.status),
    location: typeof row.location === "string" ? row.location : null,
    remote: typeof row.remote === "boolean" ? row.remote : null,
    jobUrl: typeof row.job_url === "string" ? row.job_url : null,
    jobDescription: typeof row.job_description === "string" ? row.job_description : null,
    sourceLabel: typeof row.source_label === "string" ? row.source_label : null,
    sourceSystem: typeof row.source_system === "string" ? row.source_system : null,
    sourceExternalId: typeof row.source_external_id === "string" ? row.source_external_id : null,
    matchScore: row.match_score === null || row.match_score === undefined ? null : Number(row.match_score),
    strengths: stringArray(row.strengths),
    gaps: stringArray(row.gaps),
    notes: typeof row.notes === "string" ? row.notes : null,
    appliedAt: typeof row.applied_at === "string" ? row.applied_at : null,
    nextInterviewAt: typeof row.next_interview_at === "string" ? row.next_interview_at : null,
    createdAt: stringValue(row.created_at),
    updatedAt: stringValue(row.updated_at),
  };
}

function mapOpportunityEvent(row: Row): OpportunityEvent {
  return {
    id: stringValue(row.id),
    userId: stringValue(row.user_id),
    opportunityId: stringValue(row.opportunity_id),
    eventType: row.event_type as OpportunityEventType,
    fromStatus: nullableOpportunityStatus(row.from_status),
    toStatus: nullableOpportunityStatus(row.to_status),
    occurredAt: stringValue(row.occurred_at),
    note: typeof row.note === "string" ? row.note : null,
    metadata: jsonRecord(row.metadata),
    createdAt: stringValue(row.created_at),
  };
}

function rpcResultId(data: unknown, key: string): string | null {
  const result = Array.isArray(data) ? data[0] as Row | undefined : data as Row | undefined;
  const id = result && stringValue(result[key]);
  return id || null;
}

/** Options for the atomic lifecycle RPCs; each becomes one `opportunity_events` row. */
export type OpportunityLifecycleEventOptions = {
  occurredAt?: string;
  note?: string;
  metadata?: Record<string, unknown>;
};

/**
 * Creates an opportunity and its `created` history event atomically via the
 * `create_opportunity` RPC, then reloads it. New opportunities are always
 * created in `considering` status — status only moves onward through
 * {@link transitionOpportunity}.
 */
export async function createOpportunity(
  supabase: SupabaseClient,
  userId: string,
  input: CreateOpportunityInput,
): Promise<Opportunity> {
  const { data, error } = await supabase.rpc("create_opportunity", {
    p_company: input.company,
    p_role: input.role,
    p_location: input.location ?? null,
    p_remote: input.remote ?? null,
    p_job_url: input.jobUrl ?? null,
    p_job_description: input.jobDescription ?? null,
    p_source_label: input.sourceLabel ?? null,
    p_source_system: input.sourceSystem ?? null,
    p_source_external_id: input.sourceExternalId ?? null,
    p_match_score: input.matchScore ?? null,
    p_strengths: input.strengths ?? [],
    p_gaps: input.gaps ?? [],
    p_notes: input.notes ?? null,
  });
  const opportunityId = error ? null : rpcResultId(data, "opportunity_id");
  if (!opportunityId) throw new RepositoryError("Could not create the opportunity.", error?.code ?? "NO_OWNED_ROW");
  const opportunity = await getOpportunity(supabase, userId, opportunityId);
  if (!opportunity) throw new RepositoryError("Could not reload the created opportunity.", "NO_OWNED_ROW");
  return opportunity;
}

/** Loads one opportunity owned by `userId`, or null if it does not exist/isn't owned by them. */
export async function getOpportunity(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
): Promise<Opportunity | null> {
  const { data, error } = await supabase
    .from("opportunities")
    .select("*")
    .eq("id", opportunityId)
    .eq("user_id", userId)
    .maybeSingle();
  if (error) throw new RepositoryError("Could not load the opportunity.", error.code);
  return data ? mapOpportunity(data as Row) : null;
}

/** Lists all opportunities owned by `userId`, most recently updated first. */
export async function listOpportunities(supabase: SupabaseClient, userId: string): Promise<Opportunity[]> {
  const { data, error } = await supabase
    .from("opportunities")
    .select("*")
    .eq("user_id", userId)
    .order("updated_at", { ascending: false });
  if (error) throw new RepositoryError("Could not load your opportunities.", error.code);
  return ((data ?? []) as Row[]).map(mapOpportunity);
}

/**
 * Updates ordinary descriptive fields (company, role, location, notes, etc.).
 * Never touches `status`, `applied_at`, or `next_interview_at` — those
 * lifecycle fields only change through the atomic RPCs below, which keep
 * them in sync with their history.
 */
export async function updateOpportunityDetails(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
  input: UpdateOpportunityDetailsInput,
): Promise<Opportunity> {
  const patch: Row = { updated_at: new Date().toISOString() };
  if (input.company !== undefined) patch.company = input.company;
  if (input.role !== undefined) patch.role = input.role;
  if (input.location !== undefined) patch.location = input.location;
  if (input.remote !== undefined) patch.remote = input.remote;
  if (input.jobUrl !== undefined) patch.job_url = input.jobUrl;
  if (input.jobDescription !== undefined) patch.job_description = input.jobDescription;
  if (input.sourceLabel !== undefined) patch.source_label = input.sourceLabel;
  if (input.sourceSystem !== undefined) patch.source_system = input.sourceSystem;
  if (input.sourceExternalId !== undefined) patch.source_external_id = input.sourceExternalId;
  if (input.matchScore !== undefined) patch.match_score = input.matchScore;
  if (input.strengths !== undefined) patch.strengths = input.strengths;
  if (input.gaps !== undefined) patch.gaps = input.gaps;
  if (input.notes !== undefined) patch.notes = input.notes;

  const { data, error } = await supabase
    .from("opportunities")
    .update(patch)
    .eq("id", opportunityId)
    .eq("user_id", userId)
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not update the opportunity.", error?.code ?? "NO_OWNED_ROW");
  return mapOpportunity(data as Row);
}

/**
 * Moves an opportunity to `toStatus` and appends one `status_changed` event,
 * atomically, via the `transition_opportunity` RPC. Use this instead of
 * updating `status` directly so the summary row and its history never
 * disagree.
 */
export async function transitionOpportunity(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
  toStatus: OpportunityStatus,
  options?: OpportunityLifecycleEventOptions,
): Promise<Opportunity> {
  const { error } = await supabase.rpc("transition_opportunity", {
    p_opportunity_id: opportunityId,
    p_to_status: toStatus,
    p_occurred_at: options?.occurredAt ?? null,
    p_note: options?.note ?? null,
    p_metadata: options?.metadata ?? {},
  });
  if (error) throw new RepositoryError("Could not update the opportunity status.", error.code ?? "NO_OWNED_ROW");
  const opportunity = await getOpportunity(supabase, userId, opportunityId);
  if (!opportunity) throw new RepositoryError("Could not reload the opportunity after its status change.", "NO_OWNED_ROW");
  return opportunity;
}

/**
 * Sets `next_interview_at` and appends one `interview_scheduled` event,
 * atomically, via the `schedule_opportunity_interview` RPC. The RPC also
 * moves a `considering`/`applied` opportunity into `interviewing`; it never
 * moves an `offer`, `rejected`, `withdrawn`, or `closed` opportunity back.
 *
 * NOTE: `options?.occurredAt` is accepted but silently ignored here -- the
 * `schedule_opportunity_interview` SQL function has no `p_occurred_at`
 * parameter, this call site does not pass one, and the RPC hardcodes the
 * appended event's `occurred_at` to `now()`. Callers supplying `occurredAt`
 * get no error and no effect; only `note`/`metadata` reach the RPC. Compare
 * {@link transitionOpportunity}, whose RPC does accept `p_occurred_at`.
 */
export async function scheduleOpportunityInterview(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
  interviewAt: string,
  options?: OpportunityLifecycleEventOptions,
): Promise<Opportunity> {
  const { error } = await supabase.rpc("schedule_opportunity_interview", {
    p_opportunity_id: opportunityId,
    p_interview_at: interviewAt,
    p_note: options?.note ?? null,
    p_metadata: options?.metadata ?? {},
  });
  if (error) throw new RepositoryError("Could not schedule the opportunity interview.", error.code ?? "NO_OWNED_ROW");
  const opportunity = await getOpportunity(supabase, userId, opportunityId);
  if (!opportunity) throw new RepositoryError("Could not reload the opportunity after scheduling the interview.", "NO_OWNED_ROW");
  return opportunity;
}

/**
 * Appends one `note` event to an opportunity's history and returns it.
 *
 * This is the only opportunity-event kind a caller creates directly --
 * `created`, `status_changed`, and `interview_scheduled` events are always
 * emitted by the atomic RPCs above, never inserted here. There is
 * deliberately no update or delete: like every other opportunity event, a
 * note is append-only once written.
 */
export async function addOpportunityNote(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
  note: string,
): Promise<OpportunityEvent> {
  const trimmed = note.trim();
  if (!trimmed) throw new RepositoryError("A note cannot be empty.", "INVALID_NOTE");
  const { data, error } = await supabase
    .from("opportunity_events")
    .insert({
      user_id: userId,
      opportunity_id: opportunityId,
      event_type: "note",
      note: trimmed,
      metadata: {},
    })
    .select("*")
    .maybeSingle();
  if (error || !data) throw new RepositoryError("Could not add the note.", error?.code ?? "NO_OWNED_ROW");
  return mapOpportunityEvent(data as Row);
}

/** Lists an opportunity's append-only history, most recent first. */
export async function listOpportunityEvents(
  supabase: SupabaseClient,
  userId: string,
  opportunityId: string,
): Promise<OpportunityEvent[]> {
  const { data, error } = await supabase
    .from("opportunity_events")
    .select("*")
    .eq("user_id", userId)
    .eq("opportunity_id", opportunityId)
    .order("occurred_at", { ascending: false });
  if (error) throw new RepositoryError("Could not load the opportunity history.", error.code);
  return ((data ?? []) as Row[]).map(mapOpportunityEvent);
}

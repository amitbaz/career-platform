"use client";

import { FormEvent, useEffect, useMemo, useState } from "react";
import type {
  CreateOpportunityRequest,
  OpportunityTransitionOptions,
  ScheduleOpportunityInterviewOptions,
} from "@/app/api-client";
import type { Opportunity, OpportunityEvent, OpportunityStatus, PracticePlan, UpdateOpportunityDetailsInput } from "@/lib/types";

/**
 * Props for {@link ApplicationsView}. Follows the shell's mutation-callback
 * pattern (mirrors {@link import("./home-view").HomeViewProps}): this view
 * is presentational and prop-driven, never calling `fetch` or `api-client`
 * itself. Every mutation callback resolves to the server's updated record
 * (never `void`) so the view can reflect the authoritative result -- e.g.
 * selecting a freshly created opportunity, or showing the exact
 * `nextInterviewAt` the server accepted -- without re-deriving it
 * client-side.
 */
export type ApplicationsViewProps = {
  opportunities: Opportunity[];
  busy: boolean;
  onCreate: (input: CreateOpportunityRequest) => Promise<Opportunity>;
  onUpdate: (opportunityId: string, input: UpdateOpportunityDetailsInput) => Promise<Opportunity>;
  onTransition: (opportunityId: string, toStatus: OpportunityStatus, options?: OpportunityTransitionOptions) => Promise<Opportunity>;
  onScheduleInterview: (opportunityId: string, interviewAt: string, options?: ScheduleOpportunityInterviewOptions) => Promise<Opportunity>;
  onAddNote: (opportunityId: string, note: string) => Promise<OpportunityEvent>;
  /** Loads one opportunity's real, persisted, append-only event history (its timeline). Never fabricated client-side. */
  onLoadEvents: (opportunityId: string) => Promise<OpportunityEvent[]>;
  /**
   * The dashboard's already-loaded `CareerDashboard.recentPracticePlans` --
   * no new fetch. Capped at 20 (Task 4's bounded listing), most recent
   * first, so an opportunity's older plans may not appear here; the "Recent
   * practice" heading and copy say so explicitly rather than implying a
   * complete history.
   */
  recentPracticePlans: PracticePlan[];
};

/** Lifecycle states whose outcome is settled -- shown only under the "Terminal" filter. */
const TERMINAL_STATUSES = new Set<OpportunityStatus>(["offer", "rejected", "withdrawn", "closed"]);

type ListFilter = "active" | "terminal";

type EditDraft = {
  company: string;
  role: string;
  location: string;
  jobUrl: string;
  jobDescription: string;
  notes: string;
};

function draftFromOpportunity(opportunity: Opportunity): EditDraft {
  return {
    company: opportunity.company,
    role: opportunity.role,
    location: opportunity.location ?? "",
    jobUrl: opportunity.jobUrl ?? "",
    jobDescription: opportunity.jobDescription ?? "",
    notes: opportunity.notes ?? "",
  };
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** Human-readable summary of one persisted `OpportunityEvent`, covering every `OpportunityEventType`. */
function describeEvent(event: OpportunityEvent): string {
  switch (event.eventType) {
    case "created":
      return "Application created";
    case "status_changed":
      return event.fromStatus ? `Status changed from ${event.fromStatus} to ${event.toStatus}` : `Status set to ${event.toStatus}`;
    case "interview_scheduled":
      return "Interview scheduled";
    case "interview_completed":
      return "Interview completed";
    case "source_updated":
      return "Source details updated";
    case "note":
      return event.note ?? "Note";
  }
}

const inputClass = "mt-1 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-2 text-sm outline-none focus:border-[var(--pine)]";
const labelClass = "block text-sm font-semibold";
const primaryButtonClass = "rounded-full bg-[var(--pine)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50";
const secondaryButtonClass = "rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-50";

/**
 * Applications: the full opportunity lifecycle surface (design section 8.1).
 * Mobile-first list of every opportunity plus a selected detail/editor pane
 * -- the job description and every mutating action live in the detail pane,
 * never the list row. Terminal transitions (`offer`/`rejected`/`withdrawn`/
 * `closed`) are separate labeled buttons rather than a single status picker,
 * so a terminal move is always a deliberate, named action. `nextInterviewAt`
 * (the opportunity's own scheduled-interview field) and a note event's own
 * `occurredAt` (when the note was recorded) are rendered as distinct facts
 * in the timeline -- never conflated, per the release's binding UX ruling.
 */
export function ApplicationsView({ opportunities, busy, onCreate, onUpdate, onTransition, onScheduleInterview, onAddNote, onLoadEvents, recentPracticePlans }: ApplicationsViewProps) {
  const [filter, setFilter] = useState<ListFilter>("active");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [createCompany, setCreateCompany] = useState("");
  const [createRole, setCreateRole] = useState("");
  const [createAlreadyApplied, setCreateAlreadyApplied] = useState(false);

  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [interviewAt, setInterviewAt] = useState("");
  const [noteText, setNoteText] = useState("");
  const [events, setEvents] = useState<OpportunityEvent[]>([]);
  const [eventsError, setEventsError] = useState<string | null>(null);

  const filtered = useMemo(
    () => opportunities.filter((item) => (filter === "terminal") === TERMINAL_STATUSES.has(item.status)),
    [opportunities, filter],
  );
  const selected = selectedId ? opportunities.find((item) => item.id === selectedId) ?? null : null;
  // `recentPracticePlans` is already the dashboard's bounded (most-recent-20)
  // list -- no new fetch. Matched by the plan's own `opportunities` links,
  // never by any other field, so a plan never appears under an opportunity
  // it isn't actually linked to.
  const associatedPlans = selected
    ? recentPracticePlans.filter((plan) => plan.opportunities.some((link) => link.opportunityId === selected.id))
    : [];

  // Loads the real, persisted event history whenever the selected opportunity
  // changes -- never fabricated client-side (see `onLoadEvents` doc comment).
  // Every `setEvents`/`setEventsError` call here happens inside the promise's
  // `then`/`catch`, never synchronously in the effect body: `selectOpportunity`
  // below clears stale events immediately on selection instead, so this never
  // needs to call setState outside the async continuation.
  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    onLoadEvents(selectedId).then((loaded) => {
      if (active) { setEvents(loaded); setEventsError(null); }
    }).catch((caught) => {
      if (active) setEventsError(caught instanceof Error ? caught.message : "Could not load this application's history.");
    });
    return () => { active = false; };
  }, [selectedId, onLoadEvents]);

  function selectOpportunity(opportunity: Opportunity) {
    setSelectedId(opportunity.id);
    setCreating(false);
    setEditing(false);
    setError(null);
    setInterviewAt("");
    setNoteText("");
    setEvents([]);
    setEventsError(null);
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const created = await onCreate({
        company: createCompany,
        role: createRole,
        ...(createAlreadyApplied ? { initialStatus: "applied" as const } : {}),
      });
      setCreating(false);
      setCreateCompany("");
      setCreateRole("");
      setCreateAlreadyApplied(false);
      setSelectedId(created.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not add that application.");
    }
  }

  async function submitEdit(event: FormEvent) {
    event.preventDefault();
    if (!selected || !editDraft) return;
    setError(null);
    try {
      await onUpdate(selected.id, {
        company: editDraft.company,
        role: editDraft.role,
        location: editDraft.location || null,
        jobUrl: editDraft.jobUrl || null,
        jobDescription: editDraft.jobDescription || null,
        notes: editDraft.notes || null,
      });
      setEditing(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save those changes.");
    }
  }

  async function transition(toStatus: OpportunityStatus) {
    if (!selected) return;
    setError(null);
    try {
      await onTransition(selected.id, toStatus, {});
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update that application's status.");
    }
  }

  async function submitSchedule(event: FormEvent) {
    event.preventDefault();
    if (!selected || !interviewAt) return;
    setError(null);
    try {
      await onScheduleInterview(selected.id, new Date(interviewAt).toISOString(), {});
      setInterviewAt("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not schedule that interview.");
    }
  }

  async function submitNote(event: FormEvent) {
    event.preventDefault();
    if (!selected || !noteText.trim()) return;
    setError(null);
    try {
      await onAddNote(selected.id, noteText.trim());
      setNoteText("");
      // Re-fetch the real persisted history rather than fabricating a local
      // entry -- the server assigns the note's id and its authoritative `occurredAt`.
      const refreshed = await onLoadEvents(selected.id);
      setEvents(refreshed);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save that note.");
    }
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-3xl font-semibold tracking-[-.04em]">Applications</h1>
        <button
          onClick={() => { setCreating(true); setSelectedId(null); setEditing(false); setError(null); }}
          className={primaryButtonClass}
        >
          Add application
        </button>
      </div>

      {error && <div role="alert" className="mt-4 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-4 py-3 text-sm text-[#8e3226]">{error}</div>}

      <div className="mt-5 flex gap-2">
        <button
          onClick={() => setFilter("active")}
          aria-pressed={filter === "active"}
          className={filter === "active" ? primaryButtonClass : secondaryButtonClass}
        >
          Active
        </button>
        <button
          onClick={() => setFilter("terminal")}
          aria-pressed={filter === "terminal"}
          className={filter === "terminal" ? primaryButtonClass : secondaryButtonClass}
        >
          Terminal
        </button>
      </div>

      {creating && (
        <form onSubmit={submitCreate} className="mt-5 space-y-4 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5">
          <label className={labelClass}>
            Company
            <input required value={createCompany} onChange={(event) => setCreateCompany(event.target.value)} className={inputClass} />
          </label>
          <label className={labelClass}>
            Role
            <input required value={createRole} onChange={(event) => setCreateRole(event.target.value)} className={inputClass} />
          </label>
          <label className="flex items-center gap-2 text-sm font-semibold">
            <input type="checkbox" checked={createAlreadyApplied} onChange={(event) => setCreateAlreadyApplied(event.target.checked)} />
            I&apos;ve already applied
          </label>
          <div className="flex gap-3">
            <button disabled={busy} className={primaryButtonClass}>Save application</button>
            <button type="button" onClick={() => setCreating(false)} className={secondaryButtonClass}>Cancel</button>
          </div>
        </form>
      )}

      <div className="mt-5 grid gap-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <ul className="space-y-2">
          {filtered.length === 0 && (
            <li className="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm text-[var(--ink-muted)]">
              {filter === "active" ? "No applications need attention right now." : "No terminal applications yet."}
            </li>
          )}
          {filtered.map((item) => (
            <li key={item.id}>
              <button
                onClick={() => selectOpportunity(item)}
                aria-current={selectedId === item.id}
                className={`w-full rounded-2xl border p-4 text-left ${selectedId === item.id ? "border-[var(--pine)] bg-[#eef3e7]" : "border-[var(--line)] bg-[var(--paper)]"}`}
              >
                <p className="font-semibold">{item.company}</p>
                <p className="text-sm text-[var(--ink-muted)]">{item.role}</p>
                <p className="mt-1 text-xs uppercase tracking-[.1em] text-[var(--ink-muted)]">{item.status}</p>
              </button>
            </li>
          ))}
        </ul>

        {selected && (
          <div className="rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5">
            {!editing ? (
              <>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <h2 className="text-2xl font-semibold">{selected.company}</h2>
                    <p className="text-[var(--ink-muted)]">{selected.role}</p>
                    {selected.location && <p className="mt-1 text-sm text-[var(--ink-muted)]">{selected.location}</p>}
                    {selected.jobUrl && (
                      <a
                        href={selected.jobUrl}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="mt-1 inline-block break-all text-sm font-semibold text-[var(--pine)] underline"
                      >
                        Open job posting
                      </a>
                    )}
                  </div>
                  <button onClick={() => { setEditDraft(draftFromOpportunity(selected)); setEditing(true); }} className={secondaryButtonClass}>
                    Edit details
                  </button>
                </div>
                {selected.jobDescription && (
                  <p className="mt-4 whitespace-pre-wrap leading-6 text-[var(--ink-muted)]">{selected.jobDescription}</p>
                )}

                <div className="mt-6 flex flex-wrap gap-2">
                  {selected.status === "considering" && (
                    <button disabled={busy} onClick={() => transition("applied")} className={secondaryButtonClass}>Mark as applied</button>
                  )}
                  {(selected.status === "considering" || selected.status === "applied") && (
                    <button disabled={busy} onClick={() => transition("interviewing")} className={secondaryButtonClass}>Mark as interviewing</button>
                  )}
                  {!TERMINAL_STATUSES.has(selected.status) && (
                    <>
                      <button disabled={busy} onClick={() => transition("offer")} className={secondaryButtonClass}>Mark as offer</button>
                      <button disabled={busy} onClick={() => transition("rejected")} className={secondaryButtonClass}>Mark as rejected</button>
                      <button disabled={busy} onClick={() => transition("withdrawn")} className={secondaryButtonClass}>Withdraw</button>
                      <button disabled={busy} onClick={() => transition("closed")} className={secondaryButtonClass}>Close</button>
                    </>
                  )}
                </div>

                {!TERMINAL_STATUSES.has(selected.status) && (
                  <form onSubmit={submitSchedule} className="mt-6 space-y-3 border-t border-[var(--line)] pt-5">
                    <label className={labelClass}>
                      Interview date and time
                      <input
                        type="datetime-local"
                        value={interviewAt}
                        onChange={(event) => setInterviewAt(event.target.value)}
                        className={inputClass}
                      />
                    </label>
                    <button disabled={busy || !interviewAt} className={secondaryButtonClass}>Schedule interview</button>
                  </form>
                )}

                <section aria-label="Timeline" className="mt-6 border-t border-[var(--line)] pt-5">
                  <h3 className="text-sm font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">Timeline</h3>
                  <ul className="mt-3 space-y-2 text-sm leading-6 text-[var(--ink-muted)]">
                    <li>Created {formatDateTime(selected.createdAt)}</li>
                    {selected.appliedAt && <li>Applied {formatDateTime(selected.appliedAt)}</li>}
                    {selected.nextInterviewAt && <li className="font-semibold text-[#38502e]">Next interview {formatDateTime(selected.nextInterviewAt)}</li>}
                  </ul>
                  <h4 className="mt-4 text-xs font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">History</h4>
                  {eventsError && <p role="alert" className="mt-2 text-sm text-[#8e3226]">{eventsError}</p>}
                  <ul className="mt-2 space-y-2 text-sm leading-6 text-[var(--ink-muted)]">
                    {events.length === 0 && !eventsError && <li>No history recorded yet.</li>}
                    {events.map((event) => (
                      <li key={event.id}>{describeEvent(event)} <span className="text-xs">({formatDateTime(event.occurredAt)})</span></li>
                    ))}
                  </ul>
                  <form onSubmit={submitNote} className="mt-4 space-y-3">
                    <label className={labelClass}>
                      Add a note
                      <textarea value={noteText} onChange={(event) => setNoteText(event.target.value)} className={`${inputClass} min-h-20`} />
                    </label>
                    <button disabled={busy || !noteText.trim()} className={secondaryButtonClass}>Save note</button>
                  </form>
                </section>

                <section aria-label="Recent practice" className="mt-6 border-t border-[var(--line)] pt-5">
                  <h3 className="text-sm font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">Recent practice</h3>
                  <p className="mt-1 text-xs leading-5 text-[var(--ink-muted)]">
                    Showing your most recent practice history -- older sessions may not appear here.
                  </p>
                  {associatedPlans.length === 0 ? (
                    <p className="mt-3 text-sm text-[var(--ink-muted)]">No recent practice sessions are linked to this application yet.</p>
                  ) : (
                    <ul className="mt-3 space-y-2 text-sm leading-6">
                      {associatedPlans.map((plan) => (
                        <li key={plan.id} className="rounded-2xl bg-[#f3f5ef] p-3">
                          <p className="font-semibold">{plan.primaryFocus}</p>
                          <p className="text-xs uppercase tracking-[.1em] text-[var(--ink-muted)]">
                            {plan.status}{plan.estimatedMinutes ? ` · ${plan.estimatedMinutes} min` : ""}
                          </p>
                        </li>
                      ))}
                    </ul>
                  )}
                </section>
              </>
            ) : editDraft && (
              <form onSubmit={submitEdit} className="space-y-4">
                <label className={labelClass}>
                  Company
                  <input required value={editDraft.company} onChange={(event) => setEditDraft({ ...editDraft, company: event.target.value })} className={inputClass} />
                </label>
                <label className={labelClass}>
                  Role
                  <input required value={editDraft.role} onChange={(event) => setEditDraft({ ...editDraft, role: event.target.value })} className={inputClass} />
                </label>
                <label className={labelClass}>
                  Location
                  <input value={editDraft.location} onChange={(event) => setEditDraft({ ...editDraft, location: event.target.value })} className={inputClass} />
                </label>
                <label className={labelClass}>
                  Job URL
                  <input value={editDraft.jobUrl} onChange={(event) => setEditDraft({ ...editDraft, jobUrl: event.target.value })} className={inputClass} />
                </label>
                <label className={labelClass}>
                  Job description
                  <textarea value={editDraft.jobDescription} onChange={(event) => setEditDraft({ ...editDraft, jobDescription: event.target.value })} className={`${inputClass} min-h-32`} />
                </label>
                <label className={labelClass}>
                  Notes
                  <textarea value={editDraft.notes} onChange={(event) => setEditDraft({ ...editDraft, notes: event.target.value })} className={`${inputClass} min-h-20`} />
                </label>
                <div className="flex gap-3">
                  <button disabled={busy} className={primaryButtonClass}>Save changes</button>
                  <button type="button" onClick={() => setEditing(false)} className={secondaryButtonClass}>Cancel</button>
                </div>
              </form>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

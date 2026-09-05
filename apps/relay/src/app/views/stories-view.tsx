"use client";

import { FormEvent, useState } from "react";
import type { CreateCareerStoryRequest, UpdateCareerStoryRequest } from "@/app/api-client";
import type { CareerStory, CareerStoryEvidence, CareerStoryReviewState, CareerStorySummary, EvidenceItem } from "@/lib/types";

/**
 * Props for {@link StoriesView}. Follows the shell's mutation-callback
 * pattern (mirrors {@link import("./applications-view").ApplicationsViewProps}):
 * presentational and prop-driven, never calling `fetch` or `api-client`
 * itself. `stories` is the caller's FULL story list, retired ones included
 * (design section 4.3 preserves retired rows for their provenance) -- this
 * view is what filters the default list to draft/confirmed (R16 from the
 * task-10 brief), never the server.
 */
export type StoriesViewProps = {
  stories: CareerStorySummary[];
  /** The signed-in caller's current profile evidence, offered as attachable provenance. */
  profileEvidence: EvidenceItem[];
  busy: boolean;
  onCreate: (input: CreateCareerStoryRequest) => Promise<CareerStory>;
  onUpdate: (storyId: string, input: UpdateCareerStoryRequest) => Promise<CareerStory>;
  onConfirm: (storyId: string) => Promise<CareerStory>;
  onRetire: (storyId: string) => Promise<CareerStory>;
  onAttachProfileEvidence: (storyId: string, profileEvidenceId: string, note?: string | null) => Promise<CareerStoryEvidence>;
};

/** Review states shown in the default list -- retired stories are preserved (never deleted) but hidden here, per design section 4.3. */
const VISIBLE_REVIEW_STATES = new Set<CareerStoryReviewState>(["draft", "confirmed"]);

/**
 * The six factual dimensions `careerStoryCompleteness` (`src/lib/career-story.ts`)
 * scores, in its exact grouping, used only to label and order this form's
 * fields -- the actual 0-1 score is always server-computed, never
 * recalculated here.
 */
const DIMENSION_GROUPS: { legend: string; fields: { key: keyof StoryDraftText; label: string }[] }[] = [
  { legend: "Situation / problem", fields: [{ key: "situation", label: "Situation" }, { key: "problem", label: "Problem" }] },
  { legend: "Responsibility / ownership", fields: [{ key: "responsibility", label: "Responsibility" }, { key: "ownership", label: "Ownership" }] },
  { legend: "Actions / decisions", fields: [{ key: "actions", label: "Actions / decisions" }] },
  { legend: "Alternatives / tradeoffs", fields: [{ key: "alternatives", label: "Alternatives considered" }, { key: "tradeoffs", label: "Tradeoffs" }] },
  { legend: "Outcome", fields: [{ key: "outcome", label: "Outcome" }] },
  { legend: "Lessons", fields: [{ key: "lessons", label: "Lessons" }] },
];

type StoryDraftText = {
  situation: string;
  problem: string;
  responsibility: string;
  ownership: string;
  actions: string;
  alternatives: string;
  tradeoffs: string;
  outcome: string;
  lessons: string;
};

type StoryDraft = StoryDraftText & {
  title: string;
  tags: string;
};

function emptyDraft(): StoryDraft {
  return { title: "", situation: "", problem: "", responsibility: "", ownership: "", actions: "", alternatives: "", tradeoffs: "", outcome: "", lessons: "", tags: "" };
}

function draftFromStory(story: CareerStorySummary): StoryDraft {
  return {
    title: story.title,
    situation: story.situation ?? "",
    problem: story.problem ?? "",
    responsibility: story.responsibility ?? "",
    ownership: story.ownership ?? "",
    actions: story.actions ?? "",
    alternatives: story.alternatives ?? "",
    tradeoffs: story.tradeoffs ?? "",
    outcome: story.outcome ?? "",
    lessons: story.lessons ?? "",
    tags: story.tags.join(", "),
  };
}

/** Every draft text field, cleared to `null` (never sent as an empty string) -- the shape both create and update requests expect. */
function draftTextFields(draft: StoryDraft): Record<keyof StoryDraftText, string | null> {
  return {
    situation: draft.situation.trim() || null,
    problem: draft.problem.trim() || null,
    responsibility: draft.responsibility.trim() || null,
    ownership: draft.ownership.trim() || null,
    actions: draft.actions.trim() || null,
    alternatives: draft.alternatives.trim() || null,
    tradeoffs: draft.tradeoffs.trim() || null,
    outcome: draft.outcome.trim() || null,
    lessons: draft.lessons.trim() || null,
  };
}

function draftTags(draft: StoryDraft): string[] {
  return draft.tags.split(",").map((item) => item.trim()).filter(Boolean);
}

const REVIEW_STATE_LABELS: Record<CareerStoryReviewState, string> = {
  draft: "Draft",
  confirmed: "Confirmed",
  retired: "Retired",
};

const inputClass = "mt-1 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-2 text-sm outline-none focus:border-[var(--pine)]";
const textAreaClass = `${inputClass} min-h-20`;
const labelClass = "block text-sm font-semibold";
const primaryButtonClass = "rounded-full bg-[var(--pine)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50";
const secondaryButtonClass = "rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-50";

/**
 * Stories: the story bank surface (design section 4.3). Lists active
 * draft/confirmed stories, supports creating and editing the nine narrative
 * fields grouped into the six factual dimensions `careerStoryCompleteness`
 * scores, confirming, retiring (a state change that preserves the row and
 * its provenance, never a delete), and attaching current profile evidence as
 * provenance. Completeness is always shown as FACTUAL COVERAGE, never as a
 * judgment of delivery or answer quality -- that distinction belongs to the
 * observation/evaluation loop, not this view (design section 4.3).
 */
export function StoriesView({ stories, profileEvidence, busy, onCreate, onUpdate, onConfirm, onRetire, onAttachProfileEvidence }: StoriesViewProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<StoryDraft>(emptyDraft());
  const [evidenceSelection, setEvidenceSelection] = useState("");
  const [evidenceNote, setEvidenceNote] = useState("");

  const visible = stories.filter((item) => VISIBLE_REVIEW_STATES.has(item.reviewState));
  const selected = selectedId ? visible.find((item) => item.id === selectedId) ?? null : null;

  function selectStory(story: CareerStorySummary) {
    setSelectedId(story.id);
    setCreating(false);
    setEditing(false);
    setError(null);
    setEvidenceSelection("");
    setEvidenceNote("");
  }

  function updateField(key: keyof StoryDraftText, value: string) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  async function submitCreate(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const created = await onCreate({ title: draft.title, ...draftTextFields(draft), tags: draftTags(draft) });
      setCreating(false);
      setDraft(emptyDraft());
      setSelectedId(created.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save that story.");
    }
  }

  async function submitEdit(event: FormEvent) {
    event.preventDefault();
    if (!selected) return;
    setError(null);
    try {
      await onUpdate(selected.id, { title: draft.title, ...draftTextFields(draft), tags: draftTags(draft) });
      setEditing(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save those changes.");
    }
  }

  async function confirm() {
    if (!selected) return;
    setError(null);
    try {
      await onConfirm(selected.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not confirm that story.");
    }
  }

  async function retire() {
    if (!selected) return;
    setError(null);
    try {
      await onRetire(selected.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not retire that story.");
    }
  }

  async function submitAttachEvidence(event: FormEvent) {
    event.preventDefault();
    if (!selected || !evidenceSelection) return;
    setError(null);
    try {
      await onAttachProfileEvidence(selected.id, evidenceSelection, evidenceNote.trim() || undefined);
      setEvidenceSelection("");
      setEvidenceNote("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not attach that evidence.");
    }
  }

  const coveredDimensions = selected ? Math.round(selected.completeness * 6) : 0;

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-3xl font-semibold tracking-[-.04em]">Story bank</h1>
        <button
          onClick={() => { setDraft(emptyDraft()); setCreating(true); setSelectedId(null); setEditing(false); setError(null); }}
          className={primaryButtonClass}
        >
          New story
        </button>
      </div>
      <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--ink-muted)]">
        Structured, source-backed examples you can reuse across interviews. Relay never invents or auto-extracts a story from an answer -- you draft it and confirm it yourself.
      </p>

      {error && <div role="alert" className="mt-4 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-4 py-3 text-sm text-[#8e3226]">{error}</div>}

      {creating && (
        <form onSubmit={submitCreate} className="mt-5 space-y-4 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5">
          <label className={labelClass} htmlFor="story-title">
            Title
            <input id="story-title" required value={draft.title} onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))} className={inputClass} />
          </label>
          {DIMENSION_GROUPS.map((group) => (
            <fieldset key={group.legend} className="rounded-2xl border border-[var(--line)] p-4">
              <legend className="px-1 text-xs font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">{group.legend}</legend>
              <div className="space-y-3">
                {group.fields.map((field) => (
                  <label key={field.key} className={labelClass} htmlFor={`story-${field.key}`}>
                    {field.label}
                    <textarea id={`story-${field.key}`} value={draft[field.key]} onChange={(event) => updateField(field.key, event.target.value)} className={textAreaClass} />
                  </label>
                ))}
              </div>
            </fieldset>
          ))}
          <label className={labelClass} htmlFor="story-tags">
            Tags <span className="font-normal text-[var(--ink-muted)]">(comma separated)</span>
            <input id="story-tags" value={draft.tags} onChange={(event) => setDraft((current) => ({ ...current, tags: event.target.value }))} className={inputClass} />
          </label>
          <div className="flex gap-3">
            <button disabled={busy} className={primaryButtonClass}>Save story</button>
            <button type="button" onClick={() => setCreating(false)} className={secondaryButtonClass}>Cancel</button>
          </div>
        </form>
      )}

      <div className="mt-5 grid gap-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <ul className="space-y-2">
          {visible.length === 0 && (
            <li className="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4 text-sm text-[var(--ink-muted)]">
              You haven&apos;t added any career stories yet.
            </li>
          )}
          {visible.map((item) => (
            <li key={item.id}>
              <button
                onClick={() => selectStory(item)}
                aria-current={selectedId === item.id}
                className={`w-full rounded-2xl border p-4 text-left ${selectedId === item.id ? "border-[var(--pine)] bg-[#eef3e7]" : "border-[var(--line)] bg-[var(--paper)]"}`}
              >
                <p className="font-semibold">{item.title}</p>
                <p className="mt-1 text-xs uppercase tracking-[.1em] text-[var(--ink-muted)]">{REVIEW_STATE_LABELS[item.reviewState]} · {Math.round(item.completeness * 6)} of 6 dimensions</p>
              </button>
            </li>
          ))}
        </ul>

        {selected && !editing && (
          <div className="rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-2xl font-semibold">{selected.title}</h2>
                <p className="text-sm text-[var(--ink-muted)]">{REVIEW_STATE_LABELS[selected.reviewState]}{selected.confirmedAt ? ` · confirmed ${new Date(selected.confirmedAt).toLocaleDateString()}` : ""}</p>
              </div>
              <button onClick={() => { setDraft(draftFromStory(selected)); setEditing(true); setCreating(false); }} className={secondaryButtonClass}>
                Edit story
              </button>
            </div>

            <div className="mt-4 rounded-2xl bg-[#f3f5ef] p-4">
              <p className="text-2xl font-semibold">{coveredDimensions} of 6 factual dimensions covered</p>
              <p className="mt-1 text-sm leading-6 text-[var(--ink-muted)]">
                This reflects factual coverage only -- whether the six structural facts of the story are filled in. It never grades delivery: telling the story well in practice is a separate signal Relay tracks elsewhere.
              </p>
            </div>

            {selected.tags.length > 0 && (
              <div className="mt-4 flex flex-wrap gap-2">
                {selected.tags.map((tag) => <span key={tag} className="rounded-full bg-[#edf0e8] px-3 py-1.5 text-sm">{tag}</span>)}
              </div>
            )}

            <div className="mt-5 space-y-4 text-sm leading-6">
              {DIMENSION_GROUPS.flatMap((group) => group.fields).map((field) => selected[field.key] && (
                <div key={field.key}>
                  <p className="text-xs font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">{field.label}</p>
                  <p className="mt-1 whitespace-pre-wrap text-[var(--ink-muted)]">{selected[field.key]}</p>
                </div>
              ))}
            </div>

            <div className="mt-6 flex flex-wrap gap-2 border-t border-[var(--line)] pt-5">
              {selected.reviewState === "draft" && (
                <button disabled={busy} onClick={confirm} className={secondaryButtonClass}>Confirm story</button>
              )}
              <button disabled={busy} onClick={retire} className={secondaryButtonClass}>Retire story</button>
            </div>

            <section aria-label="Provenance" className="mt-6 border-t border-[var(--line)] pt-5">
              <h3 className="text-sm font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">Provenance</h3>
              <p className="mt-2 text-sm text-[var(--ink-muted)]">
                {selected.evidenceCount} attached evidence item{selected.evidenceCount === 1 ? "" : "s"}
              </p>
              {profileEvidence.length === 0 ? (
                <p className="mt-3 text-sm text-[var(--ink-muted)]">Your profile has no evidence to attach yet.</p>
              ) : (
                <form onSubmit={submitAttachEvidence} className="mt-3 space-y-3">
                  <label className={labelClass} htmlFor="attach-evidence">
                    Attach profile evidence
                    <select id="attach-evidence" value={evidenceSelection} onChange={(event) => setEvidenceSelection(event.target.value)} className={inputClass}>
                      <option value="">Choose evidence…</option>
                      {profileEvidence.map((item) => (
                        <option key={item.id} value={item.id}>
                          {(item.projectOrEmployer ?? item.ownership ?? item.sourceExcerpt).slice(0, 80)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className={labelClass} htmlFor="attach-evidence-note">
                    Note <span className="font-normal text-[var(--ink-muted)]">(optional)</span>
                    <input id="attach-evidence-note" value={evidenceNote} onChange={(event) => setEvidenceNote(event.target.value)} className={inputClass} />
                  </label>
                  <button disabled={busy || !evidenceSelection} className={secondaryButtonClass}>Attach evidence</button>
                </form>
              )}
            </section>
          </div>
        )}

        {selected && editing && (
          <form onSubmit={submitEdit} className="space-y-4 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5">
            <label className={labelClass} htmlFor="story-edit-title">
              Title
              <input id="story-edit-title" required value={draft.title} onChange={(event) => setDraft((current) => ({ ...current, title: event.target.value }))} className={inputClass} />
            </label>
            {DIMENSION_GROUPS.map((group) => (
              <fieldset key={group.legend} className="rounded-2xl border border-[var(--line)] p-4">
                <legend className="px-1 text-xs font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">{group.legend}</legend>
                <div className="space-y-3">
                  {group.fields.map((field) => (
                    <label key={field.key} className={labelClass} htmlFor={`story-edit-${field.key}`}>
                      {field.label}
                      <textarea id={`story-edit-${field.key}`} value={draft[field.key]} onChange={(event) => updateField(field.key, event.target.value)} className={textAreaClass} />
                    </label>
                  ))}
                </div>
              </fieldset>
            ))}
            <label className={labelClass} htmlFor="story-edit-tags">
              Tags <span className="font-normal text-[var(--ink-muted)]">(comma separated)</span>
              <input id="story-edit-tags" value={draft.tags} onChange={(event) => setDraft((current) => ({ ...current, tags: event.target.value }))} className={inputClass} />
            </label>
            <div className="flex gap-3">
              <button disabled={busy} className={primaryButtonClass}>Save story</button>
              <button type="button" onClick={() => setEditing(false)} className={secondaryButtonClass}>Cancel</button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

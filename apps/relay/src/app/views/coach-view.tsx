"use client";

import { FormEvent, useState } from "react";
import type { CoachObservation, CoachObservationReviewState, CoachObservationSummary, CoachObservationTrend, CoachObservationType } from "@/lib/types";

/**
 * Props for {@link CoachView}. Follows the shell's mutation-callback pattern
 * (mirrors {@link import("./applications-view").ApplicationsViewProps}):
 * presentational and prop-driven, never calling `fetch` or `api-client`
 * itself. `active`/`history` arrive pre-split exactly as `GET
 * /api/observations` returns them -- `history` is the dismissed
 * observations, `active` is everything else -- so this view never needs to
 * filter by review state itself, only render the two lists into their own
 * sections.
 *
 * Reviewing (`confirm`/`correct`/`dismiss`) is the ONLY write this view can
 * make -- Release 2 never creates or reconciles observations from the
 * client (design section 4.4), so there is deliberately no create-observation
 * prop here.
 */
export type CoachViewProps = {
  active: CoachObservationSummary[];
  history: CoachObservationSummary[];
  busy: boolean;
  onConfirm: (observationId: string) => Promise<CoachObservation>;
  onCorrect: (observationId: string, correction: string) => Promise<CoachObservation>;
  onDismiss: (observationId: string) => Promise<CoachObservation>;
};

const TYPE_LABELS: Record<CoachObservationType, string> = {
  strength: "Strength",
  weakness: "Weakness",
  answer_habit: "Answer habit",
  knowledge_gap: "Knowledge gap",
  story_gap: "Story gap",
  story_strength: "Story strength",
  delivery_pattern: "Delivery pattern",
  other: "Other",
};

const TREND_LABELS: Record<CoachObservationTrend, string> = {
  unresolved: "Unresolved",
  improving: "Improving",
  stable: "Stable",
  worsening: "Worsening",
};

/** Mirrors the `REVIEW_STATE_LABELS` pattern in `stories-view.tsx` -- design section 4.4 requires review state as its own displayed field, distinct from trend or type. */
const REVIEW_STATE_LABELS: Record<CoachObservationReviewState, string> = {
  unreviewed: "Unreviewed",
  confirmed: "Confirmed",
  corrected: "Corrected",
  dismissed: "Dismissed",
};

const secondaryButtonClass = "rounded-full border border-[var(--line)] px-3 py-1.5 text-xs font-semibold disabled:opacity-50";
const inputClass = "mt-1 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-2 text-sm outline-none focus:border-[var(--pine)]";

/** One reviewable observation card. Local to {@link CoachView} since neither history nor active rendering needs it outside this file. */
function ObservationCard({
  item,
  busy,
  readOnly,
  onConfirm,
  onCorrect,
  onDismiss,
}: {
  item: CoachObservationSummary;
  busy: boolean;
  readOnly: boolean;
  onConfirm?: (observationId: string) => Promise<CoachObservation>;
  onCorrect?: (observationId: string, correction: string) => Promise<CoachObservation>;
  onDismiss?: (observationId: string) => Promise<CoachObservation>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const [correction, setCorrection] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function confirm() {
    if (!onConfirm) return;
    setError(null);
    try {
      await onConfirm(item.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not confirm that observation.");
    }
  }

  async function dismiss() {
    if (!onDismiss) return;
    setError(null);
    try {
      await onDismiss(item.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not dismiss that observation.");
    }
  }

  async function submitCorrection(event: FormEvent) {
    event.preventDefault();
    if (!onCorrect || !correction.trim()) return;
    setError(null);
    try {
      await onCorrect(item.id, correction.trim());
      setCorrecting(false);
      setCorrection("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not save that correction.");
    }
  }

  return (
    <li className="rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[.1em] text-[var(--ink-muted)]">
            <span>{TYPE_LABELS[item.observationType]}</span> · <span>{TREND_LABELS[item.trend]}</span> · <span>{REVIEW_STATE_LABELS[item.reviewState]}</span>
          </p>
          <p className="mt-1 text-sm leading-6">{item.effectiveText}</p>
        </div>
        <p className="whitespace-nowrap text-xs text-[var(--ink-muted)]">
          {Math.round(item.confidence * 100)}% confidence · {Math.round(item.importance * 100)}% importance
        </p>
      </div>

      <button onClick={() => setExpanded((current) => !current)} aria-expanded={expanded} className="mt-3 text-xs font-semibold text-[var(--pine)]">
        Why does Relay think this?
      </button>
      {expanded && (
        <div className="mt-2 rounded-xl bg-[#f3f5ef] p-3 text-sm leading-6 text-[var(--ink-muted)]">
          <p><span className="font-semibold text-[var(--foreground)]">Original observation:</span> {item.claim}</p>
          {item.evidence.length === 0 ? (
            <p className="mt-2">No supporting evidence is attached yet.</p>
          ) : (
            <ul className="mt-2 space-y-2">
              {item.evidence.map((piece, index) => (
                <li key={`${piece.kind}-${index}`}>
                  <span className="font-semibold text-[var(--foreground)]">{piece.label}</span> <span className="text-xs uppercase tracking-[.08em]">({piece.role})</span>
                  <p className="mt-0.5">{piece.summary}</p>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {error && <div role="alert" className="mt-3 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-3 py-2 text-xs text-[#8e3226]">{error}</div>}

      {!readOnly && (
        <div className="mt-3 flex flex-wrap gap-2">
          {item.reviewState !== "confirmed" && <button disabled={busy} onClick={confirm} className={secondaryButtonClass}>Confirm</button>}
          <button disabled={busy} onClick={() => { setCorrecting((current) => !current); setError(null); }} className={secondaryButtonClass}>Correct</button>
          <button disabled={busy} onClick={dismiss} className={secondaryButtonClass}>Dismiss</button>
        </div>
      )}
      {!readOnly && correcting && (
        <form onSubmit={submitCorrection} className="mt-3 space-y-2">
          <label className="block text-xs font-semibold" htmlFor={`correction-${item.id}`}>
            Your correction
            <textarea id={`correction-${item.id}`} value={correction} onChange={(event) => setCorrection(event.target.value)} className={`${inputClass} min-h-16`} />
          </label>
          <button disabled={busy || !correction.trim()} className={secondaryButtonClass}>Save correction</button>
        </form>
      )}
    </li>
  );
}

/**
 * Coach: the inspectable long-term-memory review surface (design section 4.4).
 * Every observation shows its current effective guidance (the user's
 * correction when corrected, otherwise the original claim), its type, trend,
 * and review state (Unreviewed/Confirmed/Corrected/Dismissed -- so an
 * observation the user has already acted on is visibly distinct from one
 * they haven't), confidence/importance in a restrained secondary
 * presentation, and an expandable `Why does Relay think this?` detail that
 * always keeps the original claim and supporting evidence visible -- even
 * once corrected. Confirm/Correct/Dismiss are the complete review surface
 * (Confirm hides itself once already confirmed, matching
 * `StoriesView`'s "Confirm story"); Release 2 never lets the browser create
 * or reconcile an observation.
 */
export function CoachView({ active, history, busy, onConfirm, onCorrect, onDismiss }: CoachViewProps) {
  const isEmpty = active.length === 0 && history.length === 0;

  return (
    <div>
      <h1 className="text-3xl font-semibold tracking-[-.04em]">Coach</h1>
      <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--ink-muted)]">
        What Relay has noticed about your interview practice. Review each observation so the most accurate wording drives future coaching.
      </p>

      {isEmpty ? (
        <p className="mt-6 max-w-xl rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-5 leading-6 text-[var(--ink-muted)]">
          Relay hasn&apos;t recorded any coaching observations yet. Release 2 does not generate them automatically after a session -- persistent observations appear once evidence-backed learning is introduced.
        </p>
      ) : (
        <>
          <section aria-label="Active observations" className="mt-6">
            <h2 className="text-xl font-semibold">Active</h2>
            {active.length === 0 ? (
              <p className="mt-3 leading-6 text-[var(--ink-muted)]">No active observations right now.</p>
            ) : (
              <ul className="mt-4 space-y-3">
                {active.map((item) => (
                  <ObservationCard key={item.id} item={item} busy={busy} readOnly={false} onConfirm={onConfirm} onCorrect={onCorrect} onDismiss={onDismiss} />
                ))}
              </ul>
            )}
          </section>

          <section aria-label="Observation history" className="mt-8">
            <h2 className="text-xl font-semibold">History</h2>
            {history.length === 0 ? (
              <p className="mt-3 leading-6 text-[var(--ink-muted)]">No dismissed observations yet.</p>
            ) : (
              <ul className="mt-4 space-y-3">
                {history.map((item) => (
                  <ObservationCard key={item.id} item={item} busy={busy} readOnly />
                ))}
              </ul>
            )}
          </section>
        </>
      )}
    </div>
  );
}

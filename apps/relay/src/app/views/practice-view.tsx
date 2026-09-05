"use client";

import { FormEvent, useState } from "react";
import type { ManualPracticeRequest } from "@/app/api-client";
import { profileReadinessCopy } from "@/app/profile-readiness";
import type { CareerDashboard, PracticeFormat } from "@/lib/types";

/**
 * Props for {@link PracticeView}. Follows the shell's mutation-callback
 * pattern (mirrors {@link import("./home-view").HomeViewProps}): this view is
 * presentational and prop-driven, never calling `fetch` or `api-client`
 * itself. `dashboard` is the single canonical read model -- the same one
 * `HomeView` renders from -- so the recommendation summary here is always
 * identical to Home's, never independently derived.
 *
 * Design section 4.5: Practice is "the manual override and practice history
 * surface rather than the primary starting point." `onStartManual` is the
 * ONLY session-start path this view uses for user-chosen practice, INCLUDING
 * the hands-on option -- design sections 4.5/6.2 forbid a second, unrelated
 * session-start architecture, and section 7.4 confirms `hands_on` still
 * resolves to the existing hands-on session flow server-side.
 */
export type PracticeViewProps = {
  dashboard: CareerDashboard;
  busy: boolean;
  onStartRecommended: () => Promise<void>;
  onStartManual: (request: ManualPracticeRequest) => Promise<void>;
};

const FORMAT_LABELS: Record<PracticeFormat, string> = {
  targeted_drill: "Targeted drill",
  story_work: "Story work",
  self_presentation: "Self-presentation",
  behavioral: "Behavioral practice",
  technical_communication: "Technical communication",
  role_prep: "Role prep",
  full_simulation: "Full simulation",
  hands_on: "Hands-on exercise",
};

const FORMAT_OPTIONS = Object.keys(FORMAT_LABELS) as PracticeFormat[];

/** Focus text sent for the one-click hands-on option, which skips the manual form entirely. */
const HANDS_ON_DEFAULT_FOCUS = "Hands-on technical exercise";

const inputClass = "mt-1 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-2 text-sm outline-none focus:border-[var(--pine)]";
const labelClass = "block text-sm font-semibold";
const primaryButtonClass = "rounded-full bg-[var(--pine)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50";
const secondaryButtonClass = "rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-50";

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/**
 * Practice: the manual override and practice history surface (design section
 * 4.5). Shows the current server-computed recommendation (never re-derived
 * client-side), a manual focus/format form that creates the same persisted
 * `PracticePlan` contract as recommended practice, and recent plans/sessions
 * so the user can see what they've already practiced.
 */
export function PracticeView({ dashboard, busy, onStartRecommended, onStartManual }: PracticeViewProps) {
  const { recommendation, opportunities, recentPracticePlans, recentSessions, profile } = dashboard;
  // A sparse profile never disables Start -- it only changes what the first
  // session looks like (broader discovery questions instead of grounded
  // ones). `readinessSparse` gates the guidance copy below, not the CTA.
  const readinessSparse = profile.readiness?.ready === false;
  const readinessReason = readinessSparse ? profileReadinessCopy(profile.readiness) : null;

  const [primaryFocus, setPrimaryFocus] = useState("");
  const [secondaryFocus, setSecondaryFocus] = useState("");
  const [format, setFormat] = useState<PracticeFormat>("targeted_drill");
  const [estimatedMinutes, setEstimatedMinutes] = useState("");
  const [primaryOpportunityId, setPrimaryOpportunityId] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submitManual(event: FormEvent) {
    event.preventDefault();
    if (!primaryFocus.trim()) return;
    setError(null);
    try {
      await onStartManual({
        format,
        primaryFocus: primaryFocus.trim(),
        secondaryFocus: secondaryFocus.trim() || null,
        estimatedMinutes: estimatedMinutes.trim() ? Number(estimatedMinutes) : null,
        primaryOpportunityId: primaryOpportunityId || null,
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start that practice session.");
    }
  }

  async function startHandsOn() {
    setError(null);
    try {
      await onStartManual({ format: "hands_on", primaryFocus: HANDS_ON_DEFAULT_FOCUS });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start a hands-on exercise.");
    }
  }

  async function startRecommended() {
    setError(null);
    try {
      await onStartRecommended();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start recommended practice.");
    }
  }

  return (
    <div>
      <p className="text-sm text-[var(--ink-muted)]">Practice</p>
      <h1 className="mt-1 text-4xl font-semibold tracking-[-.04em]">Choose deliberate practice.</h1>

      {error && <div role="alert" className="mt-4 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-4 py-3 text-sm text-[#8e3226]">{error}</div>}

      <section aria-label="Current recommendation" className="mt-7 rounded-3xl bg-[var(--pine)] p-6 text-white md:p-8">
        <h2 className="text-2xl font-semibold">Currently recommended</h2>
        <p className="mt-3 text-sm uppercase tracking-[.12em] text-[#c8d7cf]">{FORMAT_LABELS[recommendation.format]} · {recommendation.estimatedMinutes} min</p>
        <p className="mt-2 text-xl font-semibold">{recommendation.primaryFocus}</p>
        <p className="mt-4 max-w-2xl leading-6 text-[#dbe7df]">{recommendation.rationale}</p>
        <button onClick={startRecommended} disabled={busy} className="mt-6 rounded-full bg-[var(--lime)] px-5 py-3 text-sm font-semibold text-[#18281f] disabled:opacity-50">
          {busy ? "Starting…" : "Start recommended practice"}
        </button>
        {readinessReason && <p className="mt-3 max-w-md text-sm leading-6 text-[#dbe7df]">{readinessReason}</p>}
      </section>

      <section aria-label="Manual practice" className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6">
        <h2 className="text-xl font-semibold">Choose it yourself</h2>
        <p className="mt-2 text-sm leading-6 text-[var(--ink-muted)]">
          Override the recommendation with your own focus. Manual practice creates the same persisted plan as recommended practice -- there is no separate session-start path.
        </p>
        <form onSubmit={submitManual} className="mt-5 space-y-4">
          <label className={labelClass} htmlFor="practice-focus">
            Focus
            <input id="practice-focus" required value={primaryFocus} onChange={(event) => setPrimaryFocus(event.target.value)} className={inputClass} />
          </label>
          <label className={labelClass} htmlFor="practice-secondary-focus">
            Secondary focus (optional)
            <input id="practice-secondary-focus" value={secondaryFocus} onChange={(event) => setSecondaryFocus(event.target.value)} className={inputClass} />
          </label>
          <label className={labelClass} htmlFor="practice-format">
            Format
            <select id="practice-format" value={format} onChange={(event) => setFormat(event.target.value as PracticeFormat)} className={inputClass}>
              {FORMAT_OPTIONS.map((option) => <option key={option} value={option}>{FORMAT_LABELS[option]}</option>)}
            </select>
          </label>
          <label className={labelClass} htmlFor="practice-minutes">
            Approximate minutes (optional)
            <input id="practice-minutes" type="number" min={1} max={180} value={estimatedMinutes} onChange={(event) => setEstimatedMinutes(event.target.value)} className={inputClass} />
          </label>
          <label className={labelClass} htmlFor="practice-opportunity">
            Opportunity (optional)
            <select id="practice-opportunity" value={primaryOpportunityId} onChange={(event) => setPrimaryOpportunityId(event.target.value)} className={inputClass}>
              <option value="">No specific opportunity</option>
              {opportunities.map((item) => <option key={item.id} value={item.id}>{item.company} · {item.role}</option>)}
            </select>
          </label>
          <button disabled={busy || !primaryFocus.trim()} className={primaryButtonClass}>{busy ? "Starting…" : "Start practice"}</button>
        </form>
      </section>

      <section aria-label="Hands-on practice" className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6">
        <h2 className="text-xl font-semibold">Hands-on exercise</h2>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--ink-muted)]">
          An accessible React + TypeScript product search. Save checkpoints, explain decisions, then receive interviewer-style feedback -- no form to fill in first.
        </p>
        <button onClick={startHandsOn} disabled={busy} className={secondaryButtonClass}>
          {busy ? "Starting…" : "Start hands-on practice"}
        </button>
      </section>

      <section aria-label="Recent practice plans" className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6">
        <h2 className="text-xl font-semibold">Recent practice plans</h2>
        {recentPracticePlans.length === 0 ? (
          <p className="mt-3 leading-6 text-[var(--ink-muted)]">No practice plans yet.</p>
        ) : (
          <ul className="mt-4 space-y-3">
            {recentPracticePlans.map((plan) => (
              <li key={plan.id} className="rounded-2xl bg-[#f3f5ef] p-4">
                <p className="font-semibold">{plan.primaryFocus}</p>
                <p className="mt-1 text-xs uppercase tracking-[.1em] text-[var(--ink-muted)]">
                  {FORMAT_LABELS[plan.format]} · {plan.status}{plan.estimatedMinutes ? ` · ${plan.estimatedMinutes} min` : ""}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-label="Recent sessions" className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6">
        <h2 className="text-xl font-semibold">Recent sessions</h2>
        {recentSessions.length === 0 ? (
          <p className="mt-3 leading-6 text-[var(--ink-muted)]">No completed sessions yet.</p>
        ) : (
          <ul className="mt-4 space-y-3">
            {recentSessions.map((item) => (
              <li key={item.id} className="rounded-2xl bg-[#f3f5ef] p-4">
                <p className="font-semibold">{item.kind === "hands-on" ? "Hands-on session" : "Conversation session"}</p>
                <p className="mt-1 text-xs uppercase tracking-[.1em] text-[var(--ink-muted)]">
                  {item.status}
                  {item.completedAt ? ` · ${formatDate(item.completedAt)}` : ""}
                </p>
                {item.overallScore !== null && <p className="mt-1 text-sm font-semibold">{item.overallScore}/10</p>}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

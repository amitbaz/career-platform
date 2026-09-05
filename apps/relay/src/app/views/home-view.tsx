"use client";

import { profileReadinessCopy } from "@/app/profile-readiness";
import type { CareerDashboard, Opportunity, OpportunityStatus, PracticeFormat, PracticeRecommendationSignal } from "@/lib/types";

/**
 * Props for {@link HomeView}, Relay's Career Brain command center. Verbatim
 * per the Release 2 design contract -- `dashboard` is the single canonical
 * read model (`src/lib/career-dashboard.ts`); `onOpenStories`/`onOpenCoach`
 * exist ahead of their own views (Task 10 fills the `"stories"`/`"coach"`
 * render cases in `relay-shell.tsx`) so the buttons that navigate to them
 * can ship now without a placeholder.
 */
export type HomeViewProps = {
  dashboard: CareerDashboard;
  busy: boolean;
  onStartRecommended: () => Promise<void>;
  onOpenApplications: () => void;
  onOpenStories: () => void;
  onOpenCoach: () => void;
  onOpenProgress: () => void;
};

/** Lifecycle states that no longer need attention -- the outcome is settled. */
const TERMINAL_STATUSES = new Set<OpportunityStatus>(["offer", "rejected", "withdrawn", "closed"]);

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

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const cardClass = "rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6";
const sectionButtonClass = "mt-5 text-sm font-semibold text-[var(--pine)]";

/**
 * Home: the signed-in landing surface and this release's headline UX. Renders
 * the deterministic, server-computed practice recommendation (never derived
 * client-side) as the dominant action, then four supporting sections in a
 * fixed order: applications needing attention, coach observations, the story
 * bank, and progress. Every section states plainly when it has nothing to
 * show rather than inventing filler.
 */
export function HomeView({ dashboard, busy, onStartRecommended, onOpenApplications, onOpenStories, onOpenCoach, onOpenProgress }: HomeViewProps) {
  const { profile, recommendation, opportunities, observations, stories, readiness } = dashboard;
  // A sparse profile never disables Start -- it only changes what the first
  // session looks like (broader discovery questions instead of grounded
  // ones). `readinessSparse` gates the guidance copy below, not the CTA.
  const readinessSparse = profile.readiness?.ready === false;
  const readinessReason = readinessSparse ? profileReadinessCopy(profile.readiness) : null;
  const signals = recommendation.signals.slice(0, 3);
  const primaryOpportunity = recommendation.primaryOpportunityId
    ? opportunities.find((item) => item.id === recommendation.primaryOpportunityId) ?? null
    : null;
  const attentionOpportunities = opportunities.filter((item) => !TERMINAL_STATUSES.has(item.status));
  const confirmedStoryCount = stories.filter((item) => item.reviewState === "confirmed").length;

  return (
    <div className="space-y-7">
      <section aria-label="Recommended practice" className="rounded-3xl bg-[var(--pine)] p-6 text-white md:p-8">
        <h2 className="text-2xl font-semibold">Recommended practice</h2>
        <p className="mt-3 text-sm uppercase tracking-[.12em] text-[#c8d7cf]">
          {FORMAT_LABELS[recommendation.format]} · {recommendation.estimatedMinutes} min
        </p>
        <p className="mt-2 text-xl font-semibold">{recommendation.primaryFocus}</p>
        {recommendation.secondaryFocus && <p className="mt-1 text-[#dbe7df]">{recommendation.secondaryFocus}</p>}
        <p className="mt-4 max-w-2xl leading-6 text-[#dbe7df]">{recommendation.rationale}</p>
        {signals.length > 0 && (
          <ul className="mt-4 flex flex-wrap gap-2">
            {signals.map((signal: PracticeRecommendationSignal, index) => (
              <li key={`${signal.kind}-${index}`} className="rounded-full bg-white/10 px-3 py-1.5 text-xs">
                <span className="font-semibold uppercase tracking-[.08em]">{signal.label}</span> <span aria-hidden="true">·</span> <span>{signal.detail}</span>
              </li>
            ))}
          </ul>
        )}
        {recommendation.successCriteria.length > 0 && (
          <ul className="mt-4 list-disc space-y-1 pl-5 text-sm leading-6 text-[#dbe7df]">
            {recommendation.successCriteria.map((criterion) => (
              <li key={criterion}>{criterion}</li>
            ))}
          </ul>
        )}
        {primaryOpportunity && (
          <p className="mt-4 text-sm text-[#dbe7df]">
            For {primaryOpportunity.company} · {primaryOpportunity.role}
          </p>
        )}
        <button
          onClick={() => { void onStartRecommended(); }}
          disabled={busy}
          className="mt-6 rounded-full bg-[var(--lime)] px-5 py-3 text-sm font-semibold text-[#18281f] disabled:opacity-50"
        >
          {busy ? "Starting…" : "Start recommended practice"}
        </button>
        {readinessReason && <p className="mt-3 max-w-md text-sm leading-6 text-[#dbe7df]">{readinessReason}</p>}
      </section>

      <section aria-label="Applications needing attention" className={cardClass}>
        <h2 className="text-xl font-semibold">Applications needing attention</h2>
        {attentionOpportunities.length === 0 ? (
          <p className="mt-3 leading-6 text-[var(--ink-muted)]">No applications need attention right now.</p>
        ) : (
          <ul className="mt-4 space-y-3">
            {attentionOpportunities.map((item: Opportunity) => (
              <li key={item.id} className="rounded-2xl bg-[#f3f5ef] p-4">
                <p className="font-semibold">{item.company}</p>
                <p className="text-sm text-[var(--ink-muted)]">{item.role}</p>
                <p className="mt-1 text-xs uppercase tracking-[.1em] text-[var(--ink-muted)]">{item.status}</p>
                {item.nextInterviewAt && (
                  <p className="mt-2 text-sm text-[#38502e]">Next interview: {formatDate(item.nextInterviewAt)}</p>
                )}
              </li>
            ))}
          </ul>
        )}
        <button onClick={onOpenApplications} className={sectionButtonClass}>Open applications <span aria-hidden="true">→</span></button>
      </section>

      <section aria-label="What Relay is noticing" className={cardClass}>
        <h2 className="text-xl font-semibold">What Relay is noticing</h2>
        {observations.length === 0 ? (
          <p className="mt-3 leading-6 text-[var(--ink-muted)]">Relay hasn&apos;t noticed any coaching patterns yet.</p>
        ) : (
          <ul className="mt-4 space-y-3">
            {observations.map((item) => (
              <li key={item.id} className="rounded-2xl bg-[#f3f5ef] p-4 text-sm leading-6">{item.effectiveText}</li>
            ))}
          </ul>
        )}
        <button onClick={onOpenCoach} className={sectionButtonClass}>Open coach <span aria-hidden="true">→</span></button>
      </section>

      <section aria-label="Story bank" className={cardClass}>
        <h2 className="text-xl font-semibold">Story bank</h2>
        {stories.length === 0 ? (
          <p className="mt-3 leading-6 text-[var(--ink-muted)]">You haven&apos;t added any career stories yet.</p>
        ) : (
          <p className="mt-3 text-sm text-[var(--ink-muted)]">{confirmedStoryCount} confirmed · {stories.length} total</p>
        )}
        <button onClick={onOpenStories} className={sectionButtonClass}>Open story bank <span aria-hidden="true">→</span></button>
      </section>

      <section aria-label="Progress" className={cardClass}>
        <h2 className="text-xl font-semibold">Progress</h2>
        {readiness.overall === null ? (
          <p className="mt-3 leading-6 text-[var(--ink-muted)]">Complete your first practice session to see progress.</p>
        ) : (
          <p className="mt-3 text-3xl font-semibold">
            {readiness.overall}<span className="ml-1 text-base font-normal text-[var(--ink-muted)]">/ 100</span>
          </p>
        )}
        <button onClick={onOpenProgress} className={sectionButtonClass}>Open progress <span aria-hidden="true">→</span></button>
      </section>
    </div>
  );
}

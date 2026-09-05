import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi, type Mock } from "vitest";
import type { CoachObservationSummary } from "@/lib/types";
import { CoachView } from "@/app/views/coach-view";

type CoachViewMocks = {
  onConfirm: Mock;
  onCorrect: Mock;
  onDismiss: Mock;
};

function observation(overrides: Partial<CoachObservationSummary> = {}): CoachObservationSummary {
  const claim = overrides.claim ?? "You skip tradeoffs when explaining technical decisions.";
  return {
    id: "obs-1",
    userId: "user-1",
    observationType: "delivery_pattern",
    claim,
    confidence: 0.7,
    importance: 0.6,
    trend: "unresolved",
    reviewState: "unreviewed",
    userCorrection: null,
    firstSeenAt: "2026-08-10T10:00:00.000Z",
    lastSeenAt: "2026-08-20T10:00:00.000Z",
    confirmedAt: null,
    correctedAt: null,
    dismissedAt: null,
    createdAt: "2026-08-10T10:00:00.000Z",
    updatedAt: "2026-08-20T10:00:00.000Z",
    effectiveText: claim,
    evidence: [
      { kind: "question_evaluation", label: "React architecture · Aug 20", summary: "Chose virtualization without naming the alternative.", role: "supporting", reason: null },
    ],
    ...overrides,
  };
}

function renderCoach(
  active: CoachObservationSummary[],
  history: CoachObservationSummary[] = [],
  overrides: Partial<CoachViewMocks> = {},
) {
  const defaults: CoachViewMocks = {
    onConfirm: vi.fn().mockResolvedValue(observation({ reviewState: "confirmed" })),
    onCorrect: vi.fn().mockResolvedValue(observation({ reviewState: "corrected" })),
    onDismiss: vi.fn().mockResolvedValue(observation({ reviewState: "dismissed" })),
  };
  const effective = { ...defaults, ...overrides };
  render(<CoachView active={active} history={history} busy={false} {...effective} />);
  return effective;
}

describe("CoachView", () => {
  it("shows an honest empty state when there are no observations at all", () => {
    renderCoach([], []);

    expect(screen.getByText(/hasn.t recorded any coaching observations yet/i)).toBeInTheDocument();
    expect(screen.queryByText(/quiz|score below|failing/i)).not.toBeInTheDocument();
  });

  // `effectiveText` is server-computed (`src/app/api/observations/route.ts`): the
  // user's correction when corrected, otherwise the original claim. This proves
  // the view shows the CORRECTION as the primary guidance text -- a wrong
  // implementation that always rendered `claim` instead would fail it.
  it("shows the user's correction as the effective guidance text for a corrected observation", () => {
    renderCoach([
      observation({
        id: "obs-corrected",
        claim: "You never mention tradeoffs.",
        reviewState: "corrected",
        userCorrection: "I do mention tradeoffs, but only when asked directly.",
        effectiveText: "I do mention tradeoffs, but only when asked directly.",
      }),
    ]);

    expect(screen.getByText("I do mention tradeoffs, but only when asked directly.")).toBeInTheDocument();
    expect(screen.queryByText("You never mention tradeoffs.", { selector: "p, h2, h3" })).not.toBeInTheDocument();
  });

  // The original `claim` must remain visible in detail even when corrected --
  // design section 4.4. A wrong implementation that dropped `claim` once
  // corrected (or overwrote it) would fail this.
  it("keeps the original claim visible in the evidence detail, unchanged, for a corrected observation", () => {
    renderCoach([
      observation({
        id: "obs-corrected",
        claim: "You never mention tradeoffs.",
        reviewState: "corrected",
        userCorrection: "I do mention tradeoffs, but only when asked directly.",
        effectiveText: "I do mention tradeoffs, but only when asked directly.",
      }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: "Why does Relay think this?" }));

    expect(screen.getByText("You never mention tradeoffs.")).toBeInTheDocument();
  });

  it("expands to show the evidence behind an observation", () => {
    renderCoach([observation()]);

    expect(screen.queryByText("Chose virtualization without naming the alternative.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Why does Relay think this?" }));

    expect(screen.getByText("Chose virtualization without naming the alternative.")).toBeInTheDocument();
  });

  it("confirms an observation", async () => {
    const { onConfirm } = renderCoach([observation()]);

    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(onConfirm).toHaveBeenCalledWith("obs-1"));
  });

  it("corrects an observation with replacement text", async () => {
    const { onCorrect } = renderCoach([observation()]);

    fireEvent.click(screen.getByRole("button", { name: "Correct" }));
    fireEvent.change(screen.getByLabelText("Your correction"), { target: { value: "It only happens under time pressure." } });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));

    await waitFor(() => expect(onCorrect).toHaveBeenCalledWith("obs-1", "It only happens under time pressure."));
  });

  it("dismisses an observation", async () => {
    const { onDismiss } = renderCoach([observation()]);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(onDismiss).toHaveBeenCalledWith("obs-1"));
  });

  // Design section 4.4: dismissed observations are hidden from the default
  // active list but shown under history. `active`/`history` arrive pre-split
  // from `GET /api/observations`; this proves the view actually renders them
  // into two distinct sections rather than merging them (which would make a
  // dismissed item show up next to live Confirm/Dismiss actions) or dropping
  // history outright.
  it("renders dismissed observations only under history, never in the active section", () => {
    renderCoach(
      [observation({ id: "obs-active", claim: "Active one." })],
      [observation({ id: "obs-dismissed", claim: "Dismissed one.", reviewState: "dismissed", dismissedAt: "2026-08-25T10:00:00.000Z", effectiveText: "Dismissed one." })],
    );

    const activeSection = screen.getByRole("region", { name: "Active observations" });
    const historySection = screen.getByRole("region", { name: "Observation history" });

    expect(within(activeSection).getByText("Active one.")).toBeInTheDocument();
    expect(within(activeSection).queryByText("Dismissed one.")).not.toBeInTheDocument();
    expect(within(historySection).getByText("Dismissed one.")).toBeInTheDocument();
    expect(within(historySection).queryByText("Active one.")).not.toBeInTheDocument();
  });

  it("keeps confidence and importance secondary, without a create-observation affordance", () => {
    renderCoach([observation({ confidence: 0.73, importance: 0.55 })]);

    expect(screen.queryByRole("button", { name: /add observation/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /new observation/i })).not.toBeInTheDocument();
  });

  // Design section 4.4 lists review state as its own field Coach must
  // display, distinct from trend/type -- without it, an unreviewed and a
  // confirmed observation render identically. This proves the two are
  // actually distinguishable in the rendered output, per card, not just that
  // *a* label string appears somewhere on the page.
  it("shows each observation's review state, distinguishing unreviewed from confirmed", () => {
    renderCoach([
      observation({ id: "obs-unreviewed", claim: "Skips tradeoffs.", reviewState: "unreviewed", effectiveText: "Skips tradeoffs." }),
      observation({ id: "obs-confirmed", claim: "Rushes through system design.", reviewState: "confirmed", confirmedAt: "2026-08-25T10:00:00.000Z", effectiveText: "Rushes through system design." }),
    ]);

    const unreviewedCard = screen.getByText("Skips tradeoffs.").closest("li");
    const confirmedCard = screen.getByText("Rushes through system design.").closest("li");
    if (!unreviewedCard || !confirmedCard) throw new Error("Expected both observation cards to render as <li> elements.");

    expect(within(unreviewedCard).getByText("Unreviewed")).toBeInTheDocument();
    expect(within(unreviewedCard).queryByText("Confirmed")).not.toBeInTheDocument();
    expect(within(confirmedCard).getByText("Confirmed")).toBeInTheDocument();
    expect(within(confirmedCard).queryByText("Unreviewed")).not.toBeInTheDocument();
  });

  // Mirrors StoriesView hiding "Confirm story" once a story is no longer a
  // draft (stories-view.tsx:312) -- an already-confirmed observation should
  // not silently accept a redundant re-confirm.
  it("does not offer to confirm an already-confirmed observation", () => {
    renderCoach([observation({ reviewState: "confirmed", confirmedAt: "2026-08-25T10:00:00.000Z" })]);

    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
  });
});

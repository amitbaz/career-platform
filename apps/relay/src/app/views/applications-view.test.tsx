import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi, type Mock } from "vitest";
import type { Opportunity, OpportunityEvent, PracticePlan } from "@/lib/types";
import { ApplicationsView } from "@/app/views/applications-view";

type ApplicationsViewMocks = {
  onCreate: Mock;
  onUpdate: Mock;
  onTransition: Mock;
  onScheduleInterview: Mock;
  onAddNote: Mock;
  onLoadEvents: Mock;
};

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "opp-1",
    userId: "user-1",
    company: "Northwind",
    role: "Staff Engineer",
    status: "considering",
    location: "Remote",
    remote: true,
    jobUrl: null,
    jobDescription: "Own the checkout platform end to end.",
    sourceLabel: null,
    sourceSystem: null,
    sourceExternalId: null,
    matchScore: null,
    strengths: [],
    gaps: [],
    notes: null,
    appliedAt: null,
    nextInterviewAt: null,
    createdAt: "2026-08-15T10:00:00.000Z",
    updatedAt: "2026-08-15T10:00:00.000Z",
    ...overrides,
  };
}

function opportunityEvent(overrides: Partial<OpportunityEvent> = {}): OpportunityEvent {
  return {
    id: "event-1",
    userId: "user-1",
    opportunityId: "opp-1",
    eventType: "note",
    fromStatus: null,
    toStatus: null,
    occurredAt: "2026-09-01T09:00:00.000Z",
    note: "Recruiter confirmed the loop.",
    metadata: {},
    createdAt: "2026-09-01T09:00:00.000Z",
    ...overrides,
  };
}

function practicePlan(overrides: Partial<PracticePlan> = {}): PracticePlan {
  return {
    id: "plan-1",
    userId: "user-1",
    status: "completed",
    primaryFocus: "Role prep for Northwind",
    secondaryFocus: null,
    rationale: "Your Staff Engineer interview at Northwind is coming up.",
    format: "role_prep",
    estimatedMinutes: 18,
    successCriteria: [],
    priorityScore: null,
    priorityFactors: {},
    generationError: null,
    completedAt: "2026-08-20T10:00:00.000Z",
    createdAt: "2026-08-18T10:00:00.000Z",
    updatedAt: "2026-08-20T10:00:00.000Z",
    opportunities: [{ userId: "user-1", practicePlanId: "plan-1", opportunityId: "opp-1", relevance: "primary", createdAt: "2026-08-18T10:00:00.000Z" }],
    ...overrides,
  };
}

function renderApplications(
  opportunities: Opportunity[],
  overrides: Partial<ApplicationsViewMocks> = {},
  recentPracticePlans: PracticePlan[] = [],
) {
  const defaults: ApplicationsViewMocks = {
    onCreate: vi.fn().mockResolvedValue(opportunity()),
    onUpdate: vi.fn().mockResolvedValue(opportunity()),
    onTransition: vi.fn().mockResolvedValue(opportunity()),
    onScheduleInterview: vi.fn().mockResolvedValue(opportunity()),
    onAddNote: vi.fn().mockResolvedValue(opportunityEvent()),
    onLoadEvents: vi.fn().mockResolvedValue([]),
  };
  const effective = { ...defaults, ...overrides };
  const view = render(<ApplicationsView opportunities={opportunities} busy={false} recentPracticePlans={recentPracticePlans} {...effective} />);
  return {
    ...effective,
    rerenderWithOpportunities: (next: Opportunity[]) =>
      view.rerender(<ApplicationsView opportunities={next} busy={false} recentPracticePlans={recentPracticePlans} {...effective} />),
  };
}

describe("ApplicationsView", () => {
  it("creates a new opportunity in considering status by default", async () => {
    const { onCreate } = renderApplications([]);

    fireEvent.click(screen.getByRole("button", { name: "Add application" }));
    fireEvent.change(screen.getByLabelText("Company"), { target: { value: "Globex" } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "Principal Engineer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save application" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate.mock.calls[0][0]).toMatchObject({ company: "Globex", role: "Principal Engineer" });
    expect(onCreate.mock.calls[0][0].initialStatus).toBeUndefined();
  });

  it("creates an already-applied opportunity when the applicant marks it so", async () => {
    const { onCreate } = renderApplications([]);

    fireEvent.click(screen.getByRole("button", { name: "Add application" }));
    fireEvent.change(screen.getByLabelText("Company"), { target: { value: "Globex" } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "Principal Engineer" } });
    fireEvent.click(screen.getByLabelText("I've already applied"));
    fireEvent.click(screen.getByRole("button", { name: "Save application" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledTimes(1));
    expect(onCreate.mock.calls[0][0]).toMatchObject({ company: "Globex", role: "Principal Engineer", initialStatus: "applied" });
  });

  it("edits an opportunity's details from the detail panel", async () => {
    const { onUpdate } = renderApplications([opportunity()]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));
    fireEvent.click(screen.getByRole("button", { name: "Edit details" }));
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "Senior Staff Engineer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(onUpdate).toHaveBeenCalledTimes(1));
    expect(onUpdate).toHaveBeenCalledWith("opp-1", expect.objectContaining({ role: "Senior Staff Engineer" }));
  });

  it("keeps the job description out of the list and only in the detail panel", () => {
    renderApplications([opportunity({ jobDescription: "Own the checkout platform end to end." })]);

    expect(screen.queryByText("Own the checkout platform end to end.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    expect(screen.getByText("Own the checkout platform end to end.")).toBeInTheDocument();
  });

  /**
   * Design section 4.2 requires the detail panel to "open the job URL when
   * present". Both fields were editable and persisted but never rendered, so a
   * user could record a posting URL and then had no way to reach it.
   */
  it("renders the location and an openable job URL in the detail panel", () => {
    renderApplications([opportunity({ location: "Berlin, hybrid", jobUrl: "https://jobs.example.com/staff-engineer" })]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    expect(screen.getByText("Berlin, hybrid")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Open job posting" });
    expect(link).toHaveAttribute("href", "https://jobs.example.com/staff-engineer");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("omits the job URL affordance when no URL is recorded", () => {
    renderApplications([opportunity({ jobUrl: null })]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    expect(screen.queryByRole("link", { name: "Open job posting" })).not.toBeInTheDocument();
  });

  it("transitions an opportunity to the next lifecycle status", async () => {
    const { onTransition } = renderApplications([opportunity({ status: "considering" })]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));
    fireEvent.click(screen.getByRole("button", { name: "Mark as applied" }));

    await waitFor(() => expect(onTransition).toHaveBeenCalledTimes(1));
    expect(onTransition).toHaveBeenCalledWith("opp-1", "applied", expect.anything());
  });

  it("makes terminal actions explicit distinct controls", async () => {
    const { onTransition } = renderApplications([opportunity({ status: "interviewing" })]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));
    expect(screen.getByRole("button", { name: "Mark as offer" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark as rejected" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Mark as offer" }));
    await waitFor(() => expect(onTransition).toHaveBeenCalledWith("opp-1", "offer", expect.anything()));
  });

  it("schedules an interview and shows nextInterviewAt distinctly from any note's occurredAt", async () => {
    const scheduled = opportunity({ status: "interviewing", nextInterviewAt: "2026-09-10T14:00:00.000Z" });
    const { onScheduleInterview, rerenderWithOpportunities } = renderApplications([opportunity({ status: "interviewing" })], {
      onScheduleInterview: vi.fn().mockResolvedValue(scheduled),
    });

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));
    fireEvent.change(screen.getByLabelText("Interview date and time"), { target: { value: "2026-09-10T14:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Schedule interview" }));

    await waitFor(() => expect(onScheduleInterview).toHaveBeenCalledTimes(1));
    const [, interviewAt] = onScheduleInterview.mock.calls[0];
    expect(new Date(interviewAt).toISOString()).toBe(new Date("2026-09-10T14:00").toISOString());

    // The view is prop-driven: the shell re-renders with the server's updated
    // opportunity once the mutation resolves -- this mirrors that refresh.
    rerenderWithOpportunities([scheduled]);
    expect(await screen.findByText(/Next interview/)).toBeInTheDocument();
  });

  it("loads and renders the opportunity's real persisted event history when it is selected", async () => {
    const statusChanged = opportunityEvent({
      id: "event-status",
      eventType: "status_changed",
      fromStatus: "considering",
      toStatus: "applied",
      note: null,
      occurredAt: "2026-08-20T09:00:00.000Z",
    });
    const { onLoadEvents } = renderApplications([opportunity()], {
      onLoadEvents: vi.fn().mockResolvedValue([statusChanged]),
    });

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    await waitFor(() => expect(onLoadEvents).toHaveBeenCalledWith("opp-1"));
    const timeline = screen.getByRole("region", { name: "Timeline" });
    expect(await within(timeline).findByText(/considering/)).toBeInTheDocument();
    expect(within(timeline).getByText(/applied/)).toBeInTheDocument();
  });

  it("shows an honest empty state in the timeline when no history has been recorded", async () => {
    const onLoadEvents = vi.fn().mockResolvedValue([]);
    renderApplications([opportunity()], { onLoadEvents });

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    // `events` also starts as `[]` before the fetch resolves, so without this
    // wait the empty-state text would already be present on first render --
    // a component that never called `onLoadEvents` at all would pass this
    // test unchanged. Waiting on the call proves the empty state reflects a
    // real (empty) server response, not just the pre-fetch initial state.
    await waitFor(() => expect(onLoadEvents).toHaveBeenCalledWith("opp-1"));
    const timeline = screen.getByRole("region", { name: "Timeline" });
    expect(await within(timeline).findByText(/no history/i)).toBeInTheDocument();
  });

  it("adds a note and refreshes the timeline with the server's persisted event, showing nextInterviewAt as a distinct fact", async () => {
    const withInterview = opportunity({ nextInterviewAt: "2026-09-10T14:00:00.000Z" });
    const savedEvent = opportunityEvent({ note: "Recruiter confirmed the loop.", occurredAt: "2026-09-01T09:00:00.000Z" });
    const onLoadEvents = vi.fn()
      .mockResolvedValueOnce([]) // the load triggered by selecting the opportunity
      .mockResolvedValueOnce([savedEvent]); // the refetch triggered by a successful note save
    const { onAddNote } = renderApplications([withInterview], {
      onLoadEvents,
      onAddNote: vi.fn().mockResolvedValue(savedEvent),
    });

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));
    await waitFor(() => expect(onLoadEvents).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByLabelText("Add a note"), { target: { value: "Recruiter confirmed the loop." } });
    fireEvent.click(screen.getByRole("button", { name: "Save note" }));

    await waitFor(() => expect(onAddNote).toHaveBeenCalledWith("opp-1", "Recruiter confirmed the loop."));
    // The note is not fabricated locally -- the view re-fetches the real persisted history.
    await waitFor(() => expect(onLoadEvents).toHaveBeenCalledTimes(2));

    const timeline = screen.getByRole("region", { name: "Timeline" });
    expect(await within(timeline).findByText("Recruiter confirmed the loop.")).toBeInTheDocument();
    // The interview date itself is shown as a distinct fact, not as when the note was recorded.
    expect(within(timeline).getByText(/Next interview/)).toBeInTheDocument();
  });

  it("filters the list to active applications by default and shows terminal ones only on request", () => {
    renderApplications([
      opportunity({ id: "opp-active", company: "Northwind", status: "interviewing" }),
      opportunity({ id: "opp-terminal", company: "Acme", status: "rejected" }),
    ]);

    expect(screen.getByText("Northwind")).toBeInTheDocument();
    expect(screen.queryByText("Acme")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));

    expect(screen.queryByText("Northwind")).not.toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
  });

  it("shows an honest empty state when there are no applications in the active filter", () => {
    renderApplications([]);

    expect(screen.getByText(/no applications/i)).toBeInTheDocument();
  });

  it("shows recent practice sessions associated with the selected opportunity", () => {
    const plan = practicePlan({ primaryFocus: "Role prep for Northwind" });
    renderApplications([opportunity()], {}, [plan]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    const practiceSection = screen.getByRole("region", { name: "Recent practice" });
    expect(within(practiceSection).getByText("Role prep for Northwind")).toBeInTheDocument();
  });

  it("only shows plans linked to the selected opportunity, not another opportunity's plans", () => {
    const planForOther = practicePlan({
      id: "plan-other",
      primaryFocus: "Role prep for Acme",
      opportunities: [{ userId: "user-1", practicePlanId: "plan-other", opportunityId: "opp-other", relevance: "primary", createdAt: "2026-08-18T10:00:00.000Z" }],
    });
    const planForSelected = practicePlan({ id: "plan-selected", primaryFocus: "Role prep for Northwind" });
    renderApplications([opportunity()], {}, [planForOther, planForSelected]);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    const practiceSection = screen.getByRole("region", { name: "Recent practice" });
    expect(within(practiceSection).getByText("Role prep for Northwind")).toBeInTheDocument();
    // A filter bug (e.g. showing every plan, or matching on something other
    // than the opportunity link) would leak the other opportunity's plan in here.
    expect(within(practiceSection).queryByText("Role prep for Acme")).not.toBeInTheDocument();
  });

  it("shows an honest empty state, labeled as recent rather than complete, when no practice is linked", () => {
    renderApplications([opportunity()], {}, []);

    fireEvent.click(screen.getByRole("button", { name: /Northwind.*Staff Engineer/ }));

    const practiceSection = screen.getByRole("region", { name: "Recent practice" });
    expect(within(practiceSection).getByText(/no recent practice/i)).toBeInTheDocument();
  });
});

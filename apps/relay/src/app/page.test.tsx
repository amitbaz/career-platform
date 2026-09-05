import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AssistanceRecord, CareerDashboard, CareerStorySummary, CoachObservationSummary, Competency, InterviewSession, Opportunity, PlannedQuestion, PracticePlan, PracticeRecommendation, Profile, ReadinessDimensionResult, ReadinessModel } from "@/lib/types";
import App from "@/app/page";
import { ResultsFeedbackCards } from "@/app/results-feedback-cards";

const getUser = vi.fn();
const signInWithOAuth = vi.fn();
const signOut = vi.fn();

vi.mock("@/lib/supabase/client", () => ({
  createBrowserSupabaseClient: () => ({
    auth: {
      getUser,
      signInWithOAuth,
      signOut,
    },
  }),
}));

function question(
  sequence: number,
  prompt: string,
  answer: string,
  competencyId: string | null,
  competencyName: string | null,
  overrides: Partial<PlannedQuestion> = {},
): PlannedQuestion {
  return {
    id: `question-${sequence}`,
    sequence,
    category: "technical",
    competencyId,
    competencyName,
    difficulty: "senior",
    isFollowUp: false,
    prompt,
    answer,
    createdAt: "2026-08-29T10:00:00.000Z",
    askedIntent: null,
    assistance: [],
    nonAnswer: false,
    setAsideAt: null,
    setAsideReason: null,
    nonAnswers: [],
    ...overrides,
  };
}

/** One rescue record, for building a question that only reached its score via Coach-mode assistance. */
function assistanceRecord(overrides: Partial<AssistanceRecord> = {}): AssistanceRecord {
  return { style: "hook", at: "2026-08-29T10:00:00.000Z", ...overrides };
}

/** A question answered, but only after `count` Coach-mode rescues -- exercises the results card's assistance suffix (spec §8.4). */
function answeredQuestionWithAssistance(count: number): PlannedQuestion {
  return question(1, "How would you phase a large React migration?", "I would phase by route.", "react-architecture", "React architecture", {
    assistance: Array.from({ length: count }, () => assistanceRecord()),
  });
}

/** A question the candidate never attempted -- results should show "Not attempted", never a zero score (spec §11.3). */
function unansweredNonAnswerQuestion(): PlannedQuestion {
  return question(1, "How would you phase a large React migration?", "", "react-architecture", "React architecture", {
    answer: null,
    nonAnswer: true,
  });
}

function competency(overrides: Partial<Competency>): Competency {
  return {
    id: "react-architecture",
    name: "React architecture",
    relevance: 1,
    expectedLevel: "senior",
    estimatedLevel: "senior",
    confidence: "high",
    lastPracticedAt: "2026-08-29T10:00:00.000Z",
    questionCount: 2,
    averageScore: 8,
    recentScore: 8,
    strengths: ["Frames migration trade-offs clearly."],
    weaknesses: ["Name rollback triggers earlier."],
    ...overrides,
  };
}

function profile(overrides: Partial<Profile> = {}): Profile {
  return {
    userId: "user-1",
    role: "Senior Frontend Engineer",
    seniority: "Senior",
    summary: "Builds product UIs and frontend systems.",
    narrative: "Leads complex React work.",
    expertise: ["React", "TypeScript"],
    characteristics: ["Clear communicator"],
    evidence: [
      {
        id: "evidence-1",
        sourceKind: "cv",
        sourceExcerpt: "Led the checkout migration from legacy React Router to App Router.",
        projectOrEmployer: "Checkout rewrite",
        ownership: "Frontend lead",
        technologies: ["React", "Next.js"],
        decision: "Phased the migration by route.",
        constraint: "Needed rollback safety.",
        outcome: "Reduced rollout risk during launch season.",
        recency: "2025",
        confidence: 0.92,
      },
      {
        id: "evidence-2",
        sourceKind: "cv",
        sourceExcerpt: "Improved a large search experience for a product catalog.",
        projectOrEmployer: "Catalog search",
        ownership: "Senior engineer",
        technologies: ["React", "TypeScript"],
        decision: "Virtualized the results list.",
        constraint: "Needed keyboard navigation to remain intact.",
        outcome: "Kept the UI responsive at high result counts.",
        recency: "2024",
        confidence: 0.85,
      },
    ],
    readiness: {
      ready: true,
      missing: [],
    },
    competencies: [
      competency({}),
      competency({
        id: "performance",
        name: "Performance",
        confidence: "medium",
        averageScore: 6,
        recentScore: 6,
        strengths: ["Recognizes virtualization quickly."],
        weaknesses: ["Explain keyboard state under virtualization."],
      }),
    ],
    source: { cvText: "Frontend engineer", coverLetter: "" },
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

function session(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return {
    id: "session-1",
    userId: "user-1",
    kind: "conversation",
    roundId: "tech-lead",
    mode: "real",
    degraded: false,
    status: "complete",
    startedAt: "2026-08-29T10:00:00.000Z",
    completedAt: "2026-08-29T11:00:00.000Z",
    exercise: {},
    resultSummary: { summary: "Complete" },
    overallScore: 8,
    questions: [
      question(
        1,
        "How would you phase a large React migration?",
        "I would phase by route, keep the old shell available, and track rollback gates per milestone.",
        "react-architecture",
        "React architecture",
      ),
      question(
        2,
        "How do you keep a search UI responsive at 50,000 results?",
        "I would virtualize the list, debounce network work, and keep keyboard focus state outside each row.",
        "performance",
        "Performance",
      ),
    ],
    checkpoints: [],
    evaluations: [
      {
        score: 8,
        competencyId: "react-architecture",
        competency: "React architecture",
        dimensions: { structure: 9, tradeOffAwareness: 8, clarity: 7 },
        strengths: ["Phased the migration with rollback gates."],
        needsWork: ["Call out how you would verify each rollout stage."],
        missingPoints: ["Name the signal that would trigger a rollback."],
        betterStructure: ["Start with constraints, then walk through phases, and close with rollback criteria."],
        improvedAnswer: "I would begin with the constraints, phase the migration by route, and define explicit rollback criteria for each milestone.",
      },
      {
        score: 6,
        competencyId: "performance",
        competency: "Performance",
        dimensions: {},
        strengths: ["Recognized virtualization quickly."],
        needsWork: ["Explain how keyboard state survives list windowing."],
        missingPoints: [],
        betterStructure: [],
        improvedAnswer: "",
      },
    ],
    messages: [],
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T11:00:00.000Z",
    practicePlanId: null,
    opportunityId: null,
    ...overrides,
  };
}

function opportunity(overrides: Partial<Opportunity> = {}): Opportunity {
  return {
    id: "opp-1",
    userId: "user-1",
    company: "Northwind",
    role: "Staff Engineer",
    status: "considering",
    location: null,
    remote: null,
    jobUrl: null,
    jobDescription: null,
    sourceLabel: null,
    sourceSystem: null,
    sourceExternalId: null,
    matchScore: null,
    strengths: [],
    gaps: [],
    notes: null,
    appliedAt: null,
    nextInterviewAt: null,
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

/** The baseline "no urgent signals" recommendation `recommendPractice` returns for an empty/quiet account. */
function fallbackRecommendation(): PracticeRecommendation {
  return {
    format: "full_simulation",
    primaryFocus: "Run a full mock interview simulation",
    secondaryFocus: null,
    rationale: "There's no urgent opportunity, reviewed observation, or progress signal driving practice right now, so a full simulation keeps every competency fresh.",
    estimatedMinutes: 30,
    successCriteria: [
      "Complete a full mock interview across the core competencies.",
      "Receive scored feedback on every answer.",
    ],
    primaryOpportunityId: null,
    supportingOpportunityIds: [],
    signals: [{ kind: "fallback", label: "general readiness", detail: "no urgent signals right now" }],
  };
}

/** The `GET /api/career/dashboard` payload shape -- the shell's canonical read model once a profile exists. */
function dashboardPayload(
  profilePayload: Profile,
  sessionsPayload: InterviewSession[],
  readinessPayload: ReadinessModel,
  overrides: Partial<CareerDashboard> = {},
): CareerDashboard {
  return {
    profile: profilePayload,
    coachMode: "demo",
    readiness: readinessPayload,
    recentSessions: sessionsPayload,
    opportunities: [],
    upcomingOpportunities: [],
    observations: [],
    stories: [],
    recentPracticePlans: [],
    recommendation: fallbackRecommendation(),
    ...overrides,
  };
}

function mockCoachData(options: {
  profile?: Profile;
  readiness: ReadinessModel;
  sessions?: InterviewSession[];
  dashboardOverrides?: Partial<CareerDashboard>;
}) {
  const profilePayload = options.profile ?? profile();
  const sessionsPayload = options.sessions ?? [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;

    if (url === "/api/profile") {
      return {
        ok: true,
        status: 200,
        json: async () => ({ profile: profilePayload, demoMode: false }),
      } satisfies Partial<Response>;
    }

    if (url === "/api/career/dashboard") {
      return {
        ok: true,
        status: 200,
        json: async () => dashboardPayload(profilePayload, sessionsPayload, options.readiness, options.dashboardOverrides),
      } satisfies Partial<Response>;
    }

    // Stories and Coach are dedicated detail views with their own read
    // models, fetched alongside the dashboard at bootstrap (see
    // `loadCareerData` in relay-shell.tsx). Callers that only care about
    // progress/home/applications don't need to know about these -- default
    // to empty so this helper's existing callers keep working unmodified.
    if (url === "/api/stories") {
      return { ok: true, status: 200, json: async () => ({ stories: [] }) } satisfies Partial<Response>;
    }
    if (url === "/api/observations") {
      return { ok: true, status: 200, json: async () => ({ active: [], history: [] }) } satisfies Partial<Response>;
    }

    throw new Error(`Unexpected request: ${url}`);
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function renderProgressView(options: {
  profile?: Profile;
  readiness: ReadinessModel;
  sessions?: InterviewSession[];
}) {
  mockCoachData(options);
  render(<App />);

  // "progress" is not a primary nav tab (task-10 brief step 7) -- it stays
  // reachable from Home's "Open progress" button instead.
  await screen.findByRole("heading", { name: "Ready when you are." });
  fireEvent.click(screen.getByRole("button", { name: "Open progress" }));
  await screen.findByRole("heading", { name: "Practice with a memory." });
}

async function renderHomeView(options: {
  profile?: Profile;
  readiness: ReadinessModel;
  sessions?: InterviewSession[];
}) {
  mockCoachData(options);
  render(<App />);

  await screen.findByRole("heading", { name: "Ready when you are." });
}

async function renderPracticeView(options: {
  profile?: Profile;
  readiness: ReadinessModel;
  sessions?: InterviewSession[];
}) {
  mockCoachData(options);
  render(<App />);

  await screen.findByRole("heading", { name: "Ready when you are." });
  fireEvent.click(screen.getByRole("button", { name: "practice" }));
  await screen.findByRole("heading", { name: "Choose deliberate practice." });
}

type RouteResponse = { ok?: boolean; status?: number; body: unknown };
type RouteHandler = (init: RequestInit | undefined) => RouteResponse;

/**
 * The shell bootstraps `GET /api/stories` and `GET /api/observations`
 * alongside the dashboard (see `loadCareerData` in relay-shell.tsx). Callers
 * of `mockRoutes` that don't care about Stories/Coach content don't have to
 * know that -- these defaults answer both with empty read models, and any
 * caller that DOES care overrides them by supplying its own handler for the
 * same path in `routes`.
 */
const DEFAULT_ROUTES: Record<string, RouteHandler> = {
  "/api/stories": () => ({ body: { stories: [] } }),
  "/api/observations": () => ({ body: { active: [], history: [] } }),
};

/**
 * Stubs `fetch` with a per-path handler map so interaction tests can drive the
 * shell's real request/response cycle instead of only its first paint. Each
 * handler receives the `RequestInit`, so one path can answer both its GET read
 * model and its POST actions.
 */
function mockRoutes(routes: Record<string, RouteHandler>) {
  const effectiveRoutes = { ...DEFAULT_ROUTES, ...routes };
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    const handler = effectiveRoutes[url];
    if (!handler) throw new Error(`Unexpected request: ${url}`);
    const { ok = true, status = 200, body } = handler(init);
    return { ok, status, json: async () => body } satisfies Partial<Response>;
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function requestBody(init: RequestInit | undefined): Record<string, unknown> {
  return JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
}

function emptyReadiness(overrides: Partial<ReadinessModel> = {}): ReadinessModel {
  return {
    overall: null,
    overallConfidence: null,
    overallTrend: "unresolved",
    dimensions: [],
    unmappedEvidenceCount: 0,
    computedAt: "2026-08-29T10:00:00.000Z",
    ...overrides,
  };
}

/** One readiness dimension result, defaulting to a confident, mid-scoring `system-design` entry. */
function readinessDimension(overrides: Partial<ReadinessDimensionResult> = {}): ReadinessDimensionResult {
  return {
    dimension: "system-design",
    score: 60,
    confidence: "medium",
    trend: "stable",
    evidenceCount: 2,
    totalWeight: 2,
    contributions: [],
    ...overrides,
  };
}

/** The shape `/api/interview` returns for a freshly started conversation: one open question, no evaluations. */
function activeConversationSession(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return session({
    id: "session-active",
    status: "active",
    completedAt: null,
    overallScore: null,
    resultSummary: {},
    evaluations: [],
    questions: [question(1, "How would you phase a large React migration?", "", "react-architecture", "React architecture")],
    messages: [
      {
        id: "question-1:prompt",
        role: "interviewer",
        content: "How would you phase a large React migration?",
        createdAt: "2026-08-29T10:00:00.000Z",
      },
    ],
    ...overrides,
  });
}

/** The shape `/api/interview` returns for a freshly started hands-on exercise. */
function activeHandsOnSession(overrides: Partial<InterviewSession> = {}): InterviewSession {
  return session({
    id: "session-hands-on",
    kind: "hands-on",
    status: "active",
    completedAt: null,
    overallScore: null,
    resultSummary: {},
    evaluations: [],
    questions: [],
    exercise: {
      title: "Accessible product search",
      durationMinutes: 60,
      briefing: "Build an accessible product search over a large catalog.",
      requirements: ["Debounce the query", "Keep keyboard focus stable"],
      starterCode: "export function ProductSearch() {}",
      interviewerOpening: "Talk me through your plan before you type.",
    },
    messages: [
      {
        id: "opening",
        role: "interviewer",
        content: "Talk me through your plan before you type.",
        createdAt: "2026-08-29T10:00:00.000Z",
      },
    ],
    ...overrides,
  });
}

/** A plausible `POST /api/practice` `start_manual` response's plan half -- these tests only assert on the returned `session`. */
function startedPracticePlan(format: "targeted_drill" | "hands_on"): PracticePlan {
  return {
    id: "plan-started",
    userId: "user-1",
    status: "started",
    primaryFocus: "Practice focus",
    secondaryFocus: null,
    rationale: "You chose this practice focus yourself.",
    format,
    estimatedMinutes: null,
    successCriteria: [],
    priorityScore: null,
    priorityFactors: {},
    generationError: null,
    completedAt: null,
    createdAt: "2026-08-29T10:00:00.000Z",
    updatedAt: "2026-08-29T10:00:00.000Z",
    opportunities: [],
  };
}

/**
 * Drives the shell from home into an active interview so answer, checkpoint,
 * and transcription assertions all start from the same place. Home's own CTA
 * starts the server-recommended practice, not an ad-hoc mixed interview, so
 * this goes through Practice view's manual-start form/hands-on option
 * instead (design section 4.5) -- both resolve through `POST /api/practice`
 * `start_manual`, which this helper stubs itself so callers only need to
 * supply routes for whatever happens AFTER the session starts (`respond`,
 * `checkpoint`, `complete`, `/api/transcribe`).
 */
async function startInterviewFrom(mode: "conversation" | "hands-on", started: InterviewSession, routes: Record<string, RouteHandler>) {
  const fetchMock = mockRoutes({
    ...routes,
    "/api/practice": (init) => {
      const body = requestBody(init);
      if (body.action !== "start_manual") throw new Error(`Unexpected /api/practice action in this test: ${body.action}`);
      return { body: { plan: startedPracticePlan(mode === "hands-on" ? "hands_on" : "targeted_drill"), session: started } };
    },
  });
  render(<App />);

  await screen.findByRole("heading", { name: "Ready when you are." });
  fireEvent.click(screen.getByRole("button", { name: "practice" }));
  await screen.findByRole("heading", { name: "Choose deliberate practice." });
  if (mode === "hands-on") {
    fireEvent.click(screen.getByRole("button", { name: "Start hands-on practice" }));
  } else {
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "Practice focus" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));
  }
  await screen.findByRole("heading", {
    name: started.kind === "hands-on" ? "Build, narrate, adapt." : "Stay in the conversation.",
  });

  return fetchMock;
}

/** A grounded, unanswered question with non-empty evidence -- the shape that used to trigger the removed "Grounded in <evidence>" provenance line. */
function sessionWithUnansweredQuestion(): InterviewSession {
  return activeConversationSession({
    blueprint: {
      status: "grounded",
      fallbackReason: null,
      maxFollowUps: 3,
      maxQuestions: 8,
      createdAt: "2026-09-01T12:00:00.000Z",
      roundId: "tech-lead",
      turnBudget: 8,
      targets: [],
      questions: [
        {
          id: "question-1",
          sequence: 1,
          category: "experience",
          competencyId: "react-architecture",
          competencyName: "React architecture",
          difficulty: "senior",
          isFollowUp: false,
          prompt: "How would you phase a large React migration?",
          answer: null,
          createdAt: "2026-09-01T12:00:00.000Z",
          objective: "Probe migration ownership and rollout trade-offs.",
          evidenceIds: ["evidence-1"],
          expectedSignals: ["ownership"],
          missingSignalPrompts: [],
          followUpLimit: 1,
          sourceConfidence: 0.9,
          askedIntent: null,
          assistance: [],
          nonAnswer: false,
          setAsideAt: null,
          setAsideReason: null,
          nonAnswers: [],
        },
      ],
    },
  });
}

/**
 * Like `mockRoutes`, but a `POST /api/interview` `respond` request never
 * resolves -- simulating a turn still in flight (spec §13.1's two
 * sequential model calls) so a test can assert on the pending interviewer
 * state without also having to resolve the request.
 */
function mockRoutesWithHangingRespond(routes: Record<string, RouteHandler>) {
  const effectiveRoutes = { ...DEFAULT_ROUTES, ...routes };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
    if (url === "/api/interview" && requestBody(init).action === "respond") {
      return new Promise<Response>(() => {}); // deliberately never resolves
    }
    const handler = effectiveRoutes[url];
    if (!handler) throw new Error(`Unexpected request: ${url}`);
    const { ok = true, status = 200, body } = handler(init);
    return Promise.resolve({ ok, status, json: async () => body } as Response);
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/**
 * Drives the real shell into the states the mode-picker, pending-turn, and
 * grounding-provenance tests need, following the same practice-view start
 * flow `startInterviewFrom` already uses. Named (and shaped as an options
 * bag) to match how the task-12 brief sketches its own tests, even though
 * this file builds the driving logic itself rather than importing it from
 * anywhere -- there is no pre-existing `renderShell` in this suite.
 */
async function renderShell(options: { pendingTurn?: boolean; activeSession?: InterviewSession } = {}) {
  const defaultRoutes = {
    "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
    "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
  };

  if (options.pendingTurn) {
    mockRoutesWithHangingRespond({
      ...defaultRoutes,
      "/api/practice": (init) => {
        const body = requestBody(init);
        if (body.action !== "start_manual") throw new Error(`Unexpected /api/practice action in this test: ${body.action}`);
        return { body: { plan: startedPracticePlan("targeted_drill"), session: activeConversationSession() } };
      },
    });
    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "practice" }));
    await screen.findByRole("heading", { name: "Choose deliberate practice." });
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "Practice focus" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));
    await screen.findByRole("heading", { name: "Stay in the conversation." });
    fireEvent.change(screen.getByPlaceholderText("Answer as if you were in the room…"), { target: { value: "Phase by route." } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    return;
  }

  if (options.activeSession) {
    await startInterviewFrom("conversation", options.activeSession, defaultRoutes);
    return;
  }

  // Default: land on the results view of a just-completed conversation
  // session, which is where "start another interview" -- and its mode
  // picker -- lives.
  const completed = session({ id: "session-active", overallScore: 7.5, resultSummary: { summary: "Strong migration reasoning." } });
  await startInterviewFrom("conversation", activeConversationSession(), {
    ...defaultRoutes,
    "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [completed], emptyReadiness()) }),
    "/api/interview": () => ({ body: { session: completed, profile: profile() } }),
  });
  fireEvent.change(screen.getByPlaceholderText("Answer as if you were in the room…"), { target: { value: "Phase by route." } });
  fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
  await screen.findByRole("heading", { name: "A useful baseline." });
}

/** Renders `ResultsFeedbackCards` directly, mirroring the `describe("ResultsFeedbackCards", ...)` block below -- no full shell/fetch driving needed since the component takes `session` as a plain prop. */
function renderResults(overrides: { questions: PlannedQuestion[] }) {
  const evaluations = overrides.questions.map((item, index) => ({
    score: 7,
    questionId: item.id,
    competencyId: item.competencyId,
    competency: item.competencyName ?? `Question ${index + 1}`,
    dimensions: {},
    strengths: [],
    needsWork: [],
    missingPoints: [],
    betterStructure: [],
    improvedAnswer: "",
  }));
  render(<ResultsFeedbackCards session={session({ questions: overrides.questions, evaluations })} />);
}

beforeEach(() => {
  getUser.mockResolvedValue({ data: { user: { id: "user-1" } }, error: null });
  signInWithOAuth.mockResolvedValue({ error: null });
  signOut.mockResolvedValue({ error: null });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("App progress view", () => {
  it("shows a no-evidence state before the first completed interview", async () => {
    await renderProgressView({
      readiness: emptyReadiness(),
      profile: profile({
        competencies: [
          competency({ averageScore: null, recentScore: null, confidence: null, strengths: [], weaknesses: [] }),
        ],
      }),
    });

    expect(screen.getByRole("heading", { name: "Not enough data yet" })).toBeInTheDocument();
    expect(screen.getByText("Finish your first mixed interview to establish a baseline before Relay shows readiness or competency scores.")).toBeInTheDocument();
    expect(screen.queryByText("Interview readiness")).not.toBeInTheDocument();
  });

  it("shows practice-first readiness guidance on the progress view when the profile is sparse", async () => {
    await renderProgressView({
      readiness: emptyReadiness({
        overall: 75,
        overallConfidence: "low",
        dimensions: [readinessDimension({ dimension: "system-design", score: 60, confidence: "low" })],
      }),
      profile: profile({
        readiness: {
          ready: false,
          missing: [
            "two concrete engineering projects or work examples",
            "identifiable technologies",
            "responsibilities or outcomes",
          ],
        },
      }),
      sessions: [session({ overallScore: 8.2 })],
    });

    expect(screen.getByText(/You can practice now\./)).toBeInTheDocument();
    expect(screen.getByText(/help you uncover stronger/i)).toBeInTheDocument();
    expect(screen.queryByText(/Profile evidence gate/i)).not.toBeInTheDocument();
  });

  it("shows a readiness state with coaching guidance when confidence is still low", async () => {
    await renderProgressView({
      readiness: emptyReadiness({
        overall: 75,
        overallConfidence: "low",
        overallTrend: "unresolved",
        dimensions: [readinessDimension({ dimension: "system-design", score: 60, confidence: "low" })],
      }),
      profile: profile({ competencies: [competency({})] }),
      sessions: [session({ overallScore: 8.2 })],
    });

    expect(screen.getAllByText("Not enough evidence yet")).toHaveLength(2);
    expect(screen.getByText("A coaching signal based on your completed practice, not a hiring prediction.")).toBeInTheDocument();
    expect(screen.getByText("75")).toBeInTheDocument();
    expect(screen.getByText("Low")).toBeInTheDocument();
    expect(screen.getByText("System design")).toBeInTheDocument();
  });

  it("shows multi-dimension readiness insights and an improving trend from the server-computed model", async () => {
    await renderProgressView({
      readiness: emptyReadiness({
        overall: 81,
        overallConfidence: "high",
        overallTrend: "improving",
        dimensions: [
          readinessDimension({ dimension: "communication", score: 88, confidence: "high" }),
          readinessDimension({ dimension: "system-design", score: 62, confidence: "medium" }),
        ],
      }),
      profile: profile({ competencies: [] }),
      sessions: [session({ id: "latest", overallScore: 8.4 }), session({ id: "previous", overallScore: 7.3 })],
    });

    await waitFor(() => expect(screen.getByText("81")).toBeInTheDocument());

    expect(screen.getByText("A coaching signal based on your completed practice, not a hiring prediction.")).toBeInTheDocument();
    expect(screen.getAllByText("Improving")).toHaveLength(2);
    expect(screen.getByText("High")).toBeInTheDocument();
    expect(screen.getByText("System design")).toBeInTheDocument();
  });
});

describe("App profile view", () => {
  it("shows evidence-grounded readiness state on the profile screen", async () => {
    mockCoachData({
      readiness: emptyReadiness({ overall: 75, overallConfidence: "low", overallTrend: "unresolved" }),
      profile: profile({
        readiness: {
          ready: true,
          missing: [],
        },
      }),
    });
    render(<App />);

    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "profile" }));

    expect(await screen.findByText("Your source profile has enough detail for evidence-grounded practice.")).toBeInTheDocument();
  });

  it("shows source-backed evidence and no-fabrication copy on the profile screen", async () => {
    mockCoachData({
      readiness: emptyReadiness({ overall: 75, overallConfidence: "low", overallTrend: "unresolved" }),
      profile: profile(),
    });
    render(<App />);

    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "profile" }));

    expect(await screen.findByRole("heading", { name: "Grounded evidence" })).toBeInTheDocument();
    expect(screen.getByText("Checkout rewrite")).toBeInTheDocument();
    expect(screen.getByText("Led the checkout migration from legacy React Router to App Router.")).toBeInTheDocument();
    expect(screen.getByText("Relay will only plan and critique against source-backed details shown here.")).toBeInTheDocument();
  });
});

describe("App home view", () => {
  it("does not block the recommended-practice CTA when the profile readiness is not ready", async () => {
    await renderHomeView({
      readiness: emptyReadiness(),
      profile: profile({
        readiness: {
          ready: false,
          missing: ["two concrete engineering projects or work examples"],
        },
      }),
    });

    expect(screen.getByRole("button", { name: "Start recommended practice" })).toBeEnabled();
    expect(screen.getByText(/You can practice now\./)).toBeInTheDocument();
  });

  it("navigates to Applications when its open-affordance button is clicked", async () => {
    await renderHomeView({ readiness: emptyReadiness() });

    fireEvent.click(screen.getByRole("button", { name: "Open applications" }));

    expect(await screen.findByRole("heading", { name: "Applications" })).toBeInTheDocument();
  });

  // The shell wires Home's onOpenStories/onOpenCoach to navigate("stories")/navigate("coach"),
  // which render StoriesView/CoachView (Task 10). This proves both the navigation AND that a
  // real view -- not a dead target -- replaces Home's content.
  it("navigates to Stories with real content when Open story bank is clicked", async () => {
    await renderHomeView({ readiness: emptyReadiness() });

    fireEvent.click(screen.getByRole("button", { name: "Open story bank" }));

    expect(await screen.findByRole("heading", { name: "Story bank" })).toBeInTheDocument();
  });

  it("navigates to Coach with real content when Open coach is clicked", async () => {
    await renderHomeView({ readiness: emptyReadiness() });

    fireEvent.click(screen.getByRole("button", { name: "Open coach" }));

    expect(await screen.findByRole("heading", { name: "Coach" })).toBeInTheDocument();
  });
});

describe("App applications view", () => {
  /**
   * Drives an Applications mutation through the *real* shell (not
   * ApplicationsView's own isolated tests, which mock every callback) so
   * `handleCreateOpportunity`'s `setBusy`/`setError`/`refreshDashboard()`/
   * rethrow wiring in `relay-shell.tsx` is actually exercised. The dashboard
   * route's handler tracks its own call count and only starts returning the
   * created opportunity from its second response onward, so the new
   * opportunity appearing in the list can only mean the shell re-fetched the
   * dashboard after the mutation succeeded -- not that it read the create
   * response directly (the view never does that; it navigates by prop).
   */
  it("creates an opportunity through the real shell and refreshes the dashboard so it appears in the list", async () => {
    const created = opportunity({ id: "opp-new", company: "Globex", role: "Principal Engineer" });
    let dashboardCalls = 0;
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => {
        dashboardCalls += 1;
        return { body: dashboardPayload(profile(), [], emptyReadiness(), { opportunities: dashboardCalls > 1 ? [created] : [] }) };
      },
      "/api/opportunities": (init) => {
        const body = requestBody(init);
        if (body.action === "create") return { body: { opportunity: created } };
        throw new Error(`Unexpected /api/opportunities action in this test: ${body.action}`);
      },
      "/api/opportunities?opportunityId=opp-new": () => ({ body: { events: [] } }),
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "applications" }));
    await screen.findByRole("heading", { name: "Applications" });

    expect(dashboardCalls).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Add application" }));
    fireEvent.change(screen.getByLabelText("Company"), { target: { value: "Globex" } });
    fireEvent.change(screen.getByLabelText("Role"), { target: { value: "Principal Engineer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save application" }));

    // The detail heading (not the list row, which would also match "Globex")
    // confirms the freshly created opportunity was auto-selected from the
    // refreshed dashboard's own data.
    expect(await screen.findByRole("heading", { name: "Globex" })).toBeInTheDocument();
    expect(dashboardCalls).toBe(2);
  });
});

describe("App stories view", () => {
  /**
   * Drives a Stories mutation through the *real* shell so `handleCreateStory`'s
   * refresh wiring is actually exercised: R18/R9 (task-10 brief) require a
   * dedicated `GET /api/stories` re-fetch, distinct from `refreshDashboard`.
   * The stories route tracks its own GET call count and only starts
   * returning the created story from its second GET onward, so the new
   * story appearing in the list can only mean the shell re-fetched
   * `/api/stories` after the mutation succeeded.
   */
  it("creates a story through the real shell and refreshes the stories list", async () => {
    const created: CareerStorySummary = {
      id: "story-new",
      userId: "user-1",
      title: "Checkout migration",
      situation: null,
      responsibility: null,
      problem: null,
      actions: null,
      alternatives: null,
      tradeoffs: null,
      ownership: null,
      outcome: null,
      lessons: null,
      tags: [],
      completeness: 0,
      reviewState: "draft",
      confirmedAt: null,
      createdAt: "2026-09-01T10:00:00.000Z",
      updatedAt: "2026-09-01T10:00:00.000Z",
      evidenceCount: 0,
    };
    let storiesCalls = 0;
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/stories": (init) => {
        if (init?.method !== "POST") {
          storiesCalls += 1;
          return { body: { stories: storiesCalls > 1 ? [created] : [] } };
        }
        const body = requestBody(init);
        if (body.action === "create") return { body: { story: created } };
        throw new Error(`Unexpected /api/stories action in this test: ${body.action}`);
      },
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "stories" }));
    await screen.findByRole("heading", { name: "Story bank" });

    expect(storiesCalls).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "New story" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Checkout migration" } });
    fireEvent.click(screen.getByRole("button", { name: "Save story" }));

    expect(await screen.findByRole("heading", { name: "Checkout migration" })).toBeInTheDocument();
    expect(storiesCalls).toBe(2);
  });
});

describe("App coach view", () => {
  /**
   * Drives a dismiss through the *real* shell so `handleDismissObservation`'s
   * refresh wiring is exercised: the observations route tracks its own GET
   * call count and only starts returning the dismissed split from its second
   * GET onward, so the observation moving from Active to History can only
   * mean the shell re-fetched `/api/observations` after the mutation
   * succeeded -- not that the view re-sorted its own stale props.
   */
  it("dismisses an observation through the real shell and moves it to history", async () => {
    const active: CoachObservationSummary = {
      id: "obs-1",
      userId: "user-1",
      observationType: "delivery_pattern",
      claim: "You skip tradeoffs when explaining technical decisions.",
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
      effectiveText: "You skip tradeoffs when explaining technical decisions.",
      evidence: [],
    };
    const dismissed: CoachObservationSummary = { ...active, reviewState: "dismissed", dismissedAt: "2026-09-01T10:00:00.000Z" };
    let observationsCalls = 0;
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/observations": (init) => {
        if (init?.method !== "POST") {
          observationsCalls += 1;
          return { body: observationsCalls > 1 ? { active: [], history: [dismissed] } : { active: [active], history: [] } };
        }
        const body = requestBody(init);
        if (body.action === "dismiss") return { body: { observation: dismissed } };
        throw new Error(`Unexpected /api/observations action in this test: ${body.action}`);
      },
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "coach" }));
    await screen.findByRole("heading", { name: "Coach" });

    expect(observationsCalls).toBe(1);
    const activeSection = screen.getByRole("region", { name: "Active observations" });
    expect(within(activeSection).getByText("You skip tradeoffs when explaining technical decisions.")).toBeInTheDocument();
    fireEvent.click(within(activeSection).getByRole("button", { name: "Dismiss" }));

    await waitFor(() => expect(observationsCalls).toBe(2));
    const historySection = await screen.findByRole("region", { name: "Observation history" });
    expect(within(historySection).getByText("You skip tradeoffs when explaining technical decisions.")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Active observations" })).queryByText("You skip tradeoffs when explaining technical decisions.")).not.toBeInTheDocument();
  });
});

describe("App practice view", () => {
  it("does not gate the recommended-practice action on profile readiness", async () => {
    await renderPracticeView({
      readiness: emptyReadiness(),
      profile: profile({
        readiness: {
          ready: false,
          missing: ["two concrete engineering projects or work examples"],
        },
      }),
    });

    expect(screen.getByRole("button", { name: "Start recommended practice" })).toBeEnabled();
    expect(screen.getByText(/You can practice now\./)).toBeInTheDocument();
  });

  /**
   * Drives a manual-practice start through the *real* shell (not
   * PracticeView's own isolated tests, which mock every callback) so
   * `handleStartManualPractice`'s `POST /api/practice` `start_manual` request
   * and its `{ plan, session }` -> `navigate("interview")` wiring in
   * relay-shell.tsx are actually exercised.
   */
  it("starts manual practice through the real shell and opens the interview", async () => {
    const started = activeConversationSession();
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/practice": (init) => {
        const body = requestBody(init);
        if (body.action !== "start_manual") throw new Error(`Unexpected /api/practice action in this test: ${body.action}`);
        return { body: { plan: startedPracticePlan("targeted_drill"), session: started } };
      },
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "practice" }));
    await screen.findByRole("heading", { name: "Choose deliberate practice." });

    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "System design tradeoffs" } });
    fireEvent.change(screen.getByLabelText("Format"), { target: { value: "story_work" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));

    await screen.findByRole("heading", { name: "Stay in the conversation." });
  });

  /**
   * Pins the Finish control to the SERVER completion rule
   * (`canExplicitlyCompleteConversation`). A planned practice conversation is
   * shorter than the generic five-question backbone, so a five-answer client
   * gate left Finish permanently dead for every 2-4 question practice format.
   */
  it("enables Finish on a fully answered planned practice conversation", async () => {
    const planned = session({
      id: "session-planned",
      status: "active",
      completedAt: null,
      overallScore: null,
      resultSummary: {},
      evaluations: [],
      practicePlanId: "plan-started",
      questions: [1, 2, 3].map((sequence) => question(sequence, `Planned prompt ${sequence}`, "An answer.", null, null)),
      messages: [],
    });
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/practice": () => ({ body: { plan: startedPracticePlan("targeted_drill"), session: planned } }),
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "practice" }));
    await screen.findByRole("heading", { name: "Choose deliberate practice." });
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "System design tradeoffs" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));

    await screen.findByRole("heading", { name: "Stay in the conversation." });
    expect(screen.getByRole("button", { name: "Finish" })).toBeEnabled();
    expect(screen.getByText("Practice session · 3 of 3 answered")).toBeInTheDocument();
  });

  it("keeps Finish disabled on a generic conversation under five answers", async () => {
    const generic = session({
      id: "session-generic",
      status: "active",
      completedAt: null,
      overallScore: null,
      resultSummary: {},
      evaluations: [],
      practicePlanId: null,
      questions: [1, 2, 3].map((sequence) => question(sequence, `Prompt ${sequence}`, "An answer.", null, null)),
      messages: [],
    });
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/practice": () => ({ body: { plan: startedPracticePlan("targeted_drill"), session: generic } }),
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "practice" }));
    await screen.findByRole("heading", { name: "Choose deliberate practice." });
    fireEvent.change(screen.getByLabelText("Focus"), { target: { value: "System design tradeoffs" } });
    fireEvent.click(screen.getByRole("button", { name: "Start practice" }));

    await screen.findByRole("heading", { name: "Stay in the conversation." });
    expect(screen.getByRole("button", { name: "Finish" })).toBeDisabled();
    expect(screen.getByText("Mixed interview · 3 of 3 answered")).toBeInTheDocument();
  });
});

describe("ResultsFeedbackCards", () => {
  it("starts collapsed and expands to show the answered question with non-empty coaching details", () => {
    render(<ResultsFeedbackCards session={session()} />);

    const toggle = screen.getByRole("button", { name: "React architecture feedback" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("region", { name: "React architecture feedback" })).not.toBeInTheDocument();

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const details = screen.getByRole("region", { name: "React architecture feedback" });
    expect(details).toBeInTheDocument();
    expect(screen.getByText("How would you phase a large React migration?")).toBeInTheDocument();
    expect(screen.getByText("I would phase by route, keep the old shell available, and track rollback gates per milestone.")).toBeInTheDocument();
    expect(within(details).getByText("Structure")).toBeInTheDocument();
    expect(within(details).getByText("9/10")).toBeInTheDocument();
    expect(within(details).getByText("Trade-off awareness")).toBeInTheDocument();
    expect(within(details).getByText("8/10")).toBeInTheDocument();
    expect(within(details).getByText("Phased the migration with rollback gates.")).toBeInTheDocument();
    expect(within(details).getByText("Name the signal that would trigger a rollback.")).toBeInTheDocument();
    expect(within(details).getByText("Start with constraints, then walk through phases, and close with rollback criteria.")).toBeInTheDocument();
    expect(within(details).getByText("I would begin with the constraints, phase the migration by route, and define explicit rollback criteria for each milestone.")).toBeInTheDocument();
  });

  it("surfaces exact question grounding, relevance, claims, and dimension reasons", () => {
    render(<ResultsFeedbackCards
      session={session({
      blueprint: {
        status: "grounded",
        fallbackReason: null,
        maxFollowUps: 3,
        maxQuestions: 5,
        createdAt: "2026-08-29T10:00:00.000Z",
        roundId: "tech-lead",
        turnBudget: 8,
        targets: [],
        questions: [
          {
            id: "question-1",
            sequence: 1,
            category: "experience",
            competencyId: "react-architecture",
            competencyName: "React architecture",
            difficulty: "senior",
            isFollowUp: false,
            prompt: "How would you phase a large React migration?",
            answer: null,
            createdAt: "2026-08-29T10:00:00.000Z",
            objective: "Probe migration ownership and rollout trade-offs.",
            evidenceIds: ["evidence-1", "evidence-2"],
            expectedSignals: ["ownership", "trade-off", "impact"],
            missingSignalPrompts: ["Name the rollback trigger."],
            followUpLimit: 1,
            sourceConfidence: 0.92,
            askedIntent: null,
            assistance: [],
            nonAnswer: false,
            setAsideAt: null,
            setAsideReason: null,
            nonAnswers: [],
          },
        ],
      },
      evaluations: [
        {
          score: 8,
          questionId: "question-1",
          competencyId: "react-architecture",
          competency: "React architecture",
          relevance: 8.7,
          dimensions: { structure: 9, tradeOffAwareness: 8, clarity: 7 },
          strengths: ["Phased the migration with rollback gates."],
          needsWork: ["Call out how you would verify each rollout stage."],
          missingPoints: ["Name the signal that would trigger a rollback."],
          betterStructure: ["Start with constraints, then walk through phases, and close with rollback criteria."],
          improvedAnswer: "I would begin with the constraints, phase the migration by route, and define explicit rollback criteria for each milestone.",
          supportedClaims: ["phase the rollout carefully"],
          expectedSignalsPresent: ["trade-off", "impact"],
          unsupportedClaims: ["We shipped it perfectly."],
          dimensionReasons: {
            correctness: "The answer matches the migration question.",
            depth: "The answer mentions rollout phases and a metric.",
            clarity: "It is organized and specific.",
            structure: "It starts with the constraint and ends with the rollback trigger.",
            practicalExperience: "It refers to an actual migration.",
            tradeOffAwareness: "It names the rollout trade-off.",
            communication: "It is concise and explainable.",
            confidence: "It is stated directly.",
            relevance: "It answers the checkout migration question.",
          },
        },
      ],
    })}
      evidence={profile().evidence}
    />);

    fireEvent.click(screen.getByRole("button", { name: "React architecture feedback" }));

    const details = screen.getByRole("region", { name: "React architecture feedback" });
    expect(within(details).getByText("Question objective")).toBeInTheDocument();
    expect(within(details).getByText("Probe migration ownership and rollout trade-offs.")).toBeInTheDocument();
    expect(within(details).getByText("Evidence target")).toBeInTheDocument();
    expect(within(details).getByText("Checkout rewrite · Frontend lead · Reduced rollout risk during launch season.")).toBeInTheDocument();
    expect(within(details).getByText("Expected signals")).toBeInTheDocument();
    expect(within(details).getByText("Relevance")).toBeInTheDocument();
    expect(within(details).getByText("8.7/10")).toBeInTheDocument();
    expect(within(details).getByText("Supported claims")).toBeInTheDocument();
    expect(within(details).getByText("phase the rollout carefully")).toBeInTheDocument();
    expect(within(details).getByText("Expected signals present")).toBeInTheDocument();
    expect(within(details).getByText("trade-off")).toBeInTheDocument();
    expect(within(details).getByText("Unsupported claims")).toBeInTheDocument();
    expect(within(details).getByText("We shipped it perfectly.")).toBeInTheDocument();
    expect(within(details).getByText("Dimension reasons")).toBeInTheDocument();
    expect(within(details).getByText("answers the checkout migration question", { exact: false })).toBeInTheDocument();
  });

  it("labels limited-grounding legacy sessions clearly", () => {
    render(<ResultsFeedbackCards session={session({
      blueprint: {
        status: "limited-grounding",
        fallbackReason: "Gemini returned invalid blueprint JSON after one repair attempt.",
        maxFollowUps: 3,
        maxQuestions: 5,
        createdAt: "2026-08-29T10:00:00.000Z",
        roundId: "tech-lead",
        turnBudget: 8,
        targets: [],
        questions: [
          {
            id: "question-1",
            sequence: 1,
            category: "experience",
            competencyId: "react-architecture",
            competencyName: "React architecture",
            difficulty: "senior",
            isFollowUp: false,
            prompt: "How would you phase a large React migration?",
            answer: null,
            createdAt: "2026-08-29T10:00:00.000Z",
            objective: "Probe migration ownership and rollout trade-offs.",
            evidenceIds: ["evidence-1"],
            expectedSignals: ["ownership"],
            missingSignalPrompts: ["Name the rollback trigger."],
            followUpLimit: 1,
            sourceConfidence: 0.64,
            askedIntent: null,
            assistance: [],
            nonAnswer: false,
            setAsideAt: null,
            setAsideReason: null,
            nonAnswers: [],
          },
        ],
      },
      evaluations: [
        {
          score: 6,
          questionId: "question-1",
          competencyId: "react-architecture",
          competency: "React architecture",
          relevance: 5.1,
          dimensions: {},
          strengths: ["Stayed broadly on topic."],
          needsWork: ["Tie the answer back to the source evidence."],
          missingPoints: [],
          betterStructure: [],
          improvedAnswer: "",
          supportedClaims: [],
          expectedSignalsPresent: [],
          unsupportedClaims: [],
          dimensionReasons: {
            correctness: "The answer stays on the interview topic.",
            depth: "The answer is broad rather than specific.",
            clarity: "The answer is easy to follow.",
            structure: "The answer is concise but generic.",
            practicalExperience: "The answer does not anchor to a concrete project.",
            tradeOffAwareness: "The answer does not name a trade-off.",
            communication: "The answer is understandable.",
            confidence: "The answer is tentative.",
            relevance: "The answer is only partially aligned with the exact question.",
          },
        },
      ],
    })} />);

    fireEvent.click(screen.getByRole("button", { name: "React architecture feedback" }));

    const details = screen.getByRole("region", { name: "React architecture feedback" });
    expect(within(details).getByText("Broader practice")).toBeInTheDocument();
    expect(within(details).getByText("Gemini returned invalid blueprint JSON after one repair attempt.")).toBeInTheDocument();
  });

  it("omits legacy-empty sections while still showing question and answer content", () => {
    render(<ResultsFeedbackCards session={session()} />);

    fireEvent.click(screen.getByRole("button", { name: "Performance feedback" }));

    const details = screen.getByRole("region", { name: "Performance feedback" });
    expect(details).toBeInTheDocument();
    expect(screen.getByText("How do you keep a search UI responsive at 50,000 results?")).toBeInTheDocument();
    expect(screen.getByText("I would virtualize the list, debounce network work, and keep keyboard focus state outside each row.")).toBeInTheDocument();
    expect(within(details).queryByText("Missing points")).not.toBeInTheDocument();
    expect(within(details).queryByText("Better structure")).not.toBeInTheDocument();
    expect(within(details).queryByText("Improved answer")).not.toBeInTheDocument();
  });

  it("pairs each evaluation with the matching answered question even when evaluation rows were hydrated out of order", () => {
    render(<ResultsFeedbackCards session={session({
      evaluations: [
        {
          score: 6,
          questionId: "question-2",
          competencyId: "performance",
          competency: "Performance",
          dimensions: {},
          strengths: ["Recognized virtualization quickly."],
          needsWork: ["Explain how keyboard state survives list windowing."],
          missingPoints: [],
          betterStructure: [],
          improvedAnswer: "",
        },
        {
          score: 8,
          questionId: "question-1",
          competencyId: "react-architecture",
          competency: "React architecture",
          dimensions: { structure: 9, tradeOffAwareness: 8, clarity: 7 },
          strengths: ["Phased the migration with rollback gates."],
          needsWork: ["Call out how you would verify each rollout stage."],
          missingPoints: ["Name the signal that would trigger a rollback."],
          betterStructure: ["Start with constraints, then walk through phases, and close with rollback criteria."],
          improvedAnswer: "I would begin with the constraints, phase the migration by route, and define explicit rollback criteria for each milestone.",
        },
      ],
    })} />);

    fireEvent.click(screen.getByRole("button", { name: "Performance feedback" }));

    const details = screen.getByRole("region", { name: "Performance feedback" });
    expect(within(details).getByText("How do you keep a search UI responsive at 50,000 results?")).toBeInTheDocument();
    expect(within(details).getByText("I would virtualize the list, debounce network work, and keep keyboard focus state outside each row.")).toBeInTheDocument();
    expect(within(details).queryByText("How would you phase a large React migration?")).not.toBeInTheDocument();
  });

  it("uses whitespace-free stable disclosure ids and labels the region from its toggle", () => {
    render(<ResultsFeedbackCards session={session({
      evaluations: [
        {
          score: 8,
          questionId: "question-1",
          competencyId: "react architecture",
          competency: "React architecture",
          dimensions: { structure: 9, tradeOffAwareness: 8, clarity: 7 },
          strengths: ["Phased the migration with rollback gates."],
          needsWork: ["Call out how you would verify each rollout stage."],
          missingPoints: ["Name the signal that would trigger a rollback."],
          betterStructure: ["Start with constraints, then walk through phases, and close with rollback criteria."],
          improvedAnswer: "I would begin with the constraints, phase the migration by route, and define explicit rollback criteria for each milestone.",
        },
      ],
    })} />);

    const toggle = screen.getByRole("button", { name: "React architecture feedback" });
    fireEvent.click(toggle);

    const region = screen.getByRole("region", { name: "React architecture feedback" });
    const controls = toggle.getAttribute("aria-controls");
    expect(controls).toBeTruthy();
    expect(controls).not.toMatch(/\s/);
    expect(toggle.id).not.toMatch(/\s/);
    expect(region.id).toBe(controls);
    expect(region).toHaveAttribute("aria-labelledby", toggle.id);
    expect(region.parentElement?.style.viewTransitionName).toBe("evaluation-card-question-question-1-0");
  });

  it("shows the assistance that produced a score", async () => {
    renderResults({ questions: [answeredQuestionWithAssistance(2)] });

    expect(await screen.findByText(/after two rescues/i)).toBeInTheDocument();
  });

  it("shows Not attempted, not a zero score, for a question the candidate never attempted", async () => {
    renderResults({ questions: [unansweredNonAnswerQuestion()] });

    expect(await screen.findByText("Not attempted")).toBeInTheDocument();
  });
});

describe("App authentication shell", () => {
  it("shows the sign-in screen while nobody is signed in", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: null });
    mockRoutes({});

    render(<App />);
    expect(screen.getByText("Checking your sign-in…")).toBeInTheDocument();

    expect(await screen.findByRole("heading", { name: "Practice with your own career context." })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
  });

  it("starts Google OAuth from the sign-in screen", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: null });
    mockRoutes({});

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Continue with Google" }));

    await waitFor(() => expect(signInWithOAuth).toHaveBeenCalledWith({
      provider: "google",
      options: { redirectTo: `${window.location.origin}/auth/callback` },
    }));
  });

  it("surfaces a failed sign-in without leaving the sign-in screen", async () => {
    getUser.mockResolvedValue({ data: { user: null }, error: null });
    signInWithOAuth.mockResolvedValue({ error: new Error("Google rejected the request.") });
    mockRoutes({});

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Continue with Google" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Google rejected the request.");
    expect(screen.getByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
  });

  it("signs out back to the sign-in screen", async () => {
    mockRoutes({
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Ready when you are." });
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(signOut).toHaveBeenCalled());
    expect(await screen.findByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
  });

  it("returns to the sign-in screen when coach data comes back unauthenticated", async () => {
    mockRoutes({
      "/api/profile": () => ({ ok: false, status: 401, body: { error: "Sign in to continue." } }),
    });

    render(<App />);

    expect(await screen.findByRole("button", { name: "Continue with Google" })).toBeInTheDocument();
    // A 401 signs the shell out silently; it never shows the raw request error.
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("App onboarding and profile review", () => {
  it("creates a profile from pasted CV text and opens profile review", async () => {
    const created = profile();
    const fetchMock = mockRoutes({
      "/api/profile": (init) => init?.method
        ? { body: { profile: created, demoMode: false } }
        : { body: { profile: null, demoMode: false } },
      "/api/career/dashboard": () => ({ body: dashboardPayload(created, [], emptyReadiness()) }),
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Make your next interview feel familiar." });

    fireEvent.change(screen.getByPlaceholderText("Senior Frontend Engineer · React · TypeScript · achievements…"), {
      target: { value: "Senior frontend engineer with checkout migration experience." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create my profile" }));

    await screen.findByRole("heading", { name: "Make the coach accurate." });
    const createCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(requestBody(createCall?.[1] as RequestInit)).toEqual({
      cvText: "Senior frontend engineer with checkout migration experience.",
      coverLetter: "",
    });
    // The review form is seeded from the returned profile, not left blank.
    expect(screen.getByDisplayValue("Senior Frontend Engineer")).toBeInTheDocument();
    expect(screen.getByDisplayValue("React, TypeScript")).toBeInTheDocument();
  });

  it("reports a failed profile creation instead of advancing to review", async () => {
    mockRoutes({
      "/api/profile": (init) => init?.method
        ? { ok: false, status: 500, body: { error: "Could not read that CV." } }
        : { body: { profile: null, demoMode: false } },
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Make your next interview feel familiar." });

    fireEvent.change(screen.getByPlaceholderText("Senior Frontend Engineer · React · TypeScript · achievements…"), {
      target: { value: "Frontend engineer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create my profile" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not read that CV.");
    expect(screen.queryByRole("heading", { name: "Make the coach accurate." })).not.toBeInTheDocument();
  });

  it("confirms an edited profile and lands on home", async () => {
    const created = profile();
    const fetchMock = mockRoutes({
      "/api/profile": (init) => init?.method
        ? { body: { profile: created, demoMode: false } }
        : { body: { profile: null, demoMode: false } },
      "/api/career/dashboard": () => ({ body: dashboardPayload(created, [], emptyReadiness()) }),
    });

    render(<App />);
    await screen.findByRole("heading", { name: "Make your next interview feel familiar." });
    fireEvent.change(screen.getByPlaceholderText("Senior Frontend Engineer · React · TypeScript · achievements…"), {
      target: { value: "Frontend engineer" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create my profile" }));

    await screen.findByRole("heading", { name: "Make the coach accurate." });
    fireEvent.change(screen.getByDisplayValue("Senior Frontend Engineer"), { target: { value: "Staff Frontend Engineer" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm profile" }));

    await screen.findByRole("heading", { name: "Ready when you are." });
    const confirmCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PUT");
    expect(requestBody(confirmCall?.[1] as RequestInit)).toEqual({
      profile: {
        role: "Staff Frontend Engineer",
        seniority: "Senior",
        narrative: "Leads complex React work.",
        expertise: ["React", "TypeScript"],
      },
    });
  });
});

describe("App conversation interview", () => {
  it("starts a grounded conversation and renders the interviewer message", async () => {
    const started = activeConversationSession();
    const fetchMock = await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    const startCall = fetchMock.mock.calls.find(([url, init]) => url === "/api/practice" && (init as RequestInit | undefined)?.method === "POST");
    expect(requestBody(startCall?.[1] as RequestInit)).toMatchObject({ action: "start_manual", primaryFocus: "Practice focus" });
    expect(screen.getByText("Mixed interview · 0 of 1 answered")).toBeInTheDocument();
    expect(screen.getByText("How would you phase a large React migration?")).toBeInTheDocument();
    expect(screen.getByText("Grounded question")).toBeInTheDocument();
  });

  // A sparse profile's discovery blueprint and a Gemini provider fallback
  // share the same `limited-grounding` status (no new enum value), so the
  // shell's own heading stays neutral -- "Broader practice" -- and
  // `fallbackReason` is what actually distinguishes the two causes for the
  // user reading it live, in an active conversation.
  it("shows a Broader practice discovery banner through the real shell when the session starts with limited grounding", async () => {
    const started = activeConversationSession({
      blueprint: {
        status: "limited-grounding",
        fallbackReason: "Your source profile has limited concrete example detail, so this session starts broader and helps you uncover real examples as you answer.",
        maxFollowUps: 3,
        maxQuestions: 8,
        createdAt: "2026-09-01T12:00:00.000Z",
        roundId: "tech-lead",
        turnBudget: 8,
        targets: [],
        questions: [
          {
            id: "question-1",
            sequence: 1,
            category: "experience",
            competencyId: "react-architecture",
            competencyName: "React architecture",
            difficulty: "senior",
            isFollowUp: false,
            prompt: "How would you phase a large React migration?",
            answer: null,
            createdAt: "2026-09-01T12:00:00.000Z",
            objective: "Discover a concrete project to ground later questions in.",
            evidenceIds: [],
            expectedSignals: ["ownership"],
            missingSignalPrompts: [],
            followUpLimit: 1,
            sourceConfidence: null,
            askedIntent: null,
            assistance: [],
            nonAnswer: false,
            setAsideAt: null,
            setAsideReason: null,
            nonAnswers: [],
          },
        ],
      },
    });
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    expect(screen.getByText("Broader practice")).toBeInTheDocument();
    expect(screen.getByText(/helps you uncover real examples/i)).toBeInTheDocument();
  });

  // A discovery question's `evidenceIds` is deliberately empty (finding 2 in the
  // final review dropped the evidence anchor entirely). The interviewer-message
  // grounding line must not describe that as a deficit ("Grounded in 0 source
  // evidence items") -- across a full discovery session every non-introduction
  // question would render that same discouraging count.
  it("does not present an evidence-free discovery question's grounding line as a deficit", async () => {
    const started = activeConversationSession({
      blueprint: {
        status: "limited-grounding",
        fallbackReason: "Your source profile has limited concrete example detail, so this session starts broader.",
        maxFollowUps: 3,
        maxQuestions: 8,
        createdAt: "2026-09-01T12:00:00.000Z",
        roundId: "tech-lead",
        turnBudget: 8,
        targets: [],
        questions: [
          {
            id: "question-1",
            sequence: 1,
            category: "experience",
            competencyId: null,
            competencyName: null,
            difficulty: "senior",
            isFollowUp: false,
            prompt: "How would you phase a large React migration?",
            answer: null,
            createdAt: "2026-09-01T12:00:00.000Z",
            objective: "General objective: Surface one concrete example of real work the candidate can describe in detail.",
            evidenceIds: [],
            expectedSignals: ["ownership"],
            missingSignalPrompts: [],
            followUpLimit: 1,
            sourceConfidence: null,
            askedIntent: null,
            assistance: [],
            nonAnswer: false,
            setAsideAt: null,
            setAsideReason: null,
            nonAnswers: [],
          },
        ],
      },
    });
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    expect(screen.queryByText(/0 source evidence item/)).not.toBeInTheDocument();
    expect(screen.getByText(/Broader question/)).toBeInTheDocument();
  });

  // The blueprint's `objective`, `expectedSignals`, and rubric are the
  // interviewer's own contract: showing them beside the live question tells the
  // candidate exactly what the evaluator scores before they answer. Only the
  // grounding provenance line belongs in the live conversation; the objective
  // and signals stay in the results feedback card, after the answer is scored.
  it("keeps the interviewer's objective and expected signals out of the live conversation", async () => {
    const started = activeConversationSession({
      blueprint: {
        status: "grounded",
        fallbackReason: null,
        maxFollowUps: 3,
        maxQuestions: 8,
        createdAt: "2026-09-01T12:00:00.000Z",
        roundId: "tech-lead",
        turnBudget: 8,
        targets: [],
        questions: [
          {
            id: "question-1",
            sequence: 1,
            category: "experience",
            competencyId: "react-architecture",
            competencyName: "React architecture",
            difficulty: "senior",
            isFollowUp: false,
            prompt: "How would you phase a large React migration?",
            answer: null,
            createdAt: "2026-09-01T12:00:00.000Z",
            objective: "Probe migration ownership and rollout trade-offs.",
            evidenceIds: [],
            expectedSignals: ["ownership", "trade-off"],
            missingSignalPrompts: [],
            followUpLimit: 1,
            sourceConfidence: null,
            askedIntent: null,
            assistance: [],
            nonAnswer: false,
            setAsideAt: null,
            setAsideReason: null,
            nonAnswers: [],
          },
        ],
      },
    });
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    expect(screen.queryByText("Question objective")).not.toBeInTheDocument();
    expect(screen.queryByText(/Probe migration ownership/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Expected signals/)).not.toBeInTheDocument();
    expect(screen.getByText(/Broader question/)).toBeInTheDocument();
  });

  it("sends an answer and clears the composer", async () => {
    const started = activeConversationSession();
    const answered = session({
      ...started,
      questions: [question(1, "How would you phase a large React migration?", "Phase by route.", "react-architecture", "React architecture")],
      messages: [
        ...started.messages,
        { id: "question-1:answer", role: "candidate", content: "Phase by route.", createdAt: "2026-08-29T10:05:00.000Z" },
      ],
    });
    const fetchMock = await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/interview": () => ({ body: { session: answered } }),
    });

    const composer = screen.getByPlaceholderText("Answer as if you were in the room…");
    fireEvent.change(composer, { target: { value: "Phase by route." } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));

    await screen.findByText("Mixed interview · 1 of 1 answered");
    expect(composer).toHaveValue("");
    const respondCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST"
      && requestBody(init as RequestInit).action === "respond");
    expect(requestBody(respondCall?.[1] as RequestInit)).toEqual({
      action: "respond",
      sessionId: "session-active",
      answer: "Phase by route.",
    });
  });

  it("shows the results view once the interview completes", async () => {
    const started = activeConversationSession();
    const completed = session({ id: "session-active", overallScore: 7.5, resultSummary: { summary: "Strong migration reasoning." } });
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [completed], emptyReadiness()) }),
      "/api/interview": () => ({ body: { session: completed, profile: profile() } }),
    });

    fireEvent.change(screen.getByPlaceholderText("Answer as if you were in the room…"), { target: { value: "Phase by route." } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));

    expect(await screen.findByRole("heading", { name: "A useful baseline." })).toBeInTheDocument();
    expect(screen.getByText("Strong migration reasoning.")).toBeInTheDocument();
    expect(screen.getByText("7.5")).toBeInTheDocument();
  });

  it("reports a failed answer without clearing the composer", async () => {
    const started = activeConversationSession();
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/interview": () => ({ ok: false, status: 500, body: { error: "The coach is unavailable." } }),
    });

    fireEvent.change(screen.getByPlaceholderText("Answer as if you were in the room…"), { target: { value: "Phase by route." } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The coach is unavailable.");
    expect(screen.getByPlaceholderText("Answer as if you were in the room…")).toHaveValue("Phase by route.");
  });
});

describe("App interview mode choice and pending turn state", () => {
  it("offers coach and real mode before a practice session starts", async () => {
    await renderShell();

    expect(await screen.findByRole("radio", { name: /coach/i })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /real/i })).toBeInTheDocument();
  });

  it("shows a pending state on the interviewer while the next question is authored", async () => {
    await renderShell({ pendingTurn: true });

    expect(await screen.findByText(/thinking/i)).toBeInTheDocument();
  });

  it("does not show the grounding provenance line during a live conversation", async () => {
    await renderShell({ activeSession: sessionWithUnansweredQuestion() });

    expect(screen.queryByText(/^Grounded in/)).not.toBeInTheDocument();
  });

  it("sends the chosen mode, hardcoded round, and the finished session's opportunity when starting another interview", async () => {
    const withOpportunity = session({ id: "session-active", overallScore: 7.5, opportunityId: "opp-1" });
    const nextSession = activeConversationSession();
    const fetchMock = await startInterviewFrom("conversation", activeConversationSession(), {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [withOpportunity], emptyReadiness()) }),
      "/api/interview": (init) => {
        const body = requestBody(init);
        if (body.action === "respond") return { body: { session: withOpportunity, profile: profile() } };
        if (body.action === "start") return { body: { session: nextSession } };
        throw new Error(`Unexpected /api/interview action in this test: ${body.action}`);
      },
    });

    fireEvent.change(screen.getByPlaceholderText("Answer as if you were in the room…"), { target: { value: "Phase by route." } });
    fireEvent.click(screen.getByRole("button", { name: "Send answer" }));
    await screen.findByRole("heading", { name: "A useful baseline." });

    fireEvent.click(screen.getByRole("radio", { name: /coach/i }));
    fireEvent.click(screen.getByRole("button", { name: "Start another interview" }));

    await screen.findByRole("heading", { name: "Stay in the conversation." });
    const startCall = fetchMock.mock.calls.find(([url, init]) => url === "/api/interview" && requestBody(init as RequestInit).action === "start");
    expect(requestBody(startCall?.[1] as RequestInit)).toEqual({
      action: "start",
      mode: "coach",
      roundId: "tech-lead",
      opportunityId: "opp-1",
    });
  });
});

describe("App hands-on interview", () => {
  it("opens the exercise brief with the starter code loaded into the workspace", async () => {
    const started = activeHandsOnSession();
    const fetchMock = await startInterviewFrom("hands-on", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    const startCall = fetchMock.mock.calls.find(([url, init]) => url === "/api/practice" && (init as RequestInit | undefined)?.method === "POST");
    expect(requestBody(startCall?.[1] as RequestInit)).toMatchObject({ action: "start_manual", format: "hands_on" });
    expect(screen.getByRole("heading", { name: "Accessible product search" })).toBeInTheDocument();
    expect(screen.getByText("Debounce the query")).toBeInTheDocument();
    expect(screen.getByLabelText("TypeScript code workspace")).toHaveValue("export function ProductSearch() {}");
    expect(screen.getByText("0 checkpoints saved")).toBeInTheDocument();
  });

  it("saves a checkpoint and clears the note", async () => {
    const started = activeHandsOnSession();
    const withCheckpoint = activeHandsOnSession({
      checkpoints: [
        {
          id: "checkpoint-1",
          code: "export function ProductSearch() {}",
          note: "Cancelling in-flight searches first.",
          interviewerPrompt: "How will you keep focus stable?",
          createdAt: "2026-08-29T10:10:00.000Z",
        },
      ],
    });
    const fetchMock = await startInterviewFrom("hands-on", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/interview": () => ({ body: { session: withCheckpoint } }),
    });

    const note = screen.getByPlaceholderText("For example: I am cancelling in-flight searches and will add keyboard state next…");
    fireEvent.change(note, { target: { value: "Cancelling in-flight searches first." } });
    fireEvent.click(screen.getByRole("button", { name: "Save checkpoint" }));

    await screen.findByText("1 checkpoint saved");
    expect(note).toHaveValue("");
    const checkpointCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST"
      && requestBody(init as RequestInit).action === "checkpoint");
    expect(requestBody(checkpointCall?.[1] as RequestInit)).toEqual({
      action: "checkpoint",
      sessionId: "session-hands-on",
      code: "export function ProductSearch() {}",
      note: "Cancelling in-flight searches first.",
    });
  });
});

describe("App answer transcription", () => {
  /**
   * Minimal `MediaRecorder` stand-in: jsdom has none, and the shell only uses
   * `state`, `mimeType`, `ondataavailable`, `onstop`, `start`, and `stop`.
   */
  class FakeMediaRecorder {
    state: "inactive" | "recording" = "inactive";
    mimeType = "audio/webm";
    ondataavailable: ((event: { data: Blob }) => void) | null = null;
    onstop: (() => void) | null = null;

    constructor(readonly stream: MediaStream) {}

    start() {
      this.state = "recording";
    }

    stop() {
      this.state = "inactive";
      this.ondataavailable?.({ data: new Blob(["audio"], { type: "audio/webm" }) });
      this.onstop?.();
    }
  }

  /**
   * Stubs Web Audio with an analyser that always reports `level` as the sample
   * amplitude, so a test can present a silent or an audible microphone.
   */
  function stubAudioContext(level: number) {
    class FakeAudioContext {
      createAnalyser() {
        return { fftSize: 2048, getFloatTimeDomainData: (target: Float32Array) => target.fill(level) };
      }
      createMediaStreamSource() {
        return { connect: vi.fn() };
      }
      close() {
        return Promise.resolve();
      }
    }
    vi.stubGlobal("AudioContext", FakeAudioContext);
  }

  function stubMicrophone() {
    const track = { stop: vi.fn() };
    const getUserMedia = vi.fn(async () => ({ getTracks: () => [track] }) as unknown as MediaStream);
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia }, configurable: true });
    vi.stubGlobal("MediaRecorder", FakeMediaRecorder);
    return { getUserMedia, track };
  }

  afterEach(() => {
    Reflect.deleteProperty(navigator, "mediaDevices");
    vi.unstubAllGlobals();
  });

  it("appends the transcript of a recorded answer to the composer", async () => {
    const { track } = stubMicrophone();
    const started = activeConversationSession();
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/transcribe": () => ({ body: { transcript: "I would phase the migration by route." } }),
    });

    fireEvent.click(screen.getByRole("button", { name: "● Record answer" }));
    fireEvent.click(await screen.findByRole("button", { name: "■ Stop & transcribe" }));

    await waitFor(() => expect(screen.getByPlaceholderText("Answer as if you were in the room…"))
      .toHaveValue("I would phase the migration by route."));
    expect(track.stop).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "● Record answer" })).toBeInTheDocument();
  });

  it("reports a failed transcription and leaves the composer untouched", async () => {
    stubMicrophone();
    const started = activeConversationSession();
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/transcribe": () => ({ ok: false, status: 500, body: { error: "Could not transcribe recording." } }),
    });

    fireEvent.click(screen.getByRole("button", { name: "● Record answer" }));
    fireEvent.click(await screen.findByRole("button", { name: "■ Stop & transcribe" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not transcribe recording.");
    expect(screen.getByPlaceholderText("Answer as if you were in the room…")).toHaveValue("");
  });

  it("refuses to transcribe a recording that picked up no speech", async () => {
    stubMicrophone();
    stubAudioContext(0);
    const transcribe = vi.fn(() => ({ body: { transcript: "An answer nobody spoke." } }));
    const started = activeConversationSession();
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/transcribe": transcribe,
    });

    fireEvent.click(screen.getByRole("button", { name: "● Record answer" }));
    fireEvent.click(await screen.findByRole("button", { name: "■ Stop & transcribe" }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("No speech was picked up. Check your microphone, or type your answer instead.");
    expect(transcribe).not.toHaveBeenCalled();
    expect(screen.getByPlaceholderText("Answer as if you were in the room…")).toHaveValue("");
    expect(screen.getByRole("button", { name: "● Record answer" })).toBeInTheDocument();
  });

  it("transcribes a recording that picked up speech", async () => {
    stubMicrophone();
    stubAudioContext(0.4);
    const started = activeConversationSession();
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
      "/api/transcribe": () => ({ body: { transcript: "I would phase the migration by route." } }),
    });

    fireEvent.click(screen.getByRole("button", { name: "● Record answer" }));
    fireEvent.click(await screen.findByRole("button", { name: "■ Stop & transcribe" }));

    await waitFor(() => expect(screen.getByPlaceholderText("Answer as if you were in the room…"))
      .toHaveValue("I would phase the migration by route."));
  });

  it("explains that recording is unavailable when the browser has no MediaRecorder", async () => {
    const started = activeConversationSession();
    await startInterviewFrom("conversation", started, {
      "/api/profile": () => ({ body: { profile: profile(), demoMode: false } }),
      "/api/career/dashboard": () => ({ body: dashboardPayload(profile(), [], emptyReadiness()) }),
    });

    fireEvent.click(screen.getByRole("button", { name: "● Record answer" }));

    expect(await screen.findByRole("alert"))
      .toHaveTextContent("Voice recording is not available in this browser. Type your answer instead.");
  });
});

"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  ApiError,
  api,
  addOpportunityNote,
  attachCareerStoryProfileEvidence,
  confirmCareerStory,
  confirmObservation,
  correctObservation,
  createCareerStory,
  createOpportunity,
  dismissObservation,
  fetchCareerDashboard,
  fetchCareerStories,
  fetchObservations,
  fetchOpportunityEvents,
  retireCareerStory,
  scheduleOpportunityInterview,
  startManualPractice,
  startRecommendedPractice,
  transitionOpportunity,
  updateCareerStory,
  updateOpportunity,
  type CreateCareerStoryRequest,
  type CreateOpportunityRequest,
  type ManualPracticeRequest,
  type ObservationsOverview,
  type OpportunityTransitionOptions,
  type ScheduleOpportunityInterviewOptions,
  type UpdateCareerStoryRequest,
} from "@/app/api-client";
import { ResultsFeedbackCards } from "@/app/results-feedback-cards";
import { ApplicationsView } from "@/app/views/applications-view";
import { CoachView } from "@/app/views/coach-view";
import { HomeView } from "@/app/views/home-view";
import { PracticeView } from "@/app/views/practice-view";
import { StoriesView } from "@/app/views/stories-view";
import type {
  CareerDashboard,
  CareerStory,
  CareerStoryEvidence,
  CareerStorySummary,
  CoachObservation,
  EvidenceItem,
  HandsOnExercise,
  InterviewMode,
  InterviewSession,
  Opportunity,
  OpportunityEvent,
  OpportunityStatus,
  Profile,
  ReadinessModel,
  UpdateOpportunityDetailsInput,
} from "@/lib/types";
import { profileReadinessCopy } from "@/app/profile-readiness";
import { readinessViewModel } from "@/app/progress-view-model";
import { canExplicitlyCompleteConversation } from "@/lib/conversation-completion";
import { createBrowserSupabaseClient } from "@/lib/supabase/client";
import { MAX_CV_PDF_BYTES } from "@/lib/upload-limits";

type View = "home" | "onboarding" | "profile-review" | "interview" | "results" | "progress" | "profile" | "practice" | "applications" | "stories" | "coach";
/**
 * Which kind of session {@link RelayShell.startInterview} starts: a normal
 * conversation round, or the one-off hands-on coding exercise. Distinct from
 * the real `InterviewMode` (`"coach" | "real"`, imported from `@/lib/types`)
 * that governs how a CONVERSATION session behaves once it's running --
 * unrelated value spaces that happened to share a name.
 */
type SessionKind = "conversation" | "hands-on";
type AuthState = "loading" | "signed-out" | "signed-in";
// Primary navigation order per the Release 2 information architecture (task-10
// brief step 7). "progress" is deliberately absent -- it stays reachable from
// Home's "Open progress" button instead of a sidebar tab, and "interview"/
// "results" are transient views entered from Practice, never listed here.
const nav: View[] = ["home", "applications", "practice", "stories", "coach", "profile"];
function startViewTransition(update: () => void) {
  const documentWithTransition = document as Document & { startViewTransition?: (callback: () => void) => void };
  if (documentWithTransition.startViewTransition) {
    documentWithTransition.startViewTransition(update);
    return;
  }
  update();
}

type CareerData = {
  profile: Profile | null;
  demoMode: boolean;
  /** The full Career Brain dashboard, or null when the caller has no profile yet (onboarding). */
  dashboard: CareerDashboard | null;
  /** The Stories view's full read model -- every story, retired ones included (see {@link fetchCareerStories}). */
  stories: CareerStorySummary[];
  /** The Coach view's full read model -- `dashboard.observations` excludes dismissed rows and carries no history at all. */
  observations: ObservationsOverview;
};

const EMPTY_OBSERVATIONS: ObservationsOverview = { active: [], history: [] };

/**
 * Loads the shell's post-auth data. `/api/profile` decides the onboarding
 * vs. home branch (and carries `demoMode`, which reflects process
 * configuration rather than Career Brain data); when a profile exists,
 * `/api/career/dashboard` -- the single canonical Career Brain read model --
 * is fetched for readiness, sessions, opportunities, observations, stories,
 * and the current practice recommendation. Replaces the old `loadCoachData`,
 * which read readiness/sessions from `GET /api/interview`; that endpoint is
 * still used for interview actions (`POST /api/interview`), just no longer
 * for the shell's bootstrapping read model.
 *
 * Stories and Coach are dedicated detail views (design sections 4.3/4.4),
 * not derived from the Home-oriented dashboard, so their own read models are
 * fetched alongside it: `GET /api/stories` (every story, retired ones
 * included) and `GET /api/observations` (active/history, since
 * `dashboard.observations` carries no dismissed-observation history at all).
 */
async function loadCareerData(): Promise<CareerData> {
  const profileResult = await api<{ profile: Profile | null; demoMode: boolean }>("/api/profile");
  if (!profileResult.profile) {
    return { profile: null, demoMode: profileResult.demoMode, dashboard: null, stories: [], observations: EMPTY_OBSERVATIONS };
  }
  const [dashboard, stories, observations] = await Promise.all([
    fetchCareerDashboard(),
    fetchCareerStories(),
    fetchObservations(),
  ]);
  return { profile: dashboard.profile, demoMode: profileResult.demoMode, dashboard, stories, observations };
}

function progressTrendLabel(trend: ReadinessModel["overallTrend"]): string {
  switch (trend) {
    case "improving":
      return "Improving";
    case "stable":
      return "Stable";
    case "worsening":
      return "Needs attention";
    case "unresolved":
      return "Not enough evidence yet";
  }
}

function progressTrendDescription(trend: ReadinessModel["overallTrend"]): string {
  switch (trend) {
    case "improving":
      return "Your latest sessions are trending upward against your earlier practice history.";
    case "stable":
      return "Your recent sessions are holding steady, which makes focused practice the next lever.";
    case "worsening":
      return "Recent sessions dipped below your earlier trend, so revisit the recurring weak spots next.";
    case "unresolved":
      return "There isn't enough evidence yet to call a trend — keep practicing to build a reliable signal.";
  }
}

/** Turns a machine dimension id like `"system-design"` into display text like `"System design"`. */
function humanizeDimension(dimension: string): string {
  const spaced = dimension.replace(/-/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

function evidenceLabel(item: EvidenceItem): string {
  const label = item.projectOrEmployer?.trim()
    || item.ownership?.trim()
    || item.outcome?.trim()
    || item.sourceExcerpt.trim();
  return label.replace(/\s+/g, " ").slice(0, 96);
}

function evidenceSummary(item: EvidenceItem): string {
  const summary = [
    item.projectOrEmployer?.trim(),
    item.ownership?.trim(),
    item.outcome?.trim(),
  ].filter((value): value is string => Boolean(value));

  return summary.length > 0 ? summary.join(" · ") : evidenceLabel(item);
}

/**
 * Relay's client shell: the signed-in application surface rendered by the
 * route at `src/app/page.tsx`. It owns auth state, the coach data it loads
 * once after sign-in, and every view the navigation switches between.
 *
 * This is the `"use client"` boundary for the whole app — every view it
 * renders is a Client Component, so browser-only concerns (Supabase auth,
 * `MediaRecorder`, View Transitions) stay on this side of the boundary.
 */
/**
 * Loudest sample a take must reach before it is worth transcribing.
 *
 * Gemini does not fail on silence: handed a silent recording it returns a
 * fluent, entirely invented interview answer. The composer must therefore
 * decide for itself whether the microphone heard anything.
 */
const speechFloor = 0.02;

/**
 * Watches a microphone stream and reports the loudest sample seen so far.
 *
 * Returns null when the browser has no Web Audio support, in which case the
 * caller transcribes unguarded rather than losing the feature outright.
 */
function monitorSpeech(stream: MediaStream): { peak: () => number; stop: () => void } | null {
  const AudioContextConstructor = window.AudioContext
    ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
  if (!AudioContextConstructor) return null;
  const context = new AudioContextConstructor();
  const analyser = context.createAnalyser();
  analyser.fftSize = 2048;
  context.createMediaStreamSource(stream).connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  let peak = 0;
  const sample = () => {
    analyser.getFloatTimeDomainData(samples);
    for (const value of samples) peak = Math.max(peak, Math.abs(value));
  };
  sample();
  const timer = window.setInterval(sample, 100);
  return {
    peak: () => peak,
    stop: () => { window.clearInterval(timer); void context.close(); },
  };
}

export function RelayShell() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [dashboard, setDashboard] = useState<CareerDashboard | null>(null);
  const [stories, setStories] = useState<CareerStorySummary[]>([]);
  const [observations, setObservations] = useState<ObservationsOverview>(EMPTY_OBSERVATIONS);
  const [session, setSession] = useState<InterviewSession | null>(null);
  const [sessions, setSessions] = useState<InterviewSession[]>([]);
  const [readinessModel, setReadinessModel] = useState<ReadinessModel | null>(null);
  const [view, setView] = useState<View>("onboarding");
  const [authState, setAuthState] = useState<AuthState>("loading");
  const [coachDataLoading, setCoachDataLoading] = useState(true);
  const [cvText, setCvText] = useState("");
  const [cvPdf, setCvPdf] = useState<File | null>(null);
  const [coverLetter, setCoverLetter] = useState("");
  const [draftRole, setDraftRole] = useState("");
  const [draftSeniority, setDraftSeniority] = useState("");
  const [draftNarrative, setDraftNarrative] = useState("");
  const [draftExpertise, setDraftExpertise] = useState("");
  const [answer, setAnswer] = useState("");
  const [code, setCode] = useState("");
  const [checkpointNote, setCheckpointNote] = useState("");
  /** The mode the NEXT conversation session (chosen on the results screen) will start in. Real by default, so practice defaults to an honest signal rather than a softened one. */
  const [mode, setMode] = useState<InterviewMode>("real");
  /** True only while a sent answer's turn (two sequential model calls, spec §13.1) is in flight -- distinct from the shell-wide `busy` flag, which also covers unrelated requests that shouldn't show the interviewer "thinking". */
  const [pendingTurn, setPendingTurn] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(() => typeof window !== "undefined" && new URLSearchParams(window.location.search).get("authError") === "signin"
    ? "Google sign-in did not complete. Please try again."
    : "");
  const [isRecording, setIsRecording] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const recordingChunks = useRef<BlobPart[]>([]);
  const supabase = useRef<ReturnType<typeof createBrowserSupabaseClient> | null>(null);

  useEffect(() => {
    let active = true;
    Promise.resolve().then(() => {
      const client = createBrowserSupabaseClient();
      supabase.current = client;
      return client.auth.getUser();
    }).then(({ data, error: authError }) => {
        if (!active) return;
        if (authError) setError("Could not confirm your sign-in. Please try again.");
        setAuthState(data.user ? "signed-in" : "signed-out");
    }).catch((caught) => {
      if (active) {
        setError(caught instanceof Error ? caught.message : "Could not confirm your sign-in. Please try again.");
        setAuthState("signed-out");
      }
    });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (authState !== "signed-in") return;
    let active = true;
    loadCareerData().then((careerData) => {
      if (!active) return;
      setProfile(careerData.profile);
      setDemoMode(careerData.demoMode);
      setDashboard(careerData.dashboard);
      setSessions(careerData.dashboard?.recentSessions ?? []);
      setReadinessModel(careerData.dashboard?.readiness ?? null);
      setStories(careerData.stories);
      setObservations(careerData.observations);
      setView(careerData.profile ? "home" : "onboarding");
      setCoachDataLoading(false);
    }).catch((caught) => {
      if (!active) return;
      if (caught instanceof ApiError && caught.status === 401) {
        setProfile(null); setReadinessModel(null); setSession(null); setSessions([]); setDashboard(null); setStories([]); setObservations(EMPTY_OBSERVATIONS); setView("onboarding"); setAuthState("signed-out"); setCoachDataLoading(false);
        return;
      }
      setError(caught instanceof Error ? caught.message : "Could not open your coach data.");
      setCoachDataLoading(false);
    });
    return () => { active = false; };
  }, [authState]);

  /**
   * Re-fetches the canonical Career Brain dashboard after a mutation
   * (interview completion, an opportunity change) and mirrors it into the
   * shell's flat `profile`/`sessions`/`readinessModel` state so every view
   * stays consistent with a single source of truth.
   */
  async function refreshDashboard(): Promise<CareerDashboard | null> {
    try {
      const next = await fetchCareerDashboard();
      setProfile(next.profile);
      setDashboard(next);
      setSessions(next.recentSessions);
      setReadinessModel(next.readiness);
      return next;
    } catch (caught) {
      handleRequestError(caught, "Could not refresh your dashboard.");
      return null;
    }
  }

  /** Re-fetches the Stories view's full read model after a story mutation -- see {@link loadCareerData}'s doc comment for why this is separate from {@link refreshDashboard}. */
  async function refreshStories(): Promise<CareerStorySummary[] | null> {
    try {
      const next = await fetchCareerStories();
      setStories(next);
      return next;
    } catch (caught) {
      handleRequestError(caught, "Could not refresh your stories.");
      return null;
    }
  }

  /** Re-fetches the Coach view's full active/history split after an observation mutation. */
  async function refreshObservations(): Promise<ObservationsOverview | null> {
    try {
      const next = await fetchObservations();
      setObservations(next);
      return next;
    } catch (caught) {
      handleRequestError(caught, "Could not refresh your coach observations.");
      return null;
    }
  }

  const { hasEvidence, readiness, weakest } = readinessViewModel(readinessModel);
  const handsOn = session?.kind === "hands-on";
  const exercise = handsOn ? session?.exercise as HandsOnExercise : null;
  const sessionSummary = session ? String(session.resultSummary.summary ?? "Complete a few questions to receive personalized feedback.") : "";
  const answeredQuestions = session?.questions.filter((question) => question.answer).length ?? 0;
  // Mirrors the server rule in `@/lib/conversation-completion`, which the
  // interview route enforces: a planned practice conversation is shorter than
  // the generic five-question backbone, so gating Finish on five answers here
  // would leave the control permanently dead for every 2-4 question format.
  const canFinishConversation = session ? canExplicitlyCompleteConversation(session) : false;
  const conversationLabel = session?.practicePlanId ? "Practice session" : "Mixed interview";
  const progressTrend = readinessModel?.overallTrend ?? "unresolved";
  const progressTrendName = progressTrendLabel(progressTrend);
  const overallConfidence = readinessModel?.overallConfidence ?? null;
  const confidenceLabel = overallConfidence
    ? overallConfidence.charAt(0).toUpperCase() + overallConfidence.slice(1)
    : "Not enough evidence yet";
  const weakestDimensionLabel = weakest ? humanizeDimension(weakest.dimension) : null;
  const profileReadinessNote = profileReadinessCopy(profile?.readiness);

  function navigate(next: View) {
    startViewTransition(() => setView(next));
  }

  function persistSession(updated: InterviewSession) {
    setSession(updated);
    setSessions((items) => items.some((item) => item.id === updated.id) ? items.map((item) => item.id === updated.id ? updated : item) : [updated, ...items]);
  }
  function handleRequestError(caught: unknown, fallback: string) {
    if (caught instanceof ApiError && caught.status === 401) {
      setProfile(null); setReadinessModel(null); setSession(null); setSessions([]); setView("onboarding"); setAuthState("signed-out");
      return;
    }
    setError(caught instanceof Error ? caught.message : fallback);
  }
  function beginProfileReview(nextProfile: Profile) {
    setProfile(nextProfile);
    setDraftRole(nextProfile.role ?? "");
    setDraftSeniority(nextProfile.seniority ?? "");
    setDraftNarrative(nextProfile.narrative ?? "");
    setDraftExpertise(nextProfile.expertise.join(", "));
    navigate("profile-review");
  }
  async function createProfile(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      let result: { profile: Profile; demoMode: boolean };
      if (cvPdf) {
        if (cvPdf.size > MAX_CV_PDF_BYTES) {
          throw new Error("Keep CV PDFs under 4 MB so the upload fits Vercel's request limit.");
        }
        const formData = new FormData();
        formData.append("cv", cvPdf);
        formData.append("coverLetter", coverLetter);
        const response = await fetch("/api/profile", { method: "POST", body: formData });
        const body = await response.json() as { profile: Profile; demoMode: boolean; error?: string };
        if (!response.ok) throw new ApiError(body.error ?? "Could not create profile.", response.status);
        result = body;
      } else {
        result = await api<{ profile: Profile; demoMode: boolean }>("/api/profile", { method: "POST", body: JSON.stringify({ cvText, coverLetter }) });
      }
      setDemoMode(result.demoMode); beginProfileReview(result.profile);
    } catch (caught) { handleRequestError(caught, "Could not create profile."); } finally { setBusy(false); }
  }
  async function confirmProfile(event: FormEvent) {
    event.preventDefault(); if (!profile) return; setBusy(true); setError("");
    try {
      const expertise = draftExpertise.split(",").map((item) => item.trim()).filter(Boolean);
      const result = await api<{ profile: Profile; demoMode: boolean }>("/api/profile", { method: "PUT", body: JSON.stringify({ profile: { role: draftRole, seniority: draftSeniority, narrative: draftNarrative, expertise } }) });
      setProfile(result.profile); setDemoMode(result.demoMode);
      // A brand-new account's initial load never fetched the dashboard (no profile existed yet) -- confirming here is the first moment one exists, so Home needs it loaded before navigating.
      await refreshDashboard();
      navigate("home");
    } catch (caught) { handleRequestError(caught, "Could not save profile."); } finally { setBusy(false); }
  }
  /**
   * `body.mode` is overloaded by the `/api/interview` route (task 11): the
   * literal `"hands-on"` selects the hands-on branch, while any other value
   * chooses a conversation session's `InterviewMode` (defaulting to
   * `"real"` server-side). `roundId` is hardcoded to the only implemented
   * round (`IMPLEMENTED_ROUNDS` in `@/lib/interview-rounds`); `opportunityId`
   * carries forward the just-finished session's opportunity, if any, so
   * "start another" continues the same practice context.
   */
  async function startInterview(kind: SessionKind = "conversation") {
    setBusy(true); setError("");
    try {
      const body = kind === "hands-on"
        ? { action: "start", mode: "hands-on" }
        : { action: "start", mode, roundId: "tech-lead", opportunityId: session?.opportunityId ?? null };
      const result = await api<{ session: InterviewSession }>("/api/interview", { method: "POST", body: JSON.stringify(body) });
      persistSession(result.session); setAnswer(""); setCheckpointNote(""); setCode((result.session.exercise as Partial<HandsOnExercise>).starterCode ?? ""); navigate("interview");
    } catch (caught) { handleRequestError(caught, "Could not start interview."); } finally { setBusy(false); }
  }
  /** Home's dominant CTA. The recommendation is recomputed server-side on start, never replayed from what's on screen. */
  async function handleStartRecommended() {
    setBusy(true); setError("");
    try {
      const { session: startedSession } = await startRecommendedPractice();
      persistSession(startedSession); setAnswer(""); setCheckpointNote(""); setCode((startedSession.exercise as Partial<HandsOnExercise>).starterCode ?? ""); navigate("interview");
    } catch (caught) { handleRequestError(caught, "Could not start recommended practice."); } finally { setBusy(false); }
  }
  // --- Applications mutations -------------------------------------------
  // Each wraps one api-client call, refreshes the dashboard on success so
  // Home and Applications both reflect the change, and rethrows on failure
  // so ApplicationsView's local handler can keep its form open instead of
  // silently discarding the user's edits.
  async function handleCreateOpportunity(input: CreateOpportunityRequest): Promise<Opportunity> {
    setBusy(true); setError("");
    try {
      const created = await createOpportunity(input);
      await refreshDashboard();
      return created;
    } catch (caught) { handleRequestError(caught, "Could not add that application."); throw caught; } finally { setBusy(false); }
  }
  async function handleUpdateOpportunity(opportunityId: string, input: UpdateOpportunityDetailsInput): Promise<Opportunity> {
    setBusy(true); setError("");
    try {
      const updated = await updateOpportunity(opportunityId, input);
      await refreshDashboard();
      return updated;
    } catch (caught) { handleRequestError(caught, "Could not save those changes."); throw caught; } finally { setBusy(false); }
  }
  async function handleTransitionOpportunity(opportunityId: string, toStatus: OpportunityStatus, options?: OpportunityTransitionOptions): Promise<Opportunity> {
    setBusy(true); setError("");
    try {
      const transitioned = await transitionOpportunity(opportunityId, toStatus, options);
      await refreshDashboard();
      return transitioned;
    } catch (caught) { handleRequestError(caught, "Could not update that application's status."); throw caught; } finally { setBusy(false); }
  }
  async function handleScheduleOpportunityInterview(opportunityId: string, interviewAt: string, options?: ScheduleOpportunityInterviewOptions): Promise<Opportunity> {
    setBusy(true); setError("");
    try {
      const scheduled = await scheduleOpportunityInterview(opportunityId, interviewAt, options);
      await refreshDashboard();
      return scheduled;
    } catch (caught) { handleRequestError(caught, "Could not schedule that interview."); throw caught; } finally { setBusy(false); }
  }
  async function handleAddOpportunityNote(opportunityId: string, note: string): Promise<OpportunityEvent> {
    setBusy(true); setError("");
    try {
      const event = await addOpportunityNote(opportunityId, note);
      await refreshDashboard();
      return event;
    } catch (caught) { handleRequestError(caught, "Could not save that note."); throw caught; } finally { setBusy(false); }
  }
  /**
   * A read, not a mutation -- deliberately does not toggle the shell's global
   * `busy` flag (opening a detail panel shouldn't disable unrelated buttons)
   * and leaves ordinary failures to ApplicationsView's own local error state
   * rather than the shell's global banner. Still routes a 401 through
   * `handleRequestError` so an expired session still drops back to sign-in.
   *
   * Memoized: it sits in ApplicationsView's event-loading effect deps, so a
   * fresh function identity on every shell render (e.g. `busy` toggling
   * during an unrelated mutation while a detail panel is open) would refire
   * that effect and re-fetch the same events for no reason.
   */
  const handleLoadOpportunityEvents = useCallback(async (opportunityId: string): Promise<OpportunityEvent[]> => {
    try {
      return await fetchOpportunityEvents(opportunityId);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 401) handleRequestError(caught, "Could not load that application's history.");
      throw caught;
    }
  }, []);
  // --- Career stories mutations -------------------------------------------
  // Each refreshes BOTH the dedicated stories list (Stories view's own read
  // model) and the dashboard (Home's story-bank summary), matching R9 from
  // the task-10 brief. Mirrors the Applications mutation pattern: rethrow on
  // failure so StoriesView's own handler keeps its form open.
  async function handleCreateStory(input: CreateCareerStoryRequest): Promise<CareerStory> {
    setBusy(true); setError("");
    try {
      const created = await createCareerStory(input);
      await refreshStories();
      await refreshDashboard();
      return created;
    } catch (caught) { handleRequestError(caught, "Could not save that story."); throw caught; } finally { setBusy(false); }
  }
  async function handleUpdateStory(storyId: string, input: UpdateCareerStoryRequest): Promise<CareerStory> {
    setBusy(true); setError("");
    try {
      const updated = await updateCareerStory(storyId, input);
      await refreshStories();
      await refreshDashboard();
      return updated;
    } catch (caught) { handleRequestError(caught, "Could not save those changes."); throw caught; } finally { setBusy(false); }
  }
  async function handleConfirmStory(storyId: string): Promise<CareerStory> {
    setBusy(true); setError("");
    try {
      const confirmed = await confirmCareerStory(storyId);
      await refreshStories();
      await refreshDashboard();
      return confirmed;
    } catch (caught) { handleRequestError(caught, "Could not confirm that story."); throw caught; } finally { setBusy(false); }
  }
  /** Retiring is a state change, not a delete -- the row and its provenance survive; only the default list stops showing it. */
  async function handleRetireStory(storyId: string): Promise<CareerStory> {
    setBusy(true); setError("");
    try {
      const retired = await retireCareerStory(storyId);
      await refreshStories();
      await refreshDashboard();
      return retired;
    } catch (caught) { handleRequestError(caught, "Could not retire that story."); throw caught; } finally { setBusy(false); }
  }
  async function handleAttachStoryEvidence(storyId: string, profileEvidenceId: string, note?: string | null): Promise<CareerStoryEvidence> {
    setBusy(true); setError("");
    try {
      const evidence = await attachCareerStoryProfileEvidence(storyId, profileEvidenceId, note);
      await refreshStories();
      await refreshDashboard();
      return evidence;
    } catch (caught) { handleRequestError(caught, "Could not attach that evidence."); throw caught; } finally { setBusy(false); }
  }
  // --- Coach observation mutations -----------------------------------------
  // Reviewing is the only write the browser can make to coach memory
  // (Release 2 never creates or reconciles observations client-side).
  // Each refreshes BOTH the dedicated active/history split and the dashboard.
  async function handleConfirmObservation(observationId: string): Promise<CoachObservation> {
    setBusy(true); setError("");
    try {
      const confirmed = await confirmObservation(observationId);
      await refreshObservations();
      await refreshDashboard();
      return confirmed;
    } catch (caught) { handleRequestError(caught, "Could not confirm that observation."); throw caught; } finally { setBusy(false); }
  }
  async function handleCorrectObservation(observationId: string, correction: string): Promise<CoachObservation> {
    setBusy(true); setError("");
    try {
      const corrected = await correctObservation(observationId, correction);
      await refreshObservations();
      await refreshDashboard();
      return corrected;
    } catch (caught) { handleRequestError(caught, "Could not save that correction."); throw caught; } finally { setBusy(false); }
  }
  async function handleDismissObservation(observationId: string): Promise<CoachObservation> {
    setBusy(true); setError("");
    try {
      const dismissed = await dismissObservation(observationId);
      await refreshObservations();
      await refreshDashboard();
      return dismissed;
    } catch (caught) { handleRequestError(caught, "Could not dismiss that observation."); throw caught; } finally { setBusy(false); }
  }
  /**
   * Starts practice the user chose manually (design section 6.2), including
   * the Practice view's one-click hands-on option -- `format: "hands_on"`
   * still resolves through this SAME path server-side (design section 7.4),
   * so there is never a second, unrelated session-start architecture. Uses
   * the identical `{ plan, session }` navigation as {@link handleStartRecommended}.
   */
  async function handleStartManualPractice(request: ManualPracticeRequest): Promise<void> {
    setBusy(true); setError("");
    try {
      const { session: startedSession } = await startManualPractice(request);
      persistSession(startedSession); setAnswer(""); setCheckpointNote(""); setCode((startedSession.exercise as Partial<HandsOnExercise>).starterCode ?? ""); navigate("interview");
    } catch (caught) { handleRequestError(caught, "Could not start that practice session."); throw caught; } finally { setBusy(false); }
  }
  async function sendAnswer(event: FormEvent) {
    event.preventDefault(); if (!session || !answer.trim()) return; setBusy(true); setPendingTurn(true); setError("");
    try {
      const result = await api<{ session: InterviewSession; profile?: Profile }>("/api/interview", { method: "POST", body: JSON.stringify({ action: "respond", sessionId: session.id, answer }) });
      setAnswer(""); persistSession(result.session);
      if (result.session.status === "complete" && result.profile) {
        const refreshed = await refreshDashboard();
        if (!refreshed) setProfile(result.profile);
        navigate("results");
      }
    } catch (caught) { handleRequestError(caught, "Could not send answer."); } finally { setBusy(false); setPendingTurn(false); }
  }
  async function toggleRecording() {
    if (recorder.current?.state === "recording") {
      recorder.current.stop();
      setIsRecording(false);
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setError("Voice recording is not available in this browser. Type your answer instead.");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const nextRecorder = new MediaRecorder(stream);
      const monitor = monitorSpeech(stream);
      recordingChunks.current = [];
      nextRecorder.ondataavailable = (event) => { if (event.data.size) recordingChunks.current.push(event.data); };
      nextRecorder.onstop = async () => {
        stream.getTracks().forEach((track) => track.stop());
        const peak = monitor?.peak() ?? null;
        monitor?.stop();
        const audio = new Blob(recordingChunks.current, { type: nextRecorder.mimeType || "audio/webm" });
        const heardSpeech = peak === null || peak >= speechFloor;
        if (!audio.size || !heardSpeech) {
          if (audio.size && !heardSpeech) setError("No speech was picked up. Check your microphone, or type your answer instead.");
          recorder.current = null; setIsRecording(false);
          return;
        }
        setBusy(true); setError("");
        try {
          const formData = new FormData();
          formData.append("audio", audio, "interview-answer.webm");
          const response = await fetch("/api/transcribe", { method: "POST", body: formData });
          const body = await response.json();
          if (!response.ok) throw new ApiError(body.error ?? "Could not transcribe recording.", response.status);
          setAnswer((current) => current ? `${current}\n${body.transcript}` : body.transcript);
        } catch (caught) { handleRequestError(caught, "Could not transcribe recording."); }
        finally { setBusy(false); recorder.current = null; setIsRecording(false); }
      };
      recorder.current = nextRecorder;
      nextRecorder.start();
      setIsRecording(true);
    } catch { setError("Microphone access was not granted. Type your answer instead."); }
  }
  async function saveCheckpoint(event: FormEvent) {
    event.preventDefault(); if (!session || !code.trim() || !checkpointNote.trim()) return; setBusy(true); setError("");
    try {
      const result = await api<{ session: InterviewSession }>("/api/interview", { method: "POST", body: JSON.stringify({ action: "checkpoint", sessionId: session.id, code, note: checkpointNote }) });
      setCheckpointNote(""); persistSession(result.session);
    } catch (caught) { handleRequestError(caught, "Could not save checkpoint."); } finally { setBusy(false); }
  }
  async function finishInterview() {
    if (!session) return; setBusy(true); setError("");
    try {
      const result = await api<{ session: InterviewSession; profile: Profile }>("/api/interview", { method: "POST", body: JSON.stringify({ action: "complete", sessionId: session.id }) });
      persistSession(result.session);
      const refreshed = await refreshDashboard();
      if (!refreshed) setProfile(result.profile);
      navigate("results");
    } catch (caught) { handleRequestError(caught, "Could not finish interview."); } finally { setBusy(false); }
  }
  async function signInWithGoogle() {
    if (!supabase.current) {
      setError("Sign-in is not available. Refresh the page and try again.");
      return;
    }
    setBusy(true); setError("");
    try {
      const { error: authError } = await supabase.current.auth.signInWithOAuth({
        provider: "google",
        options: { redirectTo: `${window.location.origin}/auth/callback` },
      });
      if (authError) throw authError;
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start Google sign-in.");
    } finally { setBusy(false); }
  }
  async function signOut() {
    if (!supabase.current) return;
    setBusy(true); setError("");
    try {
      const { error: authError } = await supabase.current.auth.signOut();
      if (authError) throw authError;
      setProfile(null); setReadinessModel(null); setSession(null); setSessions([]); setAnswer(""); setCode(""); setCheckpointNote(""); setView("onboarding"); setAuthState("signed-out");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not sign out.");
    } finally { setBusy(false); }
  }

  const renderConversationMessage = (message: InterviewSession["messages"][number]) => {
    const questionId = message.id.split(":")[0];
    const conversationQuestion = session?.questions.find((item) => item.id === questionId) ?? null;
    const blueprintQuestion = session?.blueprint?.questions.find((item) => item.id === questionId) ?? null;

    return (
      <div
        key={message.id}
        className={`max-w-2xl rounded-2xl p-5 leading-7 ${message.role === "interviewer" ? "border border-[var(--line)] bg-[var(--paper)]" : "ml-auto bg-[#dff0d4]"}`}
      >
        <p className="mb-2 text-xs font-semibold uppercase tracking-[.14em] text-[var(--ink-muted)]">
          {message.role === "interviewer" ? "Interviewer" : "You"}
        </p>
        {/*
          A real interviewer does not narrate its sources, so the "Grounded
          in <evidence>" provenance line that used to render here is gone --
          that's interface metadata, and belongs in the results card, where
          the objective and expected signals already moved. The "Broader
          question" notice stays: it tells the candidate the question isn't
          anchored to one of their own examples, which is about what kind of
          question this is, not where it came from.
        */}
        {message.role === "interviewer" && blueprintQuestion && blueprintQuestion.evidenceIds.length === 0 && (
          <p className="mb-3 text-xs leading-5 text-[#537053]">
            Broader question — draws on what you actually say, not a fixed source example.
          </p>
        )}
        {message.role === "interviewer" && conversationQuestion && !blueprintQuestion && (
          <p className="mb-3 text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Grounded question</p>
        )}
        {message.content}
      </div>
    );
  };

  if (authState === "loading" || (authState === "signed-in" && coachDataLoading)) {
    return <main className="grid min-h-screen place-items-center bg-[var(--background)] px-5 text-sm text-[var(--ink-muted)]">Checking your sign-in…</main>;
  }

  if (authState === "signed-out") {
    return <main className="min-h-screen bg-[var(--background)] px-5 py-5 text-[var(--foreground)]"><section className="mx-auto flex min-h-screen max-w-md items-center"><div className="w-full rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-7 shadow-[0_12px_32px_rgba(27,42,34,.06)]"><div className="grid h-10 w-10 place-items-center rounded-xl bg-[var(--pine)] text-lg text-[var(--lime)]">R</div><p className="mt-6 text-sm font-semibold uppercase tracking-[.18em] text-[#5d7567]">Relay interview coach</p><h1 className="mt-3 text-4xl font-semibold tracking-[-.04em]">Practice with your own career context.</h1><p className="mt-4 leading-7 text-[var(--ink-muted)]">Sign in to create a private coaching profile and keep your interview evidence separate from every other account.</p>{error && <div role="alert" className="mt-5 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-4 py-3 text-sm text-[#8e3226]">{error}</div>}<button onClick={signInWithGoogle} disabled={busy} className="mt-7 w-full rounded-full bg-[var(--pine)] px-5 py-3 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Connecting…" : "Continue with Google"}</button></div></section></main>;
  }

  return <main className="min-h-screen bg-[var(--background)] text-[var(--foreground)]"><div className="mx-auto flex min-h-screen max-w-6xl flex-col px-5 py-5 md:px-8">
    <header className="mb-8 flex items-center justify-between gap-4"><button onClick={() => profile && navigate("home")} className="flex items-center gap-2"><span className="grid h-9 w-9 place-items-center rounded-xl bg-[var(--pine)] text-lg text-[var(--lime)]">R</span><span className="text-lg font-semibold">relay</span></button><div className="flex items-center gap-3"><span className={`hidden rounded-full border px-3 py-1 text-xs sm:inline ${demoMode ? "border-[#c9d3bf] bg-[#edf5d1] text-[#38502e]" : "border-[#bad7d0] bg-[#e4f5f0] text-[#24564b]"}`}>{demoMode ? "Demo coach · add GEMINI_API_KEY for live AI" : "Gemini AI configured"}</span><button onClick={signOut} disabled={busy} className="rounded-full border border-[var(--line)] px-3 py-2 text-sm font-semibold disabled:opacity-50">Sign out</button></div></header>
    {error && <div role="alert" className="mb-5 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-4 py-3 text-sm text-[#8e3226]">{error}</div>}
    {view === "onboarding" && <section className="mx-auto w-full max-w-3xl py-6 md:py-14"><p className="mb-4 text-sm font-semibold uppercase tracking-[.18em] text-[#5d7567]">Your personal interview coach</p><h1 className="max-w-2xl text-4xl font-semibold tracking-[-.04em] md:text-6xl">Make your next interview feel familiar.</h1><p className="mt-5 max-w-xl text-lg leading-8 text-[var(--ink-muted)]">Start with the facts of your career. Relay turns them into focused practice, not a generic question bank.</p><form onSubmit={createProfile} className="mt-10 space-y-5 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5 shadow-[0_12px_32px_rgba(27,42,34,.06)] md:p-8"><label className="block text-sm font-semibold">Upload your CV <span className="font-normal text-[var(--ink-muted)]">(PDF, optional)</span><input accept="application/pdf" type="file" onChange={(event) => setCvPdf(event.target.files?.[0] ?? null)} className="mt-2 block w-full rounded-xl border border-[var(--line)] bg-white p-3 text-sm" />{cvPdf && <span className="mt-2 block text-xs font-normal text-[#38502e]">{cvPdf.name} will be read with Gemini.</span>}</label><label className="block text-sm font-semibold">Paste your CV or LinkedIn-style summary <span className="font-normal text-[var(--ink-muted)]">{cvPdf ? "(optional when a PDF is selected)" : ""}</span><textarea required={!cvPdf} value={cvText} onChange={(event) => setCvText(event.target.value)} placeholder="Senior Frontend Engineer · React · TypeScript · achievements…" className="mt-2 min-h-52 w-full rounded-2xl border border-[var(--line)] bg-white p-4 text-sm leading-6 outline-none focus:border-[var(--pine)]" /></label><label className="block text-sm font-semibold">Career narrative <span className="font-normal text-[var(--ink-muted)]">(optional)</span><textarea value={coverLetter} onChange={(event) => setCoverLetter(event.target.value)} placeholder="What kind of role are you moving toward?" className="mt-2 min-h-28 w-full rounded-2xl border border-[var(--line)] bg-white p-4 text-sm leading-6 outline-none focus:border-[var(--pine)]" /></label><button disabled={busy} className="rounded-full bg-[var(--pine)] px-5 py-3 text-sm font-semibold text-white disabled:opacity-50">{busy ? cvPdf ? "Reading CV…" : "Building profile…" : "Create my profile"}</button></form></section>}
    {profile && view === "profile-review" && <section className="mx-auto w-full max-w-3xl py-6 md:py-14"><p className="text-sm font-semibold uppercase tracking-[.18em] text-[#5d7567]">Profile review</p><h1 className="mt-3 text-4xl font-semibold tracking-[-.04em]">Make the coach accurate.</h1><p className="mt-4 max-w-2xl leading-7 text-[var(--ink-muted)]">Relay will use this profile to choose questions and practice focus. Correct anything that is off before you begin.</p><form onSubmit={confirmProfile} className="mt-8 space-y-5 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-5 md:p-8"><div className="grid gap-5 md:grid-cols-2"><label className="block text-sm font-semibold">Role<input required value={draftRole} onChange={(event) => setDraftRole(event.target.value)} className="mt-2 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-3 text-sm outline-none focus:border-[var(--pine)]" /></label><label className="block text-sm font-semibold">Seniority<input required value={draftSeniority} onChange={(event) => setDraftSeniority(event.target.value)} className="mt-2 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-3 text-sm outline-none focus:border-[var(--pine)]" /></label></div><label className="block text-sm font-semibold">Professional narrative<textarea required value={draftNarrative} onChange={(event) => setDraftNarrative(event.target.value)} className="mt-2 min-h-32 w-full rounded-xl border border-[var(--line)] bg-white p-3 text-sm leading-6 outline-none focus:border-[var(--pine)]" /></label><label className="block text-sm font-semibold">Primary expertise <span className="font-normal text-[var(--ink-muted)]">(comma separated)</span><textarea required value={draftExpertise} onChange={(event) => setDraftExpertise(event.target.value)} className="mt-2 min-h-24 w-full rounded-xl border border-[var(--line)] bg-white p-3 text-sm leading-6 outline-none focus:border-[var(--pine)]" /></label><div className="flex flex-wrap gap-3"><button disabled={busy} className="rounded-full bg-[var(--pine)] px-5 py-3 text-sm font-semibold text-white disabled:opacity-50">{busy ? "Saving…" : "Confirm profile"}</button><button type="button" onClick={() => { setCvText(profile.source.cvText); setCoverLetter(profile.source.coverLetter); navigate("onboarding"); }} className="rounded-full border border-[var(--line)] px-5 py-3 text-sm font-semibold">Edit source text</button></div></form></section>}
    {profile && view !== "onboarding" && view !== "profile-review" && <div className="flex flex-1 flex-col gap-7 md:flex-row"><aside className="order-2 flex shrink-0 gap-1 overflow-auto rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-2 md:order-1 md:w-44 md:flex-col md:self-start">{nav.map((item) => <button key={item} onClick={() => navigate(item)} className={`rounded-xl px-3 py-2.5 text-left text-sm capitalize ${view === item ? "bg-[var(--pine)] text-white" : "text-[var(--ink-muted)] hover:bg-[#eef0ea]"}`}>{item}</button>)}</aside><section className="order-1 min-w-0 flex-1 md:order-2">
      {view === "home" && dashboard && <><p className="text-sm text-[var(--ink-muted)]">Welcome back</p><h1 className="mt-1 text-4xl font-semibold tracking-[-.04em]">Ready when you are.</h1><div className="mt-7"><HomeView dashboard={dashboard} busy={busy} onStartRecommended={handleStartRecommended} onOpenApplications={() => navigate("applications")} onOpenStories={() => navigate("stories")} onOpenCoach={() => navigate("coach")} onOpenProgress={() => navigate("progress")} /></div></>}
      {view === "stories" && <StoriesView stories={stories} profileEvidence={profile?.evidence ?? []} busy={busy} onCreate={handleCreateStory} onUpdate={handleUpdateStory} onConfirm={handleConfirmStory} onRetire={handleRetireStory} onAttachProfileEvidence={handleAttachStoryEvidence} />}
      {view === "coach" && <CoachView active={observations.active} history={observations.history} busy={busy} onConfirm={handleConfirmObservation} onCorrect={handleCorrectObservation} onDismiss={handleDismissObservation} />}
      {view === "applications" && <ApplicationsView opportunities={dashboard?.opportunities ?? []} recentPracticePlans={dashboard?.recentPracticePlans ?? []} busy={busy} onCreate={handleCreateOpportunity} onUpdate={handleUpdateOpportunity} onTransition={handleTransitionOpportunity} onScheduleInterview={handleScheduleOpportunityInterview} onAddNote={handleAddOpportunityNote} onLoadEvents={handleLoadOpportunityEvents} />}
      {view === "interview" && session && !handsOn && <><div className="flex items-start justify-between gap-4"><div><p className="text-sm text-[var(--ink-muted)]">{conversationLabel} · {answeredQuestions} of {session.questions.length} answered</p><h1 className="mt-1 text-3xl font-semibold tracking-[-.04em]">Stay in the conversation.</h1></div><button onClick={finishInterview} disabled={busy || !canFinishConversation} className="rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-40">Finish</button></div>{session.blueprint?.status === "limited-grounding" && <article className="mt-6 rounded-3xl border border-[#e4c9a0] bg-[#fff6eb] p-5 text-[#8e5e20]"><p className="text-sm font-semibold uppercase tracking-[.14em]">Broader practice</p><p className="mt-2 leading-6">{session.blueprint.fallbackReason ?? "This session is using broader questions because Relay has less source grounding for this practice than usual."}</p></article>}<div className="mt-6 space-y-4">{session.messages.map((message) => renderConversationMessage(message))}{pendingTurn && (
        <div className="rounded-2xl border border-[var(--line)] p-5">
          <p className="mb-3 text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Interviewer</p>
          <p className="animate-pulse text-sm text-[var(--ink-muted)]">Thinking…</p>
        </div>
      )}</div><form onSubmit={sendAnswer} className="sticky bottom-3 mt-5 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-3 shadow-[0_10px_30px_rgba(25,41,33,.1)]"><textarea value={answer} onChange={(event) => setAnswer(event.target.value)} disabled={busy} placeholder="Answer as if you were in the room…" className="min-h-28 w-full resize-none bg-transparent p-3 text-sm leading-6 outline-none" /><div className="flex items-center justify-between gap-3 border-t border-[var(--line)] px-2 pt-3"><button type="button" onClick={toggleRecording} disabled={busy} className={`rounded-full px-3 py-2 text-xs font-semibold ${isRecording ? "bg-[#fff0ed] text-[#8e3226]" : "bg-[#eef3e7] text-[#38502e]"}`}>{isRecording ? "■ Stop & transcribe" : "● Record answer"}</button><span className="text-xs text-[var(--ink-muted)]">Edit the transcript before sending.</span><button disabled={busy || !answer.trim()} className="rounded-full bg-[var(--pine)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">{busy ? "Sending…" : "Send answer"}</button></div></form></>}
      {view === "interview" && session && handsOn && <><div className="flex flex-wrap items-start justify-between gap-4"><div><p className="text-sm text-[var(--ink-muted)]">Hands-on technical interview · React + TypeScript · {exercise?.durationMinutes} minutes</p><h1 className="mt-1 text-3xl font-semibold tracking-[-.04em]">Build, narrate, adapt.</h1></div><button onClick={finishInterview} disabled={busy || !session.checkpoints.length} className="rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-40">Finish &amp; review</button></div><div className="mt-6 grid gap-5 xl:grid-cols-[.8fr_1.2fr]"><aside className="rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6"><p className="text-sm font-semibold text-[var(--ink-muted)]">Your brief</p><h2 className="mt-2 text-2xl font-semibold">{exercise?.title}</h2><p className="mt-4 leading-7 text-[var(--ink-muted)]">{exercise?.briefing}</p><h3 className="mt-6 text-sm font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Requirements</h3><ul className="mt-3 space-y-3 text-sm leading-6 text-[var(--ink-muted)]">{exercise?.requirements.map((requirement) => <li key={requirement} className="flex gap-2"><span className="text-[var(--pine)]">•</span>{requirement}</li>)}</ul><p className="mt-6 rounded-xl bg-[#eef3e7] p-3 text-sm leading-6 text-[#38502e]">Think aloud at each checkpoint. The interviewer can challenge your approach, but will not write the solution for you.</p></aside><div><label className="block text-sm font-semibold">Workspace<textarea aria-label="TypeScript code workspace" spellCheck={false} value={code} onChange={(event) => setCode(event.target.value)} disabled={busy} className="mt-2 min-h-[31rem] w-full resize-y rounded-2xl border border-[#1d332b] bg-[#13241e] p-5 font-mono text-sm leading-6 text-[#e7f2e6] outline-none focus:border-[var(--lime)]" /></label><form onSubmit={saveCheckpoint} className="mt-4 rounded-2xl border border-[var(--line)] bg-[var(--paper)] p-4"><label className="block text-sm font-semibold">What are you doing and why?<textarea value={checkpointNote} onChange={(event) => setCheckpointNote(event.target.value)} disabled={busy} placeholder="For example: I am cancelling in-flight searches and will add keyboard state next…" className="mt-2 min-h-24 w-full resize-none rounded-xl border border-[var(--line)] bg-white p-3 text-sm leading-6 outline-none focus:border-[var(--pine)]" /></label><div className="mt-3 flex items-center justify-between gap-3"><span className="text-xs text-[var(--ink-muted)]">{session.checkpoints.length} checkpoint{session.checkpoints.length === 1 ? "" : "s"} saved</span><button disabled={busy || !code.trim() || !checkpointNote.trim()} className="rounded-full bg-[var(--pine)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">{busy ? "Interviewer is reviewing…" : "Save checkpoint"}</button></div></form></div></div><div className="mt-6 space-y-3">{session.messages.map((message) => <article key={message.id} className={`max-w-3xl rounded-2xl p-4 text-sm leading-6 ${message.role === "interviewer" ? "border border-[var(--line)] bg-[var(--paper)]" : "bg-[#dff0d4]"}`}><p className="mb-1 text-xs font-semibold uppercase tracking-[.14em] text-[var(--ink-muted)]">{message.role === "interviewer" ? "Interviewer" : "Your checkpoint"}</p>{message.content}</article>)}</div></>}
      {view === "results" && session && <><p className="text-sm text-[var(--ink-muted)]">{handsOn ? "Hands-on interview complete" : "Interview complete"}</p><h1 className="mt-1 text-4xl font-semibold tracking-[-.04em]">{handsOn ? "A realistic technical signal." : "A useful baseline."}</h1>{session.blueprint?.status === "limited-grounding" && <article className="mt-7 rounded-3xl border border-[#e4c9a0] bg-[#fff6eb] p-5 text-[#8e5e20]"><p className="text-sm font-semibold uppercase tracking-[.14em]">Broader practice</p><p className="mt-2 leading-6">{session.blueprint.fallbackReason ?? "This session is using broader questions because Relay has less source grounding for this practice than usual."}</p></article>}<article className="mt-7 rounded-3xl bg-[var(--pine)] p-7 text-white"><p className="text-sm text-[#c8d7cf]">Overall coaching signal</p><strong className="mt-2 block text-6xl tracking-[-.06em]">{session.overallScore}<span className="ml-2 text-2xl text-[#c8d7cf]">/ 10</span></strong><p className="mt-5 max-w-2xl leading-7 text-[#dbe7df]">{sessionSummary}</p></article><ResultsFeedbackCards session={session} evidence={profile?.evidence} /><div className="mt-7">{!handsOn && <fieldset className="mb-4"><legend className="mb-2 text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Interview mode</legend>{([
        { value: "real" as const, label: "Real", hint: "Lets you fail, so the feedback is honest." },
        { value: "coach" as const, label: "Coach", hint: "Helps you when you get stuck." },
      ]).map((option) => <label key={option.value} className="flex cursor-pointer items-start gap-3 py-2"><input type="radio" name="interview-mode" value={option.value} checked={mode === option.value} onChange={() => setMode(option.value)} className="mt-1" /><span><span className="block text-sm font-medium">{option.label}</span><span className="block text-xs text-[var(--ink-muted)]">{option.hint}</span></span></label>)}</fieldset>}<button onClick={() => startInterview(handsOn ? "hands-on" : "conversation")} disabled={busy} className="rounded-full bg-[var(--pine)] px-5 py-3 text-sm font-semibold text-white">Start another {handsOn ? "hands-on interview" : "interview"}</button></div></>}
      {view === "progress" && <><p className="text-sm text-[var(--ink-muted)]">Progress</p><h1 className="mt-1 text-4xl font-semibold tracking-[-.04em]">Practice with a memory.</h1>{profileReadinessNote && <p className="mt-3 max-w-xl text-sm leading-6 text-[var(--ink-muted)]">{profileReadinessNote}</p>}{hasEvidence && readiness !== null ? <div className="mt-7 grid gap-6 xl:grid-cols-[.8fr_1.2fr]"><div className="space-y-6"><article className="rounded-3xl bg-[#e7efd9] p-6"><p className="text-sm text-[#537053]">Interview readiness</p><strong className="mt-2 block text-6xl tracking-[-.06em]">{readiness}<span className="ml-2 text-2xl text-[#537053]">/ 100</span></strong><p className="mt-4 text-sm leading-6 text-[#537053]">A coaching signal based on your completed practice, not a hiring prediction.</p></article><article className="rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6"><p className="text-sm font-semibold text-[var(--ink-muted)]">Trend</p><h2 className="mt-2 text-2xl font-semibold">{progressTrendName}</h2><p className="mt-3 text-sm text-[var(--ink-muted)]">{progressTrendDescription(progressTrend)}</p><div className="mt-5 grid gap-4 sm:grid-cols-2"><div className="rounded-2xl bg-[#f3f5ef] p-4"><p className="text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Confidence</p><p className="mt-2 text-3xl font-semibold">{confidenceLabel}</p></div><div className="rounded-2xl bg-[#f3f5ef] p-4"><p className="text-xs font-semibold uppercase tracking-[.12em] text-[var(--ink-muted)]">Trend</p><p className="mt-2 text-3xl font-semibold">{progressTrendName}</p></div></div></article></div><div className="space-y-6"><article className="rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6"><p className="text-sm font-semibold text-[var(--ink-muted)]">Recommended focus</p><h2 className="mt-2 text-2xl font-semibold">{weakestDimensionLabel ?? "Choose a fresh practice area"}</h2><p className="mt-3 leading-6 text-[var(--ink-muted)]">{weakest ? `Scoring ${weakest.score}/100 with ${weakest.confidence} confidence.` : "Relay will surface the next coaching target once enough evidence accumulates."}</p></article><article className="rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6"><h2 className="font-semibold">Competencies</h2><div className="mt-5 space-y-4">{profile.competencies.map((item) => <div key={item.id}><div className="mb-2 flex justify-between text-sm"><span>{item.name}</span><span className="text-[var(--ink-muted)]">{item.averageScore === null ? "Not assessed" : `${item.averageScore}/10`}</span></div>{item.averageScore !== null && <div className="h-2 overflow-hidden rounded-full bg-[#e6e9e1]"><div className="h-full rounded-full bg-[var(--pine)]" style={{ width: `${item.averageScore * 10}%` }} /></div>}</div>)}</div></article></div></div> : <article className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6"><h2 className="text-2xl font-semibold">Not enough data yet</h2><p className="mt-3 max-w-xl leading-6 text-[var(--ink-muted)]">Finish your first mixed interview to establish a baseline before Relay shows readiness or competency scores.</p></article>}<p className="mt-7 text-sm text-[var(--ink-muted)]">{sessions.filter((item) => item.status === "complete").length} completed interviews saved to your account, including {sessions.filter((item) => item.status === "complete" && item.kind === "hands-on").length} hands-on sessions.</p></>}
      {view === "profile" && <><p className="text-sm text-[var(--ink-muted)]">Professional profile</p><h1 className="mt-1 text-4xl font-semibold tracking-[-.04em]">{profile.role}</h1><p className="mt-2 text-lg text-[var(--ink-muted)]">{profile.seniority} · personal coaching profile</p>{profileReadinessNote && <p className="mt-3 max-w-2xl text-sm leading-6 text-[var(--ink-muted)]">{profileReadinessNote}</p>}<article className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6"><h2 className="font-semibold">Professional narrative</h2><p className="mt-3 max-w-2xl leading-7 text-[var(--ink-muted)]">{profile.narrative}</p><h2 className="mt-7 font-semibold">Primary expertise</h2><div className="mt-3 flex flex-wrap gap-2">{profile.expertise.map((item) => <span key={item} className="rounded-full bg-[#edf0e8] px-3 py-1.5 text-sm">{item}</span>)}</div>{profile.evidence?.length ? <div className="mt-7 border-t border-[var(--line)] pt-7"><h2 className="text-2xl font-semibold">Grounded evidence</h2><p className="mt-3 max-w-2xl text-sm leading-6 text-[var(--ink-muted)]">Relay will only plan and critique against source-backed details shown here.</p><div className="mt-5 space-y-4">{profile.evidence.map((item) => <article key={item.id} className="rounded-2xl border border-[var(--line)] bg-[#f8f7f2] p-4"><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="font-semibold">{item.projectOrEmployer ?? item.ownership ?? "Source-backed experience"}</h3><p className="mt-1 text-sm leading-6 text-[var(--ink-muted)]">{item.sourceExcerpt}</p></div><span className="rounded-full bg-[#eef3e7] px-3 py-1 text-xs font-semibold text-[#38502e]">{Math.round(item.confidence * 100)}% extraction confidence</span></div><p className="mt-3 text-sm leading-6 text-[var(--ink-muted)]">{evidenceSummary(item)}</p>{item.technologies.length > 0 && <div className="mt-3 flex flex-wrap gap-2">{item.technologies.map((technology) => <span key={technology} className="rounded-full bg-white px-3 py-1 text-xs font-semibold text-[#38502e]">{technology}</span>)}</div>}</article>)}</div></div> : null}<button onClick={() => beginProfileReview(profile)} className="mt-8 rounded-full border border-[var(--pine)] px-4 py-2 text-sm font-semibold text-[var(--pine)]">Review and edit profile</button><button onClick={() => { setCvText(profile.source.cvText); setCoverLetter(profile.source.coverLetter); navigate("onboarding"); }} className="mt-3 block text-sm font-semibold text-[var(--ink-muted)]">Replace source information</button></article></>}
      {view === "practice" && dashboard && <PracticeView dashboard={dashboard} busy={busy} onStartRecommended={handleStartRecommended} onStartManual={handleStartManualPractice} />}
    </section></div>}
  </div></main>;
}

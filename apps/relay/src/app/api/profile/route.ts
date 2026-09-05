import { NextResponse } from "next/server";
import { analyzeProfile, assessProfileReadiness, extractEngineeringEvidence, extractPdfText } from "@/lib/coach";
import { GeminiRequestError } from "@/lib/gemini";
import { getProfile, saveProfile } from "@/lib/repositories/profile";
import { requireUser } from "@/lib/supabase/server";
import type { ProfileDraft, ProfileSource } from "@/lib/types";

export const runtime = "nodejs";

/** Returns the signed-in user's saved profile bundle and current demo-mode flag. */
export async function GET() {
  try {
    const { supabase, user } = await requireUser();
    return NextResponse.json({ profile: await getProfile(supabase, user.id), demoMode: !process.env.GEMINI_API_KEY });
  } catch (error) {
    return errorResponse(error);
  }
}

/**
 * Extracts a profile from pasted text or a PDF and persists it. Readiness is
 * computed and stored alongside the profile as an advisory source-grounding
 * signal -- it never blocks profile creation; see
 * `docs/superpowers/specs/2026-09-01-practice-first-profile-grounding-design.md` §4.
 */
export async function POST(request: Request) {
  let supabase;
  let user;
  try {
    ({ supabase, user } = await requireUser());
  } catch (error) {
    return errorResponse(error);
  }

  let cvText: unknown;
  let coverLetter: unknown = "";
  let cvFileName: string | null = null;
  try {
    if (request.headers.get("content-type")?.includes("multipart/form-data")) {
      const formData = await request.formData();
      const cv = formData.get("cv");
      coverLetter = formData.get("coverLetter") ?? "";
      if (!(cv instanceof File) || cv.type !== "application/pdf") return NextResponse.json({ error: "Upload a PDF CV or paste a professional summary." }, { status: 400 });
      cvFileName = cv.name;
      cvText = await extractPdfText(cv);
    } else {
      const body = await request.json();
      cvText = body.cvText;
      coverLetter = body.coverLetter ?? "";
    }
  } catch (caught) {
    if (caught instanceof GeminiRequestError) {
      return NextResponse.json({ error: caught.message }, { status: profileUploadStatus(caught.state) });
    }
    return NextResponse.json({ error: caught instanceof Error ? caught.message : "Could not read that CV." }, { status: 400 });
  }
  if (typeof cvText !== "string" || cvText.trim().length === 0) {
    return NextResponse.json({ error: "Paste at least a short CV or professional summary." }, { status: 400 });
  }
  try {
    const cv = cvText.trim();
    const narrative = String(coverLetter);
    const [profile, evidence] = await Promise.all([
      analyzeProfile(cv, narrative),
      extractEngineeringEvidence(cv, narrative),
    ]);
    const readiness = assessProfileReadiness(evidence);
    const source: ProfileSource = {
      cvText: cv,
      coverLetter: narrative,
      cvFileName,
    };
    return NextResponse.json({
      profile: await saveProfile(supabase, user.id, profile, source, evidence, readiness),
      demoMode: !process.env.GEMINI_API_KEY,
    });
  } catch (error) {
    return errorResponse(error);
  }
}

/** Persists user-confirmed profile edits while preserving the stored evidence bundle. */
export async function PUT(request: Request) {
  let supabase;
  let user;
  try {
    ({ supabase, user } = await requireUser());
  } catch (error) {
    return errorResponse(error);
  }

  let profile: unknown;
  try {
    ({ profile } = await request.json());
  } catch {
    return NextResponse.json({ error: "Profile data is required." }, { status: 400 });
  }
  if (!profile || typeof profile !== "object") return NextResponse.json({ error: "Profile data is required." }, { status: 400 });
  const editable = profile as Record<string, unknown>;
  if (typeof editable.role !== "string" || !editable.role.trim() || typeof editable.seniority !== "string" || !editable.seniority.trim()) {
    return NextResponse.json({ error: "Add a role and seniority before confirming your profile." }, { status: 400 });
  }
  if (typeof editable.narrative !== "string" || !editable.narrative.trim() || !Array.isArray(editable.expertise)) {
    return NextResponse.json({ error: "Add a short narrative and at least one area of expertise." }, { status: 400 });
  }
  const expertise = editable.expertise.filter((item: unknown) => typeof item === "string" && item.trim()).map((item: string) => item.trim()).slice(0, 10);
  if (!expertise.length) return NextResponse.json({ error: "Add at least one area of expertise." }, { status: 400 });
  try {
    const existing = await getProfile(supabase, user.id);
    if (!existing) return NextResponse.json({ error: "Create a profile first." }, { status: 400 });
    const updated: ProfileDraft = {
      role: editable.role.trim(),
      seniority: editable.seniority.trim(),
      summary: existing.summary,
      narrative: editable.narrative.trim(),
      expertise,
      characteristics: existing.characteristics,
      competencies: existing.competencies.map((competency) => ({ name: competency.name, relevance: competency.relevance })),
    };
    return NextResponse.json({
      profile: await saveProfile(
        supabase,
        user.id,
        updated,
        existing.source,
        existing.evidence ?? [],
        existing.readiness ?? assessProfileReadiness(existing.evidence ?? []),
      ),
      demoMode: !process.env.GEMINI_API_KEY,
    });
  } catch (error) {
    return errorResponse(error);
  }
}

function errorResponse(error: unknown) {
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  return NextResponse.json({ error: "Could not complete your profile request." }, { status: 500 });
}

function profileUploadStatus(state: GeminiRequestError["state"]): number {
  return state === "temporary" || state === "rate-limited" ? 503 : 502;
}

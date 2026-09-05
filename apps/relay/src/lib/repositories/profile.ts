import "server-only";

import { createHash } from "node:crypto";
import type { SupabaseClient } from "@supabase/supabase-js";
import { assessProfileReadiness } from "@/lib/coach";
import type {
  Competency,
  CompetencyScope,
  Difficulty,
  EvidenceItem,
  Profile,
  ProfileDraft,
  ProfileReadiness,
  ProfileSource,
} from "@/lib/types";

type Row = Record<string, unknown>;

/** Wraps storage-layer failures in user-safe messages plus a stable error code. */
export class RepositoryError extends Error {
  constructor(message: string, public readonly code = "REPOSITORY_ERROR") {
    super(message);
    this.name = "RepositoryError";
  }
}

function normalizedEvidenceText(value: string | null | undefined): string {
  return (value ?? "").trim().replace(/\s+/g, " ").toLowerCase();
}

/**
 * Deterministic identity for a piece of extracted evidence, independent of its
 * temporary array position/id or its volatile confidence score. Equivalent
 * source-backed content (same excerpt, project, decision, outcome, etc., modulo
 * whitespace/case) always yields the same key, so `save_profile_bundle` can
 * reconcile evidence across profile saves instead of deleting and recreating it.
 */
export function evidenceKeyFor(item: EvidenceItem): string {
  const canonical = JSON.stringify({
    sourceKind: item.sourceKind,
    sourceExcerpt: normalizedEvidenceText(item.sourceExcerpt),
    projectOrEmployer: normalizedEvidenceText(item.projectOrEmployer),
    ownership: normalizedEvidenceText(item.ownership),
    technologies: [...item.technologies].map(normalizedEvidenceText).sort(),
    decision: normalizedEvidenceText(item.decision),
    constraint: normalizedEvidenceText(item.constraint),
    outcome: normalizedEvidenceText(item.outcome),
    recency: normalizedEvidenceText(item.recency),
  });
  return createHash("sha256").update(canonical).digest("hex");
}

const baselineCompetencies = [
  "Coding and implementation",
  "Debugging and reliability",
  "Architecture and system design",
  "Testing and quality",
  "Performance and scalability",
  "Accessibility and user impact",
  "Delivery and trade-offs",
  "Collaboration and communication",
];

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function expectedLevel(seniority: string | null): Difficulty {
  const value = (seniority ?? "").toLowerCase();
  if (/staff|principal|lead|advanced/.test(value)) return "advanced";
  if (/senior/.test(value)) return "senior";
  if (/junior|entry|graduate|foundational/.test(value)) return "foundational";
  return "intermediate";
}

function mapCompetency(row: Row): Competency {
  const confidence = row.confidence;
  return {
    id: stringValue(row.id),
    name: stringValue(row.name),
    relevance: Number(row.relevance ?? 0),
    expectedLevel: row.expected_level as Difficulty,
    estimatedLevel: (row.estimated_level as Difficulty | null) ?? null,
    confidence: confidence === "low" || confidence === "medium" || confidence === "high"
      ? confidence
      : typeof confidence === "number" || typeof confidence === "string"
        ? Number(confidence) < 0.4 ? "low" : Number(confidence) < 0.75 ? "medium" : "high"
        : null,
    lastPracticedAt: typeof row.last_practiced_at === "string" ? row.last_practiced_at : null,
    questionCount: Number(row.question_count ?? 0),
    averageScore: row.average_score === null || row.average_score === undefined ? null : Number(row.average_score),
    recentScore: row.recent_score === null || row.recent_score === undefined ? null : Number(row.recent_score),
    strengths: stringArray(row.strengths),
    weaknesses: stringArray(row.weaknesses),
  };
}

function mapSource(rows: Row[]): ProfileSource {
  const cv = rows.find((row) => row.kind === "cv");
  const coverLetter = rows.find((row) => row.kind === "cover_letter");
  return {
    cvText: stringValue(cv?.content),
    coverLetter: stringValue(coverLetter?.content),
    cvFileName: typeof cv?.file_name === "string" ? cv.file_name : null,
    coverLetterFileName: typeof coverLetter?.file_name === "string" ? coverLetter.file_name : null,
  };
}

function mapEvidence(row: Row): EvidenceItem {
  return {
    id: stringValue(row.id),
    sourceKind: row.source_kind === "cv" || row.source_kind === "cover_letter" || row.source_kind === "summary"
      ? row.source_kind
      : null,
    sourceExcerpt: stringValue(row.source_excerpt),
    projectOrEmployer: typeof row.project_or_employer === "string" ? row.project_or_employer : null,
    ownership: typeof row.ownership === "string" ? row.ownership : null,
    technologies: stringArray(row.technologies),
    decision: typeof row.decision === "string" ? row.decision : null,
    constraint: typeof row.constraint_text === "string" ? row.constraint_text : null,
    outcome: typeof row.outcome === "string" ? row.outcome : null,
    recency: typeof row.recency === "string" ? row.recency : null,
    confidence: Number(row.confidence ?? 0),
  };
}

function readinessFromRow(row: Row, evidence: EvidenceItem[]): ProfileReadiness {
  const fallback = assessProfileReadiness(evidence);
  const ready = typeof row.profile_ready === "boolean" ? row.profile_ready : fallback.ready;
  const missing = Array.isArray(row.profile_missing) ? stringArray(row.profile_missing) : fallback.missing;
  return { ready, missing };
}

function mapProfile(row: Row, competencies: Competency[], source: ProfileSource, evidence: EvidenceItem[]): Profile {
  return {
    userId: stringValue(row.user_id),
    role: typeof row.role === "string" ? row.role : null,
    seniority: typeof row.seniority === "string" ? row.seniority : null,
    summary: typeof row.summary === "string" ? row.summary : null,
    narrative: typeof row.narrative === "string" ? row.narrative : null,
    expertise: stringArray(row.expertise),
    characteristics: stringArray(row.characteristics),
    competencies,
    evidence,
    readiness: readinessFromRow(row, evidence),
    source,
    createdAt: stringValue(row.created_at),
    updatedAt: stringValue(row.updated_at),
  };
}

function relatedExpertise(name: string, expertise: string[]): boolean {
  const values = expertise.map((item) => item.toLowerCase());
  if (name === "Coding and implementation") return values.length > 0;
  if (name === "Debugging and reliability") return values.some((item) => /debug|reliab|observability|incident|production|backend|platform|infra/.test(item));
  if (name === "Architecture and system design") return values.some((item) => /react|frontend|front-end|ui|backend|platform|infrastructure|distributed|architecture|system|mobile|security|data|api/.test(item));
  if (name === "Testing and quality") return values.some((item) => /test|quality|qa|e2e|unit|integration|coverage/.test(item));
  if (name === "Performance and scalability") return values.some((item) => /performance|scale|latency|throughput|optimi[sz]e/.test(item));
  if (name === "Accessibility and user impact") return values.some((item) => /accessibility|a11y|frontend|front-end|react|typescript|ui|user|mobile|product/.test(item));
  if (name === "Delivery and trade-offs") return values.some((item) => /delivery|launch|release|migration|trade|ownership|roadmap/.test(item));
  if (name === "Collaboration and communication") return values.some((item) => /collaboration|communication|leadership|stakeholder|cross-functional|partner/.test(item));
  return false;
}

/** Creates scope only; it deliberately never creates an assessment score. */
export function competencyScopeFor(expertise: string[]): CompetencyScope[] {
  const scopes = new Map<string, CompetencyScope>();
  for (const name of expertise.map((item) => item.trim()).filter(Boolean)) {
    scopes.set(name.toLowerCase(), { name, relevance: 1 });
  }
  for (const name of baselineCompetencies) {
    const key = name.toLowerCase();
    if (!scopes.has(key)) {
      scopes.set(key, { name, relevance: relatedExpertise(name, expertise) ? 0.9 : 0.7 });
    }
  }
  return [...scopes.values()];
}

export function profileScopeRows(userId: string, profile: ProfileDraft) {
  return competencyScopeFor(profile.expertise).map((competency) => ({
    user_id: userId,
    name: competency.name,
    relevance: competency.relevance,
    expected_level: expectedLevel(profile.seniority),
    updated_at: new Date().toISOString(),
  }));
}

/**
 * Loads the signed-in user's latest profile bundle, including source documents,
 * active competencies, persisted evidence, and a readiness state that can
 * hydrate legacy rows when newer columns are absent.
 */
export async function getProfile(supabase: SupabaseClient, userId: string): Promise<Profile | null> {
  const { data: profile, error } = await supabase.from("profiles").select("*").eq("user_id", userId).maybeSingle();
  if (error) throw new RepositoryError("Could not load your profile.", error.code);
  if (!profile) return null;

  const [documentsResult, competenciesResult, evidenceResult] = await Promise.all([
    supabase.from("source_documents").select("*").eq("user_id", userId),
    supabase.from("competencies").select("*").eq("user_id", userId).eq("is_active", true).order("name"),
    supabase.from("profile_evidence").select("*").eq("user_id", userId).eq("is_active", true).order("created_at"),
  ]);
  if (documentsResult.error) throw new RepositoryError("Could not load your source documents.", documentsResult.error.code);
  if (competenciesResult.error) throw new RepositoryError("Could not load your competencies.", competenciesResult.error.code);
  if (evidenceResult.error && evidenceResult.error.code !== "42P01") {
    throw new RepositoryError("Could not load your evidence.", evidenceResult.error.code);
  }

  const evidence = ((evidenceResult.data ?? []) as Row[]).map(mapEvidence);
  return mapProfile(
    profile as Row,
    ((competenciesResult.data ?? []) as Row[]).map(mapCompetency),
    mapSource((documentsResult.data ?? []) as Row[]),
    evidence,
  );
}

/**
 * Replaces the signed-in user's profile bundle atomically with the latest
 * source text, extracted evidence, readiness state, and competency scope.
 */
export async function saveProfile(
  supabase: SupabaseClient,
  userId: string,
  profile: ProfileDraft,
  source: ProfileSource,
  evidence: EvidenceItem[] = [],
  readiness: ProfileReadiness = assessProfileReadiness(evidence),
): Promise<Profile> {
  const { error } = await supabase.rpc("save_profile_bundle", {
    p_role: profile.role,
    p_seniority: profile.seniority,
    p_summary: profile.summary,
    p_narrative: profile.narrative,
    p_expertise: profile.expertise,
    p_characteristics: profile.characteristics,
    p_cv_text: source.cvText,
    p_cv_file_name: source.cvFileName ?? null,
    p_cover_letter_text: source.coverLetter,
    p_cover_letter_file_name: source.coverLetterFileName ?? null,
    p_evidence: evidence.map((item, index) => ({
      id: item.id || `evidence-${index + 1}`,
      evidence_key: evidenceKeyFor(item),
      source_kind: item.sourceKind,
      source_excerpt: item.sourceExcerpt,
      project_or_employer: item.projectOrEmployer,
      ownership: item.ownership,
      technologies: item.technologies,
      decision: item.decision,
      constraint: item.constraint,
      outcome: item.outcome,
      recency: item.recency,
      confidence: item.confidence,
    })),
    p_profile_ready: readiness.ready,
    p_profile_missing: readiness.missing,
    p_scope: profileScopeRows(userId, profile).map((row) => ({
      name: row.name,
      relevance: row.relevance,
      expected_level: row.expected_level,
    })),
  });
  if (error) throw new RepositoryError("Could not save your profile.", error.code);

  const saved = await getProfile(supabase, userId);
  if (!saved) throw new RepositoryError("Your saved profile could not be reloaded.", "NO_OWNED_ROW");
  return saved;
}

export { mapCompetency };

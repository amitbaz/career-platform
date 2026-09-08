import { Buffer } from "node:buffer";
import { NextResponse } from "next/server";
import {
  deleteProviderCredential,
  listProviderCredentials,
  setProviderCredential,
} from "@/lib/repositories/provider-credentials";
import { requireUser } from "@/lib/supabase/server";
import type { ProviderCredential } from "@/lib/types";

export const runtime = "nodejs";

const providers = new Set<ProviderCredential>(["gemini", "brave"]);
const MAX_PROVIDER_SECRET_BYTES = 16_384;

class ValidationError extends Error {}

function isProvider(value: unknown): value is ProviderCredential {
  return typeof value === "string" && providers.has(value as ProviderCredential);
}

async function readBody(request: Request): Promise<Record<string, unknown>> {
  const body: unknown = await request.json().catch(() => {
    throw new ValidationError();
  });
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new ValidationError();
  return body as Record<string, unknown>;
}

function errorResponse(error: unknown) {
  if (error instanceof Error && error.message === "UNAUTHENTICATED") {
    return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
  }
  if (error instanceof ValidationError) {
    return NextResponse.json({ error: "A valid provider credential request is required." }, { status: 400 });
  }
  return NextResponse.json({ error: "Could not update provider credentials." }, { status: 500 });
}

/** Returns non-secret credential configuration metadata for the signed-in user. */
export async function GET() {
  try {
    const { supabase } = await requireUser();
    return NextResponse.json({ credentials: await listProviderCredentials(supabase) });
  } catch (error) {
    return errorResponse(error);
  }
}

/** Stores one signed-in user's provider secret without returning its submitted value. */
export async function PUT(request: Request) {
  try {
    const { supabase } = await requireUser();
    const { provider, secret } = await readBody(request);
    if (
      !isProvider(provider)
      || typeof secret !== "string"
      || !secret.trim()
      || Buffer.byteLength(secret, "utf8") > MAX_PROVIDER_SECRET_BYTES
    ) {
      throw new ValidationError();
    }
    const credential = await setProviderCredential(supabase, provider, secret);
    return NextResponse.json({ credential });
  } catch (error) {
    return errorResponse(error);
  }
}

/** Deletes one signed-in user's provider secret and returns only its cleared status. */
export async function DELETE(request: Request) {
  try {
    const { supabase } = await requireUser();
    const { provider } = await readBody(request);
    if (!isProvider(provider)) throw new ValidationError();
    const credential = await deleteProviderCredential(supabase, provider);
    return NextResponse.json({ credential });
  } catch (error) {
    return errorResponse(error);
  }
}

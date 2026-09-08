import "server-only";

import type { SupabaseClient } from "@supabase/supabase-js";
import { RepositoryError } from "@/lib/repositories/profile";
import type { ProviderCredential, ProviderCredentialStatus } from "@/lib/types";

type StatusRow = { provider: unknown; configured: unknown; updated_at: unknown };

function isProvider(value: unknown): value is ProviderCredential {
  return value === "gemini" || value === "brave";
}

function mapStatusRow(value: unknown): ProviderCredentialStatus {
  if (!value || typeof value !== "object") {
    throw new RepositoryError("Could not read provider credential status.", "INVALID_CREDENTIAL_STATUS");
  }

  const row = value as StatusRow;
  if (
    !isProvider(row.provider)
    || typeof row.configured !== "boolean"
    || (row.updated_at !== null && typeof row.updated_at !== "string")
  ) {
    throw new RepositoryError("Could not read provider credential status.", "INVALID_CREDENTIAL_STATUS");
  }

  return {
    provider: row.provider,
    configured: row.configured,
    updatedAt: row.updated_at,
  };
}

function mapStatusRows(value: unknown): ProviderCredentialStatus[] {
  if (!Array.isArray(value)) {
    throw new RepositoryError("Could not read provider credential status.", "INVALID_CREDENTIAL_STATUS");
  }
  return value.map(mapStatusRow);
}

function mapSingleStatus(value: unknown): ProviderCredentialStatus {
  const statuses = mapStatusRows(value);
  if (statuses.length !== 1) {
    throw new RepositoryError("Could not read provider credential status.", "INVALID_CREDENTIAL_STATUS");
  }
  return statuses[0];
}

/** Lists credential configuration metadata for the authenticated Supabase caller. */
export async function listProviderCredentials(
  supabase: SupabaseClient,
): Promise<ProviderCredentialStatus[]> {
  const { data, error } = await supabase.rpc("list_user_provider_credentials");
  if (error) throw new RepositoryError("Could not load provider credentials.", error.code);
  return mapStatusRows(data);
}

/** Saves one provider secret and returns only its non-secret configuration metadata. */
export async function setProviderCredential(
  supabase: SupabaseClient,
  provider: ProviderCredential,
  secret: string,
): Promise<ProviderCredentialStatus> {
  const { data, error } = await supabase.rpc("set_user_provider_credential", {
    p_provider: provider,
    p_secret: secret,
  });
  if (error) throw new RepositoryError("Could not save that provider credential.", error.code);
  return mapSingleStatus(data);
}

/** Deletes one provider secret and returns its resulting unconfigured status. */
export async function deleteProviderCredential(
  supabase: SupabaseClient,
  provider: ProviderCredential,
): Promise<ProviderCredentialStatus> {
  const { data, error } = await supabase.rpc("delete_user_provider_credential", {
    p_provider: provider,
  });
  if (error) throw new RepositoryError("Could not delete that provider credential.", error.code);
  return mapSingleStatus(data);
}

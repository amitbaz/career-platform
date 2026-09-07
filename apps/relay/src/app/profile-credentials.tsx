"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  fetchProviderCredentials,
  removeProviderCredential,
  saveProviderCredential,
} from "@/app/api-client";
import type { ProviderCredential, ProviderCredentialStatus } from "@/lib/types";

const providerCopy = {
  gemini: { label: "Gemini", help: "Required for Job Hunter runs." },
  brave: { label: "Brave Search", help: "Optional. DuckDuckGo remains available without it." },
} satisfies Record<ProviderCredential, { label: string; help: string }>;

const providers = Object.keys(providerCopy) as ProviderCredential[];
const emptySecrets: Record<ProviderCredential, string> = { gemini: "", brave: "" };

function statusLabel(status: ProviderCredentialStatus): string {
  if (!status.configured) return "Not configured";
  if (!status.updatedAt) return "Configured";
  return `Configured. Updated ${new Date(status.updatedAt).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  })}`;
}

function replaceStatus(
  current: ProviderCredentialStatus[] | null,
  next: ProviderCredentialStatus,
): ProviderCredentialStatus[] {
  const existing = current ?? providers.map((provider) => ({ provider, configured: false, updatedAt: null }));
  return existing.map((status) => status.provider === next.provider ? next : status);
}

/**
 * Lets a signed-in person manage their own optional provider secrets from Profile.
 * Only non-secret metadata is read or rendered; submitted inputs are cleared after
 * each save attempt and never repopulated from the API.
 */
export function ProfileCredentials() {
  const [statuses, setStatuses] = useState<ProviderCredentialStatus[] | null>(null);
  const [secrets, setSecrets] = useState(emptySecrets);
  const [pending, setPending] = useState<ProviderCredential | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    fetchProviderCredentials()
      .then((next) => { if (active) setStatuses(next); })
      .catch((caught: unknown) => {
        if (active) setError(caught instanceof Error ? caught.message : "Could not load provider credentials.");
      });
    return () => { active = false; };
  }, []);

  function updateStatus(next: ProviderCredentialStatus) {
    setStatuses((current) => replaceStatus(current, next));
  }

  async function save(provider: ProviderCredential, event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const secret = secrets[provider];
    if (!secret.trim()) {
      setError(`Enter a ${providerCopy[provider].label} API key before saving.`);
      return;
    }

    setPending(provider);
    setError("");
    try {
      updateStatus(await saveProviderCredential(provider, secret));
    } catch (caught) {
      const message = caught instanceof Error ? caught.message : "Could not update this provider credential.";
      setError(message.includes(secret) ? "Could not update this provider credential." : message);
    } finally {
      setSecrets((current) => ({ ...current, [provider]: "" }));
      setPending(null);
    }
  }

  async function remove(provider: ProviderCredential) {
    setPending(provider);
    setError("");
    try {
      updateStatus(await removeProviderCredential(provider));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not remove this provider credential.");
    } finally {
      setPending(null);
    }
  }

  return <article className="mt-7 rounded-3xl border border-[var(--line)] bg-[var(--paper)] p-6">
    <h2 className="font-semibold">Provider credentials</h2>
    <p className="mt-2 max-w-2xl text-sm leading-6 text-[var(--ink-muted)]">Add a key only when you want Relay to use that provider for your account.</p>
    {error && <p role="alert" className="mt-4 rounded-xl border border-[#e7b9b0] bg-[#fff0ed] px-4 py-3 text-sm text-[#8e3226]">{error}</p>}
    <div className="mt-5 space-y-5">
      {providers.map((provider) => {
        const status = statuses?.find((item) => item.provider === provider);
        const configured = status?.configured ?? false;
        const loading = statuses === null;
        const isPending = pending === provider;
        return <fieldset key={provider} className="border-t border-[var(--line)] pt-5 first:border-t-0 first:pt-0">
          <legend className="text-sm font-semibold">{providerCopy[provider].label}</legend>
          <p className="mt-2 text-sm leading-6 text-[var(--ink-muted)]">{providerCopy[provider].help}</p>
          <p className="mt-1 text-xs text-[var(--ink-muted)]" aria-live="polite">{loading ? "Checking configuration…" : statusLabel(status ?? { provider, configured: false, updatedAt: null })}</p>
          <form onSubmit={(event) => save(provider, event)} className="mt-4">
            <label className="block text-sm font-semibold">{providerCopy[provider].label} API key
              <input
                type="password"
                autoComplete="new-password"
                value={secrets[provider]}
                onChange={(event) => setSecrets((current) => ({ ...current, [provider]: event.target.value }))}
                disabled={isPending}
                className="mt-2 w-full rounded-xl border border-[var(--line)] bg-white px-3 py-3 text-sm outline-none focus:border-[var(--pine)] disabled:opacity-50"
              />
            </label>
            <div className="mt-3 flex flex-wrap gap-3">
              <button disabled={isPending} className="rounded-full bg-[var(--pine)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50">{isPending ? "Saving…" : configured ? "Replace" : "Save"}</button>
              {configured && <button type="button" onClick={() => remove(provider)} disabled={isPending} className="rounded-full border border-[var(--line)] px-4 py-2 text-sm font-semibold disabled:opacity-50">Delete</button>}
            </div>
          </form>
        </fieldset>;
      })}
    </div>
  </article>;
}

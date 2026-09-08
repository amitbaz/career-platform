import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  deleteProviderCredential,
  listProviderCredentials,
  setProviderCredential,
} from "@/lib/repositories/provider-credentials";
import { RepositoryError } from "@/lib/repositories/profile";

describe("provider credential repository", () => {
  it("maps the two status rows without exposing an unknown field", async () => {
    const rpc = vi.fn().mockResolvedValue({
      data: [
        { provider: "brave", configured: false, updated_at: null, secret: "must-not-escape" },
        { provider: "gemini", configured: true, updated_at: "2026-09-07T12:00:00.000Z", secret: "must-not-escape" },
      ],
      error: null,
    });

    const credentials = await listProviderCredentials({ rpc } as never);

    expect(credentials).toEqual([
      { provider: "brave", configured: false, updatedAt: null },
      { provider: "gemini", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" },
    ]);
    expect(credentials[0]).not.toHaveProperty("secret");
    expect(credentials[1]).not.toHaveProperty("secret");
  });

  it("passes only provider and secret to the set RPC", async () => {
    const rpc = vi.fn().mockResolvedValue({
      data: [{ provider: "gemini", configured: true, updated_at: "2026-09-07T12:00:00.000Z" }],
      error: null,
    });

    const credential = await setProviderCredential({ rpc } as never, "gemini", "submitted");

    expect(rpc).toHaveBeenCalledWith("set_user_provider_credential", {
      p_provider: "gemini",
      p_secret: "submitted",
    });
    expect(credential).toEqual({
      provider: "gemini",
      configured: true,
      updatedAt: "2026-09-07T12:00:00.000Z",
    });
  });

  it("passes only provider to the delete RPC", async () => {
    const rpc = vi.fn().mockResolvedValue({
      data: [{ provider: "brave", configured: false, updated_at: null }],
      error: null,
    });

    const credential = await deleteProviderCredential({ rpc } as never, "brave");

    expect(rpc).toHaveBeenCalledWith("delete_user_provider_credential", { p_provider: "brave" });
    expect(credential).toEqual({ provider: "brave", configured: false, updatedAt: null });
  });

  it("wraps database failures without preserving the submitted value", async () => {
    const rpc = vi.fn().mockResolvedValue({ data: null, error: { code: "42501" } });

    const operation = setProviderCredential({ rpc } as never, "gemini", "submitted");

    await expect(operation).rejects.toMatchObject(
      new RepositoryError("Could not save that provider credential.", "42501"),
    );
    await expect(operation).rejects.not.toHaveProperty("message", expect.stringContaining("submitted"));
  });
});

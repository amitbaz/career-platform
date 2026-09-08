import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireUser: vi.fn(),
  listProviderCredentials: vi.fn(),
  setProviderCredential: vi.fn(),
  deleteProviderCredential: vi.fn(),
}));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));
vi.mock("@/lib/repositories/provider-credentials", () => ({
  listProviderCredentials: mocks.listProviderCredentials,
  setProviderCredential: mocks.setProviderCredential,
  deleteProviderCredential: mocks.deleteProviderCredential,
}));

import { DELETE, GET, PUT } from "@/app/api/profile/credentials/route";
import { RepositoryError } from "@/lib/repositories/profile";

function put(body: unknown) {
  return PUT(new Request("http://localhost/api/profile/credentials", {
    method: "PUT",
    body: JSON.stringify(body),
  }));
}

function remove(body: unknown) {
  return DELETE(new Request("http://localhost/api/profile/credentials", {
    method: "DELETE",
    body: JSON.stringify(body),
  }));
}

describe("/api/profile/credentials", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.requireUser.mockResolvedValue({ supabase: { client: true }, user: { id: "user-1" } });
  });

  it("GET requires authentication", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await GET();

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Sign in to continue." });
    expect(mocks.listProviderCredentials).not.toHaveBeenCalled();
  });

  it("PUT requires authentication", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await put({ provider: "gemini", secret: "a-key" });

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Sign in to continue." });
    expect(mocks.setProviderCredential).not.toHaveBeenCalled();
  });

  it("DELETE requires authentication", async () => {
    mocks.requireUser.mockRejectedValue(new Error("UNAUTHENTICATED"));

    const response = await remove({ provider: "gemini" });

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "Sign in to continue." });
    expect(mocks.deleteProviderCredential).not.toHaveBeenCalled();
  });

  it("GET returns status metadata only", async () => {
    mocks.listProviderCredentials.mockResolvedValue([
      { provider: "gemini", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" },
      { provider: "brave", configured: false, updatedAt: null },
    ]);

    const response = await GET();

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      credentials: [
        { provider: "gemini", configured: true, updatedAt: "2026-09-07T12:00:00.000Z" },
        { provider: "brave", configured: false, updatedAt: null },
      ],
    });
    expect(mocks.listProviderCredentials).toHaveBeenCalledWith(expect.anything());
  });

  it("PUT rejects unknown providers", async () => {
    const response = await put({ provider: "openai", secret: "submitted" });

    expect(response.status).toBe(400);
    expect(mocks.setProviderCredential).not.toHaveBeenCalled();
  });

  it("PUT rejects blank and values over 16384 bytes", async () => {
    const blankResponse = await put({ provider: "gemini", secret: "  \n" });
    const oversizedResponse = await put({ provider: "brave", secret: "é".repeat(8_193) });

    expect(blankResponse.status).toBe(400);
    expect(oversizedResponse.status).toBe(400);
    expect(mocks.setProviderCredential).not.toHaveBeenCalled();
  });

  it("PUT returns saved status without echoing the submitted value", async () => {
    mocks.setProviderCredential.mockResolvedValue({
      provider: "gemini",
      configured: true,
      updatedAt: "2026-09-07T12:00:00.000Z",
    });

    const response = await put({ provider: "gemini", secret: "submitted" });
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toEqual({
      credential: {
        provider: "gemini",
        configured: true,
        updatedAt: "2026-09-07T12:00:00.000Z",
      },
    });
    expect(JSON.stringify(body)).not.toContain("submitted");
  });

  it("DELETE rejects unknown providers", async () => {
    const response = await remove({ provider: "openai" });

    expect(response.status).toBe(400);
    expect(mocks.deleteProviderCredential).not.toHaveBeenCalled();
  });

  it("DELETE is status-only", async () => {
    mocks.deleteProviderCredential.mockResolvedValue({
      provider: "brave",
      configured: false,
      updatedAt: null,
    });

    const response = await remove({ provider: "brave" });

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      credential: { provider: "brave", configured: false, updatedAt: null },
    });
  });

  it("sanitizes repository errors", async () => {
    mocks.setProviderCredential.mockRejectedValue(
      new RepositoryError("Could not save submitted for gemini.", "XX000"),
    );

    const response = await put({ provider: "gemini", secret: "submitted" });
    const body = await response.json();

    expect(response.status).toBe(500);
    expect(body).toEqual({ error: "Could not update provider credentials." });
    expect(JSON.stringify(body)).not.toContain("submitted");
  });
});

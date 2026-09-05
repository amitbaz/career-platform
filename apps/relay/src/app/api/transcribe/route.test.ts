// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requireUser: vi.fn() }));

vi.mock("@/lib/supabase/server", () => ({ requireUser: mocks.requireUser }));

import { POST } from "./route";

/** Builds a multipart request carrying a small recording. */
function transcribeRequest(): Request {
  const formData = new FormData();
  formData.append("audio", new File(["recorded-audio"], "interview-answer.webm", { type: "audio/webm;codecs=opus" }), "interview-answer.webm");
  return new Request("http://localhost/api/transcribe", { method: "POST", body: formData });
}

/** Stubs Gemini with a single text candidate. */
function stubGemini(text: string) {
  const fetchMock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(async () => new Response(
    JSON.stringify({ candidates: [{ content: { parts: [{ text }] } }] }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  ));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("POST /api/transcribe", () => {
  beforeEach(() => {
    mocks.requireUser.mockResolvedValue({ id: "user-1" });
    process.env.GEMINI_API_KEY = "test-key";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("returns the transcript for a recording that contains speech", async () => {
    stubGemini("I phased the migration one route at a time.");

    const response = await POST(transcribeRequest());

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ transcript: "I phased the migration one route at a time." });
  });

  it("rejects the no-speech sentinel instead of returning it as a transcript", async () => {
    stubGemini("NO_SPEECH_DETECTED");

    const response = await POST(transcribeRequest());

    expect(response.status).toBe(422);
    await expect(response.json()).resolves.toEqual({ error: "No speech was detected. You can type your answer instead." });
  });

  it("asks Gemini for a verbatim transcript with deterministic decoding", async () => {
    const fetchMock = stubGemini("Anything.");

    await POST(transcribeRequest());

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.generationConfig.temperature).toBe(0);
    expect(body.contents[0].parts[0].text).toContain("verbatim");
    expect(body.contents[0].parts[0].text).toContain("NO_SPEECH_DETECTED");
  });
});

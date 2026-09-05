import { NextResponse } from "next/server";
import { geminiModel, geminiRequestError } from "@/lib/gemini";
import { requireUser } from "@/lib/supabase/server";

export const runtime = "nodejs";

/** Marker Gemini is asked to return instead of inventing words for silent audio. */
const noSpeechSentinel = "NO_SPEECH_DETECTED";

const noSpeechMessage = "No speech was detected. You can type your answer instead.";

export async function POST(request: Request) {
  try {
    await requireUser();
  } catch (error) {
    if (error instanceof Error && error.message === "UNAUTHENTICATED") {
      return NextResponse.json({ error: "Sign in to continue." }, { status: 401 });
    }
    return NextResponse.json({ error: "Transcription is unavailable right now. You can type your answer instead." }, { status: 502 });
  }

  const apiKey = process.env.GEMINI_API_KEY;
  if (!apiKey) return NextResponse.json({ error: "Add GEMINI_API_KEY to enable voice transcription." }, { status: 503 });

  const formData = await request.formData();
  const audio = formData.get("audio");
  if (!(audio instanceof File) || !audio.size) return NextResponse.json({ error: "Record an answer before transcribing." }, { status: 400 });
  if (audio.size > 10 * 1024 * 1024) return NextResponse.json({ error: "Keep recordings under 10 MB for transcription." }, { status: 413 });

  try {
    const model = geminiModel();
    const data = Buffer.from(await audio.arrayBuffer()).toString("base64");
    const response = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-goog-api-key": apiKey },
      body: JSON.stringify({
        // Temperature 0 keeps decoding deterministic so the same recording is not
        // reworded between attempts. The sentinel is a best-effort second line of
        // defence: Gemini happily invents a plausible interview answer when handed
        // silence, so the recorder also refuses to upload a silent take.
        generationConfig: { temperature: 0 },
        contents: [{ parts: [
          { text: `Transcribe the speech in this audio verbatim. Return only the words actually spoken, with no heading, notes, or commentary. Never guess, summarise, or invent content: if the audio contains no intelligible speech, return exactly ${noSpeechSentinel} and nothing else.` },
          { inlineData: { mimeType: audio.type || "audio/webm", data } },
        ] }],
      }),
      signal: AbortSignal.timeout(45_000),
    });
    if (!response.ok) {
      const providerError = await geminiRequestError(response, "the recording", model);
      return NextResponse.json({ error: providerError.message }, { status: 502 });
    }
    const body = await response.json() as { candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }> };
    const transcript = body.candidates?.[0]?.content?.parts?.map((part) => part.text ?? "").join("").trim();
    if (!transcript || transcript === noSpeechSentinel) {
      return NextResponse.json({ error: noSpeechMessage }, { status: 422 });
    }
    return NextResponse.json({ transcript });
  } catch (caught) {
    if (caught instanceof Error && caught.message.startsWith("GEMINI_MODEL")) {
      return NextResponse.json({ error: caught.message }, { status: 503 });
    }
    return NextResponse.json({ error: "Transcription is unavailable right now. You can type your answer instead." }, { status: 502 });
  }
}

const defaultModel = "gemini-3.6-flash";

/** Stable, credential-safe categories for Gemini transport and provider failures. */
export type GeminiFailureState =
  | "invalid-request"
  | "authentication"
  | "missing-model"
  | "rate-limited"
  | "temporary"
  | "unknown";

/**
 * Provider failure passed across the server boundary with a safe user-facing
 * message. It never stores the request payload or API key.
 */
export class GeminiRequestError extends Error {
  readonly operation: string;

  readonly status: number;

  readonly state: GeminiFailureState;

  constructor(operation: string, status: number, state: GeminiFailureState, message: string) {
    super(message);
    this.name = "GeminiRequestError";
    this.operation = operation;
    this.status = status;
    this.state = state;
  }
}

/** Returns a normalized Gemini model ID and rejects unsafe path fragments. */
export function geminiModel(): string {
  const configured = process.env.GEMINI_MODEL?.trim();
  const normalized = (configured || defaultModel).replace(/^models\//, "");
  if (!/^[a-z0-9][a-z0-9._-]*$/i.test(normalized)) {
    throw new Error("GEMINI_MODEL must be a Gemini model ID such as gemini-3.6-flash.");
  }
  return normalized;
}

function sanitizedProviderMessage(message: string): string {
  let safe = message.replace(/\b(key|api[_-]?key)=([^\s&,]+)/gi, "$1=[redacted]");
  const configuredKey = process.env.GEMINI_API_KEY;
  if (configuredKey) safe = safe.replaceAll(configuredKey, "[redacted]");
  return safe.replace(/AIza[\w-]+/g, "[redacted]").slice(0, 280).trim();
}

/** Maps a Gemini HTTP status to the retry and display behavior used by routes. */
export function geminiFailureState(status: number): GeminiFailureState {
  if (status === 400) return "invalid-request";
  if (status === 401 || status === 403) return "authentication";
  if (status === 404) return "missing-model";
  if (status === 429) return "rate-limited";
  if (status >= 500) return "temporary";
  return "unknown";
}

async function providerMessage(response: Response): Promise<string> {
  try {
    const body = await response.json() as { error?: { message?: unknown } };
    return typeof body.error?.message === "string" ? sanitizedProviderMessage(body.error.message) : "";
  } catch {
    return "";
  }
}

/** Converts a failed Gemini response into an actionable error without exposing credentials. */
export async function geminiRequestError(
  response: Response,
  operation: string,
  model: string,
): Promise<GeminiRequestError> {
  const detail = await providerMessage(response);
  const suffix = detail ? `: ${detail}` : ".";
  const state = geminiFailureState(response.status);
  if (state === "invalid-request") {
    return new GeminiRequestError(operation, response.status, state, `Gemini rejected ${operation} (400)${suffix}`);
  }
  if (state === "authentication") {
    return new GeminiRequestError(
      operation,
      response.status,
      state,
      `Gemini authentication failed for ${operation} (${response.status})${suffix} Check GEMINI_API_KEY and its API restrictions.`,
    );
  }
  if (state === "missing-model") {
    return new GeminiRequestError(
      operation,
      response.status,
      state,
      `Gemini model "${model}" is unavailable for ${operation} (404)${suffix} Check GEMINI_MODEL.`,
    );
  }
  if (state === "rate-limited") {
    return new GeminiRequestError(
      operation,
      response.status,
      state,
      `Gemini rate limit reached for ${operation} (429)${suffix} Wait briefly and try again.`,
    );
  }
  if (state === "temporary") {
    return new GeminiRequestError(
      operation,
      response.status,
      state,
      `Gemini is temporarily unavailable for ${operation} (${response.status})${suffix} Try again shortly.`,
    );
  }
  return new GeminiRequestError(operation, response.status, state, `Gemini request failed for ${operation} (${response.status})${suffix}`);
}

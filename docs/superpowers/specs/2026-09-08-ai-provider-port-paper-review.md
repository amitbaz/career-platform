# AI provider port: paper review against Anthropic and OpenAI

Issue #73 ships one adapter, Gemini. A port shaped around a single
implementation usually fits nothing else, so before finalising the interface it
was reviewed on paper against the request and response shapes of the two
providers most likely to become the second adapter: Anthropic's Messages API
and OpenAI's Chat Completions API. No Anthropic or OpenAI code was written —
neither has a free tier, so neither can serve a zero-cost alpha under
bring-your-own-key.

This document records what the review changed. It is the record the issue's
acceptance criterion asks for.

## The interface under review

```python
def generate_text(
    prompt: str,
    *,
    call_class: CallClass,
    purpose: AIPurpose | None = None,
    thinking_level: str | None = None,
    max_output_tokens: int | None = None,
    json_mode: bool = False,
    json_schema: dict | None = None,
    max_attempts: int = 1,
    read_timeout: float | None = None,
) -> str
```

Plus the port-level errors (`AIError`, `AIIncompleteResponse`,
`AIBudgetExceeded`, `AITemporaryCapacity`, `AIQuotaPaused`, `QuotaUnavailable`),
the pause kinds (`daily_quota`, `rate_limit`, `unknown`), and the credential
resolver.

## What the review changed

### 1. The incomplete-response reason became provider-neutral

Gemini reports `candidates[0].finishReason == "MAX_TOKENS"`. Anthropic reports
`stop_reason == "max_tokens"`; OpenAI reports `finish_reason == "length"`. The
original exception carried Gemini's raw string as `finish_reason`, and
`cover_letter.py` re-raised it with a literal `"MAX_TOKENS"` default — a core
module writing a Gemini constant.

**Changed:** `AIIncompleteResponse` now carries a neutral
`reason: IncompleteReason` (`max_output_tokens` | `content_filter` | `other`)
plus the provider's own word in `provider_finish_reason`, which is for logs and
ledger rows only. Core modules branch on `reason`; `cover_letter.py` no longer
names a provider constant. OpenAI's `content_filter` is why the enum has a
third value that Gemini's current handling never produces.

### 2. The credential is an opaque secret, not a header

The three providers authenticate differently: `x-goog-api-key`, `x-api-key`
(plus a required `anthropic-version` header), and `Authorization: Bearer`.

**Changed:** `CredentialResolver.resolve()` returns a `Credential` holding a
bare secret, and the adapter decides how to present it. An earlier sketch had
the resolver hand back request headers, which would have put Gemini's header
name in the credential seam that issue #72 is about to re-implement.

### 3. `max_output_tokens` stays optional, with the requirement pushed to adapters

Gemini treats `maxOutputTokens` as optional. Anthropic's `max_tokens` is
**required**; OpenAI's `max_completion_tokens` is optional.

**Changed:** nothing in the signature, but the contract is now explicit in the
port's docstring: an adapter for a provider that requires the field substitutes
its own default. The alternative — making it required at the port — would have
forced a number onto `gmail_classifier` and `facets`, which have no opinion
about one.

### 4. Structured output is a plain JSON Schema

Gemini takes `responseSchema` inside `generationConfig`. OpenAI takes
`response_format: {"type": "json_schema", "json_schema": {..., "strict": true}}`.
Anthropic has no JSON mode at all; the idiom is a single-tool call with an
`input_schema`.

**Changed:** the port documents `json_schema` as a plain JSON Schema that the
adapter translates, and `json_mode` as "JSON with no schema". Both survive the
review unchanged in shape, but the Anthropic case is why translation is
explicitly the adapter's job rather than a pass-through of Google's field.

### 5. `thinking_level` is a hint, not a budget

Gemini: `thinkingConfig.thinkingLevel` (`minimal`/`low`/`medium`). OpenAI:
`reasoning_effort` (`low`/`medium`/`high`). Anthropic: `thinking.budget_tokens`
— a token count, not a level.

**Changed:** documented as an adapter-translated hint. An earlier draft passed
the value through as Gemini spells it; an Anthropic adapter would then have had
to reverse-engineer a token budget from a word it did not choose. The word is
now the port's, and mapping it is the adapter's problem.

### 6. Usage: `total_tokens` is optional, and thinking tokens are not universal

Gemini's `usageMetadata` reports `promptTokenCount`, `candidatesTokenCount`,
`thoughtsTokenCount`, `cachedContentTokenCount` and a `totalTokenCount`.
OpenAI reports `prompt_tokens`/`completion_tokens`/`total_tokens` with
`completion_tokens_details.reasoning_tokens` and
`prompt_tokens_details.cached_tokens`. Anthropic reports `input_tokens`,
`output_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens` —
**no total, and no separate thinking count** (thinking is billed inside
`output_tokens`).

**Changed:** nothing structural — the ledger columns were already nullable and
`_row_total_tokens` already reconstructs a missing total. The review confirmed
the reconstruction formula must stay `input + output + thinking` with no cached
term, and that an Anthropic adapter must leave `thinking_tokens` NULL rather
than copy `output_tokens` into it, which would double-count. That is now
stated where the reconstruction lives.

### 7. Pause kinds survive unchanged; their evidence does not

Gemini signals quota state in the 429 body (`QuotaFailure` details, quota ids
containing `PerDay`/`PerMinute`). Anthropic and OpenAI signal it in headers —
`retry-after`, plus `anthropic-ratelimit-*` / `x-ratelimit-*` — and OpenAI
distinguishes `insufficient_quota` (billing) from rate limiting in the error
`code`.

**Changed:** nothing at the port. `daily_quota`, `rate_limit` and `unknown`
remain the three kinds core modules know, and `_classify_429` stays inside the
adapter, where a header-reading classifier is a sibling implementation rather
than a new concept. The review's finding is that a header-based adapter needs
the *response*, not just the body — which the adapter already has, and the port
never sees.

### 8. One prompt string, deliberately

Anthropic and OpenAI both separate a system prompt from the user turn; Gemini
has `systemInstruction` too. Every call site here builds one prompt string, so
an adapter can send it as the sole user message and lose nothing.

**Not changed**, and flagged: this is the one argument a second adapter is
likely to want widened, and adding an optional `system` later is additive.

## What the review confirmed

- The **call class** is provider-independent. Who funds a call and whether its
  answer is shared is a property of the work, not of the API, so it is the one
  argument no adapter can reinterpret.
- **Purpose** stays orthogonal to call class, and stays the ledger's
  discriminator.
- A **retry/backoff policy** at the port would have been wrong: the retryable
  status sets differ (Gemini's 5xx set versus OpenAI's 429-with-`retry-after`),
  so `max_attempts` bounds attempts and the adapter decides what is retryable.

# Tech Profile-Grounded Interviews

## Status

Draft for user review. This design narrows the next product milestone to software-engineering roles and prioritizes grounded, coherent interviews over broad role coverage or web research.

## Problem

The current interview flow is a deterministic five-question template with optional Gemini evaluation. Resume analysis produces a shallow profile, questions are not guaranteed to follow a coherent objective sequence, and fallback scoring relies on answer length and a few keywords. The result is repetitive scores and feedback that can be unrelated to the candidate's answer.

## Goals

- Tailor interviews to software-engineering candidates using verifiable career evidence.
- Require a meaningful profile before allowing a personalized interview.
- Generate and persist a complete interview blueprint before the first question.
- Give every question an objective, evidence target, expected signals, and scoring rubric.
- Evaluate relevance, factual grounding, and answer quality against that question-specific rubric.
- Make insufficient evidence explicit instead of inventing facts or presenting generic confidence.
- Keep AI calls bounded, observable, and replaceable behind existing server-side boundaries.

## Non-goals

- Supporting non-technical professions in this milestone.
- Targeting a specific job description or employer.
- Live web research or a market-intelligence cache in the first accuracy milestone.
- Executing candidate code or building a cloud IDE.
- Inferring unsupported resume facts.

## Product flow

```text
CV or substantial summary
  -> evidence extraction
  -> profile quality gate
  -> engineering competency map
  -> persisted interview blueprint
  -> question-by-question interview
  -> rubric-grounded evaluation
  -> evidence-backed results and progress
```

### Meaningful profile gate

The profile must contain enough usable engineering evidence before a personalized interview can start. The gate accepts a readable CV or substantial pasted summary and requires, at minimum, two concrete engineering projects or work examples, identifiable technologies, and responsibilities or outcomes. It rejects empty, unreadable, or purely generic text with an actionable message. The gate is deterministic and runs after extraction; it does not rely on a model's claim that the profile is sufficient.

The user may edit the extracted profile. Edits must remain tied to source text or be explicitly marked as user-provided context. The system never silently downgrades a rejected profile into a generic interview.

## Evidence model

Introduce a server-owned, role-focused evidence model. Each evidence item includes:

- stable identifier
- source kind and source excerpt
- project, product, or employer label when available
- role and ownership statement
- technologies and domain tags
- decision or action
- constraint or trade-off
- outcome or metric, if present
- dates or recency, if present
- extraction confidence

Extraction output must be schema-validated. Facts are preserved with their source excerpt so question generation and feedback can cite the candidate's own material. Missing fields remain null; the extractor must not fill them with plausible inventions.

## Competency map

For this milestone, use a software-engineering competency taxonomy shared across seniorities:

- coding and implementation
- debugging and reliability
- architecture and system design
- testing and quality
- performance and scalability
- accessibility and user impact
- delivery and trade-offs
- collaboration and communication

Competency relevance is derived from evidence tags and explicit responsibilities. Estimated level remains separate from evidence confidence. Historical answer evidence can update competency signals, but it cannot create resume facts.

## Interview blueprint

Before the first question, the server asks Gemini to propose a blueprint from the validated profile evidence and competency map. The blueprint is schema-validated and constrained to the five-question backbone plus at most three conditional follow-ups. Each planned question stores:

- objective and category
- competency being assessed
- evidence item identifiers being probed
- difficulty
- expected signals
- missing-signal prompts
- follow-up condition and limit
- question text
- source-confidence metadata

The server validates that every personalized question references existing evidence or a clearly labelled general competency objective. It rejects malformed or unsupported plans and uses a deterministic competency-balanced fallback blueprint with explicit generic wording only when evidence is insufficient for a particular objective.

Questions are generated once per session and persisted. Subsequent turns may choose a stored follow-up or rewrite the next stored question using the current transcript, but may not change the question's objective or evidence target.

## Grounded evaluation

Each answer is evaluated against the persisted question rubric. The evaluator returns:

- answer relevance to the exact question
- supported evidence or claims from the answer
- expected signals present
- missing points
- unsupported or contradictory claims
- dimension scores with short justifications
- strengths and next improvements
- a concise improved-answer outline using only supported candidate facts
- whether a follow-up is warranted

The server rejects evaluations that omit required rubric fields or exceed score bounds. A deterministic fallback is allowed only for transport/provider failure and must score relevance and completeness from explicit answer/question checks; it must not use answer length as a proxy for competence or repeat a fixed score. If an answer is unrelated, the result must say so and lower relevance rather than producing generic praise.

## AI and cost boundary

The first milestone uses approximately seven core Gemini calls for a five-question session: one evidence extraction call, one blueprint call, and one evaluation call per answer. Voice transcription is additive. No web calls occur in this milestone. Provider calls remain in server-only modules, use bounded timeouts, validate structured JSON, and emit safe failure telemetry without exposing credentials.

## Persistence and compatibility

Add evidence and blueprint data through additive Supabase migrations and user-scoped RPCs. Existing profiles, sessions, and evaluations must continue to hydrate. Legacy sessions may display a clearly labelled limited-feedback state when no blueprint or evidence references exist. New sessions must not start without passing the meaningful-profile gate.

## Error handling

- Extraction failure: show an actionable retry or paste-text message; do not create a personalized profile.
- Profile gate failure: identify the missing evidence category (projects, technologies, responsibilities, or outcomes).
- Blueprint validation failure: retry once with a stricter repair prompt, then use the deterministic constrained fallback and mark the session as limited-grounding.
- Evaluation failure: preserve the answer and question, record a safe provider failure state, and provide deterministic relevance feedback rather than a fabricated competency score.
- Database failure: keep client-safe errors while logging structured server diagnostics with request correlation.

## Testing and acceptance

Use red -> green -> refactor. Add tests for:

- evidence extraction schema and source-excerpt preservation
- meaningful-profile acceptance and rejection boundaries
- blueprint evidence references, five-question shape, and follow-up limits
- question objective preservation across turns
- evaluation relevance and unsupported-claim handling
- deterministic fallback score variation for clearly different answers
- legacy hydration and user isolation
- AI call counts and timeout/error mapping at the server boundary

Acceptance requires a real Supabase migration run and an authenticated deployed session that demonstrates questions grounded in the profile, scores that differ for materially different answers, and feedback tied to the exact question.

## Future web research

After grounded profile interviews are reliable, add optional role-level research. Research should be cached, source-attributed, freshness-labelled, and used to enrich competency expectations—not to override the candidate evidence model. It should be a separate provider boundary so the core interview remains useful when research is unavailable.

## Deferred roadmap commitments

These are intentionally deferred from the first accuracy milestone but remain important follow-up work:

1. Live web research and a cached market-intelligence layer for current software-engineering interview expectations.
2. Executable candidate-code evaluation and a cloud-IDE-style workspace with preview, tests, console, and runtime feedback.

They should return as dedicated design and implementation slices after profile grounding and rubric-based evaluation are reliable.

# PRD Gap Analysis

This document records the gaps between the current implementation and the approved POC PRD. It is an implementation audit, not a replacement for the PRD.

## P0 gaps

### Addressed: software-engineering profile grounding

The first accuracy-focused implementation slice is now addressed for software-engineering candidates. The app extracts source-backed engineering evidence, enforces a deterministic meaningful-profile gate, persists a five-question conversational blueprint plus bounded follow-ups, and evaluates each answer against the exact stored question objective instead of a generic score template.

Relevant PRD: sections 7, 9, 14, 16–17, 20–21, 49, and 50.

### Addressed: conversational interview structure and feedback

Results now offer expandable per-question feedback with the question, answer, source grounding, relevance, supported claims, missing points, unsupported claims, dimension reasons, and a tailored improved answer. Individual evaluations persist the PRD-aligned coaching dimensions needed to explain the signal behind each answer without inventing unsupported details.

Relevant PRD: sections 13–15 and 22–24.

### Addressed: readiness and progress insights

The Progress screen now distinguishes between no evidence, first-session baseline, and multi-session evidence states. It renders readiness, latest score, score trend, strongest competency, weakest competency, and recurring weakness coaching while preserving explicit profile-readiness states when the source evidence gate still fails.

Relevant PRD: sections 26–27 and 50.

### Hands-on exercise generation and adaptation

The app currently provides one static product-search exercise. It does not generate or select different exercises using professional background, seniority, market patterns, preferences, or prior performance. Future hands-on sessions do not become more targeted based on identified weaknesses.

Relevant PRD: sections 30.1, 30.3, 30.17, 30.20–30.24, and 51.

### Hands-on workspace and evaluation depth

- The workspace has no executable preview, console, test runner, API mock, or code-validation feedback.
- Evaluation relies primarily on regex checks against the latest saved code, not execution or a deeper review of implementation correctness.
- The result omits distinct problem-solving, performance, time-management, debugging, and seniority scores.
- There is no complete interviewer-style review covering strong signals, weak signals, observation, and a next-round recommendation.

Relevant PRD: sections 30.4–30.5 and 30.9–30.12.

### Privacy and source-document controls

- The user cannot delete uploaded/source documents or reset all locally stored profile and interview data.
- The UI does not clearly state that career documents are used to personalize preparation.
- A cover letter can be pasted but not uploaded, although the POC screen requirements call for optional cover-letter upload.

Relevant PRD: sections 7.2, 46, and 48.

### Profile completeness

The Profile area lacks a generated professional-scope view, career direction (current role, target role, seniority, location), and preparation preferences (stack, focus areas, format, market, and hands-on session types).

Relevant PRD: sections 36–38.

### AI provider boundary

Gemini requests are embedded directly in the coaching and transcription code. The PRD calls for a provider abstraction so model vendors can be changed without changing interview business logic.

Relevant PRD: section 41.

## P1 gaps

- Personal story bank derived from career history.
- Dedicated self-presentation coaching.
- Focused practice categories, rather than only mixed interview and hands-on choices.
- Current-web research and cached interview intelligence.
- Market-grounded exercise generation, source confidence, freshness, and a “Why this exercise?” explanation.
- Explicit interviewer hint flow with recorded hint level.
- Richer transcript editing and competency explanations.

Relevant PRD: sections 16–17, 28–34, and 30.7, 30.13–30.16.

## Recommended implementation order

1. Improve hands-on execution and evaluation, then make exercises adapt to performance.
2. Add document deletion/reset and complete the Profile settings.
3. Add research/market intelligence, story coaching, focused practice, and interviewer hints.

## Out of scope for the POC

Google authentication, multi-user isolation, and public deployment are now part of the foundation. Job-description overlays, richer cloud IDE infrastructure, and other P2 items remain deliberately out of scope.

Relevant PRD: section 49.

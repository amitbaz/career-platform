# Adaptive Interviewer Conversation Design

Date: 2026-09-01
Status: Approved product direction; implementation plan follows this written spec
Depends on:
- `docs/superpowers/specs/2026-08-29-adaptive-interview-foundation-design.md`
- `docs/superpowers/specs/2026-08-29-tech-profile-grounded-interviews-design.md`
- `docs/superpowers/specs/2026-09-01-practice-first-profile-grounding-design.md`

Related but independent:
- `docs/superpowers/specs/2026-09-01-career-brain-release-3-adaptive-learning-loop-design.md`
  (Release 3 learns from completed sessions; this design changes how the session
  itself is conducted. Neither blocks the other.)

## 1. Purpose

Relay's interviewer does not conduct an interview. It renders templates.

Every interviewer line in a live session comes from one of eight static strings
in `promptForPlan` or the single string in `deterministicFollowUp`. The model is
asked to generate a question on every turn — `turnSchema` declares
`question: z.string().min(1)` — and `evaluateTurn` throws that question away:

```ts
// src/lib/coach.ts:1600
const question = shouldFollowUp
  ? deterministicFollowUp(answeredQuestion)
  : promptForPlan(nextPlannedQuestion, ...);
```

The result is a conversation that cannot react to what the candidate says. It
cannot follow an interesting answer, cannot recognise that the candidate is
stuck, cannot vary its wording, and cannot hold a consistent identity.

This design replaces the template renderer with an adaptive interviewer whose
conversational decisions are made by application logic and whose speech is
authored by the model.

The release succeeds when a practice session is indistinguishable in feel from a
real round with a competent human interviewer, while every decision that affects
scoring, coverage, or long-term memory remains deterministic and testable.

## 2. Observed defects

Three defects were observed together in one live practice session. They are the
acceptance criteria for this release, and each maps to a distinct root cause.

### 2.1 The interviewer pushes harder when the candidate is stuck

The candidate answered "sorry i am having difficulties put this into words", and
later "i am having a blackout" and "i don't know". Each was scored against the
question rubric as though it were an attempt, scored low, and therefore triggered
`shouldAskFollowUp`, which asked a harder question about the same competency.

There is no code path that can recognise a non-answer, and no code path that can
set a question aside and return to it.

### 2.2 The interviewer recites the CV verbatim, including contact details

`cvExcerpt` (`src/lib/coach.ts:1424`) splits the CV on `/(?<=[.!?])\s+/`. A CV
header contains no sentence-terminating punctuation, so the first "sentence" is
the entire header block. The `experience` template then quotes up to 420
characters of it directly to the candidate, producing a question that recited the
candidate's own name, phone number, email address and LinkedIn URL back at them
before asking anything.

### 2.3 The interviewer asks the same question twice

`deterministicFollowUp` is a single sentence with the competency name
interpolated. Two consecutive follow-ups on two different targets rendered as
"Make the Self Presentation example more concrete: what trade-off did you choose,
and how did you measure the outcome?" and "Make the Frontend Architecture example
more concrete: what trade-off did you choose, and how did you measure the
outcome?".

Nothing in the system tracks what has already been asked.

### 2.4 Why the templates exist

Commit `02ec2c1` replaced model-authored question text with templates because the
model was leaking rubric content — objectives and expected signals — into
candidate-facing questions. That diagnosis was correct. The remedy removed the
adaptivity along with the leak. This design keeps the leak fixed by making it
structurally impossible rather than by removing the model from the loop.

## 3. Product principles

### 3.1 The model interprets and speaks; application logic decides

Identical to Release 3 §2.1, applied to conversation rather than memory.

The model may judge how well an answer met a rubric, and may author a natural
line of speech for a decided intent. The model must not decide whether to follow
up, whether to rescue, when to move on, which competency comes next, how much
assistance the candidate has received, or when the session ends.

Those are deterministic application decisions.

### 3.2 One rubric, one scale, always

Scoring never varies by mode. A second scale would make scores incomparable
across a mixed history and would corrupt Release 3's aggregation, which treats
sessions as commensurable evidence.

What varies is how much help the candidate received to produce the answer. That
is recorded as a separate fact on the turn, not folded into the score.

### 3.3 Assistance is signal, not noise

An answer that required a rescue is the sharpest available evidence of a
weakness: it marks the exact point where the candidate could not proceed
unaided. Discounting it would soften precisely the most diagnostic signal.

Rescue counts falling across sessions for the same competency is improvement that
raw scores may not show at all.

### 3.4 A non-answer is not a bad answer

"I don't know" and "I am having a blackout" are not weak attempts at the
question. They are the absence of an attempt. They must not be scored against the
rubric, must not depress competency scores, and must not become evidence of
inability in Release 3.

They are, however, a legitimate trigger for a change of approach.

### 3.5 The interviewer never quotes source documents

A person who has read a CV refers to what it says. They do not read it aloud. The
interviewer receives structured evidence fields, never raw CV or job-description
text.

### 3.6 One session is one round

A real loop is several rounds, each conducted by a different person with a
different agenda and a different manner. A single conversation that mixes
introduction, experience, technical depth, architecture and behavioural
questioning has no coherent identity, and an interviewer with no coherent
identity cannot sound like a person.

### 3.7 Candidate facts stay grounded

Identical to Release 3 §2.5. A job description is context for what may be asked.
It is never evidence that the candidate did anything. The interviewer may quote
the job description; it may not assert candidate experience that only the job
description mentions, and it may not invent facts about the hiring company.

## 4. Approaches considered

### 4.1 Keep the templates, improve their wording

Rewrite the eight templates and the follow-up string to sound more natural.

Rejected. Better wording does not make a fixed script react to the candidate. All
three observed defects survive: a template cannot recognise a blackout, and two
targets still render the same follow-up.

### 4.2 One model call, trust the returned question

Keep the existing single `turnSchema` call, stop discarding `result.question`,
and validate the returned text against a list of forbidden patterns.

Rejected. The same call privately scores the candidate and speaks to them, so the
rubric is present in the context that authors the question — which is exactly the
condition that produced the leak `02ec2c1` was fixing. Validation catches
anticipated leaks only. Separately, a call whose output schema is dominated by
`score`, `dimensions`, `strengths` and `needsWork` is not in character as a
person, and that shows in the prose.

Cheapest option, and it does reach some of the goal. Retained as the fallback
shape if the two-call latency proves unacceptable in practice.

### 4.3 Two model calls: assess, then interview

Split assessment and speech. The interviewer call receives the assessor's
conclusion but never the rubric text.

Better: the leak becomes structurally impossible, and the interviewer prompt can
be entirely persona and conversation.

Insufficient alone: it still leaves the consequential decisions — follow up or
advance, rescue or press, when to stop — inside a model call, where they cannot
be tested and where mode policy and coverage budgets cannot be enforced.

### 4.4 Two model calls plus a deterministic director — selected

As 4.3, with the decision of what happens next computed by application logic from
the assessment, the coverage state, the mode policy and the assistance budget.
The interviewer call authors speech for an already-decided intent.

Selected because it is the only option in which coverage, repetition, rescue
budgets and mode behaviour are pure functions that can be tested without a model,
and because it matches the architecture Release 3 already settled on.

Cost: two sequential model calls per turn, and a real piece of logic to maintain.

## 5. Release boundary

This release includes:

- the three-stage turn pipeline (assessor, director, interviewer);
- the deterministic director, with coverage, repetition, and budget enforcement;
- Coach and Real modes as policy over one engine;
- assistance recording, per turn and per question, surfaced in results;
- the blueprint reshaped from pre-written prompts into a coverage plan;
- optional anchoring of a session to an `Opportunity`;
- all six guardrails in §11;
- one round implemented: the tech lead / senior engineer evaluation.

This release does not include:

- the recruiter, HR, and founder rounds (defined in §15, not built);
- the code-discussion variant of the coding round (§15.4);
- any change to the hands-on coding flow;
- any change to the evaluation rubric or scoring dimensions;
- consumption of assistance data by Release 3 observation extraction;
- voice, streaming, or real-time transcription changes.

## 6. Turn pipeline

### 6.1 Stages

```text
candidate answer
  |
  |-- 1. ASSESSOR (model call)
  |      in:  question objective, expected signals, rubric criteria,
  |           answer, transcript
  |      out: GroundedEvaluation, plus a coarse read:
  |           "answered" | "partial" | "evasive" | "stuck"
  |      never rendered to the candidate
  |
  |-- 2. DIRECTOR (pure function, no model call)
  |      in:  assessment, derived coverage state, mode policy,
  |           assistance spent, turns remaining
  |      out: one typed Intent, plus the assistance delta for this turn
  |
  |-- 3. INTERVIEWER (model call)
         in:  intent, round definition, persona stake, job context,
              structured evidence fields, transcript,
              questions already asked
         out: one line of interviewer speech
         never receives: objective, expected signals, rubric criteria, scores
```

### 6.2 Intent

```ts
type Intent =
  | { kind: "open"; targetId: string }
  | { kind: "probe"; targetId: string; aspect: ProbeAspect; basis: string }
  | { kind: "challenge"; targetId: string; claim: string }
  | { kind: "rescue"; targetId: string; style: RescueStyle; hook?: EvidenceRef }
  | { kind: "advance"; targetId: string; reason: AdvanceReason }
  | { kind: "hypothetical"; targetId: string; basis: string }
  | { kind: "candidate-questions" }
  | { kind: "close" };

type ProbeAspect =
  | "specifics" | "ownership" | "tradeoff" | "outcome"
  | "collaboration" | "hindsight";
```

`basis` and `claim` carry the candidate's own material — what they actually said —
so the interviewer call can react to it without needing the rubric.

`Intent` is persisted on the question row it produced (§12).

### 6.3 What is removed

`promptForPlan`, `deterministicFollowUp`, `cvExcerpt`, and `shouldAskFollowUp` are
deleted. `interview-planner`'s `promptFor`, `blueprintPrompt` and `discoveryPrompt`
are deleted.

`evaluateAnswer` is retained unchanged in behaviour: it is stage 1 in isolation.

`groundedEvaluationFor` is retained as the assessor's deterministic fallback
(§13.2).

## 7. Rounds and persona

### 7.1 Round definition

```ts
type InterviewRound = {
  id: RoundId;
  label: string;
  agenda: string;          // what this interviewer is trying to find out
  register: string;        // how this interviewer talks
  moves: Intent["kind"][]; // the repertoire the director may draw from
  probeAspects: ProbeAspect[];
  outOfScope: string[];    // subjects this round never raises
  opening: Intent["kind"];
  closing: Intent["kind"];
};
```

The director may only issue intents whose `kind` appears in `moves`, and may only
use `aspect` values in `probeAspects`. This constraint is what gives a round a
stable identity across a whole session.

### 7.2 Persona

Persona is a stake, not a costume. The interviewer is given a role and a
motivation, not a name, a biography, or personal anecdotes. It does not make
small talk, does not perform enthusiasm, and does not invent shared history.

For the tech-lead round the stake is: you are the senior engineer this candidate
would work alongside; you are deciding whether they can own frontend architecture
without supervision; you have read their CV and you do not accept claims without
specifics.

A consistent motivation produces a consistent voice. Invented personality does
not.

### 7.3 The tech lead round (implemented)

```text
id:        "tech-lead"
agenda:    Can this person actually own what they claim to have owned?
register:  Direct, unhurried, specific. Follows one thread to its end
           before opening another. Sceptical of unsupported claims,
           not hostile.
moves:     open, probe, challenge, rescue, advance, hypothetical,
           candidate-questions, close
aspects:   specifics, ownership, tradeoff, outcome, collaboration, hindsight
outOfScope: salary, notice period, visa status, company values,
            "why us", live coding, take-home logistics
opening:   open
closing:   candidate-questions
```

Every move in this repertoire is a reaction to something the candidate said. None
of them can be written before the session begins, which is why a pre-written
blueprint could never conduct this round.

## 8. Modes and assistance

### 8.1 Mode policy

```ts
type ModePolicy = {
  rescuesPerQuestion: number;
  rescuesPerSession: number;
  rescueStyles: RescueStyle[];
  pushback: "light" | "firm";
  parkAndReturn: boolean;
  acknowledgeStruggle: boolean;
};

type RescueStyle = "narrow" | "hook" | "reframe" | "park";
```

Mode affects nothing outside the director. No other module branches on it.

### 8.2 Rescue styles

- `narrow` — reduce the question to one answerable piece.
- `hook` — offer a concrete anchor drawn from structured evidence or the job
  description: "you owned the design system migration — start there".
- `reframe` — ask for the same material as a story rather than an abstraction.
- `park` — acknowledge, move to another target, and return later if turns remain.

`park` is the move the current system cannot make, and the correct response to
the blackout in §2.1.

### 8.3 The two modes

```text
Coach:  rescuesPerQuestion 2, rescuesPerSession 5,
        styles [narrow, hook, reframe, park],
        pushback light, parkAndReturn true, acknowledgeStruggle true

Real:   rescuesPerQuestion 1, rescuesPerSession 2,
        styles [narrow],
        pushback firm, parkAndReturn false, acknowledgeStruggle false
```

Real mode is not punitive. A real interviewer rephrases once out of politeness
and then moves on; if the candidate blanks, that costs them. Real mode's value is
that its feedback describes where the candidate would actually have lost the
role.

### 8.4 Assistance recording

```ts
type AssistanceRecord = {
  style: RescueStyle;
  intentId: string;
  at: string;
};
```

Stored on the question row. Results display the score alongside the assistance
that produced it — "7.5, reached after two rescues". A score shown alone in Coach
mode flatters the candidate and is therefore not permitted in the results view.

Scoring itself is untouched by assistance. See §3.2.

## 9. Blueprint as a coverage plan

### 9.1 Shape

```ts
type CoverageTarget = {
  id: string;
  competencyId: string | null;
  competencyName: string | null;
  category: QuestionCategory;
  evidenceIds: string[];
  difficulty: Difficulty;
  objective: string;         // assessor only
  expectedSignals: string[]; // assessor only
  rubricCriteria: string[];  // assessor only
  required: boolean;
};

type InterviewBlueprint = {
  status: BlueprintStatus;
  fallbackReason: string | null;
  roundId: RoundId;
  turnBudget: number;
  opportunityId: string | null;
  targets: CoverageTarget[];
};
```

The rubric material remains — the assessor requires it. It simply stops being
adjacent to anything that authors candidate-facing text.

`validateInterviewBlueprint` drops its non-empty-prompt requirement and validates
coverage targets instead: at least one required target, unique ids, and rubric
criteria present on every target.

### 9.2 Coverage state

Derived on every turn from the persisted questions, intents and evaluations.
Never stored.

```ts
type TargetState = {
  target: CoverageTarget;
  status: "unasked" | "open" | "satisfied" | "parked" | "skipped";
  turnsSpent: number;
  rescuesSpent: number;
  askedIntents: Intent[];
};
```

Each POST to `/api/interview` is stateless. Deriving coverage rather than storing
it removes any possibility of the stored copy drifting from the rows it
describes, at the cost of a pure recomputation per turn.

### 9.3 Director rules

1. An intent identical to one already in `askedIntents` for a target is never
   issued again.
2. A target becomes `satisfied` when the assessor reports its expected signals
   present — not when a fixed number of questions have been asked.
3. A `parked` target is reconsidered whenever turns remain and no required target
   is unasked.
4. When remaining turns are fewer than the number of unasked required targets,
   advancing to an unasked required target outranks deepening an open one.
5. A target the session closes without covering is recorded `skipped` with a
   reason, so results can state what was not reached rather than silently
   under-reporting.
6. Assessment `stuck` never produces `probe` or `challenge`. It produces `rescue`
   while budget remains, and `advance` once it does not.
7. Rescue budgets are enforced per question and per session, per §8.3.

## 10. Job anchoring

A session optionally carries `opportunityId`.

**Anchored.** Coverage targets are built from the job description alongside the
CV, and every entry in `opportunity.gaps` becomes a `required: true` target — a
tech lead's real agenda is the places the candidate looks thin against the spec,
and that list is already computed. The interviewer receives `company`, `role`,
and job-description context, and may ask in the employer's own terms.

**Unanchored.** The existing discovery path: CV-grounded targets, no company
context. The interviewer speaks as a senior engineer at an unnamed company and
does not pretend otherwise.

Both use the same engine. The anchored path adds input; it does not add a branch
through the pipeline.

Grounding is governed by §3.7.

## 11. Guardrails

Each rule is independently testable.

### 11.1 No rubric in candidate-facing text

Structural: the interviewer call's context never contains `objective`,
`expectedSignals`, or `rubricCriteria`.

Validated as well as prevented: interviewer output containing any of those
strings is rejected.

### 11.2 No source-document quotation

The interviewer receives structured evidence fields — `projectOrEmployer`,
`ownership`, `technologies`, `decision`, `outcome` — and never raw CV or
job-description text.

Additionally, interviewer output containing an email address, a telephone number,
or a URL is rejected unconditionally. None can legitimately appear in an
interviewer's question.

### 11.3 A non-answer is not scored

Assessment `stuck` records the turn with `nonAnswer: true`, produces no rubric
score, and contributes nothing to competency scores or to Release 3 evidence.

### 11.4 One question, and it comes last

Interviewer output is at most two sentences, contains exactly one question, and
the question is the final sentence. Rejected otherwise.

### 11.5 No coaching in character

Real mode output contains no praise, encouragement, or hints. Coach mode may
acknowledge a struggle in a single clause before a rescue, never a paragraph.

### 11.6 No repetition

Enforced primarily by director rule §9.3.1. Additionally the interviewer call
receives the list of questions already asked, with an instruction not to
paraphrase them.

### 11.7 On validation failure

One retry, naming the violated rule. If the retry also fails, the turn falls back
to the deterministic line for that intent (§13.2) and the session is marked
degraded.

## 12. Data model and migration

```text
interview_sessions   + round_id text not null default 'tech-lead'
                     + mode text not null default 'real'
                       opportunity_id  -- already exists, added by
                                       -- 202608300006_session_career_context.sql;
                                       -- this release reads it, adds nothing

interview_questions    prompt text -> nullable
                     + asked_intent jsonb null
                     + assistance jsonb not null default '[]'
                     + non_answer boolean not null default false
```

TypeScript:

```ts
InterviewSession  + roundId: RoundId
                  + mode: InterviewMode
                  + opportunityId: string | null

PlannedQuestion     prompt: string | null      // authored at reveal time
                  + askedIntent: Intent | null
                  + assistance: AssistanceRecord[]
                  + nonAnswer: boolean
```

One migration, following `202608310001_planned_practice_sessions.sql`: two new
session columns, three new question columns, and dropping the `not null`
constraint on `interview_questions.prompt`
(`202608290001_adaptive_interview_foundation.sql:69`), since a question's text no
longer exists until it is revealed. `opportunity_id` already exists on
`interview_sessions` and is reused as-is, so anchoring (§10) needs no new session
storage. Existing rows backfill to `mode: "real"`, `roundId: "tech-lead"`, and
retain their persisted prompt text, so completed sessions render exactly as they
do today.

Session and turn writes go through Postgres functions rather than direct inserts
(`record_conversation_turn`, `create_conversation_session_with_blueprint`, and
the planned-session variant). The migration therefore replaces those functions as
well as the tables: `record_conversation_turn` gains the intent, assistance, and
non-answer parameters, and the session creators accept `round_id` and `mode`.
Column changes alone would leave the write path unable to populate the new
columns.

`asked_intent` is persisted although nothing reads it back in this release. It is
what makes "why did it ask me that" answerable when a session goes wrong, and
Release 3 will want it: "needed a hook to begin on architecture" is a materially
better observation than a low score, and it is only reconstructible if the intent
was recorded.

## 13. Failure, latency, and cost

### 13.1 Latency and cost

Two sequential model calls per turn. The interviewer cannot begin until the
assessor's read is available. Expect roughly 2–6 seconds between the candidate's
answer and the next question, against roughly 1–3 today.

The conversation view must show an explicit pending state on the interviewer
message. A silent gap of that length reads as a failure.

Cost is approximately double per session: two small calls against short contexts
rather than one.

### 13.2 Degradation

The interview must never break. This mirrors Release 3 §12.1.

- Assessor call fails or returns unusable output — fall back to
  `groundedEvaluationFor`.
- Interviewer call fails, or fails validation twice — emit the deterministic line
  for that intent kind. This is a small set of short strings, one per intent kind,
  and is the only surviving use of templates in the system. It is explicitly the
  degraded path.
- `GEMINI_API_KEY` absent — the whole session runs deterministically, as today.

A session in which either fallback fired records `degraded: true`. Release 3 must
not derive confident observations from a degraded session.

### 13.3 Safe logs

Following Release 3 §12.2, logs carry operation, model, status, intent kind, and
validation-failure reason. They never carry answer text, CV text, or job
description text.

### 13.4 Accepted risk

With templates removed from the normal path, question quality depends on the
model. Current output is robotic but predictable; the new path can produce a
question that is off-register or subtly leading, which validation will not catch.
Validation enforces rules, not judgment.

This is the deliberate trade for adaptivity. It is recorded here so that it is a
known cost rather than a discovery.

## 14. Testing strategy

Red → green → refactor, per `AGENTS.md`. The director being a pure function is
what makes most of this testable without a model.

### 14.1 Director unit tests

Rescue budgets per mode; no repeated intent for a target; a parked target returns
when turns remain; required unasked targets outrank deepening when the budget
runs low; `stuck` routes to rescue and never to probe; assistance accounting;
`skipped` recorded with a reason.

### 14.2 Guardrail tests

One test per rule in §11: rubric strings rejected; contact details and URLs
rejected; more than two sentences rejected; question not final rejected; Real
mode praise rejected; paraphrase of an asked question rejected.

### 14.3 Regression tests for the observed defects

One per defect in §2, expressed literally:

1. "i am having a blackout" produces `rescue` or `park`, never `probe` or
   `challenge` on the same target.
2. A CV whose header contains no sentence punctuation never has that header text
   appear in any interviewer question.
3. Two consecutive follow-ups on different targets do not produce the same
   sentence.

### 14.4 Round repertoire

The tech-lead round never issues an intent outside its `moves`, and never raises
anything in `outOfScope`.

### 14.5 Migration

Following `legacy-blueprint-migration.test.ts`: sessions completed before this
change still render their persisted prompts, and legacy blueprints without
coverage targets do not crash the turn path.

### 14.6 Flow

Following `release2-flow.test.ts`: a full session in each mode, anchored and
unanchored, asserting that required coverage completed, assistance was recorded,
and no guardrail fired.

## 15. Deferred rounds

Defined here so they can be implemented without re-deciding anything. The engine
is complete once §6 through §13 ship; each of these is a round definition plus
its repertoire.

The candidate's real loop, in order:

1. Recruiter screen
2. Tech lead / senior engineer evaluation — **implemented in this release**
3. Live coding session, or take-home followed by a discussion session
4. HR, where the company has one
5. CTO or CEO as the final step

### 15.1 Recruiter screen

Agenda: is this person plausible, available, affordable, and interested. Not an
assessment of ability. Moves: open, probe (specifics only), advance,
candidate-questions. Out of scope: technical depth, architecture, code. Register:
brisk, friendly, working through a list.

### 15.2 HR

Agenda: values, conflict, feedback, failure, how the person behaves under
pressure. Moves: open, probe (collaboration, hindsight), challenge, rescue,
advance. Out of scope: technical depth, architecture, salary negotiation
specifics.

### 15.3 CTO or founder

Agenda: judgment, commercial awareness, motivation, whether the person raises the
bar. Unstructured by nature. Meaningful only when the session is anchored to an
opportunity, because "why us" requires a specific us. Moves: open, probe
(hindsight, tradeoff), challenge, hypothetical, candidate-questions.

### 15.4 Code discussion

The second half of round 3: the candidate has already written code, and an
interviewer walks them through it — approach, structure, trade-offs, what they
would change. This is a conversation about an artifact rather than a fresh
interview, so it needs the artifact in the interviewer's context and a distinct
set of probe aspects. It reuses the pipeline but is not merely another round
definition, and is scoped as its own piece of work.

## 16. Release gate

The release ships when:

- all three regression tests in §14.3 pass;
- every guardrail in §11 has a passing test;
- a full Coach-mode and a full Real-mode session complete end to end, anchored
  and unanchored, with required coverage reached;
- completed pre-migration sessions render unchanged;
- `npm test`, `npm run lint`, and a production build pass, per `AGENTS.md`;
- the author has run a live session in each mode and judged it conversational.

The last gate is subjective on purpose. The defect being fixed is subjective, and
the tests can prove the absence of the three known failures without proving the
presence of the quality being sought.

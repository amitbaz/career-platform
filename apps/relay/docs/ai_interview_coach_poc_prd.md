# Product Requirements Document
## AI Interview Coach — POC

**Status:** Approved for POC development  
**Version:** 1.1  
**Target:** Mobile-first web POC  
**Primary device:** Smartphone browser  
**Secondary device:** Desktop browser  
**POC target:** Sunday, August 30, 2026  
**Deployment model:** Personal, single-user POC  
**Access model:** No application login; local or privately protected access only

### POC Operating Assumption

This POC is built for one known user and is not intended for public release.

The application opens directly into onboarding or the returning-user Home screen. It does not include registration, sign-in, account recovery or multi-user account management.

The implementation should preserve a clean path to future authentication by:

- keeping user-owned data behind a storage/service boundary
- associating persisted records with one implicit local owner
- avoiding authentication assumptions in interview business logic
- reserving public or multi-user deployment until authentication and tenant isolation are implemented

If the POC is made reachable outside the developer's machine, access must be protected at the deployment or network level.

---

## 1. Product Vision

Build a personalized AI interview coach that keeps a professional continuously ready for interviews within their career scope.

The product should understand:

- who the user is professionally
- what they have actually worked on
- what level of seniority they operate at
- which technologies and competencies matter for their career
- how they describe themselves professionally
- what interview topics are currently relevant
- where they perform well or poorly in interviews
- what they should practice next

The core promise is:

> **Open the app and start a relevant interview immediately.**

The user should not need to configure a job, company, technology, or interview type every time.

---

## 2. Product Positioning

This is **not** primarily:

- a question bank
- a LeetCode clone
- a CV generator
- a job application tracker
- a company-specific interview database
- a chatbot that generates random interview questions

It is:

> **A persistent AI interview coach built around the user's professional identity.**

The product continuously learns about the user and improves its understanding of their interview readiness.

---

## 3. Core Product Principle

The system should answer two different questions:

### Generic

> What should a Senior Frontend Engineer know?

### Personalized

> What should *this particular Senior Frontend Engineer* be able to explain, demonstrate and defend in an interview?

The second question drives the product.

---

## 4. POC Goal

The POC needs to prove one complete loop:

```text
Professional background
        ↓
AI understands user
        ↓
Creates interview scope
        ↓
Runs realistic interview
        ↓
Evaluates answers
        ↓
Identifies strengths/weaknesses
        ↓
Remembers performance
        ↓
Next interview becomes better targeted
```

If this loop feels convincing, the POC succeeds.

---

## 5. POC Success Criteria

The POC is successful when a test user can:

1. Open the web app on their phone.
2. Upload or paste their CV.
3. Optionally provide a cover letter template.
4. Review an automatically generated professional profile.
5. Start an interview without providing a job description.
6. Answer using text or voice.
7. Receive intelligent follow-up questions.
8. Complete a mixed interview session.
9. Receive useful personalized feedback.
10. See strengths and weaknesses recorded.
11. Start another interview that uses previous performance as context.
12. Start a realistic hands-on technical interview.
13. Complete or partially complete a coding/system-design exercise.
14. Receive interviewer-style feedback on implementation, reasoning and communication.
15. Have hands-on results influence future practice.

---

## 6. Target User

### Initial target

Experienced software engineers.

### POC specialization

Senior Frontend Engineers.

The system architecture should be extensible to other professions later, but the POC should optimize deeply for frontend engineering rather than poorly supporting every occupation.

---

## 7. User Inputs

The product builds a persistent **Professional Profile**.

### 7.1 CV — Primary source

The CV provides factual career information.

The system extracts:

- job titles
- employers
- dates
- responsibilities
- projects
- technologies
- achievements
- industries
- seniority indicators
- leadership indicators
- ownership indicators

Input methods:

- PDF upload
- paste text

### 7.2 Cover Letter — Optional

The user may upload or paste an existing cover letter or cover-letter template.

This provides a different type of information from the CV.

**CV answers:**

> What has this person done?

**Cover letter answers:**

> How does this person position themselves professionally?

The system may extract:

- professional narrative
- key strengths
- career motivation
- preferred positioning
- communication style
- recurring achievements
- value proposition
- career direction
- reasons for seeking a new role

The cover letter is **supporting context**.

It must never override factual CV information.

The AI should also be allowed to identify weak or generic positioning rather than blindly reinforcing it.

### 7.3 User Confirmation

After parsing the available information, the app displays the generated profile.

Example:

#### Professional Profile

**Role**  
Senior Frontend Engineer

**Seniority**  
Senior

**Primary expertise**

- React
- TypeScript
- JavaScript
- frontend architecture
- Next.js

**Additional expertise**

- REST
- GraphQL
- WebSockets
- testing
- accessibility
- performance
- design systems

**Professional characteristics**

- strong product ownership
- extensive frontend responsibility
- architecture experience
- independent decision-making
- cross-functional collaboration

**Career narrative**

Experienced frontend engineer with strong product ownership and deep React experience, moving toward broader senior engineering responsibilities.

The user can edit incorrect information.

Then:

> **Confirm Profile**

---

## 8. Professional Scope

After onboarding, the app automatically determines the user's interview universe.

For a Senior Frontend Engineer this may include:

```text
Frontend Engineering
│
├── JavaScript
├── TypeScript
├── React
├── Next.js / Frameworks
├── HTML
├── CSS
├── Browser Fundamentals
├── Networking
├── APIs
├── State Management
├── Accessibility
├── Testing
├── Performance
├── Security
├── Frontend Architecture
├── Frontend System Design
├── Code Quality
├── Engineering Practices
├── Product Thinking
├── Collaboration
├── Leadership / Ownership
└── Behavioral Communication
```

Not every category receives equal importance.

The system calculates relevance based on:

- user's CV
- years of experience
- seniority
- current market expectations
- technologies used
- target career direction

---

## 9. Professional Knowledge Graph

Each competency should internally maintain:

```text
competency
relevance
expected_level
user_estimated_level
confidence
last_practiced
number_of_questions
average_score
recent_score
weaknesses
strengths
```

Example:

```text
React

Professional relevance: 1.0
Expected level: Senior
Estimated user level: Advanced
Current interview score: 89
Confidence: High
Last practiced: Today
```

Another competency:

```text
Frontend System Design

Professional relevance: 0.9
Expected level: Senior
Estimated user level: Intermediate
Current interview score: 64
Confidence: Medium
Last practiced: 4 days ago
```

This profile evolves continuously.

---

## 10. Main Navigation

POC navigation:

```text
Home
Practice
Progress
Profile
```

The default landing page is **Home**.

---

## 11. Home

The home page should focus on one primary action.

Example:

### Interview Readiness

**76%**

React — 91  
TypeScript — 84  
Architecture — 78  
System Design — 64  
Behavioral — 71

### Recommended focus

Frontend system design

You were less confident explaining architectural trade-offs in your last interview.

### Primary CTA

**Start Interview**

### Secondary CTA

**Start Hands-On Interview**

No configuration should appear before the primary CTA.

---

## 12. Core Feature — Start Interview

Pressing:

> **Start Interview**

immediately begins an interview appropriate for the user's professional profile.

There should be no mandatory setup page.

---

## 13. Default Interview Format

POC session target:

**10–20 minutes**

Typical session:

**5–8 primary questions plus follow-ups.**

The interview should mix several types of questions.

Example:

### Question 1 — Introduction

> Give me a short introduction to yourself and your recent work.

### Question 2 — Experience

> You mentioned owning significant parts of the frontend architecture. What was one architectural decision you made that had a major impact?

### Follow-up

> What trade-offs did you consider?

### Technical question

> How do you decide whether state should live locally, globally or on the server?

### Follow-up

> Imagine the product now receives high-frequency WebSocket updates. Would that change your approach?

### System design

> Design the frontend architecture for a real-time analytics dashboard.

### Behavioral

> Tell me about a disagreement with another engineer regarding a technical decision.

This should feel like a conversation rather than a quiz.

---

## 14. Interviewer Rules

The AI interviewer must:

- behave professionally
- ask one question at a time
- understand previous answers
- ask intelligent follow-ups
- challenge vague answers
- ask for examples
- adapt question difficulty
- use CV experience when appropriate
- use cover-letter narrative when useful
- avoid unnecessary praise
- avoid teaching during the interview
- avoid exposing scores during the interview
- avoid repeatedly asking mastered questions
- distinguish between theoretical and practical knowledge
- occasionally introduce unexpected questions

The interviewer should behave more like an experienced hiring manager or senior engineer than a tutor.

---

## 15. Question Categories

Questions can belong to several classes.

### Knowledge

Tests understanding.

Example:

> Explain the relationship between the JavaScript event loop, microtasks and rendering.

### Practical decision-making

Tests engineering judgment.

Example:

> When would you choose React Context instead of an external state-management library?

### Debugging

Example:

> A React dashboard becomes slow after several hours of use. How would you investigate it?

### Architecture

Example:

> How would you structure a large frontend application used by several independent product teams?

### System design

Example:

> Design the frontend architecture for a collaborative document editor.

### Experience

Example:

> Tell me about the most technically complex frontend project you've owned.

### Behavioral

Example:

> Tell me about a situation where you strongly disagreed with a technical decision.

### Communication

Example:

> Explain server-side rendering to a non-technical product manager.

### Self-presentation

Example:

> Tell me about yourself.

---

## 16. Personal Story Bank

The app should automatically derive reusable interview stories from the user's professional history.

Example:

```text
Story
Frontend architecture ownership

Potential use:
- leadership
- technical decision-making
- architecture
- ownership
- difficult project
- biggest achievement
```

Another:

```text
Story
Legacy application refactor

Potential use:
- technical debt
- disagreement
- architecture
- prioritization
- risk management
```

The app can reuse these stories when coaching the user.

---

## 17. Self-Presentation Coaching

The app should help the user improve answers to questions such as:

- Tell me about yourself.
- Walk me through your career.
- What are you looking for next?
- Why are you leaving your current role?
- What are your strengths?
- What is your biggest achievement?
- Why should we hire you?
- What kind of environment do you work best in?
- Tell me about a difficult project.
- Tell me about a failure.
- Tell me about a conflict.

The app should judge answers based on:

- clarity
- relevance
- structure
- length
- confidence
- specificity
- credibility
- business impact
- technical depth where appropriate
- consistency with CV

The app should help turn lengthy or unfocused answers into concise professional narratives.

---

## 18. Answer Input

### P0 — Text

A standard text input must always be available.

### P0 — Voice

Voice should also be included in the POC.

Flow:

```text
Tap microphone
↓
Browser records audio
↓
Stop recording
↓
Speech-to-text
↓
Transcript appears
↓
Send
```

The user should be able to edit the transcript before sending if necessary.

Voice is important because interview performance involves speaking, not merely knowing the answer.

---

## 19. Interview State

For each active interview, maintain:

```text
session_id
started_at
questions
answers
followups
competencies_tested
difficulty
conversation_context
```

The model receives previous interview messages to preserve continuity.

---

## 20. Adaptive Question Selection

The question engine should decide what to ask using approximately:

```text
priority =
professional_relevance
× interview_frequency
× weakness
× time_since_last_practice
× difficulty_fit
× knowledge_freshness
```

Add controlled randomness so interviews remain unpredictable.

Questions should not simply target weaknesses.

A realistic interview also tests strengths.

Recommended conceptual balance:

```text
40% weak or uncertain areas
30% important/core competencies
20% random realistic coverage
10% strongest competencies at advanced difficulty
```

Exact values do not need to be hard-coded for the POC.

---

## 21. Difficulty

Questions should support approximately:

```text
Foundational
Intermediate
Senior
Advanced
```

Difficulty should depend on:

- user's seniority
- previous answers
- competency score
- confidence level

Example:

User consistently performs strongly in React.

Stop asking:

> What does useEffect do?

Move toward:

> You're building a large React application where several teams independently ship features. How would you prevent shared state and component abstractions from becoming organizational bottlenecks?

---

## 22. Evaluation

Every user answer should be evaluated internally.

Suggested dimensions:

```text
Correctness
Depth
Clarity
Structure
Practical experience
Trade-off awareness
Communication
Confidence
Relevance
```

Not every dimension applies to every question.

---

## 23. Evaluation Output

During the interview:

**Do not show evaluation.**

After the interview, provide a summary.

Example:

## Interview Complete

### Overall

**7.6 / 10**

### Strong

**React architecture**

You explained state ownership clearly and discussed trade-offs rather than presenting one universal solution.

**Experience**

Your examples sounded credible and connected strongly to your actual work.

### Needs work

**System design**

You moved into implementation details before establishing requirements and architecture boundaries.

**Communication**

Several answers were technically correct but longer than necessary.

---

## 24. Per-Question Feedback

Users may expand individual questions.

Example:

### Question

How would you improve frontend performance in a complex dashboard?

### Your answer

Transcript...

### Score

7.2 / 10

### Good

- mentioned profiling before optimization
- identified rendering and network bottlenecks
- discussed virtualization

### Missing

- bundle analysis
- Core Web Vitals
- caching strategy

### Better structure

1. Measure
2. Identify bottleneck
3. Separate network/rendering issues
4. Optimize
5. Measure again

### Example improved answer

Generate a concise answer using the user's actual level and experience.

---

## 25. Performance Memory

After each interview, update competency profiles.

Example:

```text
Frontend System Design
previous_score: 64
session_score: 72
new_score: 67
```

Do not let one answer radically change a competency score.

Use gradual weighting.

---

## 26. Progress

The Progress screen displays:

### Overall Interview Readiness

Example:

**78%**

### Skills

```text
React                  91
JavaScript             87
TypeScript             83
Architecture           79
Performance            74
Behavioral             71
System Design          66
Security               59
```

### Recent improvement

```text
System Design
+8 over last 3 sessions
```

### Recurring weakness

```text
You often explain implementation before discussing trade-offs.
```

---

## 27. Readiness Score

The readiness score is motivational guidance, not scientific measurement.

Conceptually:

```text
readiness =
weighted competency scores
× competency relevance
× confidence
```

The UI should not claim statistical precision.

Use wording such as:

> Interview Readiness

rather than:

> Probability of getting hired.

---

## 28. Practice Mode

Home provides realistic mixed interviews.

Practice allows deliberate study.

Categories could include:

```text
Recommended
Behavioral
React
JavaScript
TypeScript
System Design
Architecture
Performance
Accessibility
Testing
Browser
Hands-On Interview
```

The user may choose an area and run a short focused session.

---

## 29. Recommended Practice

The app should automatically surface:

> **Recommended for you**

Example:

**Frontend System Design**

You've performed below your expected seniority level in your last two sessions.

CTA:

> Practice now

---

# 30. Hands-On Technical Interview Simulation

## 30.1 Product Goal

The platform must prepare users not only to **answer interview questions**, but also to perform under realistic hands-on technical interview conditions.

For professions where practical assessments are common, the app should generate complete technical interview simulations tailored to:

- the user's profession
- seniority
- professional background
- current expertise
- target role
- location / employment market
- previous interview performance
- preferred technologies
- areas that require improvement
- current real-world interview practices

For the initial Frontend Engineering POC, this means realistic coding and system-design sessions similar to those currently used for Senior Frontend Engineer hiring.

---

## 30.2 Hands-On Interview Mode

The Practice area must contain a primary option:

> **Hands-On Interview**

This launches a realistic technical assessment rather than a normal question-and-answer session.

Example:

```text
Hands-On Interview

Senior Frontend Engineer
Estimated duration: 60 minutes

React + TypeScript
Practical implementation
Senior level

[ Start Interview ]
```

The user should know the broad format but should **not see the complete solution or evaluation criteria before starting**.

---

## 30.3 Session Types

The system may generate several realistic interview formats.

### 1. Live Coding

The user receives a problem and codes while an AI interviewer observes and interacts.

Example:

> Build an autocomplete search component that retrieves remote results while the user types.

Requirements may include:

- loading state
- error handling
- request cancellation
- keyboard navigation
- debouncing
- accessibility
- TypeScript
- clean component architecture

The interviewer can ask questions while the user works.

### 2. Machine Coding / UI Implementation

A larger frontend exercise designed for approximately 45–90 minutes.

Example:

> Build a simplified pull-request dashboard.

The candidate may need to implement:

- search
- filters
- API fetching
- status indicators
- loading/error states
- comments
- component structure
- responsive behavior

The evaluation considers both the finished result and the candidate's reasoning.

### 3. Debugging Interview

The user receives an existing broken project.

Example problems:

- excessive rerenders
- stale state
- race conditions
- memory leaks
- accessibility problems
- incorrect async logic
- broken tests
- TypeScript errors

Task:

> Investigate the issue, explain your reasoning and fix the application.

### 4. Refactoring Interview

The user receives working but poorly structured code.

The interview evaluates:

- ability to identify problems
- prioritization
- refactoring strategy
- abstraction decisions
- testability
- maintainability
- communication of trade-offs

### 5. Frontend System Design

The user receives a larger architectural problem.

Example:

> Design the frontend architecture for a real-time analytics dashboard used by several engineering teams.

The candidate must discuss:

- requirements
- application boundaries
- data model
- state ownership
- API strategy
- caching
- rendering
- performance
- accessibility
- testing
- observability
- deployment
- organizational scalability

The AI interviewer should challenge decisions throughout the exercise.

### 6. Combined Senior Interview

The most realistic advanced mode.

Example 75-minute structure:

```text
00–05 min
Problem introduction + clarification

05–15 min
Requirements and architecture discussion

15–50 min
Implementation

50–65 min
Additional requirement / edge case

65–75 min
Code review + trade-off discussion
```

This mode should closely resemble a real senior engineering interview.

---

## 30.4 Interview Environment

The hands-on interview should eventually provide an embedded coding environment.

For the POC, prioritize JavaScript / TypeScript / React.

Recommended experience:

```text
┌─────────────────────────────┐
│ Interviewer                 │
│                             │
│ Build a searchable product  │
│ list using the provided API │
├─────────────────────────────┤
│                             │
│ Code editor                 │
│                             │
│                             │
├─────────────────────────────┤
│ Preview / Console           │
├─────────────────────────────┤
│ 🎙 Talk to interviewer       │
└─────────────────────────────┘
```

On desktop:

- task panel
- editor
- preview
- interviewer/chat

can exist simultaneously.

On mobile:

Hands-on coding should remain available, but the app may recommend desktop for longer coding sessions.

The general interview and coaching experience remains mobile-first.

---

## 30.5 Starter Projects

The system should generate or select an appropriate starter environment.

Example:

```text
React
TypeScript
Vite
Testing Library
Vitest
```

The environment may include:

- existing code
- API mocks
- sample data
- tests
- requirements
- intentionally incomplete components

The candidate should not always start from an empty file.

This is important because real frontend work often involves understanding and modifying existing applications.

---

## 30.6 AI Interviewer During Coding

The AI must act like a real interviewer.

It should **not behave like Copilot**.

The interviewer must not automatically solve the task for the candidate.

It may ask:

> What are you thinking here?

> Why did you put this state at this level?

> What happens if the second network request returns before the first?

> How would a keyboard-only user interact with this?

> If this list contained 100,000 records, what would change?

> What would you test first?

The interviewer should respond intelligently to what the user is actually doing.

---

## 30.7 Help Policy

The user may explicitly ask the interviewer for help.

Example:

> I'm stuck. Can you give me a hint?

The AI may provide an interviewer-style hint rather than the solution.

Hints should be recorded because needing assistance is part of the evaluation.

Possible levels:

```text
Level 1
Clarifying hint

Level 2
Directional hint

Level 3
Strong hint
```

The final evaluation can mention how independently the user progressed.

---

## 30.8 Dynamic Requirements

Real interviews often introduce changing requirements.

The simulation should be able to do the same.

Example:

After 25 minutes:

> Good. Now assume results can update in real time through WebSockets.

Or:

> Product has changed the requirement. Users must now be able to navigate the list entirely by keyboard.

Or:

> The API can return 50,000 items.

This tests adaptability rather than memorized solutions.

---

## 30.9 Think-Aloud Evaluation

The system should encourage the candidate to explain their thought process.

Evaluation should consider:

- requirement clarification
- planning
- communication
- decomposition
- prioritization
- debugging approach
- technical decisions
- trade-off reasoning

The best solution is not necessarily the candidate who writes code fastest.

---

## 30.10 Hands-On Evaluation Model

The interview should produce separate scores.

Example:

### Technical Implementation
8.1 / 10

### Problem Solving
7.8 / 10

### Architecture
7.4 / 10

### Communication
8.3 / 10

### Testing
6.2 / 10

### Accessibility
5.9 / 10

### Time Management
7.5 / 10

---

## 30.11 Evaluation Dimensions

Depending on the exercise:

### Problem understanding

Did the candidate clarify ambiguous requirements?

### Planning

Did they create a sensible implementation strategy?

### Correctness

Does the solution work?

### Code quality

Is the code readable and maintainable?

### Component architecture

Are responsibilities divided sensibly?

### State management

Is state located appropriately?

### Type safety

Is TypeScript used effectively?

### Performance

Are important performance characteristics recognized?

### Accessibility

Is the UI usable and semantically correct?

### Testing

Did the candidate identify and test important behavior?

### Debugging

How effectively were problems identified?

### Communication

Could the candidate explain what they were doing?

### Trade-offs

Could the candidate explain alternatives and limitations?

### Seniority

Did the candidate demonstrate judgment appropriate for the expected level?

---

## 30.12 Final Interview Review

After completion, provide a review similar to feedback from an experienced interviewer.

Example:

## Hiring Signal

**Likely pass**

### Strong signals

- Good decomposition before implementation
- Strong handling of asynchronous state
- Clear React architecture
- Communicated decisions well

### Weak signals

- Accessibility was considered too late
- Tests focused mostly on happy paths
- Performance discussion lacked measurement strategy

### Interviewer observation

You started coding quickly before clarifying the API behavior. At senior level, spending another two or three minutes clarifying requirements would make the approach more deliberate.

### If this were a real interview

> I would likely advance you, but I would probe accessibility and testing more heavily in the next round.

This language should feel much more useful than simply giving a numerical score.

---

## 30.13 Market-Grounded Exercise Generation

Hands-on exercises must not be generated purely from imagination.

The platform should maintain a **Technical Interview Intelligence Layer**.

It should collect current information about interview formats and exercises from sources such as:

- candidate interview reports
- engineering hiring documentation
- company interview guides
- reputable preparation platforms
- engineering blogs
- developer communities
- current job requirements

The system should extract patterns rather than blindly copying questions.

Example source signals:

```text
Senior Frontend
React machine coding
60–90 minutes
API fetching
component architecture
state management
testing
accessibility
follow-up system-design discussion
```

From multiple signals, the system can generate a fresh realistic exercise.

---

## 30.14 Interview Pattern Model

Research findings should be normalized into structures such as:

```text
Role:
Senior Frontend Engineer

Market:
Germany / Europe

Round:
Live Coding

Observed duration:
60–90 minutes

Common technologies:
React
TypeScript

Frequently tested:
API integration
state management
component design
async behavior
testing
accessibility
performance

Typical format:
Existing starter application + requested feature

Confidence:
High

Freshness:
Recent
```

The exercise generator uses these patterns.

---

## 30.15 Source Confidence

Not all online reports are equally reliable.

Each signal should receive confidence based on:

- freshness
- source reputation
- corroboration
- specificity
- whether multiple independent reports describe the same pattern

A single anonymous post should not define the interview model.

Repeated patterns across sources should.

---

## 30.16 Exercise Authenticity

The user should optionally be able to see:

> **Why this exercise?**

Example:

> Based on recent Senior Frontend interview patterns, practical React/TypeScript implementation, asynchronous UI behavior, component architecture and follow-up design discussion are frequently assessed.

This builds trust that the exercise is not arbitrary.

---

## 30.17 Personalization

Market realism determines:

> What interviews currently look like.

The professional profile determines:

> Which version is appropriate for this user.

Example:

### Candidate A

Mid-level React developer.

Exercise:

> Build an autocomplete component.

Focus:

- React fundamentals
- hooks
- API fetching
- loading/error states

### Candidate B

Senior Frontend Engineer with significant architecture experience.

Same underlying problem may evolve into:

> Build the autocomplete experience, then explain how you would turn it into a reusable search platform used by several product teams.

Additional evaluation:

- API abstraction
- caching
- concurrency
- accessibility
- observability
- architecture boundaries
- component API design

The exercise therefore adapts to seniority and background without becoming artificial.

---

## 30.18 Personal Preferences

The user should eventually be able to influence preparation preferences.

Examples:

```text
Target roles
Senior Frontend Engineer

Preferred stack
React / TypeScript

Avoid
Heavy algorithm interviews

Focus more on
System design
Hands-on coding
Behavioral

Interview market
Berlin / Germany
```

These preferences affect weighting, not the fundamental truth of the market.

If algorithmic interviews are common for the target profile, the app should still warn the user rather than hiding them completely.

---

## 30.19 Realism vs Personalization

The product must balance three signals:

```text
MARKET REALITY
What employers actually test

+

USER PROFILE
What this person is likely to encounter

+

USER PERFORMANCE
What this person needs to improve
```

Neither should completely dominate the others.

---

## 30.20 Hands-On Interview History

Store:

```text
exercise_type
duration
technology
difficulty
competencies_tested
code_snapshot
interviewer_conversation
hints_requested
completion_state
evaluation
market_pattern
```

This contributes to the same persistent competency profile used by conversational interviews.

---

## 30.21 Adaptive Hands-On Training

Example:

Session 1:

Autocomplete

Results:

```text
React          8.5
TypeScript     8.0
Accessibility  5.5
Testing        6.0
```

Future sessions should increasingly expose accessibility and testing requirements.

The system might later generate:

> Build an accessible command palette with keyboard navigation and automated tests.

The user therefore trains their actual weaknesses while still experiencing realistic tasks.

---

## 30.22 Relationship to Standard Interview Mode

There are now two primary interview experiences:

### Interview

Conversational mixed interview.

Typical duration:

10–20 minutes.

Focus:

- technical knowledge
- behavioral
- architecture
- communication
- career history

### Hands-On Interview

Long realistic assessment.

Typical duration:

45–90 minutes.

Focus:

- coding
- debugging
- architecture
- problem solving
- communication under pressure

Both contribute to the same professional readiness model.

---

## 30.23 POC Recommendation

For the first POC, do not attempt to recreate a complete cloud IDE infrastructure.

Instead prove the concept using one of these approaches:

### Preferred

Embed an existing browser coding sandbox/runtime.

### Alternative

Provide:

- editable code files
- JavaScript/TypeScript execution
- preview
- AI access to the current code state

The POC needs to prove that the AI interviewer can:

1. give a realistic exercise
2. observe progress
3. discuss decisions
4. introduce follow-ups
5. evaluate the solution

It does not need to prove that the product can replace CodeSandbox.

---

## 30.24 Hands-On POC Acceptance Test

A Senior Frontend Engineer opens:

> Hands-On Interview

The system generates:

**60-minute React + TypeScript practical interview**

The user receives a realistic scenario.

They clarify the requirements.

They begin implementing.

The AI interviewer periodically interacts based on their work.

The user encounters an additional requirement.

The user completes or partially completes the exercise.

The AI reviews:

- code
- approach
- communication
- architecture
- testing
- performance
- accessibility
- seniority signal

The user receives interviewer-style feedback.

The results modify their persistent competency profile.

A future hands-on interview adapts accordingly.

If this loop works, the hands-on POC succeeds.

---

## 31. Current Web Knowledge

The app should not rely exclusively on static LLM knowledge.

It needs a freshness layer.

The system should periodically research topics relevant to the supported profession.

For frontend engineering:

- modern frontend interview practices
- React changes
- Next.js changes
- TypeScript developments
- browser APIs
- frontend system design
- accessibility
- testing
- performance
- common technical assignments
- interview experiences
- hiring expectations

---

## 32. Knowledge Sources

Sources should favor quality over quantity.

### Primary technical sources

- official framework documentation
- MDN
- TypeScript documentation
- standards documentation

### Interview intelligence

- engineering blogs
- reputable interview-preparation resources
- candidate interview reports
- current developer community discussions
- public company interview guides
- current job-market signals

The system should distinguish:

```text
Technical truth
```

from:

```text
Interview trend
```

A community interview report, for example, can indicate what candidates encounter but should not override official technical documentation.

---

## 33. Freshness Architecture

Conceptually:

```text
Web
↓
Research layer
↓
Source evaluation
↓
Structured interview knowledge
↓
Competency mapping
↓
Question / exercise generator
```

Each stored research item should ideally include:

```text
topic
source
source_type
published_at
retrieved_at
summary
relevance
competencies
confidence
market
interview_round
```

---

## 34. POC Freshness Strategy

Do not build a complicated crawler for the POC.

Use an AI model with web-search capability or a search provider.

The system can refresh relevant knowledge:

- on demand
- periodically
- when a competency needs updated material
- before generating a hands-on exercise where market realism matters

Cache useful findings.

This is sufficient to demonstrate the concept.

---

## 35. Optional Job Description

Job descriptions are **not required** for the main product.

They should exist as an optional future/secondary feature.

Example:

> **Upcoming Interview**

User optionally pastes:

- job description
- company
- interview date

The app analyses it against the existing professional profile.

Example:

```text
You are already well prepared for this role.

Extra focus recommended:

GraphQL
Accessibility
System Design
```

This temporarily influences preparation.

It does **not** rebuild the professional profile or replace the default interview engine.

Conceptually:

```text
Permanent profile
+
temporary job context
=
temporary preparation emphasis
```

---

## 36. Profile

The Profile page contains:

### Professional Profile

Editable.

### CV

Replace / update.

### Cover Letter

Optional.

Add / replace / remove.

### Professional Scope

View generated competencies.

### Career direction

Current role  
Target role  
Seniority  
Location

### Preparation preferences

Optional preferences such as:

- preferred stack
- stronger focus areas
- weaker focus areas
- interview format preference
- target market
- preferred hands-on session types

---

## 37. Information Precedence

When information conflicts:

```text
Explicit user corrections
↓
CV factual information
↓
User-entered profile information
↓
Cover letter
↓
AI inference
```

AI inference must never silently overwrite confirmed information.

---

## 38. Location

Location is part of the professional profile.

It can affect:

- terminology
- expected interview style
- hiring norms
- market expectations
- common role titles
- relevant job-market research
- hands-on interview patterns

For example:

```text
Berlin, Germany
```

should influence market context without making every interview Germany-specific.

---

## 39. Mobile-First UX

The POC is a responsive web application.

Design priority:

1. mobile
2. desktop

Minimum target:

**~360px viewport width**

Desktop should not simply look like a stretched phone interface.

Desktop is especially important for hands-on coding sessions.

---

## 40. Design Direction

Visual design is secondary to product functionality for the POC.

Use:

- clean typography
- simple cards
- generous spacing
- minimal navigation
- accessible contrast
- obvious CTA hierarchy
- lightweight animations only where helpful

Avoid:

- excessive illustrations
- complicated dashboards
- large design systems
- unnecessary onboarding animations

A component library is acceptable.

---

## 41. Suggested Technical Stack

Recommended:

```text
Next.js
TypeScript
Tailwind CSS
shadcn/ui
SQLite
Local private file storage
```

### Local Persistence

Use for:

- the single professional profile
- extracted CV and cover-letter text
- private document files
- interview history
- progress data
- hands-on code snapshots
- cached research data if added

All persistence should remain behind a small repository/service interface so the storage implementation can later move to a hosted database without changing interview logic.

### Future Hosted Architecture

For a future multi-user or public release, the local persistence layer may be replaced by a hosted database and private object storage such as Supabase.

That later phase must add:

- authentication
- per-user ownership
- row-level authorization
- private file storage
- account and document deletion
- production deployment controls

The POC must not expose its database, uploaded documents or AI credentials through a publicly accessible deployment.

### AI

Implement a provider abstraction so models can be changed.

Example:

```text
AIProvider
├── OpenAIProvider
└── AnthropicProvider
```

The application should avoid deeply coupling business logic to one model vendor.

---

## 42. Suggested AI Responsibilities

Do not use one giant prompt for everything.

Separate responsibilities conceptually.

### Profile Analyzer

Input:

- CV
- cover letter
- manual profile

Output:

structured professional profile.

### Scope Generator

Input:

professional profile.

Output:

competency map.

### Interview Planner

Input:

- professional profile
- competency scores
- previous interviews
- current knowledge

Output:

interview strategy.

### Interviewer

Handles the live conversation.

### Answer Evaluator

Evaluates completed answers.

### Session Evaluator

Produces final interview feedback.

### Progress Engine

Updates long-term competency state.

### Research Agent

Finds current interview and technical information.

### Hands-On Exercise Generator

Creates realistic practical exercises based on:

- market patterns
- user profile
- seniority
- current weaknesses
- preferences

### Hands-On Interviewer

Observes the candidate during the session, asks questions and introduces realistic follow-up constraints without solving the task.

### Code / Solution Evaluator

Evaluates:

- correctness
- architecture
- code quality
- testing
- accessibility
- performance
- reasoning
- communication

---

## 43. Structured AI Output

AI responses used by application logic must return structured JSON rather than free-form text whenever possible.

Example question:

```json
{
  "competency": "frontend_system_design",
  "difficulty": "senior",
  "question": "Design...",
  "intent": "Evaluate architecture decomposition and trade-off reasoning"
}
```

Evaluation:

```json
{
  "score": 7.4,
  "strengths": [],
  "weaknesses": [],
  "missing_points": [],
  "communication_score": 8,
  "technical_score": 7,
  "follow_up_needed": true
}
```

Hands-on exercise:

```json
{
  "title": "Searchable Product Dashboard",
  "session_type": "machine_coding",
  "duration_minutes": 60,
  "stack": ["React", "TypeScript"],
  "difficulty": "senior",
  "requirements": [],
  "hidden_evaluation_criteria": [],
  "dynamic_followups": [],
  "competencies": []
}
```

All AI output must be schema validated.

---

## 44. Suggested Data Model

### app_owner

```text
id
created_at
```

For the POC, this contains one fixed implicit owner such as:

```text
local-personal-user
```

There is no login or user-selection interface. All user-owned records reference this owner through `user_id`. A future authentication migration may map or replace this identifier with an authenticated account ID.

### professional_profiles

```text
id
user_id
current_role
target_role
seniority
location
summary
career_narrative
created_at
updated_at
```

### source_documents

```text
id
user_id
type
file_url
raw_text
created_at
```

Types:

```text
cv
cover_letter
```

### experiences

```text
id
profile_id
company
role
start_date
end_date
description
achievements
technologies
```

### competencies

```text
id
slug
name
parent_id
```

### user_competencies

```text
id
user_id
competency_id
relevance
expected_level
estimated_level
score
confidence
last_practiced_at
```

### interview_sessions

```text
id
user_id
started_at
completed_at
overall_score
summary
session_type
```

### interview_messages

```text
id
session_id
role
content
created_at
```

Roles:

```text
interviewer
user
```

### question_evaluations

```text
id
session_id
competency_id
question
answer
score
feedback
created_at
```

### career_stories

```text
id
user_id
title
description
competencies
source_experience
```

### research_items

```text
id
topic
source_url
source_type
published_at
retrieved_at
summary
confidence
market
interview_round
```

### hands_on_sessions

```text
id
user_id
session_type
title
duration_minutes
stack
difficulty
started_at
completed_at
completion_state
overall_score
market_pattern_id
```

### hands_on_artifacts

```text
id
session_id
artifact_type
content
snapshot_at
```

Possible artifact types:

```text
code
design_notes
architecture_notes
test_output
console_output
```

### hands_on_evaluations

```text
id
session_id
technical_score
problem_solving_score
architecture_score
communication_score
testing_score
accessibility_score
performance_score
time_management_score
hiring_signal
feedback
```

---

## 45. Access and Future Authentication

The personal POC does not include application authentication.

Required POC behavior:

- open directly into onboarding when no profile exists
- open directly into Home when a profile exists
- use one implicit local owner for all persisted records
- keep AI credentials on the server side
- do not expose the application publicly without external access protection

Future public or multi-user versions should add authentication without changing the core profile, interview or progress engines. Email magic link or Google login may be considered at that stage.

---

## 46. Privacy

CVs and cover letters contain sensitive professional information.

Minimum requirements:

- store documents locally or in access-protected private storage
- never expose uploaded documents through public URLs
- keep model-provider credentials and server configuration out of browser code
- do not send documents to services other than the configured AI provider
- ability to delete uploaded documents
- ability to reset all local profile and interview data

The UI should state clearly that documents are used to personalize interview preparation.

Before any public or multi-user release, add authentication, per-user authorization, tenant isolation and production-grade data-retention controls.

---

## 47. Error Handling

Important failures should have graceful fallbacks.

### CV parsing fails

Allow paste-text fallback.

### Voice transcription fails

Keep recording and allow retry.

### AI request fails

Preserve user's answer and retry.

### Research unavailable

Continue interview using existing knowledge.

### Hands-on runtime fails

Preserve code and allow the user to continue editing or retry execution.

Fresh web data should improve the product but should never make the core interview unavailable.

---

## 48. POC Screens

Required:

```text
1. Welcome
2. CV upload
3. Optional cover-letter upload
4. Profile confirmation
5. Home
6. Interview
7. Interview results
8. Progress
9. Practice
10. Hands-On Interview setup
11. Hands-On Interview workspace
12. Hands-On Interview results
13. Profile
```

---

## 49. POC Priority

### P0 — Must work

- mobile-first responsive app
- single-user local persistence
- direct entry into onboarding or Home without login
- CV upload/paste
- CV parsing
- generated professional profile
- optional cover letter
- professional competency scope
- Start Interview
- conversational interview
- follow-up questions
- text answers
- voice answers
- post-interview evaluation
- competency scoring
- persistent interview history
- adaptive subsequent interview
- progress screen
- hands-on interview mode
- at least one realistic React/TypeScript practical session
- editable coding environment or embedded sandbox
- AI interviewer aware of the exercise and user progress
- hands-on final evaluation
- hands-on results affecting progress profile

### P1 — Strongly desirable

- current web research
- market-grounded exercise generation
- story bank
- dedicated self-introduction coaching
- recommended practice
- focused practice categories
- improved transcript editing
- competency explanations
- dynamic hands-on requirements
- code snapshots
- interviewer hints
- “Why this exercise?” market explanation

### P2 — After POC

- application authentication
- multi-user data isolation
- public deployment
- specific job overlay
- company preparation
- more coding languages/frameworks
- richer cloud IDE
- scheduled practice reminders
- spaced repetition engine
- interview calendar
- peer interviews
- recruiter mode
- multiple CV versions
- multiple target careers
- native mobile application

---

## 50. POC Test Scenario — Conversational Interview

### Step 1

New user opens app.

### Step 2

Uploads Senior Frontend Engineer CV.

### Step 3

Optionally uploads cover letter.

### Step 4

App produces professional profile.

### Step 5

User confirms it.

### Step 6

Home displays:

```text
Interview readiness
Not enough data yet

Start your first interview
```

### Step 7

User starts interview.

AI asks:

> Tell me about yourself.

### Step 8

User answers by voice.

### Step 9

AI asks a follow-up connected to their actual experience.

### Step 10

Interview moves through technical, architectural and behavioral questions.

### Step 11

Session ends.

### Step 12

App displays personalized analysis.

### Step 13

Progress profile is updated.

### Step 14

User starts another interview.

### Step 15

The new interview demonstrably uses information learned from the first session.

If Steps 1–15 work convincingly, the conversational POC proves the core loop.

---

## 51. POC Test Scenario — Hands-On Interview

### Step 1

User opens:

> Hands-On Interview

### Step 2

The system chooses a realistic 60-minute React + TypeScript exercise based on:

- user profile
- seniority
- current market patterns
- current competency profile

### Step 3

User receives the exercise and clarifies requirements.

### Step 4

User begins coding.

### Step 5

AI interviewer asks context-aware questions while the user works.

### Step 6

The system introduces an additional requirement or edge case.

### Step 7

The user completes or partially completes the exercise.

### Step 8

The system evaluates:

- implementation
- problem solving
- architecture
- communication
- testing
- accessibility
- performance
- seniority signal

### Step 9

The app gives an interviewer-style hiring signal and detailed feedback.

### Step 10

Results update the user's competency profile.

### Step 11

A future hands-on session adapts based on the result.

If this loop works, the hands-on POC proves the second major product pillar.

---

## 52. Product Quality Bar

The POC does **not** need:

- perfect visual design
- dozens of settings
- hundreds of manually curated questions
- complex gamification
- native applications
- advanced analytics
- a custom-built cloud IDE from scratch

It **does** need the AI experience to feel intelligent.

The most damaging POC outcome would be:

> The interface looks polished but the interview feels like ChatGPT randomly asking frontend questions.

The most desirable outcome is:

> The interface is simple, but after ten minutes the user feels that the interviewer genuinely understands their career and knows what to challenge them on.

For hands-on sessions, the desired reaction is:

> This feels like the kind of practical task I could actually receive in a real interview.

---

## 53. Core Differentiator

The defensible product loop is:

```text
Know the user's career
        ↓
Know their interview scope
        ↓
Know what the market currently tests
        ↓
Interview them
        ↓
Make them perform the job
        ↓
Observe performance
        ↓
Remember
        ↓
Adapt
        ↓
Interview better next time
```

This accumulated understanding should become increasingly valuable over time.

A generic AI chat starts from zero.

This product should not.

---

## 54. Product Pillars

The product can be summarized in three pillars:

### 1. Know Me

Understand the user's real professional identity, experience, strengths, narrative, goals and market.

### 2. Interview Me

Run realistic conversational interviews that adapt to the user's level and history.

### 3. Make Me Do the Job

Run true-to-reality hands-on technical interviews that require the user to think, design, debug and code under realistic conditions.

---

## 55. North Star Experience

The product has succeeded when a user can open it after several weeks and press:

> **Start Interview**

or:

> **Start Hands-On Interview**

without configuring anything, and the resulting session feels exactly like the kind of interview that person should currently be preparing for.

The user should think:

> **This app knows what I should know, knows what I've done, knows what the market is asking, knows where I'm weak, and knows how to make me better at explaining and demonstrating it.**

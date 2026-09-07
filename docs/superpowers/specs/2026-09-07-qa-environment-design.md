# QA Environment and Release Safety Design

**Date:** 2026-09-07  
**Status:** Approved design  
**Scope:** Career Platform monorepo (`apps/job-hunter`, `apps/relay`, and `supabase`)

## Context

The platform is moving from a single-user bot to an invite-only, multi-user alpha. Today, application changes and hosted database changes are too close to production: the Vercel Preview environment can access production Supabase and Telegram credentials, and there is no durable environment in which a complete release candidate can be tested before production.

The repository already has a useful foundation:

- CI starts a disposable local Supabase stack for both Relay and Job Hunter tests.
- The Supabase project contains versioned migrations, local configuration, and a small deterministic seed.
- Vercel creates automatic previews for Git changes.
- The local Supabase stack supports local auth and email capture.

The missing layer is a shared, production-like QA environment with safe credentials, deterministic test data, and an explicit promotion path from a tested commit to production.

## Goals

1. No preview, QA, or local process can read or mutate production data or use production integration credentials.
2. Every production release is the exact Git commit and migration set that passed CI and full QA.
3. A developer can test most changes locally or in an automatic PR preview without using real external services.
4. A selected release candidate can be exercised end to end with a real QA Telegram bot and explicitly enabled provider smoke tests.
5. Test users and system states are deterministic, composable, and useful across many countries and occupations without requiring a fixture for every possible job title.
6. QA can start at zero additional platform cost within free-tier limits and evolve later without redesigning the workflow.

## Non-goals

- Copying, anonymizing, or otherwise importing production user data into QA.
- Giving every pull request its own hosted Supabase database in the first version.
- Making every automatic Vercel preview a real Telegram bot deployment.
- Building the Relay product dashboard or the invite/onboarding feature described in issues #78 and #98 as part of this infrastructure work.
- Testing every occupation, country, language, and integration permutation before each release.
- Using a Codex skill as the source of truth for fixture or release behavior.

## Core Decisions

### Four distinct environments

| Environment | Application | Database | Data | Integrations | Purpose |
| --- | --- | --- | --- | --- | --- |
| Local | Local Relay and Job Hunter | Disposable Docker Supabase | Synthetic scenarios | Fake by default | Fast development and debugging |
| CI / PR | CI plus automatic Vercel Preview | Fresh local Supabase in CI; deterministic fake persistence in Vercel Preview | Synthetic scenarios | Fake only | Automated verification and deployed UI review |
| Shared QA | Dedicated QA Vercel projects | Dedicated hosted QA Supabase project | Synthetic scenarios only | Fake by default; explicit real QA smoke tests | Full release-candidate testing |
| Production | Production Vercel projects | Production Supabase project | Real users | Production credentials | Live service |

The words **Preview**, **QA**, and **Production** describe different trust levels. A deployment's Vercel target name does not determine its logical environment. For example, the stable deployment inside a dedicated QA Vercel project may technically use Vercel's production target, but it is still the Career Platform QA environment and must only receive QA credentials.

Every process receives a non-secret environment identity such as `APP_ENV=local|ci|preview|qa|production`. Destructive QA tooling refuses to run unless both the identity and the configured Supabase project reference match an explicit QA allowlist. Unknown or production targets fail closed.

### QA is one replaceable release-candidate slot

Only one Git commit owns the shared QA environment at a time. Ordinary PRs receive CI and an automatic Vercel Preview, but they do not rebuild shared QA.

When an operator explicitly selects a PR commit as the QA candidate, automation:

1. Verifies that the target is the allowlisted QA environment.
2. Clears and rebuilds the QA database from the repository's migrations.
3. Loads the selected deterministic scenario pack.
4. Builds and deploys the same commit to the dedicated Relay and Job Hunter QA projects.
5. Moves the stable QA application URL to those deployments.
6. Runs automated QA acceptance checks.
7. Publishes a candidate manifest and test report.

QA is disposable. Manual changes made while testing can disappear at the next candidate promotion. Any test state worth preserving must be converted into a deterministic scenario fixture.

### Schema changes have one source of truth

All schema changes remain versioned under `supabase/migrations`. A migration is never manually reproduced in a second dashboard as part of the normal release path.

The same ordered migration files are applied to:

1. a fresh local database during development and CI,
2. the shared QA database when a commit becomes a candidate, and
3. production when the approved commit is released.

Provider settings that cannot be represented as SQL are stored as reviewed repository configuration or documented environment configuration and applied by automation where the provider supports it. A release checklist detects settings that still require a manual provider action.

Schema changes should follow expand-and-contract compatibility when an application and database change cannot be switched atomically. Destructive migrations require a separate, reviewed cleanup release after all running code no longer depends on the old shape.

## Deployment and Promotion Flow

```text
developer change
      |
      v
local app + local Supabase + fake integrations
      |
      v
pull request
      |
      +--> CI: fresh Supabase, migrations, seeds, tests, lint, build
      |
      +--> automatic Vercel Preview: UI/build review, fake integrations only
      |
      v
explicitly select exact commit for QA
      |
      v
rebuild shared QA + deploy both QA apps + run acceptance suite
      |
      v
manual approval of exact commit
      |
      v
merge approved commit to main
      |
      v
controlled production release: migrations first, then environment-specific builds
```

### Automatic PR Preview

Each PR keeps the existing fast preview experience, with these restrictions:

- It has no production Supabase, Telegram, Gemini, Gmail, or source-provider credential.
- It does not connect to the shared QA database, because that database belongs to the currently selected QA candidate and may have a different schema.
- Screens and request flows that need data use deterministic fake persistence or an in-process fixture snapshot. This state is disposable and is not a substitute for database verification in CI or full QA.
- It has no real QA Telegram bot token.
- Server-side flows use fake integrations and synthetic inputs.
- Telegram webhook behavior is tested by posting signed or otherwise validated synthetic Telegram updates to the preview endpoint and inspecting a fake outbound-message store.
- The preview is useful for UI, routing, build, and request-flow review; it is not the complete real-bot test.

### Selected QA Candidate

The operator initiates promotion with a GitHub workflow dispatch, a tightly controlled label/action, or a future Codex wrapper. The input identifies an immutable commit SHA and a scenario pack.

The candidate manifest records at least:

- commit SHA,
- migration fingerprint or ordered migration list,
- scenario pack and fixture version,
- Relay and Job Hunter deployment identifiers and URLs,
- QA Supabase project reference,
- automated check results,
- real smoke checks performed,
- operator decision and timestamp.

A new commit on the PR invalidates the previous approval. It must pass CI and be promoted as a new candidate.

### Production Release

Merging is blocked until the exact commit has a successful QA candidate status and manual approval. After merge, a controlled production workflow applies the already-tested migrations and then builds/deploys Relay and Job Hunter with production-only environment variables.

The production build is deliberately separate from the QA build because public application variables such as `NEXT_PUBLIC_*` are embedded at build time. The guarantee is therefore **same source commit and migration set**, not reuse of the same binary artifact.

Automatic production deployment must not race the database migration. The production workflow owns release ordering; Vercel Git behavior is configured so merging does not independently publish an application before the workflow's migration gate succeeds.

If a production migration fails, application deployment stops. Production rollback uses a previously known-good application deployment only when that application remains compatible with the current database. Database recovery uses forward fixes or an explicitly reviewed recovery procedure rather than assuming every migration can be reversed safely.

## Deterministic Test Data

### Composition model

A test user is assembled from independent dimensions:

```text
lifecycle state
  + persona
  + job corpus
  + history
  + integration state
  + optional fault
  = runnable scenario
```

This avoids creating a named fixture for every occupation in every country. The lifecycle validates platform behavior; the persona and corpus provide representative domain variation.

#### Lifecycle states

Initial states include:

- `fresh-invite`
- `invite-expired`
- `onboarding-incomplete`
- `ready`
- `suspended`
- `provider-failed`
- `run-failed`

#### Personas

Personas vary independently by attributes such as:

- occupation or job family,
- seniority,
- country and timezone,
- language,
- currency,
- search preferences and constraints.

The initial persona library should be small and deliberately diverse. New personas are added when they represent a meaningful product boundary or reproduce a defect, not merely because another job title exists.

#### Job corpora

Reusable corpora cover behavior such as:

- strongly relevant and clearly irrelevant jobs,
- expired jobs,
- duplicates from different sources,
- multilingual content,
- missing salary or optional fields,
- malformed provider data.

#### Histories and faults

Histories include prior runs, delivered jobs, user decisions, applications, and recorded failures. Optional faults simulate provider rate limits, timeouts, invalid credentials, malformed responses, and Telegram send failures.

### Determinism and identity

Fixtures use stable IDs, timestamps, and content so test failures can be reproduced. Randomized or generative data can be used for exploratory testing only when the seed and generated output are captured.

The fixture engine supports:

- rebuilding the selected scenario pack,
- adding a composed scenario without rebuilding the database,
- resetting one synthetic user and only its owned data,
- listing active scenarios and their access details,
- saving a useful composition as a named fixture.

Hosted QA users are created by trusted automation through the Supabase admin interface. The automation may generate short-lived, single-use sign-in links for an operator; passwords and links are never committed. Application-owned records should be created through the highest practical domain boundary so fixtures exercise real validation. Where authorization itself is under test, setup obtains a synthetic user's session and performs operations through the same authenticated boundary used by the application.

Production data is never copied into QA, even after anonymization. Small sanitized response fixtures may be recorded for public-source adapter contracts, but live provider checks remain a separate test category.

## Integration Strategy

### Fake by default

Local, CI, PR Preview, and normal QA acceptance use controlled substitutes for external integrations:

- Gemini returns deterministic success, error, timeout, and rate-limit responses.
- Search/job providers return versioned response corpora.
- Gmail delivery is captured locally or in a QA outbox.
- Telegram inbound updates are synthetic, and outbound messages are written to a fake outbox that tests can inspect.

The fake integration boundary must sit behind the same application interfaces as the real provider. Tests should verify domain behavior, stored state, and outbound intent without making network calls.

### Explicit real smoke tests

The selected QA candidate can run narrowly scoped live checks with dedicated QA resources:

- a separate Telegram bot,
- a dedicated, capped Gemini key,
- a dedicated test mailbox,
- optional live contract checks for public job sources.

These checks are opt-in and auditable. A provider failure is reported separately from a product assertion failure. Logs include environment, commit, scenario, and synthetic user identifiers while redacting tokens, credentials, resumes, and other sensitive content.

## Telegram Testing

Telegram's webhook model allows a bot to have one active webhook URL, so the same real bot cannot be connected to every ephemeral PR preview.

### PR-level testing

Every preview can still test the webhook flow by:

1. sending a realistic synthetic Telegram update to that preview's webhook endpoint,
2. passing through the real request parsing, authorization/validation, application logic, and persistence boundaries,
3. replacing only the final Telegram network client with a fake sender,
4. asserting the captured outbound messages and callbacks.

This provides per-preview coverage without sharing a real bot token or fighting over the bot's active webhook.

### Full QA testing

The QA Telegram bot points to one stable QA webhook URL, for example the Job Hunter QA project's stable domain plus `/telegram/webhook`. Promoting a candidate moves the stable QA deployment to that commit; the bot's webhook URL does not change.

One real tester account is sufficient for routine end-to-end verification of pairing, commands, messages, and callbacks. Automated scenarios use synthetic Telegram identities for multi-user isolation. A true two-live-user Telegram isolation check requires two real tester accounts and is reserved for milestone releases or changes to identity boundaries.

## Operator Experience

The initial operator interface is a small set of repository-owned commands and GitHub workflows. It should support actions equivalent to:

- publish PR or commit to QA with a named scenario pack,
- add an onboarding-incomplete Hebrew office-worker scenario,
- reset a provider-failed scenario,
- show the commit and scenarios currently in QA,
- run the real Telegram smoke test,
- show why a candidate is not eligible for production.

Each action returns a human-readable result containing the QA URLs, exact commit, installed scenarios, access instructions, completed checks, pending manual checks, and any blocker.

A Codex QA skill is added only after these commands are stable. The skill translates conversational requests into the same versioned commands and reports their structured output. It must not contain unique database mutation logic, credentials, or an alternative definition of scenarios.

## Verification Matrix

| Layer | Required verification |
| --- | --- |
| Local | Feature behavior, scenario composition/reset, fake integrations, migration rebuild |
| CI | Unit and integration tests, RLS/database tests, fresh migration run, lint, build, browser tests where applicable |
| Automatic PR Preview | Deployed startup, key browser journeys, synthetic Telegram webhook, fake outbox, no production credentials |
| Selected QA Candidate | Full invite/onboarding journey, real QA Telegram pairing and delivery, callbacks, explicitly selected provider smoke tests, exact-commit manifest |
| Production | Approved commit, tested migration set, production-only variables, non-destructive health and authentication checks, no synthetic seed |

### Release eligibility

A commit is eligible to merge and release only when:

1. all required CI checks are green for that exact SHA,
2. the QA database rebuild, migrations, and selected seed pack succeeded,
3. automated QA acceptance checks are green,
4. required real integration smoke checks are recorded,
5. the operator approved that exact SHA, and
6. no newer commit has replaced it.

Failure to rebuild or seed QA marks the candidate invalid. It never leaves a partially prepared candidate labeled ready.

## Security and Safety Controls

- Production credentials exist only in production-scoped secret stores.
- QA uses dedicated credentials with the minimum practical permissions and spending caps.
- Ordinary preview deployments receive fake or non-sensitive credentials only.
- Browser-visible environment variables never contain service-role or provider secrets.
- Trusted fixture and reset jobs run server-side in controlled CI/operator contexts.
- Destructive commands require an explicit QA environment identity and project-reference allowlist.
- Production project references and URLs are maintained in a denylist as a second guard.
- The automation prints the selected logical environment and project reference before mutation, but never secret values.
- Synthetic fixtures carry an unmistakable marker so accidental appearance outside QA is detectable.
- Production seeding is disabled; a release fails if a synthetic scenario pack is supplied to production.

## Cost and Evolution

The first version uses:

- the existing local Docker stack,
- one additional hosted Supabase project for shared QA,
- dedicated Vercel QA projects,
- one separately created Telegram test bot,
- fake integrations for normal test traffic.

This can begin at no additional subscription cost while the account remains within provider free-tier project and usage limits. It may still consume free-tier quotas, and inactivity pausing or account-level limits must be monitored.

The design leaves a direct upgrade path:

1. keep the deterministic scenario engine and candidate manifest unchanged,
2. replace the single shared QA database target with on-demand Supabase preview branches when parallel, per-PR full-stack environments become valuable,
3. add paid capacity only when concurrency, uptime, or quota pressure justifies it.

No early implementation choice should assume that the shared QA project is permanent.

## Delivery Plan

This design should be delivered through separate, reviewable issues and pull requests.

### Phase 1: Isolate preview and production

- Create a dedicated hosted QA Supabase project.
- Create dedicated Relay and Job Hunter QA Vercel projects with stable QA URLs.
- Create a separate QA Telegram bot.
- Remove production Supabase and integration credentials from ordinary Preview scope.
- Add hard logical-environment and project-identity guards.
- Document environment ownership and secret placement.

This phase is the immediate production-safety priority.

### Phase 2: Build the deterministic fixture engine

- Define versioned lifecycle states, personas, corpora, histories, and faults.
- Implement full-pack rebuild, additive scenario creation, per-user reset, and scenario listing.
- Provision hosted QA auth users securely and return temporary access instructions.
- Guarantee that no fixture command can target production.

### Phase 3: Add fake integrations and outboxes

- Introduce stable provider boundaries where missing.
- Implement fake Gemini, source, Gmail, and Telegram behavior.
- Add synthetic inbound Telegram updates and inspectable outbound messages.
- Cover success and failure paths in automated tests.

### Phase 4: Automate QA candidate promotion

- Add the explicit candidate-selection workflow.
- Rebuild QA, seed selected scenarios, deploy both apps, and run acceptance tests.
- Publish and persist the candidate manifest.
- Configure the stable QA Telegram webhook and live smoke-test command.
- Make exact-SHA approval a required merge/release gate.
- Add the controlled production release workflow and prevent deployment/migration races.

### Phase 5: Prove the multi-user alpha journey

- Encode the invite-to-running-Job-Hunter acceptance flow from issues #98 and #78.
- Test interrupted onboarding, settings changes, provider failures, and recovery.
- Add milestone-level real multi-user isolation checks.

### Phase 6: Add the Codex QA skill

- Wrap the stable repository commands in a conversational skill.
- Provide safe scenario composition and clear candidate-status reporting.
- Keep all execution logic and safety enforcement in repository code.

## Success Criteria

The QA architecture is working when all of the following are true:

- Inspecting any ordinary PR preview proves it has no route to production data or production provider credentials.
- A fresh clone can run the deterministic core scenarios locally.
- An operator can select an exact commit, rebuild shared QA, and receive a usable test report without manual database editing.
- The same lifecycle scenario can be combined with different personas and corpora without duplicating its setup logic.
- The real QA Telegram bot always reaches the currently selected QA candidate and never an arbitrary PR preview.
- A newer commit automatically invalidates an older QA approval.
- Production cannot deploy before its tested migrations have succeeded.
- Production never receives synthetic fixtures.
- The release record answers: what commit is running, what schema and scenarios were tested, which real checks ran, and who approved it.

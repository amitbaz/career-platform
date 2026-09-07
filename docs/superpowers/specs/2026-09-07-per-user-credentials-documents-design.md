# Per-user credentials and documents

Design for [#72](https://github.com/amitbaz/career-platform/issues/72), priority 5 of 12
under epic [#34](https://github.com/amitbaz/career-platform/issues/34). It builds on the
per-user Postgres store from #70 and the database-backed search profile from #71.

## Problem

Job Hunter still reads four user-owned values from environment variables supplied by GitHub
repository secrets:

- `CANDIDATE_PROFILE_B64`
- `COVER_LETTER_TEMPLATE_B64`
- `GEMINI_API_KEY`
- `BRAVE_SEARCH_API_KEY`

That arrangement is single-user by construction. A repository secret has one value for the
whole workflow, cannot be selected safely by user, and cannot be managed through Relay.

The document half of the target model already exists. Relay stores authenticated users' CV and
cover-letter text in `public.source_documents`, whose row-level security policies restrict each
row to its owner. Job Hunter does not use those rows yet and instead decodes duplicate copies
from environment variables.

Provider keys need a stronger boundary than ordinary owner-readable rows. A signed-in user must
be able to submit, replace, delete, and inspect the status of a key, but neither Relay's browser
code nor an ordinary Supabase session may retrieve the stored value. Only a trusted Job Hunter
run acting for that same user may decrypt it.

## Goal

Make the database the source of truth for each user's Job Hunter documents and Gemini/Brave
credentials, remove the four values from GitHub workflow configuration, and prove that:

1. a run can read only the documents and keys belonging to the user it represents;
2. an ordinary browser session can manage keys without reading them back; and
3. no plaintext key appears in application responses, database metadata, logs, or errors.

## Scope

### In scope

- Use the existing `source_documents` rows for CV and cover-letter text.
- Store Gemini and Brave keys per user in Supabase Vault.
- Add a minimal credential-management section to Relay's existing Profile view.
- Load documents and provider keys from Supabase for daily runs and on-demand cover-letter
  generation, and load the same per-user Gemini key for Gmail sync.
- Add a trusted-run claim to Job Hunter's short-lived per-user JWT.
- Remove the four legacy variables from Job Hunter runtime loading, workflows, examples, and
  setup documentation.
- Delete the corresponding GitHub repository secrets after the replacement path is verified.

### Out of scope

| Out of scope | Owner or reason |
| --- | --- |
| Gmail OAuth credentials | Gmail is not an AI or search provider; its multi-user flow is separate. |
| Telegram bot and chat credentials | Shared-bot identity and routing belong to #77. |
| GitHub dispatch token and Supabase signing material | Platform infrastructure, not user provider credentials. |
| Additional AI providers or a provider abstraction | #73 owns the provider port and quota ledger. |
| Multi-user scheduling and dispatch | #76; this change keeps the current `JOB_HUNTER_USER_ID`. |
| Full onboarding and provider walkthroughs | #78; this change supplies only the usable management controls. |
| Persisting original uploaded files | Relay currently extracts and stores text; uploads remain transient. |
| Encrypting user documents with Vault | Documents remain owner-readable and protected by RLS; the browser prohibition applies to keys. |

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Document source of truth | Existing `public.source_documents` rows | The table, ownership model, profile RPC, and RLS coverage already exist. |
| Secret store | Supabase Vault | Encryption and decryption stay inside the database; no new application master key is distributed to Actions and Vercel. |
| Credential metadata | Private registry keyed by user and provider | It links stable application ownership to opaque Vault IDs without exposing Vault or plaintext. |
| Browser contract | Write, replace, delete, and status only | A provider key is never returned after submission. |
| Trusted-reader identity | `job_hunter_runner: true` in a signed, short-lived user JWT | The existing signing key can attest that the caller is a runner; browser-issued user sessions cannot forge the claim. |
| Brave behavior | Optional | The existing unauthenticated DuckDuckGo fallback remains available. |
| Gemini behavior | Required | Both evaluation and cover-letter generation need the supported AI provider. |
| Legacy environment fallback | None | A fallback would allow stale repository secrets to remain silently authoritative. |
| User interface location | Existing Profile view | Documents are already managed there; a separate settings product area is unnecessary for this slice. |

## Security model

### Threat boundary

The following callers are distinct even though both represent an authenticated user:

- **Ordinary user session:** issued by Supabase Auth to Relay's browser. It may manage the
  signed-in user's credential records but may never receive decrypted values.
- **Trusted Job Hunter run:** a token minted from the platform-held ES256 signing key for one
  `sub`, with `role: authenticated` and `job_hunter_runner: true`. It may retrieve decrypted
  provider keys only for that same `sub`.
- **Anonymous caller:** receives no credential metadata and may perform no credential operation.

Possession of `SUPABASE_SIGNING_KEY_B64` already permits minting a token for any user. Adding the
runner claim does not expand that key's existing blast radius; it gives the database a way to
distinguish the trusted batch process from browser sessions. The signing key remains platform
infrastructure and must never enter Relay's browser bundle.

### Private credential registry

Create `private.user_provider_credentials` with:

- `user_id uuid not null references auth.users(id) on delete cascade`;
- `provider text not null` constrained to `gemini` or `brave`;
- `vault_secret_id uuid not null unique`;
- `created_at timestamptz not null` and `updated_at timestamptz not null`; and
- primary key `(user_id, provider)`.

The table lives outside the exposed `public` schema and receives no direct `anon` or
`authenticated` grants. It stores no plaintext and no application-managed ciphertext. Vault
secret names and descriptions must not contain email addresses, document text, key fragments,
or other personal values.

### Public RPC boundary

PostgREST-accessible functions provide the only credential operations:

- **Set or replace:** validate `auth.uid()`, the provider allowlist, a non-blank value, and a
  conservative maximum length; create or update the Vault secret and registry row in one
  transaction; return metadata only.
- **Delete:** validate `auth.uid()`, remove that user's Vault secret and registry row in one
  transaction, and behave idempotently when the provider is absent.
- **Status:** return provider, configured state, and `updated_at` for the calling user. It must
  never join or query Vault's decrypted view.
- **Runner retrieval:** require a non-null `auth.uid()` and boolean `job_hunter_runner` claim,
  filter the registry by that user, and only then read the matching Vault values.

These functions require elevated database privileges to reach the private registry and Vault.
Each therefore uses a fixed empty `search_path`, schema-qualifies every object, explicitly checks
the caller's identity, and is tested as a security boundary. Default `PUBLIC` execution is
revoked. Management functions are granted only to `authenticated`; the retrieval function is
also granted to `authenticated`, but rejects tokens without the signed runner claim.

This is a narrow exception to Job Hunter's existing security-invoker rule. Existing store
functions remain unchanged and continue to rely on RLS. The exception is limited to operations
that cannot work through normal RLS because ordinary authenticated users must not read their own
key values.

### Secret-handling rules

- Credential inputs use password controls and are always blank on render and after submission.
- API responses and client caches contain status metadata only.
- Validation and database errors name the provider or missing field, never the submitted value.
- Application logs must not include request bodies, Vault results, access tokens, or credential
  objects.
- Runtime secrets exist only in process memory for the duration of the run.
- Provider key formats are not deeply validated because providers may change them; the system
  only rejects blank or unreasonably large values.

## Components

### Supabase migration

One platform migration enables or verifies Vault, creates the private registry and constraints,
and defines the four RPC operations with explicit grants. The project uses imperative migrations,
so the migration is created through the Supabase CLI and verified against the local stack before
its SQL is committed.

The migration must account for Supabase's Data API exposure settings explicitly rather than
assuming a new public function is automatically reachable. No table is newly exposed.

### Relay credential repository and route

A server-only repository wraps the credential RPCs and exposes typed operations for status,
save/replace, and delete. A dedicated authenticated route under the Profile API uses
`requireUser()` before calling it.

The route contract is separate from `GET /api/profile`, so existing profile reads remain stable
and no future refactor can accidentally serialize credential data with documents. The route
accepts only `gemini` and `brave`; unknown providers return a client error. Server failures return
sanitized messages consistent with the existing Gemini-error boundary.

### Relay Profile UI

The existing Profile view gains a compact Credentials section:

- Gemini is labelled required for Job Hunter runs.
- Brave is labelled optional with DuckDuckGo fallback.
- Each provider shows only `Configured`, `Not configured`, or its last update time.
- Save replaces the existing value without revealing it.
- Delete requires an explicit user action.
- Inputs clear after every request and are never populated from fetched data.

This is management UI, not the onboarding flow owned by #78. It does not add provider education,
quota configuration, model selection, or additional navigation.

### Job Hunter runtime configuration

Job Hunter separates platform bootstrap settings from per-user runtime material. Environment
loading continues to provide the user ID, Supabase URL, publishable key, signing key, Telegram,
Gmail, and GitHub infrastructure values. After constructing the authenticated Postgres client,
focused loaders obtain:

- the user's latest `cv` document text;
- the user's latest `cover_letter` document text;
- the required Gemini key; and
- the optional Brave key.

The main-settings loader combines documents and provider credentials for daily execution and
on-demand cover-letter generation. Gmail sync loads the same Gemini credential without requiring
CV or cover-letter data, since email classification does not consume those documents. All three
entry points share one provider-credential loader so their key source and failure behavior cannot
drift. Gmail client ID, client secret, and refresh token remain environment-backed and outside
this issue.

`AccessTokenMinter` adds the signed `job_hunter_runner: true` claim to tokens used by Job Hunter.
All ordinary store requests continue under `role: authenticated` and existing RLS; only the
credential retrieval RPC attaches additional meaning to the runner claim.

If Gemini, CV, or cover-letter text is missing, configuration fails before an external provider
call or delivery. The error lists missing field names only. If Brave is absent, the run follows
the existing DuckDuckGo fallback without treating it as an error.

### GitHub workflows and documentation

Both the daily and on-demand cover-letter workflows stop injecting the four legacy variables.
Workflow tests enforce their absence. Job Hunter's environment examples, README, and app guidance
are updated to describe Relay/Supabase as the source of these values.

The workflows retain `JOB_HUNTER_USER_ID` until #76 introduces per-user orchestration. Gmail,
Telegram, GitHub dispatch, Supabase URL/publishable key, and signing-key settings are unchanged.

## Data flows

### Manage a credential

1. Relay loads credential status for the signed-in user.
2. The user submits a new Gemini or Brave key over HTTPS.
3. The authenticated server route calls the set RPC with the user's normal access token.
4. The RPC derives ownership from `auth.uid()`, writes Vault and registry state transactionally,
   and returns status metadata.
5. Relay clears the input and renders the returned status. No read-back request exists.

### Start a Job Hunter run

1. The workflow provides the existing platform bootstrap values and one `JOB_HUNTER_USER_ID`.
2. Job Hunter mints a short-lived JWT for that user with `job_hunter_runner: true`.
3. Normal PostgREST reads load that user's search profile and `source_documents` through RLS.
4. The runner-only RPC validates the signed claim and same-user ownership before decrypting the
   user's Gemini and optional Brave values from Vault.
5. The in-memory values are passed only to the consumers required by the selected command. Gmail
   sync receives Gemini but does not require or receive the user's documents.

At no point does the workflow choose among repository secrets by user. Multi-user selection and
scheduling remain the responsibility of #76.

## Failure behavior

- Vault and registry writes are one transaction; partial mappings and orphaned secrets must not
  survive a failed operation.
- Replacing a credential preserves one registry row and one Vault secret per user/provider.
- Deletion is idempotent from the caller's perspective.
- Missing or forged runner claims fail closed with no decrypted data.
- Cross-user identifiers supplied in request bodies are ignored because ownership always comes
  from the signed token.
- A database or Vault outage surfaces as a sanitized temporary failure and does not fall back to
  environment secrets.
- A missing optional Brave key is the only absence that does not stop the run.

## Migration and rollout

GitHub does not expose repository-secret values after creation, so there is no automatic import.
The owner performs a one-time transition:

1. Apply and verify the migration.
2. Deploy the Relay management route and UI.
3. Save Gemini and optional Brave keys through Relay.
4. Save or update the CV and cover-letter text through the existing Relay Profile flow, then
   confirm both rows are present and current.
5. Run a live, user-scoped retrieval smoke test and a non-delivering Job Hunter configuration
   check.
6. Deploy the workflow and runtime changes.
7. Delete `GEMINI_API_KEY`, `BRAVE_SEARCH_API_KEY`, `CANDIDATE_PROFILE_B64`, and
   `COVER_LETTER_TEMPLATE_B64` from GitHub repository secrets.
8. Verify the four names are absent with `gh secret list` and run the updated workflow.

The code does not retain a legacy fallback. Rollback means restoring the previous deployment and,
if necessary, recreating the old repository secrets manually; their values cannot be recovered
from GitHub or read back through Relay.

## Testing and verification

### Database tests

- The private registry has the intended primary key, provider constraint, foreign key, and no
  direct public grants.
- Anonymous callers cannot use any credential RPC.
- User A cannot observe or mutate user B's credential metadata.
- Ordinary authenticated tokens cannot retrieve decrypted values, including their own.
- Missing, false, malformed, or forged runner claims are rejected.
- A valid runner token for A returns A's keys and never B's.
- Set, replace, and delete leave exactly one or zero matching Vault and registry rows as expected.
- Failed writes leave neither orphaned Vault secrets nor registry rows.
- Credential status contains no secret or Vault-decrypted field.
- Existing document RLS continues to isolate A, B, and anonymous callers.

### Relay tests

- Credential routes require authentication and reject unknown providers or blank values.
- GET returns status metadata only.
- Save, replace, and delete call the expected repository operations.
- Database failures are sanitized and never echo submitted values.
- Inputs begin blank, clear after submission, and are not populated by status responses.
- Existing profile document editing and tests remain unchanged.

### Job Hunter tests

- Runtime loading maps the latest CV and cover-letter rows plus Gemini and Brave credentials.
- Missing required values fail before provider calls; missing Brave preserves fallback behavior.
- Daily, Gmail-sync, and cover-letter entry points use the same provider-credential loader;
  daily and cover-letter entry points also use the shared document loader.
- Tokens contain the runner claim without changing `sub`, `role`, expiry, or rotation behavior.
- Logs and raised errors contain no document text, provider keys, or access tokens.
- Workflow assertions prove the four legacy variables are absent.

### Completion checks

Run from the repository root:

```bash
pnpm test
pnpm db:test
pnpm relay:lint
pnpm relay:build
```

The local database suite must run against an actual Supabase stack rather than being accepted as
skipped. Completion also requires a live credential lifecycle check—save, status, runner read,
replace, and delete—and a final GitHub secret-name inventory. Passing unit tests alone does not
prove the browser/non-browser security distinction or external secret removal.

## Risks

- **Privileged-function mistakes are high impact.** The small RPC surface, explicit grants,
  claim checks, fixed search paths, and negative tests are mandatory rather than optional
  hardening.
- **The signing key remains highly privileged.** Whoever holds it can mint a runner token for any
  user. This design does not increase that pre-existing authority, but secure storage and
  rotation remain operational requirements.
- **Vault availability must be verified in both environments.** The repository currently has no
  Vault migration and local configuration only contains the default commented Vault setting.
- **Documents are intentionally browser-readable to their owner.** If future requirements demand
  write-only or separately encrypted documents, that is a different product and threat-model
  decision.
- **Workflow cutover is not reversible without retained source values.** The live smoke test and
  document/key confirmation must precede deletion of the GitHub secrets.

## Acceptance mapping

| Issue acceptance | Design evidence |
| --- | --- |
| A user's documents and keys are readable only by their own runs | Document RLS plus same-user runner-claim checks and cross-user database tests. |
| Keys are encrypted server-side and never readable back by the browser | Vault storage, status-only APIs, no ordinary-user retrieval function, and browser/runner negative tests. |
| No user-specific credential remains in repository secrets | Workflows and runtime stop reading the four variables; rollout ends with a verified GitHub secret inventory. |

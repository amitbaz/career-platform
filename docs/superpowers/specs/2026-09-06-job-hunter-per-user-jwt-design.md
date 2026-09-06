# Job Hunter per-user JWT and Supabase client

Design for [#69](https://github.com/amitbaz/career-platform/issues/69). Priority 2 of 12 under
epic [#34](https://github.com/amitbaz/career-platform/issues/34). Builds on the schema and RLS
policies from [#66](https://github.com/amitbaz/career-platform/issues/66), shipped in `55db425`.

## Problem

Job Hunter runs unattended on a schedule. There is no end-user session for it to inherit, and
there never will be — it is a batch process, not a request handler.

Its future persistence layer is a set of 18 `public.job_hunter_*` tables where every row carries
a `user_id` and every table enables row-level security. Each table has four policies of the form
`(select auth.uid()) = user_id`, granted `to authenticated`. `auth.uid()` reads the `sub` claim
out of the JWT the request arrived with. So the database will only return a row to a caller that
can prove, cryptographically, which user it is acting for.

Nothing in the Python application can produce that proof today. There is no Supabase, JWT, or
HTTP-database code in `apps/job-hunter` at all, and no concept anywhere of which user a run
belongs to — `run_pipeline` takes settings, sources, a store and clients, but no owner.

The shortcut would be to connect with the service-role key, which bypasses row-level security,
and filter by `user_id` in application code. Epic #34 and the #66 schema design both rejected
that: it turns data isolation from a rule the database enforces into a promise the code makes,
and one forgotten filter leaks another person's job search. That rejection is load-bearing on
the schema — five tables with no natural owner (ATS registry, company watch, quota state,
context cache, search API usage) were duplicated per-user specifically so that no shared table
would ever need a privileged writer.

## Goal

Give Job Hunter the ability to act as exactly one user against the shared Supabase project — a
short-lived token it mints itself, and a client that presents it on every request — and prove
against a running database that a run for one user cannot read or write another user's rows.

## Non-goals

These belong to other tickets and must not be absorbed here:

| Out of scope | Owner |
| --- | --- |
| Porting the SQLite store to Postgres; wiring the client into the pipeline | #70 |
| Per-user search configuration | #71 |
| Per-user credential and document storage outside repository secrets | #72 |
| Per-user run orchestration, scheduling, Supabase Vault | #76 |
| Per-user Telegram identity mapping | #77 |
| User provisioning and onboarding | #78 |

`apps/job-hunter/AGENTS.md` states that Job Hunter's runtime reads and writes SQLite until #70
ports the store, and that no Python may be written against the Postgres tables outside that
ticket. This design honours that: it builds and tests the client, and stops. The pipeline is
untouched, and SQLite remains the source of truth.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Signing key | A dedicated ES256 key we generate, import into Supabase, and rotate to active | See "Signing key" below. The alternative — the legacy shared JWT secret — cannot be rotated independently of the `anon` and publishable keys and is being deprecated. |
| Claims | `{"sub": <user uuid>, "role": "authenticated", "exp": <unix seconds>}`, header `{"alg": "ES256", "kid": <key uuid>, "typ": "JWT"}` | Exactly what Supabase documents for self-minted tokens. `auth.uid()` needs `sub`; PostgREST switches to the Postgres role named by `role`. Nothing else is read by the policies. |
| Token lifetime | 5 minutes, re-minted on demand | A run takes 31–42 minutes. Minting once per run leaves an hour-wide window on a leaked token; holding the key and re-minting costs a few lines. Supabase documents no maximum but advises short. |
| Transport | PostgREST over HTTP, through the app's existing `HttpClient` | No new HTTP stack, no new dependency, and the existing hand-written-fake test style applies unchanged. |
| Client library | Hand-rolled, minimal | `supabase` would add six lockstep-pinned packages plus `httpx` and `yarl` to a six-dependency app, bypass the shared retry/timeout layer, and its only caller-supplied-token path is a global header its own auth listener can overwrite. |
| Client surface | Only what #69 needs: select, insert, update, delete | #70 grows it with real requirements rather than guesses from inside the wrong ticket. |
| Verification | pytest integration test against a live local Supabase stack, in a new CI job | The ticket's acceptance is "verified against the live policies". The existing pgTAP suite fakes the identity from inside the database and never signs a token, so it cannot prove the client's path works. |
| Signing key storage | GitHub Actions secret, base64-encoded | Where every other Job Hunter secret lives today. Supabase Vault belongs to #76, which is not built. #69 already concedes that whatever mints tokens can reach any user's data. |

### Signing key

Supabase supports two ways to sign a token it will accept.

The **legacy JWT secret** is one symmetric value that both signs and verifies for the whole
project. The `anon` and `service_role` keys are themselves JWTs signed by it, so it cannot be
changed without regenerating them; Supabase no longer supports rotating it from the dashboard
and is steering projects onto per-key signing keys.

A **dedicated asymmetric signing key** is generated with `supabase gen signing-key --algorithm
ES256`, imported into the project as a standby key, then rotated to active. We keep the only
copy of the private half — Supabase cannot export it back — and the public half is published at
the project's JWKS endpoint for verification.

One correction worth recording, because it looks like a better option than it is: a standby key
seems attractive, since a key that Supabase trusts but never signs with could be revoked without
disturbing anything else. Supabase's lifecycle table rules this out — a standby key's public half
is published for verification, but tokens signed by it are **not accepted** until the key is
rotated to active. Standby exists so applications can learn a key before it starts signing.
Minting with our own key therefore requires rotating it to active, which means Supabase Auth
will also use it to sign Relay's user sessions. That is the documented pattern, not a
workaround.

Blast radius is unchanged either way: whoever holds the signing material can mint a token for
any user. What the dedicated key buys is that it can be revoked or rotated on its own, without
regenerating the publishable key or invalidating unrelated credentials, and it keeps the project
on the path Supabase supports going forward.

### Dependency cost, stated plainly

ES256 signing requires `PyJWT[crypto]`, which pulls in `cryptography`. That is two new runtime
dependencies for an application that currently has six, and `cryptography` is a large compiled
wheel.

HS256 with the legacy secret would need nothing beyond `hmac` and `hashlib` from the standard
library. This tradeoff was not surfaced when ES256 was chosen, so it is recorded here rather
than buried: the cost of the better key story is two dependencies. Hand-rolling ECDSA signing to
avoid them is not an option worth considering.

## Components

Four small units, each independently testable.

### `SupabaseSettings` and `load_supabase_settings()`

Follows the existing `WebhookSettings` / `GmailSettings` precedent in `config.py`: a separate
frozen dataclass and loader, not fields bolted onto the main `Settings`. Job Hunter's runtime
does not need Supabase yet, so nothing else has to change to accommodate it.

Environment variables, all required when Supabase settings are loaded:

| Variable | Contents |
| --- | --- |
| `JOB_HUNTER_USER_ID` | UUID of the user this run acts for. Validated as a UUID at load. |
| `SUPABASE_URL` | Project API base URL. |
| `SUPABASE_PUBLISHABLE_KEY` | Public key for the mandatory `apikey` header. Not a secret. |
| `SUPABASE_SIGNING_KEY_B64` | Base64-encoded private JWK JSON, matching the existing base64-secret convention (`CANDIDATE_PROFILE_B64`, `COVER_LETTER_TEMPLATE_B64`). Decoded in memory only. The `kid` is read from inside the JWK, so no separate variable is needed. |

Failures are raised at load with the existing `_require_env` message shape. A malformed JWK
raises without echoing any part of the key.

### `AccessTokenMinter`

One job: given the user id and the private JWK, return a currently-valid signed token. It holds
the decoded key, caches the token it last issued, and re-mints when fewer than 60 seconds
remain. It never logs the token or the key.

Public surface is a single method returning a token string. Nothing else in the app needs to
know how a token is made.

### `SupabaseClient`

Wraps `HttpClient` and the minter. Attaches both required headers to every request —
`Authorization: Bearer <token>` and `apikey: <publishable key>` (Supabase documents these as
distinct; a minted JWT is not valid in the `apikey` header). Exposes four operations against a
named table: select with filters, insert, update with filters, delete with filters.

A distinct exception type is raised for 401 and 403 responses so that #70 can tell "our
authentication is broken" apart from "no such row". An empty result set when reading another
user's rows is **not** an error — that is row-level security working as designed.

### `HttpClient` additions

The shared HTTP helper currently offers only `get`, `post` and `get_json`. PostgREST updates and
deletes need `patch` and `delete`, added in the same shape as the existing methods so retry and
timeout behaviour is inherited unchanged.

One caveat to record for #70, not to solve here: `HttpClient` retries `POST` on 5xx responses.
For inserts that are not idempotent, a retry could duplicate a row. Every Job Hunter table has a
user-scoped unique key, so #70's writes will be upserts and unaffected, but the caveat belongs
in the client's documentation.

## Data flow

```
env -> load_supabase_settings() -> SupabaseSettings(user_id, url, publishable_key, jwk)
                                        |
                                        v
                              AccessTokenMinter(user_id, jwk)
                                        |  short-lived token, re-minted on demand
                                        v
   SupabaseClient(http, settings, minter) --HTTP--> PostgREST --> Postgres
        Authorization: Bearer <token>                                |
        apikey: <publishable key>                    RLS: auth.uid() = user_id
```

## Testing

**Unit tests**, hermetic, following the project's existing hand-rolled-fake convention (no
`responses`, no `requests_mock`):

- The minter produces a token whose decoded claims are exactly `sub`, `role`, `exp`, with the
  correct `kid` in the header and an expiry in the expected window.
- The minter reuses a fresh token and re-mints an expiring one.
- Neither the token nor the key material appears in captured log output.
- Settings loading rejects a missing variable, a non-UUID user id, and a malformed JWK.
- The client sends both headers on every verb, and builds the URL and query parameters PostgREST
  expects.

**Integration test**, the acceptance criterion, run against a live local Supabase stack:

1. Seed two users, A and B, with fixed UUIDs, via `supabase/seed.sql` — a file `config.toml`
   already references but which does not exist yet.
2. As A: insert a `job_hunter_jobs` row, then read it back. Both succeed.
3. As B: select A's row (expect zero rows), update it (expect zero rows affected), delete it
   (expect zero rows affected), and insert a row carrying A's `user_id` (expect a refusal).
4. As A: confirm the row is still there and unmodified.
5. Clean up rows the test created.

The test skips itself when no stack is reachable, so ordinary local `pytest -q` runs stay fast
and offline. A new CI job boots the stack with the Supabase CLI and runs it, with path filters
covering both `apps/job-hunter/**` and `supabase/**`.

Local development needs a signing key file for the stack: `supabase gen signing-key --algorithm
ES256` written to a gitignored `supabase/signing_keys.json`, with `signing_keys_path` enabled in
`config.toml`. The key file is generated, never committed — #75 will publish this repository.

## Risks and open verifications

- **The local stack may not honour `signing_keys_path` for PostgREST verification.** The option
  exists in the config template and `supabase gen bearer-jwt` reads it, but nothing confirms
  PostgREST accepts ES256 tokens locally. This must be verified as the first implementation step,
  before any code is written against it. If it does not hold, the fallback is to run the local
  integration test against the local HS256 secret while production uses ES256 — which weakens the
  test, and would be a reason to revisit the signing-key decision rather than paper over it.
- **The migration may not have been pushed to the cloud project.** Nothing in the history
  confirms `supabase db push` ran after `55db425` merged. Irrelevant to the local test, relevant
  before anything runs against the real project.
- **Losing the private signing key means generating and rotating a new one.** Supabase cannot
  export it back.

## Applying this outside git

The following are not code changes and must be done by hand before a real run works:

1. Generate an ES256 signing key with the Supabase CLI and store the private JWK safely.
2. Import it into the Supabase project as a standby key, then rotate it to active.
3. Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SIGNING_KEY_B64` and
   `JOB_HUNTER_USER_ID` as GitHub Actions secrets.
4. Confirm the #66 migration has been pushed to the cloud project.

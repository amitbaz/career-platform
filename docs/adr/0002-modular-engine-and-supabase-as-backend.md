# ADR-0002: A modular engine, Supabase as the backend, one contract between them

Status: proposed
Date: 2026-09-11

## Context

[ADR-0001](0001-the-engine-is-the-product.md) decided what the engine is: ingestion, enrichment
and matching, with every consumer being a surface. It did not decide where the engine's borders
are in code, what sits between the engine and a surface, or what the backend of the product is.
Epic #114 carried that open item — "define the engine's public interface — what a surface may ask
for and what comes back" — and it is still unchecked. Everything since has been built around the
gap.

What that looks like in the repository on 2026-09-11:

- **One flat package does everything.** `apps/job-hunter/src/job_hunter` is 22,104 lines in a
  single package with no sub-packages. It holds the engine (crawling, facets, matching,
  ranking), product features (Engine Lab, cover letters, PDF), retired surfaces (Telegram, its
  webhook, its navigation) and outcome tracking (Gmail). Its name is the name of the POC bot.
- **One object owns all data access.** `postgres_store.py` is 4,437 lines; every module that
  touches the database goes through it.
- **The monolithic run is still here.** `pipeline.py` (1,686 lines) is the single-pass run #189
  retires.
- **Modules reach into each other's internals.** `engine_lab.py` imports private helpers
  (`_cache_key`, `_hash`, `_context_from_dict`, `_profile_row_to_legacy_dict`) from other
  modules, because no public interface exists to call instead.
- **The engine is coupled to Supabase's HTTP layer.** Nine modules use `SupabaseClient` or
  `AccessTokenMinter` (PostgREST plus minted user tokens); two use a direct Postgres connection.
- **There is no backend.** Product logic that is not engine logic — the review ledger's card
  flow, a staff login — has had nowhere to go but into the Python package or a throwaway page.
  #283 (an internal tool) stalled on exactly this: every tool option needed an engine interface
  that does not exist.

Nothing forces a fix today, which is why now is the time. The Engine-ready milestone's remaining
matching tickets (#260 ready pool, #261 serving the stack, #262 why lines, #263 learning from
swipes) have not started, and they are where the engine's interface to the product gets fixed.
Written into the current layout, each makes the untangling larger.

## Decision

### 1. The system is three layers meeting at one database contract

```
Apps (mobile, internal console)  ──►  api layer: views + functions + RLS  ◄──  Engine (Python workers)
                                          Supabase Postgres
```

- **The engine** reads sources, writes the shared corpus, computes each user's ready cards, and
  reads user signals (swipes, rules) back to learn. It serves no HTTP to users, renders nothing,
  and knows no surface exists.
- **Supabase is the backend.** Postgres holds the data; Auth holds identity; row-level security
  decides who sees what; Postgres functions carry business actions that need a transaction; the
  auto-generated REST API is how apps reach the api layer; pg_cron and the queue already schedule
  and hand off engine work. There is no custom backend server.
- **Edge Functions** exist only for work that needs a secret or a third party the database cannot
  call: payments, OAuth token exchange, AI work paid by the platform on a user's request. They
  stay thin; the logic they invoke lives in Postgres functions or the engine.
- **Apps** reach data only through the api layer. They never call engine code and never read
  engine tables.

The engine and the apps never talk to each other directly. The engine writes a result (a ready
pool, #260); an app reads it through the api layer. This is why no surface needs to call Python
at request time, and why the choice of internal tool in #283 no longer blocks on the engine.

### 2. Every table has exactly one writer

- **Engine-owned data** (postings, facets, sources, ready pool, measurement) is written only by
  the engine's database role. #179 started this ("give the shared tables a single writing role").
- **App-owned data** (accounts, swipes, bucket, applications) is written only by users through
  RLS, or by an Edge Function acting for them.
- **The api layer** — one Postgres schema of views and functions — is the only schema the
  auto-generated REST API exposes. Apps read engine results and write user signals through it.
  The engine reads user signals through views it is granted, never by writing app tables.

Target is separate Postgres schemas for engine, app and api. Moving existing `public.job_hunter_*`
tables is done table by table as modules move (section 5), not in one migration.

### 3. The engine is a modular monolith

One Python package, deployed as several independent workers. Workers already run as separate
Render jobs (crawl, extract, re-check) and fail independently; that stays. The code is split by
responsibility:

```
engine/src/engine/
  core/         config, database connection, http, AI client, clock, errors. No business logic.
  ingestion/    sources, crawling, job identity and dedupe, freshness re-checks
  enrichment/   objective facet extraction, company facets, hiring eligibility
  matching/     candidate context, ranking, hard blockers, ready pool, why lines, learning
  measurement/  Engine Lab ledger, worker runs, telemetry
  workers/      one thin entrypoint per scheduled job; wires modules, holds no logic
```

The package moves out of `apps/` to a top-level `engine/` — `apps/` is for things users or staff
open. It is named for its role, not the brand, which is still changing.

Inside each module:

- `api.py` is the only file another module may import. It is the module's public interface.
- `store.py` is that module's own database access. `postgres_store.py` is split along module
  lines and then deleted.
- Everything else is private to the module.

Between modules:

- **Direction:** `workers → modules → core`. Among modules, data flows downstream only:
  `ingestion → enrichment → matching`. `measurement` may read every module; it writes only its
  own tables. `core` imports no module.
- **Handoff through the queue, not calls.** A module passes work to the next through the
  database queue (`stage_queue`), so modules are independent in time as well as in code.
- **Ownership:** a table belongs to one module; only that module's `store.py` writes it.

The engine talks to Postgres directly and only through `core`'s connection. It does not use
Supabase's REST client or mint user tokens. The nine modules that do today are migrated as they
move.

**Tooling.** The engine's Python environment is managed by `uv`, adopted in the same pass as the
rename. `uv.lock` pins the dependency set so a laptop, CI and Render install exactly the same
versions — today `pip install -e '.[test,webhook]'` resolves fresh each time — and `uv run`
creates and syncs the environment on demand, which removes the recurring failure where a
worktree without its own `.venv` silently tests a different source tree. Render's build command
becomes `pip install uv && uv sync --frozen`.

`pnpm` stays, scoped to JavaScript: the workspace for the mobile app and the internal console
when they arrive, not the engine. It also stays the repository's single task entry point —
`package.json` holds the names (`engine:test`, `db:test`, `db:reset`) while what runs behind them
changes to `uv`. A language-neutral runner such as `just` is a better fit in principle, but it is
a third tool and a documentation rewrite for no capability the scripts lack; revisit it when the
apps bring real JavaScript tasks. When the mobile app lands, pnpm needs `node-linker=hoisted` in
`.npmrc` for React Native's bundler.

### 4. Abstraction at boundaries, not everywhere

An interface is earned by an external dependency or by a second real implementation:

- **Do** put an interface in front of each job source, the AI provider (Gemini today), the clock,
  and each module's edge (`api.py`).
- **Do not** introduce an interface, base class or factory for code with one implementation "in
  case". It reads as tidy and makes the code harder to follow and change.

### 5. Getting there

- **No file moves while code is in flight.** A move touches every import; it lands only when the
  merge train is empty. #243, the last change to `postgres_store.py` and `match_jobs`, merged on
  2026-09-12 as PR #284 (`091aa34`), and no pull request is open — so this condition is met now.
  It has to hold again for each later move.

**The whole restructure lands before #260** (owner, 2026-09-12), rather than moving each module
just before the ticket that needs it. Feature work resumes once, into a finished structure,
instead of every matching ticket carrying part of a move. The order below subtracts before it
moves, so the largest deletions happen while they are still cheap.

**A. Borders, no code moves.** This ADR accepted, the `AGENTS.md` rules, `import-linter` with
today's violations as the baseline, the pgTAP ownership tests, Relay deleted. Nothing here blocks
any engine ticket.

**B. Delete what should not be moved — deleting beats moving.** About 4,100 of the package's
22,104 lines do not belong in the engine at all, and every one deleted is a line nobody moves,
reviews or lints: the monolithic run and Telegram delivery (#189), the Telegram surface modules,
cover letters and PDF rendering (applying, #199), and Gmail. #189 goes first, because it deletes
`pipeline.py`, which the rest is wired into.

Gmail is deleted rather than parked (owner, 2026-09-12), until its place in the product is worked
out: 7 modules at 1,391 lines and 11 test files at 3,757 lines. It is entangled with the monolith
— `pipeline.py` uses `GmailStagedSource` and the review digest, `cli.py` owns the `sync-gmail`
command, `config.py` imports `GmailSettings` — so it is removed with #189, not before it. Its
tables are dropped in the same migration rather than left behind unowned, which would fail step
E's ownership tests. The deleting pull request records its own commit in #80, which owns outcome
tracking, so the code can be recovered from history when Gmail is designed back in.

**C. One mechanical rename:** `apps/job-hunter` → `engine/`, package `job_hunter` → `engine`,
`uv` adopted in the same pass, and every deployment reference updated with it — `render.yaml`'s
root directory and three start commands, the Vercel configuration, the CI workflows and the
`package.json` scripts. Measured on 2026-09-12: 224 Python files, 718 import lines, 120
references outside the package, 128 test files.

**D. Module splits, one pull request each:** `core/` first, then `ingestion/`, `enrichment/`,
`matching/`, `measurement/`. Each takes its slice of `postgres_store.py` with it, and the file is
deleted when the last slice leaves. This is the only step that is real engineering rather than
mechanical work.

**E. Database ownership:** the engine, app and api schemas, their roles and grants, with the
ownership tests going green.

**#260 onward start when E is green**, written into the finished structure. #259 waits for the
restructure too (owner, 2026-09-12), because it touches ingestion and cannot run beside the
moves.

**Rules for every pull request in B–E:**

- A move changes no test assertions — import paths only. If an assertion has to change, it is not
  a move: stop and raise it.
- Report the test count, not the colour. Without the stack environment the suite silently skips
  (1,131 passed with 675 skipped, against 1,813 with none), so a green run proves nothing on its
  own.
- One at a time, with the merge train empty. No feature ticket runs beside a move.
- The baseline only shrinks. The restructure is done when it reaches zero, and then it is over.

### 6. The rules are enforced by CI, not by memory

Agents follow what fails a build. Documentation alone drifts.

- **Import contracts.** `import-linter` runs in CI with contracts for the layer direction,
  module independence and the `api.py`-only rule. A new violation fails the PR. Existing
  violations are listed in the contract config as the baseline; each restructure ticket deletes
  its entries.
- **Ownership tests.** pgTAP asserts that only the engine role writes engine tables, that app
  roles cannot reach engine tables, and that the api schema is the only exposed schema — the
  same pattern as the existing isolation guard.
- **App boundaries.** Each app has one data-access module; nothing else imports the Supabase
  client. Enforced with a dependency linter when the first app exists.
- **Written guidance.** Each engine module has a short README: what it owns, what it exposes,
  what it depends on. Golden-path recipes ("add a source", "add a table", "add a worker") each
  point at one canonical example. `AGENTS.md` summarises the rules and links here.
- **Review.** Code review checks compliance with this ADR as a named item, whoever performs it.

### 7. Supabase is the backend for now; leaving must stay cheap

Supabase is the fastest and cheapest backend for a pre-launch product, and the owner does not
rule out a custom backend later. The design keeps that a replacement of one layer, not a rewrite:

| Piece | Lock-in | Kept cheap by |
|---|---|---|
| Postgres data and functions | none | plain Postgres; any host runs it |
| Auto-generated REST | low | the api layer is the contract; a custom backend can serve it |
| Auth | medium | standard JWT claims; staff role in `app_metadata`; no app data in the `auth` schema |
| Edge Functions | low | thin standard request handlers |
| Engine | none after migration | plain Postgres only, no Supabase client |

The rules that keep the exit open: the api layer is the only contract; each app reaches it
through one module; business logic never lives only in an Edge Function; the engine never uses
Supabase-specific clients.

Revisit this section when one of these is observed, not felt: business logic that Postgres
functions cannot express cleanly and testably; Supabase cost at scale exceeding a self-hosted
equivalent; a compliance or residency requirement Supabase cannot meet; or a request pattern
(long-lived connections, heavy fan-out) the api layer cannot serve.

## Consequences

- **The engine gets a public interface** at last, closing #114's open item: modules expose
  `api.py`, and the product sees only the api schema.
- **#283 is unblocked in principle.** An internal tool reads and writes through the api layer
  with a staff role; which tool is a separate decision.
- **Engine-ready matching work waits for one move.** #260–#263 start after the matching module
  exists. #259 runs before or after the ingestion move, not during it.
- **Some business logic moves into SQL.** Postgres functions need pgTAP tests like any other
  code; the repository already has the pattern.
- **Every new table, module or worker has a place.** An agent that cannot say which module owns
  a table, or which layer a change belongs to, is looking at a design question for the owner.

## Alternatives considered

**Microservices.** Rejected. Splitting the engine across network calls adds partial failure,
separate deploys and cross-service consistency, which pay off with many teams, not one owner and
agents. The benefit that matters — independent scheduling and failure — the separate workers
already provide.

**A custom backend server now.** Rejected for now, kept open (section 7). It duplicates what
Auth, RLS and the auto-generated API already give, adds a service to run and pay for, and would
be written before the product has a surface that needs it.

**An engine HTTP service that apps call live.** Rejected. It puts Python on every request path,
makes the engine aware of surfaces, and adds latency to the stack D1 wants served in under two
seconds. A precomputed ready pool read through the api layer does the same job.

**Keep the flat package, add rules only.** Rejected. Rules without borders to enforce them are
guidance an agent cannot check; the baseline would never shrink.

## References

- [ADR-0001](0001-the-engine-is-the-product.md) — what the engine is.
- Epic #114 — the unchecked public-interface item this closes.
- #179 — single writing role for shared tables.
- #189 — retiring the monolithic run and Telegram delivery.
- #243 (merged as #284), #259, #260–#263 — the engine work this sequences.
- #283 — the internal tool, parked behind this.
- `docs/product-vision.md` D1 (stack latency), D10 (engine readiness).

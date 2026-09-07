# AGENTS.md

Guidance for coding agents working in this repository.

## What this repository is

A single monorepo holding the whole career platform:

| Path                | What it is                          | Toolchain                  |
| ------------------- | ----------------------------------- | -------------------------- |
| `apps/job-hunter`   | Job Hunter service                  | Python 3.12+, pytest       |
| `apps/relay`        | Relay web app                       | Next.js, pnpm, vitest      |
| `supabase`          | Shared schema: migrations, SQL tests| Supabase CLI               |

A platform change that spans Job Hunter, Relay and the schema belongs in **one branch and one
PR here** — that is the reason this repository exists.

## Per-app guidance

Read the app-level guide before changing an app; they hold the real conventions:

- `apps/job-hunter/AGENTS.md`
- `apps/relay/AGENTS.md`

## Commands

Run from the repository root:

```bash
pnpm install            # JS workspace install (pnpm only; never npm/yarn)
pnpm relay:test         # Relay tests
pnpm relay:lint
pnpm relay:build
pnpm job-hunter:test    # Job Hunter tests
pnpm test               # both suites

pnpm db:key             # one-time: create the local stack's signing key
supabase start          # local Supabase stack
pnpm db:test            # pgTAP suite against that stack
```

`supabase start` will not boot until `supabase/signing_keys.json` exists, because `config.toml`
sets `signing_keys_path`. The file is generated per machine and git-ignored, so a fresh clone has
to run `pnpm db:key` once. It is a throwaway key for localhost; never put the hosted project's
signing key there.

Job Hunter's Python environment is independent of pnpm. Install it with
`pip install -e '.[test,webhook]'` from `apps/job-hunter`.

## Boundaries

- Keep the app boundary. Job Hunter stays Python, Relay stays TypeScript. There is no shared
  library layer, and one should not be created without a concrete need.
- Supabase migrations are platform-owned. Add new migrations under `supabase/migrations`, not
  inside an app.
- No Turborepo/Nx. pnpm workspaces is deliberately the only monorepo tooling.
- Paths are load-bearing: GitHub workflows use `defaults.run.working-directory: apps/job-hunter`.
  Neither Job Hunter workflow uploads or restores an artifact any more — Job Hunter's persistent
  state lives in Postgres, not on the runner.

# AGENTS.md

Guidance for coding agents working in this repository.

## What this project is optimising for

**The search-and-match engine is the product. Everything else is a surface.** Telegram, the
scheduled daily run, and any future application consume the engine; none of them is where
matching lives. See [ADR-0001](docs/adr/0001-the-engine-is-the-product.md), and epic #114 for
the decisions in full.

Four rules follow, and they decide most judgement calls in this repository:

1. **Match quality is the product; everything else is packaging.** When a change could improve
   match quality or improve a surface, match quality wins.
2. **Cost scales with jobs, not with users.** Objective facts about a posting are extracted once
   and shared. Any change that makes that work per-user is a regression, however convenient.
3. **No matching, ranking, eligibility or scoring logic in a surface.** A surface asks the engine
   and renders the answer. Logic that leaks into an adapter has to be extracted again before the
   next surface can exist.
4. **Measure quality claims; do not assert them.** Yield per source is already recorded. A claim
   about match quality without a number behind it is a hypothesis, and should be written as one.

Use the vocabulary in [CONTEXT.md](CONTEXT.md) — in code, tests, issues and specs. The terms
there exist because their synonyms have already caused confusion here.

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

## Agent skills

### Issue tracker

GitHub Issues (repo: amitbaz/career-platform), via `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.

### Marketing and brand

Read `docs/marketing-and-brand.md` before marketing, landing-page, or visual-identity work.
It records agreed direction, exploratory proposals, and open decisions.

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

## Working alongside other sessions

More than one agent may be working this repository at the same time, in separate git worktrees.
Two shared resources are **not** isolated per worktree, and both have already caused real problems
here.

### The local Supabase stack is one instance per machine

Every worktree's tests connect to the same local database. It is not per-branch and not
per-worktree.

- **Never run `supabase db reset` without first checking whether another session is mid-run.** It
  wipes state that every session shares, and the other session sees inexplicable failures rather
  than a clear error.
- **Avoid running database-touching suites concurrently from two worktrees.** Serialise them
  instead. The Job Hunter suite and the pgTAP suite both qualify.
- **Check before assuming you are alone.** `git worktree list` shows other active workspaces;
  `docker ps` shows whether the stack is already up and who brought it up.

### Migration filenames are allocated across the whole repository

Supabase applies migrations in the order their numeric filename prefixes sort, and the hosted
project records what it has already applied. A filename that sorts before an applied version fails
the push.

- **Use the full `YYYYMMDDHHMMSS` format.** The shorter `YYYYMMDDNNNN` form sorts *before* it, which
  has already forced a renumber on this repository once.
- **Check other worktrees' branches, not just `main`.** An in-flight branch may already carry a
  migration later than anything on `main`, and picking a timestamp from `main` alone will sort
  wrong.
- **A ticket may pre-assign your timestamp.** When two tickets that both add migrations can be
  worked in parallel, the timestamps are allocated on the issues rather than chosen independently.
  Use the assigned one.

### When another session is in your way

Say so rather than working around it. Deleting, resetting or force-pushing over someone else's
in-flight work costs more than waiting. Report what you found and let the human decide.

## Boundaries

- Keep the app boundary. Job Hunter stays Python, Relay stays TypeScript. There is no shared
  library layer, and one should not be created without a concrete need.
- Supabase migrations are platform-owned. Add new migrations under `supabase/migrations`, not
  inside an app.
- No Turborepo/Nx. pnpm workspaces is deliberately the only monorepo tooling.
- Paths are load-bearing: GitHub workflows use `defaults.run.working-directory: apps/job-hunter`.
  Neither Job Hunter workflow uploads or restores an artifact any more — Job Hunter's persistent
  state lives in Postgres, not on the runner.

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

### Memory (MemPalace)

Cross-session memory lives in a MemPalace **shared-brain hub**, wing `career_platform` —
one palace shared by Claude Code (`mac-claude`) and Codex (`mac-codex`). See
`docs/agents/memory.md` for the topology and full protocol. The short version:

- **Read before answering** about a past decision, a prior measurement, or why something
  is the way it is: `mempalace_search` for verbatim drawers, `mempalace_kg_query` for facts
  that have a validity window. Do not answer such questions from model memory.
- **Write at the end of substantive work**: `mempalace_diary_write` (agent `claude-code`,
  wing `career_platform`), plus `mempalace_add_drawer` for a durable decision. The capture
  hooks file the raw transcript; the diary is for what the transcript does not say — what was
  decided and why.
- **Facts that can change** (measurements, required checks, chosen models, counts) go in the
  knowledge graph. When one changes, `mempalace_kg_supersede` — never invalidate-then-add,
  which leaves both values true at the boundary.
- **Do not mine source code into the palace.** The repo is already searchable with grep and
  the file tools, and a mined chunk is a snapshot that goes stale silently. Prose that is not
  in the repo is what the palace is for.
- **`mempalace_sync` before trusting a stale-looking result**, and read its dry run before
  passing `apply`.

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
pnpm db:reset           # rebuild the local DB from migrations (destructive)
```

`pnpm db:test` and `pnpm db:reset` serialise against every other session on this machine;
`pnpm job-hunter:test` runs alongside other test runs but never alongside a reset — see
[Working alongside other sessions](#working-alongside-other-sessions).

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

Two runs can nonetheless use it at the same time, because they are isolated by `user_id` rather
than by taking turns. RLS scopes every Job Hunter query by `user_id`, so a run that owns different
users cannot see — or delete — another run's rows. `supabase/seed.sql` creates a pool of eight user
pairs; `apps/job-hunter/tests/seed_pool.py` claims one pair for the length of a run and releases it
at the end. Ownership is an `flock` on `~/.cache/career-platform/seed-slots/slot-N.lock`, so a
crashed or killed run's slot comes back on its own, with no stale claim to clear by hand.

A ninth concurrent run waits for a slot. That is a slowdown, never a wrong result.

What is still **enforced by `scripts/stack_lock.py`** is everything no user partitioning can make
safe:

- `pnpm db:reset` and `pnpm db:test` take the lock **exclusively**. A reset drops the database out
  from under every run regardless of whose users they hold.
- `pnpm job-hunter:test` takes it **shared**. Any number of suites may hold it at once; none of
  them can overlap a reset.

A reset that is waiting holds a second, "intent" lockfile beside the first, so suites started
after it queue behind it rather than slipping in alongside the ones already running. Without that
a reset could wait forever on a machine that always has some suite in flight.

The lockfile lives at `~/.cache/career-platform/stack.lock` — outside every worktree, because one
inside the tree would give each worktree its own lock and defeat the point. So does the slot
directory, for the same reason.

- **Use the pnpm scripts, not the bare commands.** `.venv/bin/python -m pytest` and
  `supabase db reset` bypass the lock, so a bare pytest run can be wiped mid-suite by someone
  else's reset. If you need a bare invocation, wrap it:
  `python3 scripts/stack_lock.py --shared <command>` (or without `--shared` if it is destructive).
- **A wait is not a hang.** `stack_lock: waiting for the local Supabase stack (...)` or
  `seed_pool: all 8 seed user slots are in use` means someone else holds it; both report progress
  every 30s and give up after 30 minutes.
- **What it looks like when the pool is bypassed:** a scatter of unrelated assertion failures
  (`assert [] == ['acme']`) or a `RuntimeError` about a foreign-key violation while cleaning seed
  users. Both mean a second writer is on this run's seed users — most likely a worktree sitting on
  a revision from before the pool existed, which uses the slot-0 pair unconditionally. Check
  `ps -eo args | grep '[-]m pytest'` and `git worktree list` before believing a red suite.

Raising the pool size means editing both `seed_pool.POOL_SIZE` and `supabase/seed.sql`, then
running `pnpm db:reset` to create the new users — test writes go through PostgREST with a minted
JWT, which cannot insert into `auth.users`, so the pool cannot grow itself.
`tests/test_seed_pool.py` fails if the two ever disagree.

### Never `supabase db push` from a worktree

Migrations are applied to the live project by CI when they reach `main` (#190). No one applies
them by hand, and a worktree must never try.

A worktree that is linked to the live project can push an unmerged branch's migration to
production. Because Supabase refuses a migration filename that sorts *before* one already
applied, doing so can permanently block a lower-numbered migration sitting on another branch:
the live project ends up ahead of `main`, and the only recovery is renumbering a migration that
has already been merged and reviewed.

- **Push nothing from a worktree.** Merge to `main` and let CI apply it.
- **Do not copy `supabase/.temp` into a new workspace.** It carries the production project link.
  `pnpm workspace:setup` deliberately omits it; the local stack does not need it.
- **`supabase db reset`, `supabase start` and `pnpm db:test` are local-only and always fine.**
  It is `db push` and `link` that reach production.

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

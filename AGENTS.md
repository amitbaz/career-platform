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
5. **An empty result must carry its reason.** Report what was attempted, not only what was
   produced, and report it where the emptiness is reported. This system's characteristic failure
   is silence that looks like a normal quiet day: an exhausted AI quota, a cancelled run, a
   corpus that can no longer be written, and a user missing the accumulated ATS rejections that
   stop worthless boards being re-crawled all present identically as "delivered a digest, found
   nothing new" — which is also what a genuinely quiet week looks like. A startup warning is not
   enough, because by day three it has scrolled out of the log.

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

### A green Job Hunter run is only evidence if the store tests ran

Every test that touches the database asks for the `_stack_env` fixture, and that fixture calls
`pytest.skip` when any of the `SUPABASE_TEST_*` variables is unset (`apps/job-hunter/tests/
conftest.py:253`). So a run with the stack environment missing **skips every store-backed test,
reports success, and exits 0**. Nothing about the output says the coverage was switched off; it
just says `passed`.

**Read the skip count, not the pass count.** With the environment exported the suite skips
nothing, so `0 skipped` is the invariant that says the run meant something. A run reporting
several hundred skips — currently around 663 — is a run in which roughly two thirds of the suite
did not execute, whatever the exit code was.

This is the same failure class as a workspace without its own `.venv`, one layer down: there, the
right suite runs against the wrong source tree; here, the right tree runs with most of its
coverage silently disabled. Both produce a green run that is not evidence. Neither is announced.

**Reverting your source proves much less here than it would elsewhere.** Most of this store is
SQL — `job_hunter_merge_jobs`, `job_hunter_upsert_job` and their siblings are database functions,
not Python. `git checkout HEAD~1 -- apps/job-hunter/src apps/job-hunter/tests` reverts one half of
the system under test and leaves the other half exactly as the local stack has it. "I reverted my
changes and it still fails" is therefore not the claim it sounds like, and it has already been
mistaken once for a defect on `main`.

**To see what a foreign migration actually did**, read the ledger rather than guessing: the
applied statements are stored, so an unmerged peer's migration is legible even though its file is
on no branch you have.

```sql
select unnest(statements) from supabase_migrations.schema_migrations where version = '<version>';
```

Diffing `schema_migrations` against `ls supabase/migrations` tells you a foreign migration is
*present*; this tells you what it *changed*, which is what answers "is this failure mine".

**The ledger is necessary, not sufficient — interrogate the object when it looks clean.** Both
checks above read `supabase_migrations.schema_migrations`, so both are blind to a migration that
was applied without recording its version. That state is worse than an unrecorded table, because
the cheap check now answers "clean" and the failure looks like your own code again. When the
ledger matches `main` and a store-backed test still fails, ask the database what the function
actually is:

```sql
select prosrc like '%<symbol from the peer''s work>%'
  from pg_proc p join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public' and p.proname = '<function>';
```

A symbol defined in no migration in your tree, present in a live function body, is proof that
body is not the one your tree describes. Ledger first because it is cheap; `pg_proc` when the
ledger looks clean and the behaviour still does not.

**If you apply a migration by hand, record its version.** Hand-applying is often the right call —
`pnpm db:reset` drops every peer's unmerged migration, so resetting to install your own is
destructive to everyone else mid-run. But applying without the ledger row leaves a stack whose
recorded migration set looks exactly like `main` while its functions do not, and the next agent's
cheap check silently fails them. Use the CLI rather than writing the row yourself — it touches
only your own version and does not depend on the ledger's shape staying as it is:

```bash
supabase migration repair --local --status applied <version>
```

If your objects are already installed from an earlier hand-apply that skipped this, drop and
re-apply them before repairing, so the row and the objects agree. And **never repair a version
that is not yours.** A row claiming a version somebody else's migration owns is worse than a
missing one: a missing row gets investigated, a wrong row gets trusted.

**A missing row is not merely absent — it is read as belonging to the highest recorded version.**
This happened on 2026-09-09: stage-queue objects were hand-applied with no row at all, the
highest recorded version was a different, since-merged migration, and a reader who checked the
ledger reasonably concluded the stage functions had arrived with it. They had not. The inference
is sound and the answer is wrong, which is why recording your own row is not a courtesy — it is
what stops the next agent's cheap check producing a confident wrong attribution.

**And know what a reset costs other people.** `pnpm db:reset` serialises against other runs
through the stack lock, so it will not corrupt anyone mid-statement — but it still drops every
migration that exists only on somebody's branch, on a machine that may have several. The lock
makes a reset safe to *perform*; it does not make it free for everyone else. Prefer applying your
own migration by hand, with its ledger row, over resetting to pick it up.

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
  a revision from before the pool existed, which uses the slot-0 pair unconditionally.
  `ps -eo args | grep '[-]m pytest'` and `git worktree list` can confirm this, but **`ps` is a
  false negative**: a run that already reset the stack and exited leaves no process to find, and
  an empty `ps` is not evidence that nothing interfered. Never treat it as an alternative to the
  migration diff below.
- **Diff the applied migrations before believing any red run**, and do it first when your branch
  carries a migration that is not on `main`. A peer's `pnpm db:reset` drops every migration that
  exists only in your worktree, and the suite then fails in ways that read as your own diff being
  broken — the symptom points at your code and the cause is somebody else's reset. Compare
  `supabase_migrations.schema_migrations` against `ls supabase/migrations`: a version present in
  one and not the other names the cause in a single look. It catches the other direction too,
  where a peer's migration *is* applied and turns unrelated tests red.
- **A green run can be wrong as easily as a red one.** A workspace without its own `.venv` runs
  against a different source tree entirely, so the suite passes while testing code you did not
  write. This is why `pnpm job-hunter:test` is mandatory rather than a convenience: it resolves
  the interpreter for the tree it is run from. Create the `.venv` in every new checkout and every
  new worktree before the first test run, and never conclude anything from a green suite you have
  not confirmed is the right tree.

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
- **Ask for your timestamp when you open the PR, not when you start.** Allocating at dispatch
  fixes an ordering using information that only exists at merge time, and every parallel agent
  makes it worse — on 2026-09-09 that mechanism produced three separate hazards in one day, none
  of them involving a line of wrong code: two tickets silently held the same number, and twice a
  ticket's assigned number would have sorted before an already-applied migration. Write a
  placeholder while you work, and ask whoever owns the board for the real number when the branch
  is ready. Issued in merge order, it is correct by construction.
- **If a ticket already carries an assigned timestamp, use it** — older issues pre-assign, and the
  number on the issue always wins over one you pick yourself.
- **If you reach mergeable state while a lower unmerged timestamp is still open, ask to be
  renumbered downward rather than waiting.** The cost is a file rename on an unmerged branch. The
  cost of waiting is a blocked agent. Never renumber a ticket that has work in flight to
  accommodate one that has none — move the one with nothing written.
- **An abandoned number stays empty.** Timestamps must be ordered, not contiguous; re-using a
  freed number recreates the hazard that freed it.

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

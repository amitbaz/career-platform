# AGENTS.md

Guidance for coding agents working in this repository.

## What this project is optimising for

**The search-and-match engine is the product. Everything else is a surface.** The mobile app —
the product's main interface — and anything else that shows a user results consume the engine;
none of them is where matching lives. See [ADR-0001](docs/adr/0001-the-engine-is-the-product.md),
epic #114 for the engine decisions in full, and [docs/product-vision.md](docs/product-vision.md)
for what the product is.

Six rules follow, and they decide most judgement calls in this repository:

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
6. **A claim about this system needs a mechanism that fails when it stops being true.** Checks,
   guards and prose all make claims — that a suite ran, that a table is covered, that a ledger
   matches the database, that a set of tables is shared. Without something that fails when the
   claim goes false, it does not decay loudly. It stays trusted and quietly wrong, and it is
   trusted *because* it looks like it was checked.

   **So when you add a claim, add the mechanism.** If the mechanism is expensive or does not
   exist yet, there are two honest options and neither is silence: make the claim fail loudly in
   the cheapest way available — an assertion, a test that reads the real thing, a check that
   names what it did not examine — or do not make the claim at all. A paragraph saying "the two
   shared tables" earns its place only if something breaks when there are three. **Prefer no
   claim to an unenforced one: an agent can work around a gap it can see, and cannot work around
   a sentence that is confidently wrong.**

   This is not a rule about documentation. It was derived from five failures in one day, and
   three of them were checks rather than prose: a workspace without its own `.venv` running the
   right suite against the wrong source tree and passing; a run without `SUPABASE_TEST_*`
   skipping two thirds of the suite and passing; the isolation guard measuring the shared
   database against one tree's list (#207); the migration ledger describing a stack it no longer
   matched (#206, #211); and the documented shared-table set drifting from the schema with
   nothing to notice (#215). #208 makes the second fail loudly. If you are filing this under
   "keep the docs updated", you have the wrong half of it.

Use the vocabulary in [CONTEXT.md](CONTEXT.md) — in code, tests, issues and specs. The terms
there exist because their synonyms have already caused confusion here.

## What this repository is

A single monorepo holding the whole career platform:

| Path                | What it is                          | Toolchain                  |
| ------------------- | ----------------------------------- | -------------------------- |
| `apps/job-hunter`   | Job Hunter service                  | Python 3.12+, pytest       |
| `apps/relay`        | Relay — legacy POC, see below       | Next.js, pnpm, vitest      |
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

### Issue areas

Every open issue carries **exactly one** `area:*` label saying which part of the product it
belongs to: `area:ingestion`, `area:matching`, `area:applying`, `area:outcomes`, `area:coach`,
`area:app`, `area:platform` or `area:business`. Epics (label `epic`) are exempt, because they
span areas. Add the area label in the same command that creates the issue, and move it when an
issue's scope moves. If no area fits, ask the owner rather than inventing a new one. What
belongs in each area is in `docs/agents/triage-labels.md`.
`.github/workflows/issue-areas.yml` checks the rule daily and fails, naming the issues, when it
stops holding.

State is separate from area: the triage labels above, plus `needs-rewrite` for an issue whose
direction changed and which must be re-specified against `docs/product-vision.md` before anyone
works on it.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.

### Marketing and brand

Read `docs/marketing-and-brand.md` before marketing, landing-page, or visual-identity work.
It records agreed direction, exploratory proposals, and open decisions.

### Competitive position

Before recommending or deciding anything on product shape, ticket priority, pricing,
positioning, the coach, or matching, read `docs/research/beating-both.md`, and name which of
its insights the change serves or conflicts with. The owner asked for this on 2026-09-10. It
says where we can beat Prep Room and caddie.careers: the loop from inbox outcomes back into
matching and preparation, with #80 as the join. It also says what not to copy from them.

Competitor facts live in `docs/research/preproom-teardown.md` and
`docs/research/caddie-careers-teardown.md`, dated as fetched. They go stale: Caddie's pricing
changed between two reads a day apart. Re-fetch before quoting a competitor's price or
feature, and update the teardown when it has changed.

### Product direction

Read [`docs/product-vision.md`](docs/product-vision.md) before recommending or deciding anything
about what the user sees, decides or is sent — and before shaping engine work, whose
requirements are collected in its "What the engine must do" section. It records the owner's
decisions (D1, D2, …) with their reasons. When it disagrees with any older document, it wins.

Three standing rules from the owner:

- **No backward compatibility with the Telegram bot or the daily digest** (2026-09-10). Nobody
  uses them; only the owner has access to the system. Do not shape engine work around keeping
  them working, and treat acceptance criteria such as "what the user receives is unchanged" as
  void unless the owner re-affirms one. This removes compatibility constraints, not rigour:
  engine behaviour is still tested and measured.
- **Nothing from Relay is kept** (2026-09-11) — not its code, not its schema. It was an early
  proof of concept; the coach is designed fresh. Relay stays deployed for one reason only: the
  engine reads the user's CV, cover letter and provider keys from Relay's Profile screen. Until
  something replaces that screen, do not remove Relay or the tables behind it.
- **Nothing sends an application for the user** (D2). The user always presses the final send,
  on the employer's own form.

### Dated records are not current truth

Specs, plans and task reports named with a date — under `apps/*/docs/superpowers/`,
`apps/*/docs/plans/` and `apps/*/.superpowers/sdd/` — are records of what was decided at the
time. They are not rewritten when things change, so many are stale. Read them for why something
was built the way it was, never as a statement of how the product or the system works today.
Current truth lives in `docs/product-vision.md`, `docs/adr/`, `CONTEXT.md`, the `AGENTS.md`
files, and the code.

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

Every test that touches the database asks for the `_stack_env` fixture. At session start,
`apps/job-hunter/tests/conftest.py` now requires all four core stack variables. If all are absent,
pytest stops once before collection and names them; if only some are present, it does the same and
names the missing ones. A partial environment is a configuration error even when an opt-out is
present, because silently accepting a typo would recreate the original false pass.

`SUPABASE_TEST_DB_URL` is the fourth, and it became required with #179. It is ingestion's
direct, privileged connection, and since the shared tables are writable only by that role, a
stack without it cannot persist a posting, a facet, a company or a board at all — so a suite
run against one would be a suite in which every write path under test is unreachable, which is
the same failure class this guard exists to stop.

A deliberate non-database run has to say so explicitly. Either form is supported:

```bash
JOB_HUNTER_ALLOW_MISSING_STACK=1 pnpm job-hunter:test
pnpm job-hunter:test --allow-missing-stack
```

Both opt-outs are honored only when none of `SUPABASE_TEST_URL`,
`SUPABASE_TEST_PUBLISHABLE_KEY`, `SUPABASE_TEST_SIGNING_KEY_B64` and `SUPABASE_TEST_DB_URL`
is set. In that deliberate
mode `_stack_env` skips store-backed tests and says which opt-out authorized it. With all three
variables present, missing stack configuration cannot skip a store-backed test: the guard either
lets that fixture run or has already failed the session.

Do not turn this into a global `0 skipped` assertion. Legitimate skips exist and vary by branch;
the enforced claim is narrower: **store-backed tests were not skipped because the core stack
environment was missing.** This is the same failure class as a workspace without its own `.venv`,
one layer down — both otherwise produce a green run that is not evidence.

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
re-apply them before repairing, so the row and the objects agree — **unless a passing suite
already asserts the installed objects.** In that case they are demonstrably the ones your
migration produces, the row is the only thing missing, and dropping and re-applying churns state
that three other worktrees are reading, for no gain. Record the row and leave the objects alone.
And **never repair a version that is not yours.** A row claiming a version somebody else's migration owns is worse than a
missing one: a missing row gets investigated, a wrong row gets trusted.

**And a missing row is not merely absent — it is read as belonging to the highest recorded
version.** That is the failure that actually occurred on 2026-09-09, and this section described it
wrongly at first. Stage-queue objects were hand-applied with *no row at all*; the highest recorded
version was a different, since-merged migration, and a reader who checked the ledger concluded the
stage functions had arrived with it. The inference is sound and the answer was wrong. Recording
your own row is therefore a requirement rather than a courtesy: it is what stops the next agent's
cheap check producing a confident wrong attribution.

**When the ledger agrees with disk and the schema still looks wrong, diff the schema itself.** The
two checks above both read `supabase_migrations.schema_migrations`, and `pg_proc` only sees
function bodies. None of them detects a schema changed **by hand, with no migration file and no
row** — a dropped column, an added constraint. In that state the ledger is byte-identical to
`ls supabase/migrations`, nothing is unexplained, and both prescribed checks report **clean**
while `job_hunter_jobs` is missing columns your tree says it has. That has happened here.

```bash
supabase db diff --local
```

It builds a shadow database from `supabase/migrations` and compares the live local database to it.
**Empty output means the schema is what the migrations say it is.** Anything else is the
difference, and a difference nobody's branch explains means the schema was edited directly. Run it
before concluding that a missing column or a failed constraint is "pre-existing" — that claim has
been made and withdrawn twice in one day, both times from a contaminated stack.

**"Not mine" and "pre-existing" are different claims, and only one of them is cheap to defend.**
*Not mine* is provable from your own diff: the file is byte-identical to `main`, your branch
touches nothing in that area, therefore the failure is not yours. *Pre-existing* asserts something
about `main` itself, and on a shared stack you cannot see `main` — you see a database several
worktrees have written to. Claiming the second when you have only established the first is how a
false report reaches the board, and it has happened twice.

So say what you actually know: **"unconfirmed as a live `main` run, but provably not mine"** is
both honest and enough to keep going. If the claim about `main` matters, CI on a clean stack is
what settles it, not a local run.

**A hand-applied change with no migration file is not a shortcut, it is an unrecorded schema.**
Recording a ledger row only helps when a file exists to record. If you change the schema directly
while developing, there is nothing for the next agent's checks to find, and the state is only
recoverable with `pnpm db:reset` — which drops every peer's unmerged migration. Write the
migration first, then apply it.

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
  `seed_pool: all 16 seed user slots are in use` means someone else holds it; both report progress
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
applying the seed to every stack that already exists — test writes go through PostgREST with a
minted JWT, which cannot insert into `auth.users`, so the pool cannot grow itself.
`psql "$SUPABASE_TEST_DB_URL" -f supabase/seed.sql` is enough and is safe on the shared stack (the
insert is `on conflict do nothing`); `pnpm db:reset` also works but wipes everyone's data.
Each xdist worker claims its own slot, so the pool is sized for workers, not runs: 16 covers a
10-core `-n auto` run with room for a second worktree. A stack that missed the reseed fails every
test on a new slot with `job_hunter_jobs_user_id_fkey` / `Key is not present in table "users"` —
hundreds of failures that read like a broken store. `tests/test_seed_pool.py` fails if the two
files ever disagree, but it cannot see what the running stack holds.

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

### Read raw files when reviewing — `git diff` and `cat` come back as paraphrase

A hook in this environment rewrites `git`, `cat`, `sed` and `grep` output into a token-compressed
summary. It is not a formatting difference. **The summary is prose about the file, and it is
confidently wrong.**

This has now produced two failures that look unrelated and share one cause:

- `git diff > wip.patch` writes a summary rather than a unified diff, so `git apply` rejects it
  with `error: No valid patches in input` — and the working tree changes are gone if they were
  reverted in the same breath.
- A review pass reading a diff through the hook reasoned about **code that does not exist**. It
  reported a function as `security invoker` when the file says `security definer`, and quoted a
  `grant execute ... to authenticated` that had been replaced by a `revoke`. Those are not
  degraded findings; they are invented facts, and they would have been acted on.

Someone who has only met the first will not recognise the second. Empty or malformed output
announces itself. A plausible fabrication does not.

**The rule.** Any review, audit, or diff pass — not only a formal code review — reads files with
the `Read` and `Grep` tools, never through `cat`, `sed`, `grep` or `git diff`. This overrides the
general preference for doing work through the shell: that preference exists to save context, and
here it costs correctness. Say so explicitly when dispatching a reviewer, because a subagent
cannot tell it is being handed a summary.

**The tell, so you can catch yourself.** If a symbol, grant, or line you are about to quote cannot
be found by a raw read of the file it supposedly came from, what you read was paraphrase. Check
before quoting, not after.

When a real diff is genuinely needed — a patch file, a byte-exact comparison — route it through
`rtk proxy git diff` or use `git format-patch`, and confirm the output starts with `diff --git`
before trusting it. To park uncommitted work, make a temporary WIP commit rather than writing a
patch file.

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
- **Make the placeholder visibly not a timestamp** — `29999999000000`, or your branch name in the
  filename. Do **not** reach for the next number after the highest on `main`: that is what every
  other agent reaches for too, so it is the one value guaranteed to collide. On 2026-09-09 two
  branches independently chose `20260909200000` because it follows `190000`, and the shared stack
  has one ledger, so `supabase migration list` showed that version applied to both agents while
  the objects belonged to only one. Each read it as evidence about its own migration and neither
  was right. **A colliding placeholder does not merely fail to help — it manufactures evidence
  that lies.**
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

# Contributing

Dev commands, per-app conventions, and repo boundaries live in [AGENTS.md](AGENTS.md) — read
that first, both here and under `apps/job-hunter/` or `apps/relay/` for the app you're touching.
This file covers the workflow around a change: branches, commits, issues, PRs.

## Before you start

A change that spans Job Hunter, Relay, and the schema belongs in one branch and one PR — that's
the reason this is a monorepo (see AGENTS.md). Don't split a cross-cutting change across
separate PRs per app.

## Branches

Never commit directly to `main`. Branch with a `type/short-description` name, e.g.
`fix/job-hunter-catastrophic-eval-exit-code`.

## Commits

Loosely [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `chore:`,
`docs:`, `ci:`. An app-scoped prefix (`job-hunter:`, `relay:`) is fine in place of the type when
the change is entirely local to that app.

## Before opening a PR

Run whatever this change touches:

```bash
pnpm relay:test          # apps/relay changed
pnpm job-hunter:test     # apps/job-hunter changed
pnpm test                # both
```

There's no separate lint gate for Job Hunter — pytest is its whole CI bar. Relay CI also runs
lint and build.

## Pull requests

Use the PR template. Reference the issue with `Closes #123` if one exists — not every change
needs an issue first, but anything more than a small, obvious fix is easier to review with one.

If a change needs something applied outside git (a Supabase migration pushed, a secret set, a
webhook re-registered), say so explicitly in the PR — don't leave it implicit.

## Issues

Bug and feature templates are there as structure, not a checklist to fill mechanically — skip
sections that don't apply. For a bug, a specific reproducing example (a run ID, a request, a log
line) is worth more than a general description.

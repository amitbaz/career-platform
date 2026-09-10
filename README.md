# Career Platform

Monorepo for the career platform: the **Job Hunter** Python service — the search-and-match
engine, which is the product — the legacy **Relay** Next.js proof of concept, and the shared
**Supabase** schema.

What the product is and where it is going: [docs/product-vision.md](docs/product-vision.md).
Relay is not the base for the product. It stays deployed only because the engine reads the
user's CV, cover letter and provider keys from its Profile screen.

This repository is the canonical home for platform work. It replaces the standalone
`amitbaz/job-hunter-bot` and `amitbaz/interviewer-app` repositories, whose Git history was
merged in here.

## Layout

```text
career-platform/
├── apps/
│   ├── job-hunter/     # Python service (own pyproject.toml / venv)
│   └── relay/          # Next.js app (pnpm workspace package)
├── supabase/           # Shared platform migrations and SQL tests
├── .github/workflows/  # CI + scheduled Job Hunter runs
├── pnpm-workspace.yaml
└── package.json        # Root workspace and convenience scripts only
```

Each app keeps its own `README.md` and `AGENTS.md` with app-specific detail.

JavaScript dependencies are managed by **pnpm workspaces** (single root `pnpm-lock.yaml`).
Job Hunter keeps its own independent Python toolchain — pnpm is not used for Python.

## Getting started

### Relay (Next.js)

```bash
pnpm install            # installs the whole JS workspace from the repo root
pnpm relay:dev          # dev server
pnpm relay:test         # vitest
pnpm relay:lint         # eslint
pnpm relay:build        # production build
```

Anything can also be run directly from `apps/relay` with `pnpm dev`, `pnpm test`, etc.

Copy `apps/relay/.env.example` to `apps/relay/.env.local` and fill it in.

### Job Hunter (Python 3.12+)

```bash
cd apps/job-hunter
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,webhook]'
.venv/bin/pytest -q
```

From the repo root, `pnpm job-hunter:install` creates that `.venv` and installs into it, and
`pnpm job-hunter:test` runs pytest from it.

Copy `apps/job-hunter/.env.example` to `apps/job-hunter/.env` and fill it in.

### Everything

```bash
pnpm test               # Relay vitest + Job Hunter pytest
```

## Supabase

Migrations live in `supabase/migrations` and are owned by the platform, not by Relay. Run the
Supabase CLI from the repository root so it picks up `supabase/`.

## Deployment

Both apps deploy from this repository as separate Vercel projects. Each project's
**Root Directory** must point at its app:

| Vercel project | Root Directory     | Notes                                            |
| -------------- | ------------------ | ------------------------------------------------ |
| Relay          | `apps/relay`       | Next.js; keep "Include files outside root directory" on so the workspace lockfile is used. |
| Job Hunter     | `apps/job-hunter`  | Flask webhook via `apps/job-hunter/vercel.json`.  |

The engine's ingestion and enrichment stages run as Render cron services defined in
[`render.yaml`](render.yaml).

The Telegram webhook dispatches GitHub workflows through `GITHUB_REPOSITORY`, which must now
be set to `amitbaz/career-platform`.

The configuration steps still outstanding from the repository consolidation — Actions secrets,
Vercel project settings, the Telegram webhook — are tracked in
[docs/monorepo-migration.md](docs/monorepo-migration.md). The Job Hunter state artifact is no
longer among them: Job Hunter's state lives in Postgres, and both workflows stopped uploading
that artifact. See the cutover runbook in [apps/job-hunter/README.md](apps/job-hunter/README.md).

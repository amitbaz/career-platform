# Career Platform

Monorepo for the career platform: the **Job Hunter** Python service — the search-and-match
engine, which is the product — and the shared **Supabase** schema.

What the product is and where it is going: [docs/product-vision.md](docs/product-vision.md).
See [ADR-0002](docs/adr/0002-modular-engine-and-supabase-as-backend.md) for the engine/Supabase
architecture. Relay, an early Next.js proof of concept, was deleted (issue #286); nothing from it
was kept, code or schema.

This repository is the canonical home for platform work. It replaces the standalone
`amitbaz/job-hunter-bot` and `amitbaz/interviewer-app` repositories, whose Git history was
merged in here.

## Layout

```text
career-platform/
├── apps/
│   └── job-hunter/     # Python service (own pyproject.toml / venv)
├── supabase/           # Shared platform migrations and SQL tests
├── .github/workflows/  # CI + scheduled Job Hunter runs
├── pnpm-workspace.yaml # Empty today; the workspace for JS apps yet to arrive
└── package.json        # Root workspace and convenience scripts only
```

Each app keeps its own `README.md` and `AGENTS.md` with app-specific detail.

## Getting started

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
pnpm test               # Job Hunter pytest
```

## Supabase

Migrations live in `supabase/migrations` and are owned by the platform. Run the Supabase CLI
from the repository root so it picks up `supabase/`.

## Deployment

Job Hunter deploys from this repository as a Vercel project. Its **Root Directory** must point
at the app:

| Vercel project | Root Directory     | Notes                                            |
| -------------- | ------------------ | ------------------------------------------------ |
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

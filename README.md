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

The engine runs on Render. Its ingestion and enrichment stages are cron services defined in
[`render.yaml`](render.yaml).

Nothing in this repository deploys to Vercel. The only Vercel project was the Flask webhook
that served the Telegram bot, and it was deleted with the bot; the `job-hunter-bot` Vercel
project itself can go once that deletion is on `main`.

The repository consolidation that brought this code here is finished, and most of what it
configured has since been deleted or moved to Render. Its runbook,
[docs/monorepo-migration.md](docs/monorepo-migration.md), is kept as a record of what moved and
how, with each superseded step marked. Job Hunter's state lives in Postgres; see the cutover
runbook in [apps/job-hunter/README.md](apps/job-hunter/README.md).

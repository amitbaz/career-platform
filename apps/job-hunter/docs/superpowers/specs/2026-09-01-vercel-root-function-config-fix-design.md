# Vercel Flask Deployment Config Fix Design

## Problem

The first production deployment failed with:

```text
The pattern "main.py" defined in `functions` doesn't match any Serverless Functions inside the `api` directory.
```

An initial attempted fix removed the `functions` block. That made the Vercel build reach `READY`, but the deployment contained no Flask function: the build completed in ~175 ms without installing Python dependencies, and `GET /health` returned Vercel's platform-level 404.

This second observation establishes the actual root cause: the project was imported with the **Other** framework preset, so Vercel did not activate its Flask backend build pipeline. The root `main.py` entrypoint itself is valid for Vercel Flask projects, and current Vercel Flask documentation supports function-specific configuration for `main.py` when the framework is Flask.

## Goal

Make the existing root-level Flask webhook deploy as an actual Vercel Python Function, without moving routes into `/api`, changing webhook behavior, or introducing new infrastructure.

## Decision

Declare the Vercel framework explicitly in repository configuration:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "framework": "flask",
  "installCommand": "pip install -e '.[webhook]'",
  "functions": {
    "main.py": {
      "maxDuration": 30,
      "excludeFiles": "{tests/**,.superpowers/**,docs/**,var/**}"
    }
  }
}
```

Preserve:

- root `main.py` and `app = create_app()`;
- `[tool.vercel] entrypoint = "main:app"` in `pyproject.toml`;
- webhook routes `/health` and `/telegram/webhook`;
- existing environment variables;
- SQLite/GitHub artifact navigation storage;
- Telegram callback behavior.

The repository-level `framework: "flask"` setting overrides the dashboard's initial **Other** preset and makes deployments reproducible from Git configuration.

## Scope

Change only Vercel deployment configuration, regression coverage, and deployment documentation. Runtime webhook/domain code must not change.

## Files

- Modify `vercel.json` — explicitly select Flask and restore the supported root `main.py` function settings.
- Modify `tests/test_vercel_config.py` — lock the Flask deployment shape.
- Modify `docs/telegram-job-navigator.md` — document the required Flask framework declaration and both observed failure modes.

## Regression Test

The test loads `vercel.json` and asserts:

1. `framework` is exactly `flask`.
2. `installCommand` remains `pip install -e '.[webhook]'`.
3. `functions.main.py` exists.
4. `functions.main.py.maxDuration` remains 30 seconds.

This guards against both regressions we observed: the original unmatched function-pattern failure under **Other**, and the empty READY deployment created by removing the function mapping without enabling Flask.

## Verification

Success requires all of the following:

- focused config and entrypoint tests pass;
- full repository suite passes;
- Vercel preview build installs Python/webhook dependencies and deploys a Flask function;
- preview `GET /health` returns HTTP 200 with `{"ok": true}`;
- no runtime webhook code changes are present in the branch diff.

## Non-goals

- No Supabase changes.
- No Telegram callback behavior changes.
- No move from Flask to another framework.
- No move from root `main.py` to `/api`.
- No changes to the Interviewer App Vercel project.

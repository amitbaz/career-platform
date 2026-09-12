# Telegram Vercel Webhook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the Telegram job navigator callback receiver as a Vercel-compatible Flask/Python Function while isolating navigation persistence behind a repository interface that can later switch from GitHub-artifact SQLite to Supabase.

**Architecture:** Keep GitHub Actions and SQLite unchanged. Introduce `NavigationSessionRepository` and a current `GitHubArtifactNavigationRepository`; make the Flask webhook depend only on that interface. Add a small Vercel Flask entrypoint and deployment config. Supabase remains documentation-only in this change.

**Tech Stack:** Python 3.12, Flask, Vercel Python Functions, GitHub Actions artifacts, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-telegram-vercel-webhook-design.md`

## Global Constraints

- Do not migrate Job Hunter Bot state to Supabase in this change.
- Do not add a Supabase client dependency or schema migration.
- Keep GitHub Actions as the scheduled job-search runtime.
- Do not modify the Interviewer App deployment.
- Keep Telegram callback payloads free of credentials.
- Keep artifact SQLite access read-only inside the webhook.
- Preserve existing Telegram navigator copy and behavior.

---

## Implementation record

The implementation followed the repository-boundary and Vercel-entrypoint design below. One deployment-specific adjustment was made after checking current Vercel Python documentation: Flask remains intentionally isolated in the project's `webhook` optional dependency so the cron bot does not install it unnecessarily, and `vercel.json` explicitly installs that extra:

```json
{
  "installCommand": "pip install -e '.[webhook]'"
}
```

This is the supported deployment path. Gunicorn and the previous Docker runtime are removed.

### Task 1: Isolate navigation persistence behind a repository

**Files:**
- Create: `src/job_hunter/navigation_repository.py`
- Create: `tests/test_navigation_repository.py`

**Interfaces:**
- Produces: `NavigationSessionRepository.get_session(session_id: str) -> NavigationSession | None`
- Produces: `GitHubArtifactNavigationRepository.get_session(session_id: str) -> NavigationSession | None`
- Consumes: `GitHubArtifactStateLoader.load_latest()`, `JobStore(..., read_only=True)`, `navigation_store.get_navigation_session()`

- [x] Write failing repository tests for success, missing snapshot/session, read-only SQLite, and loader failure.
- [x] Verify RED.
- [x] Implement `NavigationSessionRepository` and `GitHubArtifactNavigationRepository`.
- [x] Verify GREEN.

### Task 2: Make the webhook depend on the repository interface

**Files:**
- Modify: `src/job_hunter/telegram_webhook.py`
- Modify: `tests/test_telegram_webhook.py`

**Interfaces:**
- Consumes: `NavigationSessionRepository.get_session(session_id)`
- Produces: `create_app(settings=None, navigation_repository=None, telegram=None) -> Flask`

- [x] Replace artifact-loader test coupling with a fake navigation repository.
- [x] Verify RED because `create_app` still accepted `state_loader`.
- [x] Make the route call only `navigation_repository.get_session(session_id)`.
- [x] Verify GREEN.

### Task 3: Add the Vercel Python/Flask deployment adapter

**Files:**
- Create: `main.py`
- Create: `vercel.json`
- Create: `tests/test_vercel_entrypoint.py`
- Modify: `pyproject.toml`
- Delete: `Dockerfile.telegram-webhook`

**Interfaces:**
- Produces: root-level `main.app`, a Flask app discoverable by Vercel.
- Consumes: `job_hunter.telegram_webhook.create_app()`.

- [x] Add a failing entrypoint import test and verify RED (`ModuleNotFoundError: main`).
- [x] Add root `main.py` and `[tool.vercel] entrypoint = "main:app"`.
- [x] Remove Gunicorn from the webhook extra and remove the Dockerfile.
- [x] Add `vercel.json` with 30-second max duration and bundle exclusions.
- [x] Add explicit Vercel install command `pip install -e '.[webhook]'` after validating current Vercel dependency behavior.
- [x] Verify GREEN.

### Task 4: Make Vercel the documented deployment path and record Supabase migration

**Files:**
- Modify: `docs/telegram-job-navigator.md`
- Modify: `.env.example`

- [x] Separate daily-bot and server-side webhook variables in `.env.example`.
- [x] Replace Docker deployment instructions with a dedicated Job Hunter Vercel project.
- [x] Document environment variables, health check, Telegram registration, synchronization behavior, and troubleshooting.
- [x] Record migration phases: current SQLite artifact reads; gradual Supabase writes; `SupabaseNavigationRepository` cutover; optional Supabase Edge Function move.
- [x] State that Telegram payloads/card rendering do not change during storage migration.

### Task 5: Full verification

- [x] Install `.[test,webhook]` in CI.
- [x] Run the complete test suite.
- [x] Result: **326 passed, 0 failed**.
- [x] Compare against `feature/telegram-job-navigator-impl` and confirm the diff is limited to the Vercel/storage-boundary architecture adjustment and documentation.

## Integration order

This branch is intentionally stacked on top of `feature/telegram-job-navigator-impl` / PR #9.

1. Merge PR #9 (`feature/telegram-job-navigator-impl`) first.
2. Retarget the Vercel follow-up PR to `main` after #9 lands.
3. Merge the Vercel follow-up after its PR CI remains green against `main`.
4. Create/configure the dedicated Vercel project and register Telegram only after the production code is on `main`.

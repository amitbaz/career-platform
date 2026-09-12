#!/usr/bin/env bash
# Prepare a fresh worktree or clone so its tests and dev servers work.
#
# Everything copied here is git-ignored, so a new worktree has none of it:
#
#   apps/job-hunter/.env           Job Hunter's local environment
#   supabase/signing_keys.json     without it `supabase start` will not boot at all,
#                                  because config.toml sets signing_keys_path
#
# Never run `pnpm db:key` in a worktree to produce that signing key. It mints a new
# `kid`, and the integration tests then 401 against a stack booted with the other one.
# Copy the main checkout's key, which is what this script does.
#
# The Python virtualenv is per-checkout on purpose: `pnpm job-hunter:test` runs
# `.venv/bin/python`, so a worktree without its own .venv silently tests whichever
# source tree the ambient interpreter resolves to, and reports it green.
#
# SOURCE_ROOT defaults to Superset's SUPERSET_ROOT_PATH when set, so this works both
# as a workspace setup command and when run by hand from a plain clone.
set -euo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-${SUPERSET_ROOT_PATH:-}}"
if [ -z "$SOURCE_ROOT" ]; then
  echo "workspace-setup: set SOURCE_ROOT (or SUPERSET_ROOT_PATH) to the main checkout" >&2
  exit 1
fi
if [ ! -d "$SOURCE_ROOT" ]; then
  echo "workspace-setup: SOURCE_ROOT does not exist: $SOURCE_ROOT" >&2
  exit 1
fi

copy() {
  local from="$SOURCE_ROOT/$1" to="$1"
  if [ ! -e "$from" ]; then
    echo "workspace-setup: missing in the main checkout, skipping: $1" >&2
    return 0
  fi
  mkdir -p "$(dirname "$to")"
  cp -R "$from" "$to"
  echo "workspace-setup: copied $1"
}

pnpm install
copy apps/job-hunter/.env
copy supabase/signing_keys.json

# supabase/.temp is deliberately NOT copied. It carries the link to the live project,
# and a linked worktree can `supabase db push` to production from an unmerged branch --
# which, because Supabase refuses a migration filename that sorts before one already
# applied, can permanently block a migration sitting on another branch. Migrations are
# applied from CI when they reach main (#190); a worktree never pushes.

pnpm job-hunter:install
echo "workspace-setup: done"

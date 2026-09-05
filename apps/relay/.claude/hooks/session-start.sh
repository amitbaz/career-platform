#!/bin/bash
#
# SessionStart hook for Claude Code on the web.
#
# Cloud sessions start from a fresh container clone, so two things that exist on
# a developer's own machine are absent here:
#   1. node_modules  - needed before `npm run lint` or `npm test` will run.
#   2. The superpowers skills - installed globally on local machines, which means
#      they live only in that filesystem and never reach a remote container. The
#      repo already tracks superpowers *output* (docs/superpowers, .superpowers/sdd),
#      so the skills that produce it need to be present for that work to continue.
#
# Runs synchronously: skills must be on disk before the agent loop begins
# discovering them, and dependencies must exist before any test/lint command.
set -euo pipefail

# Local machines already have both, and clobbering a developer's global
# superpowers install would be rude. Remote only.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
SUPERPOWERS_REPO="${SUPERPOWERS_REPO:-https://github.com/obra/superpowers.git}"
SUPERPOWERS_CACHE="${HOME}/.cache/superpowers"
SKILLS_DEST="${HOME}/.claude/skills"

# Network calls fail intermittently behind the agent proxy; a few backed-off
# retries turn a flaky startup into a reliable one.
retry() {
  local attempt=1 max=4 delay=2
  until "$@"; do
    if [ "$attempt" -ge "$max" ]; then
      return 1
    fi
    echo "  retry ${attempt}/${max} failed, waiting ${delay}s..." >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
  return 0
}

echo "[session-start] Installing npm dependencies..."
cd "$PROJECT_DIR"
# `ci` over `install`: `npm install` resolves `^` ranges afresh and rewrites
# package-lock.json, which would leave every cloud session with a spuriously
# dirty lockfile. `ci` installs exactly what the lockfile pins and never
# writes to it. The container is cached after this hook, so the expensive
# cold install happens once; warm starts skip it entirely.
if [ -d node_modules ]; then
  echo "  node_modules present, skipping install"
else
  retry npm ci --no-audit --no-fund
fi

echo "[session-start] Installing superpowers skills..."
if [ -d "${SUPERPOWERS_CACHE}/.git" ]; then
  # Track upstream rather than pinning, so cloud sessions match the version a
  # developer gets from a global install.
  retry git -C "$SUPERPOWERS_CACHE" fetch --depth 1 origin HEAD
  git -C "$SUPERPOWERS_CACHE" reset --hard FETCH_HEAD --quiet
else
  rm -rf "$SUPERPOWERS_CACHE"
  mkdir -p "$(dirname "$SUPERPOWERS_CACHE")"
  retry git clone --depth 1 --quiet "$SUPERPOWERS_REPO" "$SUPERPOWERS_CACHE"
fi

if [ ! -d "${SUPERPOWERS_CACHE}/skills" ]; then
  echo "[session-start] ERROR: no skills/ directory in ${SUPERPOWERS_REPO}" >&2
  exit 1
fi

# Copy only the skill directories superpowers ships, leaving anything else in
# ~/.claude/skills (account-synced skills, session-start-hook) untouched.
mkdir -p "$SKILLS_DEST"
installed=0
for skill_dir in "${SUPERPOWERS_CACHE}"/skills/*/; do
  [ -f "${skill_dir}SKILL.md" ] || continue
  name="$(basename "$skill_dir")"
  rm -rf "${SKILLS_DEST:?}/${name}"
  cp -R "$skill_dir" "${SKILLS_DEST}/${name}"
  installed=$((installed + 1))
done

version="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "${SUPERPOWERS_CACHE}/.claude-plugin/plugin.json" 2>/dev/null | head -1)"
echo "[session-start] Installed ${installed} superpowers skills (v${version:-unknown}) into ${SKILLS_DEST}"
echo "[session-start] Done."

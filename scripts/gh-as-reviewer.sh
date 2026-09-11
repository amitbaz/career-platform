#!/usr/bin/env bash
# Runs a `gh` command authenticated as the reviewer bot account instead of
# the ambient user/session identity. This lets a reviewer agent submit a
# real, separate-identity PR review (approve / request-changes) so GitHub
# branch protection can enforce "approval from someone other than the
# author" even when the dev and reviewer agents both run as the repo
# owner otherwise.
#
# Usage: scripts/gh-as-reviewer.sh pr review 123 --approve --body "..."
#
# Requires a Keychain item (added once, manually, never via this repo):
#   security add-generic-password -a "career-platform-reviewer" \
#     -s "career-platform-reviewer-pat" -w "<PAT>"
set -euo pipefail

TOKEN="$(security find-generic-password -a "career-platform-reviewer" -s "career-platform-reviewer-pat" -w 2>/dev/null || true)"

if [ -z "$TOKEN" ]; then
  echo "gh-as-reviewer: reviewer PAT not found in Keychain (account: career-platform-reviewer, service: career-platform-reviewer-pat)" >&2
  exit 1
fi

GH_TOKEN="$TOKEN" GITHUB_TOKEN="$TOKEN" exec gh "$@"

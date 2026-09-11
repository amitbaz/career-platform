#!/usr/bin/env bash
# Runs `gh`, or for the developer `git`, as one of the repository's bot
# accounts instead of the ambient owner identity. Each role acts under its own
# GitHub account, so the owner's approval counts on a bot-authored PR and the
# reviewer's verdict counts as a separate identity (docs/agents/roles.md).
#
# Usage:
#   scripts/gh-as.sh <developer|reviewer> <gh args...>
#   scripts/gh-as.sh developer git <git args...>
#
# Requires a Keychain item per role, added once by the owner, never via this repo:
#   security add-generic-password -a "career-platform-<role>" \
#     -s "career-platform-<role>-pat" -w "<PAT>"
set -euo pipefail

usage() {
  echo "usage: scripts/gh-as.sh <developer|reviewer> [git] <args...>" >&2
  exit 2
}

[ $# -ge 2 ] || usage
role="$1"
shift
case "$role" in
  developer|reviewer) ;;
  *) usage ;;
esac

account="career-platform-$role"
service="career-platform-$role-pat"
TOKEN="$(security find-generic-password -a "$account" -s "$service" -w 2>/dev/null || true)"
if [ -z "$TOKEN" ]; then
  echo "gh-as: $role PAT not found in Keychain (account: $account, service: $service)" >&2
  exit 1
fi

if [ "$1" = "git" ]; then
  shift
  if [ "$role" != "developer" ]; then
    echo "gh-as: only the developer commits or pushes" >&2
    exit 1
  fi
  identity="$(GH_TOKEN="$TOKEN" gh api user --jq '"\(.login)\t\(.id)+\(.login)@users.noreply.github.com"')"
  login="${identity%%$'\t'*}"
  email="${identity#*$'\t'}"
  # The empty helper clears the machine's own helpers (osxkeychain), so the
  # bot's token is the only credential offered and is never stored.
  export GH_AS_TOKEN="$TOKEN"
  exec env GIT_AUTHOR_NAME="$login" GIT_AUTHOR_EMAIL="$email" \
    GIT_COMMITTER_NAME="$login" GIT_COMMITTER_EMAIL="$email" \
    git -c credential.helper= \
      -c 'credential.helper=!f() { test "$1" = get && echo username=x-access-token && echo "password=$GH_AS_TOKEN"; }; f' \
      "$@"
fi

GH_TOKEN="$TOKEN" GITHUB_TOKEN="$TOKEN" exec gh "$@"

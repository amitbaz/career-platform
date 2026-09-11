#!/usr/bin/env bash
# Self-test for scripts/gh-as.sh. Stubs `security` and `gh`; uses the real git.
# Run: bash scripts/tests/gh-as.test.sh
set -euo pipefail

# A credential regression should fail fast, not hang waiting on a terminal prompt.
export GIT_TERMINAL_PROMPT=0

root="$(cd "$(dirname "$0")/../.." && pwd)"
script="$root/scripts/gh-as.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"

cat > "$work/bin/security" <<'EOF'
#!/usr/bin/env bash
# Stub of: security find-generic-password -a <account> -s <service> -w
service=""
while [ $# -gt 0 ]; do
  case "$1" in -s) service="$2"; shift ;; esac
  shift
done
[ "${STUB_MISSING:-}" = "$service" ] && exit 44
echo "token-for-$service"
EOF

cat > "$work/bin/gh" <<'EOF'
#!/usr/bin/env bash
if [ "$1 $2" = "api user" ]; then
  if [ "${STUB_GH_FAIL:-}" = "1" ]; then
    exit 1
  fi
  printf 'amitbaz-developer\t123+amitbaz-developer@users.noreply.github.com\n'
  exit 0
fi
echo "GH_TOKEN=$GH_TOKEN ARGS=$*"
EOF
chmod +x "$work/bin/security" "$work/bin/gh"
export PATH="$work/bin:$PATH"

# Simulate an ambient global gitconfig (e.g. osxkeychain) that already answers
# credential requests for every host with the *owner's* credentials. The
# "developer git answers credential requests with its own token" check below
# only proves anything if this stands in the way and gets reset.
cat > "$work/gitconfig-global" <<'EOF'
[credential]
	helper = !f() { test "$1" = get && echo username=owner && echo password=owner-token; }; f
EOF
export GIT_CONFIG_GLOBAL="$work/gitconfig-global"

fail=0
check() {
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1"; echo "  expected: $2"; echo "  actual:   $3"; fail=1
  fi
}

check "reviewer runs gh with the reviewer token" \
  "GH_TOKEN=token-for-career-platform-reviewer-pat ARGS=pr view 1" \
  "$("$script" reviewer pr view 1)"

check "the reviewer shim still works" \
  "GH_TOKEN=token-for-career-platform-reviewer-pat ARGS=pr view 1" \
  "$("$root/scripts/gh-as-reviewer.sh" pr view 1)"

check "developer git answers credential requests with its own token" \
  "$(printf 'protocol=https\nhost=github.com\nusername=x-access-token\npassword=token-for-career-platform-developer-pat')" \
  "$(printf 'protocol=https\nhost=github.com\n\n' | "$script" developer git credential fill)"

out="$(printf 'protocol=https\nhost=evil.example.com\n\n' | "$script" developer git credential fill 2>&1 || true)"
case "$out" in
  *"password=token-for-career-platform-developer-pat"*)
    echo "FAIL a non-github host gets no bot credential"
    echo "  actual: $out"
    fail=1
    ;;
  *)
    echo "ok   a non-github host gets no bot credential"
    ;;
esac

git init -q "$work/repo"
(cd "$work/repo" && "$script" developer git -c commit.gpgsign=false commit -q --allow-empty -m probe)
check "developer commits are authored by the bot" \
  "amitbaz-developer <123+amitbaz-developer@users.noreply.github.com>" \
  "$(git -C "$work/repo" log -1 --format='%an <%ae>')"
check "developer commits are committed by the bot" \
  "amitbaz-developer <123+amitbaz-developer@users.noreply.github.com>" \
  "$(git -C "$work/repo" log -1 --format='%cn <%ce>')"

status=0; out="$("$script" reviewer git push 2>&1)" || status=$?
check "reviewer may not use git mode" "1" "$status"
check "reviewer git refusal says why" "gh-as: only the developer commits or pushes" "$out"

status=0; out="$("$script" developer 2>&1)" || status=$?
check "developer with no further args exits 2" "2" "$status"

status=0; out="$(STUB_MISSING=career-platform-developer-pat "$script" developer pr list 2>&1)" || status=$?
check "a missing PAT exits 1" "1" "$status"
check "a missing PAT names the Keychain entry" \
  "gh-as: developer PAT not found in Keychain (account: career-platform-developer, service: career-platform-developer-pat)" \
  "$out"

status=0; out="$(STUB_GH_FAIL=1 "$script" developer git -c commit.gpgsign=false commit -q --allow-empty -m probe2 2>&1)" || status=$?
check "a failing gh api user exits 1" "1" "$status"
check "a failing gh api user names the failure" \
  "gh-as: could not read the developer account with its PAT" \
  "$out"

status=0; "$script" owner pr list >/dev/null 2>&1 || status=$?
check "an unknown role exits 2" "2" "$status"

exit "$fail"

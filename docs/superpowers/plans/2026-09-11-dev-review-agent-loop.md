# Developer and reviewer agent loop — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Delivery runs between a developer bot and a reviewer bot that talk to each other, and the owner only triages, checks and merges.

**Architecture:** GitHub carries identity, enforcement and the record: two bot accounts, a two-approval ruleset, CODEOWNERS review requests and an assignment workflow. One agent-neutral document (`docs/agents/roles.md`) defines both roles. Thin `/dev` and `/reviewer` launchers start a session in a role for Claude and Codex. MemPalace logstream events are doorbells keyed by PR.

**Tech Stack:** bash, `gh` 2.100, git, GitHub Actions, GitHub rulesets REST API, MemPalace logstream, Claude Code skills (`.claude/skills`), Codex skills (`.agents/skills`).

**Spec:** `docs/superpowers/specs/2026-09-11-dev-review-agent-loop-design.md`

## Global Constraints

- Bot logins: `amitbaz-developer`, `amitbaz-reviewer`. Owner login: `amitbaz`.
- Keychain entries: account `career-platform-<role>`, service `career-platform-<role>-pat`.
- MemPalace identities: `cp-developer`, `cp-reviewer`. Stream `project/career-platform`, room `review`, correlation `pr-<number>`.
- Every GitHub write by a role session goes through `scripts/gh-as.sh <role>`. GitHub MCP tools are for reads only in role sessions.
- Loop cap: the same blocker survives 3 rounds, or 5 rounds in total. Silent-peer timeout: 30 minutes. Watcher idle exit: 600000 ms.
- Escalation label: `needs-owner`.
- Launcher names: `dev` and `reviewer`. The reviewer is not called `review`, because Claude Code has a built-in `/review`.
- No agent creates accounts or tokens, or edits the owner's global instruction files.
- Read files with the Read tool when reviewing (AGENTS.md: the RTK hook turns `git diff` and `cat` into paraphrase).

## Owner-only prerequisites

These are not agent tasks. Tasks 3 and 6 check for them and stop if they are missing.

- **P1.** Create the GitHub account `amitbaz-developer`, and invite it as a write collaborator on `amitbaz/career-platform`.
- **P2.** Create a fine-grained PAT for it, limited to this repository: Contents read/write, Pull requests read/write, Issues read/write, Metadata read. Store it with
  `security add-generic-password -a career-platform-developer -s career-platform-developer-pat -w <PAT>`.
- **P3.** In `~/.claude/CLAUDE.md` and `~/.codex/AGENTS.md`, directly under "Your agent identity is …", add:
  `A role command (/dev, /reviewer) assigns this session a role identity (cp-developer or cp-reviewer); it overrides the identity above for that session only.`

## Tickets

| Ticket | Tasks | Depends on |
| --- | --- | --- |
| #269 (A) — Identity and enforcement | 1, 2, 3 | Task 3 needs P1, P2 |
| #270 (B) — Roles and launchers | 4, 5 | #269 merged |
| #271 (C) — First real run | 6 | #270 merged, P3 |

Task 0 is done: the three tickets exist as sub-issues of #247, with native blocked-by edges
(#270 by #269, #271 by #270).

---

### Task 0: Open the three tickets

**Files:** none.

- [ ] **Step 1: Create the tickets**

Use the `make-repo-contribution` skill conventions. Each ticket gets exactly one area label, `area:platform`, and is assigned to `amitbaz`. The body links the spec and this plan, and quotes its task list and verification items.

```bash
gh issue create --title "Agent loop A: bot identities and an enforced two-approval gate" \
  --label area:platform --assignee amitbaz --body-file /tmp/ticket-a.md
gh issue create --title "Agent loop B: developer and reviewer roles with /dev and /reviewer launchers" \
  --label area:platform --assignee amitbaz --body-file /tmp/ticket-b.md
gh issue create --title "Agent loop C: first real ticket through the developer/reviewer loop" \
  --label area:platform --assignee amitbaz --body-file /tmp/ticket-c.md
```

- [ ] **Step 2: Link them to #247.** Add each as a sub-issue of #247 (`gh api repos/amitbaz/career-platform/issues/247/sub_issues -X POST -F sub_issue_id=<issue id>`). Mark B blocked by A, and C blocked by B, with a "Blocked by #…" line in each body.

---

### Task 1: `scripts/gh-as.sh` — run `gh` or `git` as a bot

**Files:**
- Create: `scripts/gh-as.sh`
- Create: `scripts/tests/gh-as.test.sh`
- Modify: `scripts/gh-as-reviewer.sh` (becomes a shim)

**Interfaces:**
- Produces: `scripts/gh-as.sh <developer|reviewer> <gh args…>` runs `gh` with the role's PAT as `GH_TOKEN`/`GITHUB_TOKEN`. `scripts/gh-as.sh developer git <git args…>` runs `git` with the developer's PAT as the only credential and the bot as author and committer. Exit codes: 2 for usage errors, 1 for a missing PAT or the reviewer using git mode, and otherwise the wrapped command's own exit code.

- [ ] **Step 1: Write the failing test**

`scripts/tests/gh-as.test.sh`:

```bash
#!/usr/bin/env bash
# Self-test for scripts/gh-as.sh. Stubs `security` and `gh`; uses the real git.
# Run: bash scripts/tests/gh-as.test.sh
set -euo pipefail

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
  printf 'amitbaz-developer\t123+amitbaz-developer@users.noreply.github.com\n'
  exit 0
fi
echo "GH_TOKEN=$GH_TOKEN ARGS=$*"
EOF
chmod +x "$work/bin/security" "$work/bin/gh"
export PATH="$work/bin:$PATH"

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

status=0; out="$(STUB_MISSING=career-platform-developer-pat "$script" developer pr list 2>&1)" || status=$?
check "a missing PAT exits 1" "1" "$status"
check "a missing PAT names the Keychain entry" \
  "gh-as: developer PAT not found in Keychain (account: career-platform-developer, service: career-platform-developer-pat)" \
  "$out"

status=0; "$script" owner pr list >/dev/null 2>&1 || status=$?
check "an unknown role exits 2" "2" "$status"

exit "$fail"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `bash scripts/tests/gh-as.test.sh`
Expected: exits non-zero on the first `check`, with `scripts/gh-as.sh: No such file or directory`.

- [ ] **Step 3: Write `scripts/gh-as.sh`**

```bash
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
```

Then run `chmod +x scripts/gh-as.sh scripts/tests/gh-as.test.sh`.

- [ ] **Step 4: Turn `scripts/gh-as-reviewer.sh` into a shim**

Replace the whole file with:

```bash
#!/usr/bin/env bash
# Kept so existing instructions keep working. The logic lives in scripts/gh-as.sh.
exec "$(dirname "$0")/gh-as.sh" reviewer "$@"
```

- [ ] **Step 5: Run the test and confirm it passes**

Run: `bash scripts/tests/gh-as.test.sh`
Expected: every line starts with `ok`, and the exit code is 0.

- [ ] **Step 6: Commit**

```bash
git add scripts/gh-as.sh scripts/gh-as-reviewer.sh scripts/tests/gh-as.test.sh
git commit -m "feat(platform): run gh and git as either bot account (#269)"
```

---

### Task 2: Review requests, assignment, and a correct AGENTS.md

**Files:**
- Create: `.github/CODEOWNERS`
- Create: `.github/workflows/assign.yml`
- Create: `.github/rulesets/protect-main.json`
- Modify: `AGENTS.md` (section "A reviewer session must submit its verdict as the bot account, not as you", currently lines 519-542)

**Interfaces:**
- Consumes: `scripts/gh-as.sh` from Task 1.
- Produces: `.github/rulesets/protect-main.json`, which Task 3 applies. Every PR requests `amitbaz` and `amitbaz-reviewer`. Every new issue is assigned to `amitbaz`, and a PR opened by `amitbaz-developer` is assigned to it.

- [ ] **Step 1: Write `.github/CODEOWNERS`**

```
# Every PR requests both approvers the ruleset needs: the reviewer bot reviews
# in depth, the owner does the final check (docs/agents/roles.md).
* @amitbaz @amitbaz-reviewer
```

- [ ] **Step 2: Write `.github/workflows/assign.yml`**

```yaml
# Assignment conventions (docs/agents/roles.md): every new issue belongs to the
# owner, who decides at triage whether the developer bot builds it; a PR the
# developer bot opens is assigned to it. Review requests come from CODEOWNERS.
name: Assign

on:
  issues:
    types: [opened]
  pull_request:
    types: [opened]

permissions:
  issues: write
  pull-requests: write

jobs:
  issue:
    if: github.event_name == 'issues'
    runs-on: ubuntu-latest
    steps:
      - name: Assign the new issue to the owner
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          NUMBER: ${{ github.event.issue.number }}
        run: gh issue edit "$NUMBER" --add-assignee amitbaz

  pull-request:
    if: github.event_name == 'pull_request' && github.event.pull_request.user.login == 'amitbaz-developer'
    runs-on: ubuntu-latest
    steps:
      - name: Assign the developer bot's PR to it
        env:
          GH_TOKEN: ${{ github.token }}
          GH_REPO: ${{ github.repository }}
          NUMBER: ${{ github.event.pull_request.number }}
        run: gh pr edit "$NUMBER" --add-assignee amitbaz-developer
```

- [ ] **Step 3: Lint the workflow**

Run: `command -v actionlint >/dev/null || brew install actionlint; actionlint .github/workflows/assign.yml`
Expected: no output, exit 0.

- [ ] **Step 4: Write `.github/rulesets/protect-main.json`**

This is the live ruleset 22383967 plus the pull-request rule and the admin bypass. It is committed so that the ruleset has a reviewable source.

```json
{
  "name": "Protect Main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": { "include": ["refs/heads/main"], "exclude": [] }
  },
  "bypass_actors": [
    { "actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request" }
  ],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    {
      "type": "required_status_checks",
      "parameters": {
        "do_not_enforce_on_create": false,
        "required_status_checks": [{ "context": "test", "integration_id": 15368 }],
        "strict_required_status_checks_policy": false
      }
    },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 2,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": true,
        "required_review_thread_resolution": true
      }
    }
  ]
}
```

`actor_id` 5 is GitHub's built-in repository role "admin".

- [ ] **Step 5: Correct AGENTS.md**

The new text links `docs/agents/roles.md`, which lands with ticket B (Task 4). Say so in ticket
A's PR description, so the reviewer does not flag it as a broken link.

Replace the section "A reviewer session must submit its verdict as the bot account, not as you" (from its heading up to, but not including, `## Boundaries`) with:

```markdown
### Delivery runs between two bot accounts

Delivery work is split between two roles, each with its own GitHub account: `amitbaz-developer`
builds and opens PRs, and `amitbaz-reviewer` reviews them. The owner triages, does a final check
and merges. How each role works, how they talk to each other, and when they stop and ask the
owner is in `docs/agents/roles.md`. A session takes a role only through `/dev <issue>` or
`/reviewer <pr>`.

`main` is protected by the ruleset in `.github/rulesets/protect-main.json`. It requires the `test`
check, **two approvals** (the reviewer bot's and the owner's), approval of the most recent push,
and every review thread resolved. An approval is dismissed by any later push. GitHub never counts
an author's approval of their own PR, which is why the developer role must author PRs as its bot:
only then can the owner's approval count. The repository admin can bypass the rule, explicitly and
visibly, for PRs the owner authors personally.

Every GitHub write in a role session goes through `scripts/gh-as.sh <developer|reviewer>`, which
takes the bot's PAT from the local macOS Keychain (`career-platform-<role>` /
`career-platform-<role>-pat`) for that one call. The GitHub MCP server's identity is the owner's
and is fixed at session start, so role sessions use it for reads only. If the script fails with
"PAT not found", the owner needs to add the Keychain entry, not the agent.
```

- [ ] **Step 6: Commit and open ticket A's PR**

```bash
git add .github/CODEOWNERS .github/workflows/assign.yml .github/rulesets/protect-main.json AGENTS.md
git commit -m "feat(platform): request both approvers and assign by role (#269)"
git push -u origin <branch>
gh pr create --title "feat(platform): bot identities and an enforced two-approval gate" --body-file /tmp/pr-a.md
```

The PR body follows `.github/PULL_REQUEST_TEMPLATE.md` and says `Closes #269` only if Task 3's verification is done in the same PR. Otherwise it says `Addresses #269`.

---

### Task 3: Apply the ruleset and verify the gate (after ticket A merges)

> **Superseded.** The #269 final review changed this task. Follow "Task 3 (amended)" in
> "Amendments after the #269 final review" at the end of this plan. The text below is kept only
> as history.

**Files:** none (applies `.github/rulesets/protect-main.json`).

**Interfaces:**
- Consumes: Task 1's `scripts/gh-as.sh developer git …`, Task 2's ruleset file, CODEOWNERS and `assign.yml` (now on `main`), and prerequisites P1 and P2.

- [ ] **Step 1: Check the prerequisites. Stop and tell the owner if either fails.**

Run: `gh api repos/amitbaz/career-platform/collaborators/amitbaz-developer/permission --jq .permission`
Expected: `write`.

Run: `scripts/gh-as.sh developer api user --jq .login`
Expected: `amitbaz-developer`.

- [ ] **Step 2: Ask the owner for an explicit go, then apply the ruleset**

This changes branch protection on `main`, so it needs the owner's explicit go in this session.

Run: `gh api -X PUT repos/amitbaz/career-platform/rulesets/22383967 --input .github/rulesets/protect-main.json --jq '[.rules[].type] | join(",")'`
Expected: `deletion,non_fast_forward,required_status_checks,pull_request`.

- [ ] **Step 3: Probe PR as the developer bot (verification items 1, 2, 4 and 5)**

```bash
git switch -c chore/ruleset-probe origin/main
scripts/gh-as.sh developer git commit --allow-empty -m "chore: ruleset probe (do not merge)"
scripts/gh-as.sh developer git push -u origin chore/ruleset-probe
scripts/gh-as.sh developer pr create --draft --base main --head chore/ruleset-probe \
  --title "chore: ruleset probe (do not merge)" --body "Verifies the agent-loop ruleset. Closed without merging."
```

Check assignment and review requests, after waiting about 30 seconds for the workflow:
`gh pr view chore/ruleset-probe --json author,assignees,reviewRequests --jq '{a:.author.login, as:[.assignees[].login], rr:[.reviewRequests[].login]}'`
Expected: author `amitbaz-developer`, assignees `["amitbaz-developer"]`, and review requests containing `amitbaz` and `amitbaz-reviewer`.

Check that one approval is not enough:
`scripts/gh-as.sh reviewer pr review chore/ruleset-probe --approve --body "Probe approval."` then `gh pr view chore/ruleset-probe --json reviewDecision --jq .reviewDecision`
Expected: `REVIEW_REQUIRED`.

Check that a push dismisses the approval:
`scripts/gh-as.sh developer git commit --allow-empty -m "chore: probe push" && scripts/gh-as.sh developer git push`, then `gh pr view chore/ruleset-probe --json reviews --jq '[.reviews[] | select(.author.login=="amitbaz-reviewer") | .state] | last'`
Expected: `DISMISSED`.

- [ ] **Step 4: Clean up the probe**

Run: `gh pr close chore/ruleset-probe --delete-branch --comment "Ruleset verified; closing the probe."`

- [ ] **Step 5: Check new-issue assignment (verification item 4)**

Run: `n=$(gh issue create --title "probe: assignment workflow" --label area:platform --body "Verifies assign.yml. Closed immediately." | grep -o '[0-9]*$'); sleep 30; gh issue view "$n" --json assignees --jq '[.assignees[].login]'; gh issue close "$n" --reason "not planned"`
Expected: `["amitbaz"]`.

- [ ] **Step 6: Record the results** as a comment on ticket A, quoting each command's output. Verification item 3 (the owner merging their own PR through the bypass) is recorded when the owner merges ticket B's PR if they author it, or at the next owner-authored PR.

---

### Task 4: `docs/agents/roles.md` and the `needs-owner` label

**Files:**
- Create: `docs/agents/roles.md`
- Modify: `AGENTS.md` ("Agent skills": add a "Roles" entry after "Triage labels", which currently ends at line 86)
- Modify: `docs/agents/triage-labels.md` (add `needs-owner`)

**Interfaces:**
- Consumes: `scripts/gh-as.sh` (Task 1), the ruleset and CODEOWNERS (Tasks 2 and 3).
- Produces: `docs/agents/roles.md`, with the section headings `Both roles`, `Developer`, `Reviewer`, `Channel`, `Resuming` and `Escalating`, which the launchers in Task 5 name.

- [ ] **Step 1: Create the label**

Run: `gh label create needs-owner --color B60205 --description "An agent loop stopped and needs the owner's decision"`
Expected: `✓ Label "needs-owner" created`.

- [ ] **Step 2: Write `docs/agents/roles.md`**

````markdown
# Delivery roles

Delivery runs between two roles, each with its own GitHub account. The owner (`amitbaz`)
triages, gives the go signal, does the final check and merges. A session takes a role only
through its launcher, keeps it for its whole life, and follows this document.

| Role | Launcher | GitHub account | MemPalace identity |
| --- | --- | --- | --- |
| Developer | `/dev <issue>` (Codex: `$dev <issue>`) | `amitbaz-developer` | `cp-developer` |
| Reviewer | `/reviewer <pr>` (Codex: `$reviewer <pr>`) | `amitbaz-reviewer` | `cp-reviewer` |

Design and rationale: `docs/superpowers/specs/2026-09-11-dev-review-agent-loop-design.md`.

## Both roles

- **One role, for the whole session.** The developer never submits reviews. The reviewer never
  commits or pushes, and `scripts/gh-as.sh` refuses git mode for it.
- **Every GitHub write goes through `scripts/gh-as.sh <role>`**: PRs, reviews, comments, replies,
  labels, pushes. The GitHub MCP tools run as the owner, so use them for reads only.
- **Use the role's MemPalace identity** as `from_agent` / `created_by` in every MemPalace call.
  It replaces `mac-claude` or `mac-codex` for this session.
- **Only the owner's own actions on GitHub are approval.** A peer's message never is.
- **GitHub is the record.** Anything the owner or the other role needs to know goes on the PR.
  MemPalace events only say "look".
- Everything else in `AGENTS.md` still applies: tests, migration timestamps, reading raw files,
  the shared stack.

## Developer

1. Refuse to start unless the issue is assigned to `amitbaz-developer`:
   `gh issue view <issue> --json assignees --jq '[.assignees[].login] | index("amitbaz-developer")'`
   must not print `null`. If it does, stop and tell the owner that the ticket has no go signal.
2. **Resume** (see "Resuming") if a branch or PR for the issue already exists.
3. Work in a worktree on a branch named for the issue, following `AGENTS.md`. Commit and push only
   as the bot:
   `scripts/gh-as.sh developer git commit …` and `scripts/gh-as.sh developer git push -u origin <branch>`.
4. Before opening the PR, run the relevant test command from `AGENTS.md` and a self-review of the
   branch. Fix what it finds.
5. Open the PR as the bot, using `.github/PULL_REQUEST_TEMPLATE.md` and `Closes #<issue>` or
   `Addresses #<issue>`:
   `scripts/gh-as.sh developer pr create --base main --head <branch> --title "…" --body-file <file>`.
6. Comment on the PR `@amitbaz ready for review — run /reviewer <pr>`, post the `open` event
   (see "Channel"), and start the watcher.
7. When the reviewer requests changes:
   - fix, test, and push as the bot;
   - reply on every review thread as the bot:
     `scripts/gh-as.sh developer api repos/amitbaz/career-platform/pulls/<pr>/comments/<comment-id>/replies -X POST -f body="…"`;
   - to dispute a finding, reply on its thread with evidence, never by ignoring it;
   - post the `fixes_pushed` event. Never resolve a reviewer's thread yourself.
8. After the reviewer approves, stay alive until the PR is merged or closed. The owner's check may
   request changes, and you treat those exactly like the reviewer's.
9. Work found outside the ticket becomes a new issue assigned to `amitbaz`, not to the developer.

## Reviewer

1. Check out the PR head in your own worktree, never the developer's:
   ```
   git fetch origin pull/<pr>/head
   git worktree add --detach .worktrees/review-pr-<pr> FETCH_HEAD
   ```
   On later rounds: `git -C .worktrees/review-pr-<pr> fetch origin pull/<pr>/head && git -C .worktrees/review-pr-<pr> checkout --detach FETCH_HEAD`.
2. **Resume** (see "Resuming"). If `amitbaz-reviewer` has reviewed this PR before, review the
   change since that review's `commit_id`, and re-check every blocker it raised.
3. Review with the Read tool. Check the PR's claims against evidence, including read-only queries
   against live data when a claim is about the corpus. Never write to live data.
4. Submit the verdict and the inline findings in one call, pinned to the commit you reviewed:
   ```
   scripts/gh-as.sh reviewer api repos/amitbaz/career-platform/pulls/<pr>/reviews -X POST --input review.json
   ```
   `review.json` holds `{"commit_id": "<sha>", "event": "REQUEST_CHANGES" | "APPROVE", "body": "…", "comments": [{"path": "…", "line": <n>, "side": "RIGHT", "body": "…"}]}`.
   Put blocking findings first, and say plainly what would turn the review into an approval.
5. Approve only when CI is green (`gh pr checks <pr>`) and the PR's `headRefOid` equals the
   `commit_id` you reviewed.
6. Resolve a thread once its finding is fixed or answered:
   ```
   scripts/gh-as.sh reviewer api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -f id=<thread-id>
   ```
   List thread ids with
   `gh api graphql -f query='query($n:Int!){repository(owner:"amitbaz",name:"career-platform"){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved comments(first:1){nodes{body path}}}}}}}' -F n=<pr>`.
7. After approving, comment `@amitbaz ready for your check`, post the `approved` event, and end
   the session. Never merge.
8. Each round, post the verdict event and keep the watcher running until you approve or escalate.

## Channel

Every event on a PR uses `stream = project/career-platform`, `room = review` and
`correlation_id = pr-<pr>`. Bodies carry only the PR link, a commit SHA, the round number and a
status line. Findings and replies live on GitHub.

| From → to | `type` / `status` | Body |
| --- | --- | --- |
| developer → reviewer | `task.request` / `open` | PR link, head SHA, round 1 |
| reviewer → developer | `event.ack` / `claimed` | picked up (use `mempalace_event_ack`) |
| reviewer → developer | `task.reply` / `changes_requested` or `approved` | review link, reviewed SHA |
| developer → reviewer | `task.request` / `fixes_pushed` | new head SHA, round n |
| either → the other | `task.reply` / `blocked` | reason, and the escalation comment link |

Post with `mempalace_event_append` (`from_agent` = your role identity, `to_agent` = the other
role's). Run the watcher in the background, and relaunch it after every exit:

```
mempalace logstream watch --agent <your identity> --correlation-id pr-<pr> \
  --state-file ~/.mempalace/watch/<your identity>-pr-<pr>.json --idle-exit-ms 600000 --json
```

Exit 0 means an event arrived: read it with `mempalace_event_list` from your own cursor, then
resume. Exit 2 means 10 idle minutes: resume anyway. That is how the owner's GitHub review reaches
the developer. Before the PR exists, the developer has no correlation and runs no watcher.

## Resuming

Sessions die whenever the machine closes. On start, and after every watcher exit, rebuild the
state from GitHub and act on it. Never rely on what you remember:

```
gh pr view <pr> --json state,headRefOid,reviewDecision,latestReviews,statusCheckRollup,assignees
```

- **PR merged or closed, or the issue no longer assigned to `amitbaz-developer`:** stop. This is
  the owner's kill switch.
- **Developer:** act on the newest review by `amitbaz-reviewer` or `amitbaz` that asks for changes
  and has not been answered by a later push.
- **Reviewer:** review if the head differs from the commit of your latest review, or if you have
  not reviewed yet.
- **Silent peer (developer):** your last `fixes_pushed` has had no `claimed` acknowledgement for
  30 minutes, so the reviewer session died. Escalate, naming `/reviewer <pr>` as the command that
  resumes it. The first `open` request is exempt, because no reviewer exists until the owner
  launches one.

## Escalating

Stop and escalate, rather than looping, when:

- the same blocker survives 3 rounds, or 5 rounds pass in total (the reviewer escalates);
- a disputed finding is still unresolved after one exchange on its thread;
- you are blocked: a migration timestamp, a secret, a product decision, or an environment you
  cannot fix;
- the other role has gone silent (see "Resuming").

To escalate:

1. Add the label: `scripts/gh-as.sh <role> pr edit <pr> --add-label needs-owner`, or
   `issue edit` if there is no PR yet.
2. Comment `@amitbaz`, with both sides in two or three lines each and the one decision needed.
3. Post a `blocked` event, and end the session.
````

- [ ] **Step 3: Link it from AGENTS.md**

After the "Triage labels" entry (ending `See docs/agents/triage-labels.md.`, line 86), insert:

```markdown

### Roles

Delivery is split between a developer bot and a reviewer bot, started with `/dev <issue>` and
`/reviewer <pr>`. The owner triages, checks and merges. See `docs/agents/roles.md`.
```

- [ ] **Step 4: Document the label** in `docs/agents/triage-labels.md`, next to `needs-rewrite`:
  "`needs-owner`: an agent loop stopped and needs the owner's decision; the latest `@amitbaz`
  comment says which one. Removed by the owner once they have answered."

- [ ] **Step 5: Check the links**

Run: `grep -n "docs/agents/roles.md" AGENTS.md && test -f docs/agents/roles.md && grep -c "^## " docs/agents/roles.md`
Expected: two AGENTS.md lines (the Roles entry and the section rewritten in Task 2), then `6`.

- [ ] **Step 6: Commit**

```bash
git add docs/agents/roles.md docs/agents/triage-labels.md AGENTS.md
git commit -m "docs(platform): define the developer and reviewer roles (#270)"
```

---

### Task 5: `/dev` and `/reviewer` launchers, and the parallel-watch check

**Files:**
- Create: `.agents/skills/dev/SKILL.md`
- Create: `.agents/skills/reviewer/SKILL.md`
- Create symlinks: `.claude/skills/dev` → `../../.agents/skills/dev`, and `.claude/skills/reviewer` → `../../.agents/skills/reviewer`

**Interfaces:**
- Consumes: `docs/agents/roles.md` section names from Task 4.
- Produces: `/dev <issue>` and `/reviewer <pr>` in Claude Code, and `$dev` and `$reviewer` in Codex.

- [ ] **Step 1: Write `.agents/skills/dev/SKILL.md`**

```markdown
---
name: dev
description: Start this session as the developer agent (GitHub amitbaz-developer, MemPalace cp-developer) for one GitHub issue. Use only when the owner starts the session with /dev <issue> or $dev <issue>.
---

# Developer role

This session is now the **developer** for the issue number the owner gave with this command,
until it ends.

1. Say: "Developer agent — amitbaz-developer / cp-developer — issue #<issue>."
2. Read `docs/agents/roles.md` in full with the Read tool.
3. Follow its sections "Both roles", "Developer", "Channel", "Resuming" and "Escalating". That
   document is the instruction; this file only starts it.
4. If the issue number is missing, ask the owner for it and do nothing else.
```

- [ ] **Step 2: Write `.agents/skills/reviewer/SKILL.md`**

```markdown
---
name: reviewer
description: Start this session as the reviewer agent (GitHub amitbaz-reviewer, MemPalace cp-reviewer) for one pull request. Use only when the owner starts the session with /reviewer <pr> or $reviewer <pr>.
---

# Reviewer role

This session is now the **reviewer** for the pull request number the owner gave with this
command, until it ends.

1. Say: "Reviewer agent — amitbaz-reviewer / cp-reviewer — PR #<pr>."
2. Read `docs/agents/roles.md` in full with the Read tool.
3. Follow its sections "Both roles", "Reviewer", "Channel", "Resuming" and "Escalating". That
   document is the instruction; this file only starts it.
4. If the PR number is missing, ask the owner for it and do nothing else.
```

- [ ] **Step 3: Link them for Claude Code**

```bash
ln -s ../../.agents/skills/dev .claude/skills/dev
ln -s ../../.agents/skills/reviewer .claude/skills/reviewer
test -f .claude/skills/dev/SKILL.md && test -f .claude/skills/reviewer/SKILL.md && echo linked
```

Expected: `linked`.

- [ ] **Step 4: Check that both agents discover them**

In a new Claude Code session in this worktree, type `/dev` and confirm it completes to the project
skill, not a built-in. In a new Codex session, run `/skills` and confirm `dev` and `reviewer` are
listed. If Codex does not list them, check `codex --version` against the discovery rule in
`codex-rs/ext/skills/src/host_roots.rs` (`.agents/skills` from the repository ancestry) and
report back rather than moving the files.

- [ ] **Step 5: Verify parallel watchers do not cross-wake (verification item 6)**

Start two watchers in the background, one per fake PR:

```bash
mempalace logstream watch --agent cp-developer --correlation-id pr-probe-a --idle-exit-ms 90000 --json > /tmp/watch-a.json; echo "a=$?" >> /tmp/watch-exit
mempalace logstream watch --agent cp-developer --correlation-id pr-probe-b --idle-exit-ms 90000 --json > /tmp/watch-b.json; echo "b=$?" >> /tmp/watch-exit
```

Then call `mempalace_event_append` with `from_agent=cp-reviewer`, `to_agent=cp-developer`,
`type=task.reply`, `status=changes_requested`, `stream=project/career-platform`, `room=review`,
`correlation_id=pr-probe-a`, `body="probe: parallel watch check"`.

Expected after 90 seconds: `/tmp/watch-exit` contains `a=0` and `b=2`.

- [ ] **Step 6: Commit and open ticket B's PR**

```bash
git add .agents/skills/dev .agents/skills/reviewer .claude/skills/dev .claude/skills/reviewer
git commit -m "feat(platform): add /dev and /reviewer role launchers (#270)"
```

Open the PR with `Closes #270`, quoting Step 4's and Step 5's results.

---

### Task 6: First real ticket through the loop (ticket C)

**Files:** none of this plan's own. The ticket being built decides them.

- [ ] **Step 1: Check the prerequisites.** Ticket B is merged, P3 is done (`grep -n "role identity" ~/.claude/CLAUDE.md ~/.codex/AGENTS.md` shows both lines), and the owner has picked a small real ticket and assigned `amitbaz-developer` to it.
- [ ] **Step 2: The owner runs `/dev <issue>`** in a new session, and later `/reviewer <pr>` when the `@amitbaz ready for review` comment arrives.
- [ ] **Step 3: Observe verification item 7.** At least one `changes_requested` round, bot replies on the threads, a pinned re-approval, the `ready for your check` comment, and the owner's approval and merge, with every step visible on the PR.
- [ ] **Step 4: Record the run** as a comment on ticket C: the PR link, the rounds, anything that needed the owner beyond the three touchpoints, and every gap found in `docs/agents/roles.md`. Then fix those gaps in a follow-up PR that goes through the same loop.

---

## Amendments after the #269 final review

The whole-branch review of #269 found that a ruleset bypass covers **every** rule in its
ruleset. Adding the admin bypass to "Protect Main" would therefore have made the `test` check
bypassable. It also found that CODEOWNERS never requests reviews on draft PRs, and that an owner
push to a bot PR deadlocks the two-approval rule. What changed:

**Task 2 as shipped in #269:**
- `.github/rulesets/protect-main.json` is a snapshot of the live "Protect Main" ruleset, unchanged:
  deletion, non-fast-forward, the `test` check, and `bypass_actors: []`.
- `.github/rulesets/require-approvals.json` is a second ruleset, "Require approvals": the
  `pull_request` rule (2 approvals, stale approvals dismissed on push, last-push approval, threads
  resolved), plus the admin bypass in pull-request mode.
- AGENTS.md describes both rulesets, says "Require approvals" is not enforced until Task 3
  applies it, and tells the owner never to push to a bot's PR.
- `scripts/gh-as.sh` offers the bot token to `https://github.com` only, and names a failing
  `gh api user`. The self-test proves that the machine's own helpers are cleared and that other
  hosts get no token.
- `assign.yml` grants permissions per job.

### Task 3 (amended): apply "Require approvals" and verify the gate

**Files:**
- Modify: `AGENTS.md` (Step 7 only)

**Interfaces:**
- Consumes: `scripts/gh-as.sh` (both roles, including `developer git`),
  `.github/rulesets/protect-main.json`, `.github/rulesets/require-approvals.json`, CODEOWNERS and
  `assign.yml`, all on `main`, plus prerequisites P1 and P2.

- [ ] **Step 1: Check the prerequisites. Stop and tell the owner if any fails.**

  Run: `gh api repos/amitbaz/career-platform/collaborators/amitbaz-developer/permission --jq .permission`
  Expected: `write`.

  Run: `scripts/gh-as.sh developer api user --jq .login` and `scripts/gh-as.sh reviewer api user --jq .login`
  Expected: `amitbaz-developer`, then `amitbaz-reviewer`.

  Ask the owner to confirm, in GitHub's token settings, that the **reviewer** PAT has
  Contents: **read** only. A fine-grained PAT's scopes cannot be read back through the API. Only
  that scope stops the reviewer from writing code through the contents API.

- [ ] **Step 2: Check that "Protect Main" has not drifted from its file**

  ```bash
  gh api repos/amitbaz/career-platform/rulesets/22383967 \
    --jq '{name,target,enforcement,conditions,bypass_actors,rules}' | python3 -m json.tool --sort-keys > /tmp/live.json
  python3 -m json.tool --sort-keys .github/rulesets/protect-main.json > /tmp/file.json
  diff /tmp/live.json /tmp/file.json && echo "no drift"
  ```
  Expected: `no drift`. If they differ, stop and show the owner the diff. Protect Main is never
  written by this task.

- [ ] **Step 3: Ask the owner for an explicit go, then create "Require approvals"**

  Run: `gh api -X POST repos/amitbaz/career-platform/rulesets --input .github/rulesets/require-approvals.json --jq '"\(.id) \([.rules[].type] | join(","))"'`
  Expected: `<new id> pull_request`. Record the id on ticket #269.

- [ ] **Step 4: Probe PR as the developer bot, not a draft (verification items 1, 2, 4 and 5)**

  ```bash
  git switch -c chore/ruleset-probe origin/main
  scripts/gh-as.sh developer git commit --allow-empty -m "chore: ruleset probe (do not merge)"
  scripts/gh-as.sh developer git push -u origin chore/ruleset-probe
  scripts/gh-as.sh developer pr create --base main --head chore/ruleset-probe \
    --title "chore: ruleset probe (do not merge)" --body "Verifies the agent-loop rulesets. Closed without merging."
  sleep 30
  gh pr view chore/ruleset-probe --json author,assignees,reviewRequests \
    --jq '{a:.author.login, as:[.assignees[].login], rr:[.reviewRequests[].login]}'
  ```
  Expected: author `amitbaz-developer`, assignees `["amitbaz-developer"]`, and review requests
  containing `amitbaz` and `amitbaz-reviewer`. The commits are authored by `amitbaz-developer`
  (`git log -1 --format='%an <%ae>'`).

  One approval is not enough:
  `scripts/gh-as.sh reviewer pr review chore/ruleset-probe --approve --body "Probe approval."`, then
  `gh pr view chore/ruleset-probe --json reviewDecision --jq .reviewDecision`
  Expected: `REVIEW_REQUIRED`.

  A push dismisses the approval:
  `scripts/gh-as.sh developer git commit --allow-empty -m "chore: probe push" && scripts/gh-as.sh developer git push`, then
  `gh pr view chore/ruleset-probe --json reviews --jq '[.reviews[] | select(.author.login=="amitbaz-reviewer") | .state] | last'`
  Expected: `DISMISSED`.

- [ ] **Step 5: Clean up the probe**

  Run: `gh pr close chore/ruleset-probe --delete-branch --comment "Rulesets verified; closing the probe."`

- [ ] **Step 6: Check new-issue assignment (verification item 4)**

  Run: `n=$(gh issue create --title "probe: assignment workflow" --label area:platform --body "Verifies assign.yml. Closed immediately." | grep -o '[0-9]*$'); sleep 30; gh issue view "$n" --json assignees --jq '[.assignees[].login]'; gh issue close "$n" --reason "not planned"`
  Expected: `["amitbaz"]`.

- [ ] **Step 7: Make AGENTS.md true again**

  Delete the sentence `Until #269 applies "Require approvals" to the repository, only "Protect
  Main" is enforced and a merge needs no approval.` from the "Delivery runs between two bot
  accounts" section. Commit and open a PR as the owner. That PR is owner-authored, so it can only
  merge through the admin bypass, and the owner's merge of it is verification item 3.

- [ ] **Step 8: Record the results** as a comment on #269, quoting each command's output, and
  close #269.

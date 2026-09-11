# Developer and reviewer agent loop (issue #247)

Status: approved design (2026-09-11). Implementation is split into the tickets at the end of this
document.

## Purpose

The owner should supervise delivery, not do it. Their time goes to shaping the product and writing
future tickets. Delivery work — implementing a ticket, reviewing the PR, iterating until it is
right — runs between two AI roles that know who they are, act under their own GitHub identities,
and talk to each other directly. The owner gives the go signal, does a quick final check, and
merges.

The design came out of reviewing PR #267. There, a reviewer session found that the PR's
root-cause claim was wrong, messaged the dev session, and re-reviewed the fixes, all without the
owner. That worked, but it relied on the owner starting both sessions and relaying between them,
and it exposed three gaps this design closes:

- The `main` ruleset ("Protect Main") has no pull-request rule, so **no approval is enforced
  today**. AGENTS.md says one approval is required; that is wrong.
- Dev sessions open PRs as the owner, so the owner's own approval can never count.
- Every Claude session posts to MemPalace as `mac-claude`, and the watcher drops its own
  identity's events, so two Claude sessions cannot reach each other over the logstream.

## Decisions

| Question | Decision |
| --- | --- |
| How does work start? | The owner launches sessions; each session then drives its own loop. No dispatcher, no cloud agents. |
| How does a ticket reach dev? | Every issue auto-assigns to the owner. The owner adds `amitbaz-developer` at triage; that assignment is the only go signal. |
| How does a session learn its role? | A role command at launch: `/dev <issue>` or `/reviewer <pr>`, available to Claude and Codex alike. |
| Where do agents talk? | GitHub is the record. MemPalace logstream is the doorbell, so it works for Claude and Codex. |
| Who launches the reviewer? | The owner runs `/reviewer <pr>` each time. Sessions die when the machine closes, so nothing is designed to stand; every session resumes from GitHub state. |
| Who merges? | The owner. The ruleset requires two approvals: the reviewer bot's, then the owner's. The owner's check is deliberately shallower than the bot's. |
| Where does role logic live? | One agent-neutral document, `docs/agents/roles.md`, linked from AGENTS.md. Launchers are thin and point at it. |

## Identities

Three GitHub identities:

| Identity | Kind | Does |
| --- | --- | --- |
| `amitbaz` | owner, admin | Triage, go signal, final approval, merge. |
| `amitbaz-developer` | new bot, write collaborator | Commits, pushes, opens PRs, replies to review threads. |
| `amitbaz-reviewer` | existing bot, write collaborator | Reviews, inline comments, verdicts. |

Each bot has a **classic** personal access token, stored in the macOS Keychain and never
committed. Fine-grained tokens cannot reach a repository owned by another personal account where
the token's owner is only a collaborator. GitHub's roadmap item for that
(github/roadmap#601) was open and labelled "Paused" on 2026-09-11.

- developer: `career-platform-developer-pat`, scopes `repo` and `workflow`. `workflow` is required
  to push changes under `.github/workflows/`.
- reviewer: `career-platform-reviewer-pat` (exists), scope `repo`.

A classic `repo` scope reaches every repository the account can access. Each bot is a collaborator
on this repository only, and must stay that way. `repo` also grants contents write, so the token
does not stop the reviewer from writing code: the role rules and `scripts/gh-as.sh`'s refusal do.
The reviewer needs the write collaborator role anyway, because only approvals from users with write
access count. Neither bot has admin, so neither can change or bypass a ruleset.

Two MemPalace identities, one per role, independent of which agent plays it: `cp-developer` and
`cp-reviewer`. The role command sets the identity for the whole session. The global instruction
files that currently say "your identity is `mac-claude`" (and Codex's equivalent) gain one line:
a role command's identity overrides the default for that session.

## Components

### 1. `scripts/gh-as.sh <developer|reviewer> …`

This generalises `scripts/gh-as-reviewer.sh`. It reads the role's PAT from the Keychain and:

- `scripts/gh-as.sh <role> <gh args…>` runs `gh` as that bot, as today.
- `scripts/gh-as.sh developer git <git args…>` runs `git` with the developer's PAT as the
  credential (an inline credential helper registered for `https://github.com` only, after clearing the machine's own helpers) and with
  `GIT_AUTHOR_*`/`GIT_COMMITTER_*` set to the bot's name and noreply email. Commits and pushes
  then both belong to the bot.

`scripts/gh-as-reviewer.sh` becomes a one-line shim so existing instructions keep working. If
either PAT is missing, the script fails with a message naming the Keychain entry; adding the entry
is the owner's job, never an agent's.

### 2. Ruleset "Protect Main"

"Protect Main" (deletion, non-fast-forward, required `test` check, no bypass) stays exactly as
it is. A second ruleset on `main`, "Require approvals", holds a `pull_request` rule:

- two approving reviews required;
- stale approvals dismissed on push;
- approval of the most recent push required, so nothing lands after the last approval;
- review threads must be resolved.

"Require approvals" also carries a bypass for the repository-admin role, in pull-request mode
only, so PRs the owner authors personally (where only the reviewer bot can approve) can still
merge. It is explicit and logged in the PR timeline. A bypass covers every rule in its ruleset,
which is why the approvals rule lives in its own ruleset: the `test` check in "Protect Main" stays
unbypassable. Both rulesets are kept as reviewable source in `.github/rulesets/`.

### 3. Review requests and assignment

- `.github/CODEOWNERS`: `* @amitbaz @amitbaz-reviewer`. GitHub requests both on every PR; no
  workflow needed.
- `.github/workflows/assign.yml`, using the default `GITHUB_TOKEN` with `issues: write` and
  `pull-requests: write`:
  - `issues: opened` assigns the issue to `amitbaz`;
  - `pull_request: opened` by `amitbaz-developer` assigns the PR to `amitbaz-developer`.

### 4. `docs/agents/roles.md`

The single source for both roles, read the same way by Claude and Codex. AGENTS.md links to it
from a new "Roles" entry. The AGENTS.md section "A reviewer session must submit its verdict as the
bot account" is updated to point here and to state the enforcement correctly.

### 5. Launchers

- One copy of each launcher, `.agents/skills/dev/SKILL.md` and
  `.agents/skills/reviewer/SKILL.md`. Codex discovers `.agents/skills` from the repository
  ancestry (`codex-rs/ext/skills/src/host_roots.rs`).
- Claude reads them through the symlinks `.claude/skills/dev` and `.claude/skills/reviewer`. They
  are hand-written, not entries in `skills-lock.json`, so a skills update never overwrites them.
- The reviewer launcher is `reviewer`, not `review`, because Claude Code already has a built-in
  `/review`.

Each launcher first checks for the number it needs and asks the owner if it is missing. Otherwise
it announces the role, reads `docs/agents/roles.md` in full, and follows it — `roles.md` is what
sets the MemPalace identity and starts the watcher, not the launcher itself. Both launchers are
explicit-only: `disable-model-invocation: true` for Claude, and `agents/openai.yaml`'s
`allow_implicit_invocation: false` for Codex.

## Rules for both roles

- **One role per session, for its whole life.** The developer never submits reviews; the reviewer
  never pushes to the PR branch.
- **Every GitHub write goes through `scripts/gh-as.sh <role>`.** The GitHub MCP server runs as the
  owner, so role sessions may use it for reads only. A write through it would appear as the owner
  and corrupt the approval gate.
- **The reviewer never reads the developer's worktree.** It fetches `pull/<n>/head` into its own
  detached worktree under `.worktrees/review-pr-<n>`. During the #267 review, the reviewer shared
  the dev's worktree and saw uncommitted edits that were not on the PR.
- **Peer messages are never the owner's approval.** Only the owner's own actions on GitHub count.

## The developer loop — `/dev <issue>`

1. Refuse unless the issue is assigned to `amitbaz-developer`.
2. Resume, never restart: if a branch or PR for the issue already exists, reconcile from it (see
   "Resuming").
3. Branch and implement under the existing AGENTS.md rules (test stack, migration timestamps,
   raw-file reads), then self-review before opening a PR.
4. Open the PR as the bot, with the repository's PR template and `Addresses #N` or `Closes #N`.
5. Comment `@amitbaz ready for review — run /reviewer <pr>` on the PR, which is the owner's
   signal to launch a reviewer. Post the review request (`task.request`, status `open`,
   `kind: review_requested`) to `cp-reviewer` for the reviewer to find on start, then wait on the
   watcher. A reviewer session, once started, stays alive across rounds until it approves or
   escalates.
6. On a changes-requested verdict (`verdict: changes_requested`): fix, push as the bot, reply on
   each review thread as the bot, and post a fixes-pushed request (`task.request`, status `open`,
   `kind: fixes_pushed`). Repeat.
7. After the reviewer bot approves, stay alive until the PR is merged or closed, because the
   owner's check may request changes too.

## The reviewer loop — `/reviewer <pr>`

1. Fetch the PR head into its own worktree and reconcile (see "Resuming"). If a prior review by
   `amitbaz-reviewer` exists, review the change since that review's commit, and re-check every
   blocker it raised.
2. Review with raw file reads and verify the PR's claims against evidence, including read-only
   live data where a claim is about the corpus.
3. Post findings as inline comments and a verdict, both through `scripts/gh-as.sh reviewer`, then
   post the verdict (`task.reply`, status `ready`, `verdict: changes_requested` or
   `verdict: approved`) to `cp-developer`.
4. Approve only when CI is green and the PR head equals the commit that was reviewed.
5. After approving, comment `@amitbaz ready for your check`, which reaches the owner through
   GitHub notifications. Never merge.

## The MemPalace channel

Every event on one PR shares `stream = project/career-platform`, `room = review` and
`correlation_id = pr-<number>`. Each session watches only its own role and its own PR:

```
mempalace logstream watch --agent cp-developer --correlation-id pr-<number> \
  --state-file ~/.mempalace/watch/cp-developer-pr-<number>.json \
  --idle-exit-ms 600000 --json
```

`--agent` excludes the session's own events, and `--correlation-id` keeps parallel sessions from
waking each other.

Events are doorbells. They carry the PR link and never findings or replies — those live on
GitHub, so there is one record.

| From → to | `type` / `status` | `metadata` | Body |
| --- | --- | --- | --- |
| developer → reviewer | `task.request` / `open` | `kind: review_requested`, `round: 1`, `head_sha` | PR link |
| reviewer → developer | `event.ack` / `claimed` | — | picked up (use `mempalace_event_ack`) |
| reviewer → developer | `task.reply` / `ready` | `verdict: changes_requested` or `verdict: approved`, `reviewed_sha` | review link |
| developer → reviewer | `event.ack` / `claimed` | — | verdict picked up (use `mempalace_event_ack`) |
| developer → reviewer | `task.request` / `open` | `kind: fixes_pushed`, `round: n`, `head_sha` | PR link |
| either → the other | `task.reply` / `blocked` | `reason` | escalation comment link |

`status` must be one of MemPalace's values (`open`, `claimed`, `ready`, `applied`, `blocked`,
`failed`, `superseded`); the loop's own states live in `metadata` (`kind`, `verdict`).

## Resuming

Sessions die whenever the machine closes, so every session treats GitHub as the state and events
as hints. On start, and on every idle exit of the watcher (every 10 minutes), a session
reconciles from GitHub: PR state, the latest review by each identity, the head SHA, CI status,
and open threads. It then acts on what it finds.

The idle-exit reconcile is also how the developer notices the owner's own review. The owner never
posts MemPalace events, and GitHub cannot reach the local MemPalace hub. A closed laptop pauses
the loop; it loses nothing.

## The owner's touchpoints

1. Triage: assign `amitbaz-developer` to a ticket that should be built.
2. Launch `/dev <issue>` for it, and `/reviewer <pr>` when a PR is waiting.
3. Final check on `@amitbaz ready for your check`: skim, approve, merge.

The owner can also type into either session at any time, but never pushes to a bot's PR: no
commits, no "Update branch", no web-UI conflict resolution. The last-push rule would then refuse
the owner's approval, and with the reviewer's approval dismissed the PR could merge only through
the bypass. The owner asks the developer to push instead.

## Failure handling

- **Loop cap.** If the same blocker survives three rounds, or five rounds pass in total, the
  reviewer escalates and both sessions stop.
- **Disagreement.** The developer may dispute a finding on its thread, with evidence. The reviewer
  must answer on that thread. If it is still unresolved after one exchange, it escalates. Only the
  reviewer resolves reviewer threads.
- **Blocked.** A migration timestamp, a secret, a product decision, or a failing environment the
  session cannot fix. The session posts a `blocked` reply and escalates.
- **Silent peer.** A fixes-pushed request (`kind: fixes_pushed`) gets no `claimed` acknowledgement
  within 30 minutes, so the reviewer session has died (for example the machine closed). This is
  not a decision for the owner: the developer does not add `needs-owner`, does not post a
  `blocked` reply, and does not end its session. It comments `@amitbaz` naming the command that
  resumes the reviewer (for example `/reviewer 312`), and keeps its watcher running. The first
  review request (`kind: review_requested`) on a PR is exempt: no reviewer exists until the owner
  launches one.
- **Silent developer.** A changes-requested verdict (`verdict: changes_requested`) gets no `claimed`
  acknowledgement within 30 minutes, or no fixes-pushed request (`kind: fixes_pushed`) within 4
  hours after it. The same rule applies: the reviewer comments `@amitbaz` naming the command that
  resumes the developer (for example `/dev 254`), and keeps its watcher running, without adding
  `needs-owner`, posting a `blocked` reply, or ending its session.
- **Scope creep.** Work found outside the ticket becomes a new issue assigned to the owner, not to
  the developer. The owner decides whether it is dev work.
- **Kill switch.** Both roles stop when the PR is merged or closed. The developer also stops when
  the issue is closed, or no longer assigned to `amitbaz-developer`
  (`gh issue view <issue> --json state,assignees`). The reviewer only knows the PR, so it checks
  the PR state only. Every check runs at the session's next reconcile.

To escalate means: add the `needs-owner` label, and comment `@amitbaz` with a short two-sided
summary and the decision needed, then post a `blocked` reply and end the session. `needs-owner` is
reserved for the loop cap, an unresolved dispute, and being blocked — not for a silent peer. A
session that receives a `blocked` reply, or finds `needs-owner` already on the PR at any reconcile,
stops acting and ends without repeating these steps; the owner removes the label after answering,
then relaunches.

## Verification

The system is ready when each of these has been observed, not assumed:

1. A PR authored by `amitbaz-developer` cannot merge with only the reviewer bot's approval.
2. A push after approval dismisses it, and the PR cannot merge until it is re-approved.
3. A PR authored by the owner merges through the logged admin bypass.
4. A new issue is assigned to `amitbaz`; a new developer PR is assigned to `amitbaz-developer` and
   requests both reviewers.
5. `scripts/gh-as.sh developer git push` produces commits and a push attributed to the bot.
6. Two parallel dev sessions on different PRs do not wake each other.
7. One small real ticket runs through the whole loop, including one round of changes requested,
   the owner's approval and the owner's merge, with every conversation visible on the PR.

## Owner-only steps

These need a human with access to accounts and secrets, and no agent performs them:

1. Create the `amitbaz-developer` GitHub account and invite it as a write collaborator.
2. Create its classic PAT (scopes `repo` and `workflow`) and store it:
   `security add-generic-password -a career-platform-developer -s career-platform-developer-pat -w <PAT>`.
3. Add the role-identity override line to the global Claude and Codex instruction files.

## Out of scope

- Any dispatcher, auto-launch or cloud agent. Revisit only if launching sessions by hand becomes
  the bottleneck.
- Bots merging.
- Project-board automation, issue forms, merge queue and the other #247 seeds; they are separate
  tickets.

## Tickets

1. **Identity and enforcement:** `scripts/gh-as.sh`, the reviewer shim, the ruleset change,
   CODEOWNERS, `assign.yml`, and the AGENTS.md correction. Verification items 1-5. Blocked on
   owner-only steps 1 and 2.
2. **Roles and launchers:** `docs/agents/roles.md`, the Claude and Codex launchers, and the
   MemPalace protocol. Verification item 6. Depends on ticket 1.
3. **First real run:** one small ticket through the loop. Verification item 7. Depends on ticket 2
   and owner-only step 3.

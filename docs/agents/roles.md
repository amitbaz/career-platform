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
- **The owner never pushes to a bot's PR**: no commits, no "Update branch", no web-UI conflict resolution. Under "Require approvals" that makes the owner the last pusher, so their approval is refused and the reviewer's is dismissed, and the PR can then merge only through the bypass. When the owner wants a change, the developer pushes it.
- **Tokens are not the boundary.** Both bots use classic `repo` tokens, which allow writing repository contents. What keeps the reviewer from writing code is this document and `scripts/gh-as.sh`'s refusal of git mode. The developer never submits a review on any PR, including the owner's own.
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

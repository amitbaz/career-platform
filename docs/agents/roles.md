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
- **Run only the per-PR watcher.** A role session runs the watcher in "Channel" and nothing else.
  It does not run the global session-start inbox watcher (`mempalace logstream watch --agent
  <identity> --state-file ~/.mempalace/watch/<identity>.json`) that the global instructions
  describe. Two developer sessions on different PRs must never wake each other or share a state
  file.
- **The owner never pushes to a bot's PR**: no commits, no "Update branch", no web-UI conflict
  resolution. Under "Require approvals" that makes the owner the last pusher, so their approval
  is refused and the reviewer's is dismissed, and the PR can then merge only through the bypass.
  When the owner wants a change, the developer pushes it.
- **Tokens are not the boundary.** Both bots use classic `repo` tokens, which allow writing
  repository contents. What keeps the reviewer from writing code is this document and
  `scripts/gh-as.sh`'s refusal of git mode. The developer never submits a review on any PR,
  including the owner's own.
- **GitHub is the record.** Anything the owner or the other role needs to know goes on the PR.
  MemPalace events only say "look".
- Everything else in `AGENTS.md` still applies: tests, migration timestamps, reading raw files,
  the shared stack.

## Developer

1. Refuse to start unless the issue is assigned to `amitbaz-developer`:
   `gh issue view <issue> --json assignees --jq '[.assignees[].login] | index("amitbaz-developer")'`
   must not print `null`. If it does, stop and tell the owner that the ticket has no go signal.
2. **Resume** (see "Resuming") if a branch or PR for the issue already exists. Check with:
   `gh pr list --author amitbaz-developer --state all --search "<issue>" --json number,headRefName,state`.
   If there's a match, resume with that PR number.
3. Work in a worktree on a branch named for the issue, following `AGENTS.md`. Commit and push only
   as the bot:
   `scripts/gh-as.sh developer git commit …` and `scripts/gh-as.sh developer git push -u origin <branch>`.
4. Before opening the PR, run the relevant test command from `AGENTS.md` and a self-review of the
   branch. Fix what it finds.
5. Open the PR as the bot, using `.github/PULL_REQUEST_TEMPLATE.md` and `Closes #<issue>` or
   `Addresses #<issue>`:
   `scripts/gh-as.sh developer pr create --base main --head <branch> --title "…" --body-file <file>`.
6. Comment on the PR `@amitbaz ready for review — run /reviewer <pr>`, post a review request
   (`kind: review_requested`) (see "Channel"), and start the watcher.
7. When the reviewer requests changes:
   - acknowledge the verdict within 30 minutes: `mempalace_event_ack` on its event, status
     `claimed`;
   - fix, test, and push as the bot;
   - reply on every review thread as the bot:
     `scripts/gh-as.sh developer api repos/amitbaz/career-platform/pulls/<pr>/comments/<comment-id>/replies -X POST -f body="…"`,
     where `<comment-id>` is that thread's `databaseId`, from the same GraphQL query the reviewer
     uses to list threads (see "Reviewer" step 7); `gh api graphql` is a read, so it runs without
     `gh-as.sh`;
   - to dispute a finding, reply on its thread with evidence, never by ignoring it;
   - post a fixes-pushed request (`kind: fixes_pushed`). Never resolve a reviewer's thread yourself.
8. After the reviewer approves, stay alive until the PR is merged or closed. The owner's check may
   request changes, and you treat those exactly like the reviewer's, with one difference: when the
   changes being fixed came from the owner's own review, not the reviewer bot's, comment after
   pushing `@amitbaz fixes pushed — the reviewer's approval was dismissed; run /reviewer <pr>`,
   then post the fixes-pushed request as usual. That request is exempt from silent-peer timing —
   see "Resuming".
9. Work found outside the ticket becomes a new issue assigned to `amitbaz`, not to the developer.

## Reviewer

1. Check out the PR head in your own worktree, never the developer's:
   ```
   git fetch origin pull/<pr>/head
   git worktree add --detach .worktrees/review-pr-<pr> FETCH_HEAD
   ```
   On later rounds: `git -C .worktrees/review-pr-<pr> fetch origin pull/<pr>/head && git -C .worktrees/review-pr-<pr> checkout --detach FETCH_HEAD`.
2. On start, and on every watcher wake, acknowledge each unacknowledged `task.request` addressed
   to `cp-reviewer` on `pr-<pr>` with `mempalace_event_ack`, status `claimed`, before reviewing.
   The developer's silent-peer rule depends on this ack.
3. **Resume** (see "Resuming"). If `amitbaz-reviewer` has reviewed this PR before, review the
   change since that review's `commit_id`, and re-check every blocker it raised.
4. Review with the Read tool. Check the PR's claims against evidence, including read-only queries
   against live data when a claim is about the corpus. Never write to live data.
5. Submit the verdict and the inline findings in one call, pinned to the commit you reviewed:
   ```
   scripts/gh-as.sh reviewer api repos/amitbaz/career-platform/pulls/<pr>/reviews -X POST --input review.json
   ```
   `review.json` holds `{"commit_id": "<sha>", "event": "REQUEST_CHANGES" | "APPROVE", "body": "…", "comments": [{"path": "…", "line": <n>, "side": "RIGHT", "body": "…"}]}`.
   Put blocking findings first, and say plainly what would turn the review into an approval.
6. Approve only when every required check is green (`gh pr checks <pr> --required`), the PR's
   `headRefOid` equals the `commit_id` you reviewed, and every review thread is resolved.
7. Resolve a thread once its finding is fixed or answered, and only a thread you started; a
   thread the owner started, the owner resolves:
   ```
   scripts/gh-as.sh reviewer api graphql -f query='mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{isResolved}}}' -f id=<thread-id>
   ```
   List thread ids with
   `gh api graphql -f query='query($n:Int!){repository(owner:"amitbaz",name:"career-platform"){pullRequest(number:$n){reviewThreads(first:100){nodes{id isResolved comments(first:1){nodes{databaseId body path}}}}}}}' -F n=<pr>`.
8. After approving, comment `@amitbaz ready for your check`, post the verdict (`status: ready`,
   `verdict: approved`), and end the session. Never merge.
9. Each round, post the verdict event and keep the watcher running until you approve or escalate.

## Channel

Every event on a PR uses `stream = project/career-platform`, `room = review` and
`correlation_id = pr-<pr>`. Findings and replies live on GitHub.

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

Post with `mempalace_event_append` (`from_agent` = your role identity, `to_agent` = the other
role's). Run the watcher in the background, and relaunch it after every exit:

```
mempalace logstream watch --agent <your identity> --correlation-id pr-<pr> \
  --state-file ~/.mempalace/watch/<your identity>-pr-<pr>.json --idle-exit-ms 600000 --json
```

On start, a session has no cursor yet, so read the PR's whole event history —
`mempalace_event_list(correlation_id="pr-<pr>", to_agent="<identity>")` — which is fine because one
PR's history is small. Acknowledge what you must, then keep the last event id you processed as
your cursor. Exit 0 means an event arrived: read it with `mempalace_event_list` using
`since_event_id=<cursor>`, update the cursor to the last id you processed, then resume. Exit 2
means 10 idle minutes: resume anyway, with the same cursor. That is how the owner's GitHub review
reaches the developer. On any other exit, retry the watcher once; if it fails again, escalate as
blocked — the MemPalace hub is unreachable. Before the PR exists, the developer has no correlation
and runs no watcher.

## Resuming

Sessions die whenever the machine closes. On start, and after every watcher exit, rebuild the
state from GitHub and act on it. Never rely on what you remember:

```
gh pr view <pr> --json state,headRefOid,reviewDecision,latestReviews,statusCheckRollup,assignees,labels
```

- **PR merged or closed:** both roles stop. This is the owner's kill switch.
- **Developer:** also stop when the issue is closed, or no longer assigned to
  `amitbaz-developer`: `gh issue view <issue> --json state,assignees`.
- **Reviewer:** the PR is all you know, so check the PR state only.
- **`needs-owner` on the PR, or an unprocessed `blocked` task.reply:** stop acting and end (see
  "Escalating"); someone has already escalated.
- **Developer:** act on the newest review by `amitbaz-reviewer` or `amitbaz` that asks for changes
  and has not been answered by a later push.
- **Reviewer:** review if the head differs from the commit of your latest review, or if you have
  not reviewed yet.
- **Clocks start at the later time.** Measure every silent-peer window below (30 minutes, 4 hours)
  from the later of the event's own time and this session's own start time, so a session relaunched
  after the machine was closed overnight never escalates immediately.
- **Silent peer (developer):** your last fixes-pushed request (`kind: fixes_pushed`) has had no
  `claimed` acknowledgement for 30 minutes, so the reviewer session died. This is not a decision
  for the owner: do not add `needs-owner`, do not post a `blocked` event, and do not end the
  session. Comment `@amitbaz` naming `/reviewer <pr>` as the command that resumes the peer, then
  keep your watcher running and keep reconciling on every wake. Two requests are exempt from this
  timing, because no reviewer session exists until the owner launches one: the first review
  request (`kind: review_requested`), and a fixes-pushed request answering the owner's own review
  (see "Developer" step 8).
- **Silent peer (reviewer):** your changes-requested verdict (`verdict: changes_requested`) has
  had no `claimed` acknowledgement for 30 minutes, or no fixes-pushed request (`kind: fixes_pushed`)
  for 4 hours after it was acknowledged, so the developer session died or stalled. This is not a
  decision for the owner either: comment `@amitbaz` naming `/dev <issue>` as the command that
  resumes the peer, then keep your watcher running and keep reconciling. Do not add `needs-owner`,
  post a `blocked` event, or end the session.

## Escalating

Stop and escalate, rather than looping, when:

- the same blocker survives 3 rounds, or 5 rounds pass in total (the reviewer escalates);
- a disputed finding is still unresolved after one exchange on its thread;
- you are blocked: a migration timestamp, a secret, a product decision, or an environment you
  cannot fix.

A silent peer (see "Resuming") is not one of these: name the resume command in a PR comment and
keep going. `needs-owner` is reserved for the cases above, where only the owner can decide.

To escalate:

1. Add the label: `scripts/gh-as.sh <role> pr edit <pr> --add-label needs-owner`, or
   `issue edit` if there is no PR yet.
2. Comment `@amitbaz`, with both sides in two or three lines each and the one decision needed.
3. Post a `blocked` event, and end the session.

If you receive a `task.reply` with status `blocked`, or find `needs-owner` already on the PR
during any reconcile (on start or on a wake), stop acting and end the session without repeating
these steps — someone has already escalated. The owner removes the label after answering, then
relaunches whichever role needs to continue.

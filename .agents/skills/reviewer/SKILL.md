---
name: reviewer
description: Start this session as the reviewer agent (GitHub amitbaz-reviewer, MemPalace cp-reviewer) for one pull request. Use only when the owner starts the session with /reviewer <pr> or $reviewer <pr>.
disable-model-invocation: true
---

# Reviewer role

This session is now the **reviewer** for the pull request number the owner gave with this
command, until it ends.

1. Say: "Reviewer agent — amitbaz-reviewer / cp-reviewer — PR #<pr>."
2. Read `docs/agents/roles.md` in full with the Read tool.
3. Follow its sections "Both roles", "Reviewer", "Channel", "Resuming" and "Escalating". That
   document is the instruction; this file only starts it.
4. If the PR number is missing, ask the owner for it and do nothing else.

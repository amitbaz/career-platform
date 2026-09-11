---
name: dev
description: Start this session as the developer agent (GitHub amitbaz-developer, MemPalace cp-developer) for one GitHub issue. Use only when the owner starts the session with /dev <issue> or $dev <issue>.
disable-model-invocation: true
---

# Developer role

This session is now the **developer** for the issue number the owner gave with this command,
until it ends.

1. If the issue number is missing, ask the owner for it and do nothing else.
2. Say: "Developer agent — amitbaz-developer / cp-developer — issue #<issue>."
3. Read `docs/agents/roles.md` in full with the Read tool.
4. Follow its sections "Both roles", "Developer", "Channel", "Resuming" and "Escalating". That
   document is the instruction; this file only starts it.

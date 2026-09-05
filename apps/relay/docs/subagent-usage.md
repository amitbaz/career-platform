# Subagent usage

Use subagents only when they provide clear value through genuinely independent parallel work.

Do not spawn subagents for:
- simple repository exploration or searches
- reading a small number of files
- single-file or narrowly scoped changes
- sequential work where one task depends on the previous one
- work that can be completed efficiently with a few direct tool calls

Prefer completing straightforward work in the main agent context. Avoid duplicate exploration across subagents.

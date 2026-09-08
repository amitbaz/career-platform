# Memory: MemPalace

Cross-session memory for this repo lives in a [MemPalace](https://github.com/MemPalace/mempalace)
palace at `~/.mempalace/palace` (backend: chroma). One wing, `career_platform`.

This file is the protocol. `AGENTS.md` carries the short version.

## What belongs in the palace

Things that are **not** in the repo and cannot be recovered by reading it:

- decisions and the reasoning behind them, including options rejected and why
- measurements, with the date they were taken
- constraints the code does not state (legal posture, single-user status, what is parked)
- facts that change over time, and when they changed

What does **not** belong: source code. The working tree is already searchable with grep and
the file tools, with real line numbers. A mined chunk is a snapshot taken at `source_mtime`;
the repo moves, the chunk does not, and search returns it with no staleness warning.

## Layers

| Layer | Tool | Holds |
|---|---|---|
| Drawers | `mempalace_search`, `mempalace_add_drawer` | verbatim prose, quoted back exactly |
| Knowledge graph | `mempalace_kg_query`, `kg_add`, `kg_supersede` | subject/predicate/object with validity windows |
| Diary | `mempalace_diary_read`, `diary_write` | per-agent session continuity, AAAK format |
| Identity | `~/.mempalace/identity.txt` | the L0 block injected by `mempalace wake-up` |

## Protocol

**Read.** Before answering about a past decision, a prior measurement, a person, or why
something is the way it is: search first. `mempalace_search` returns drawers verbatim — quote
them rather than paraphrasing, because verbatim recall is the point of the system. Use
`mempalace_kg_query` with `as_of` for anything time-bound.

**Write.** At the end of substantive work, `mempalace_diary_write` with agent `claude-code`
and `wing: career_platform`. The transcript is captured automatically by the hooks; the diary
records what the transcript does not make obvious — the decision, the reason, the correction.

**Change a fact.** `mempalace_kg_supersede(subject, predicate, old_object, new_object, at=…)`.
Do not hand-roll invalidate-then-add: that leaves the old and new values both valid at the
boundary instant. Use `kg_invalidate` only for a fact that ended with no replacement, and
`kg_add` for a genuinely concurrent fact.

**Prune.** `mempalace_sync` removes drawers whose sources were deleted, moved, or became
gitignored. It is dry-run by default — read the report before passing `apply: true`.

## Topology: shared-brain hub

The palace is served by a **hub**, so Claude Code and Codex share one memory. This is
MemPalace's standard shared-brain setup, not a custom arrangement.

```
mempalace serve --host 127.0.0.1 --port 8765
```

One `serve` process holds the palace's writer lease and serializes writes from every client.
It runs as a login service: `~/Library/LaunchAgents/com.mempalace.hub.plist`
(`RunAtLoad`, `KeepAlive`, log at `~/.mempalace/hub.log`).

**Clients need no configuration.** Both harnesses keep their ordinary stdio registration —
`claude mcp add mempalace -- mempalace-mcp`, `codex mcp add mempalace -- mempalace-mcp`. Each
stdio process checks for a live hub per request and auto-proxies to it rather than opening its
own database handles, so both agents read and write the same palace, and they follow a
restarted hub without reconfiguration. (`MEMPALACE_HUB_FORWARD=0` opts out.)

**Why a hub is required rather than optional here:** the local backend allows exactly one
writable owner. Without the hub, whichever `mempalace-mcp` starts first takes the lease and
everything else is locked out — the capture hooks, the daemon, and the *other* agent. Measured
2026-09-08, the `stop` hook fired correctly (`TRIGGERING SAVE at exchange 93`) and then failed
with `another mine is in progress: palace … is held by PID 54025 (mempalace-mcp)`; a daemon
refused to start with `writable daemon startup refused: another writer owns local backend`.
The hub is what makes one memory across two models possible.

**Ordering matters.** The hub cannot take the lease while an agent's MCP server holds it. The
LaunchAgent retries every 30s, so it wins the moment no agent is running. If the hub log shows
`startup refused`, quit every agent, wait ~30s, then start them again.

## Agent identities

Each harness has a stable `<machine>-<harness>` identity, used as `from_agent`/`created_by`:

| Agent | Identity | Rules block installed in |
|---|---|---|
| Claude Code | `mac-claude` | `~/.claude/CLAUDE.md` |
| Codex | `mac-codex` | `~/.codex/AGENTS.md` |

Both blocks are marker-delimited (`mempalace-shared-brain:start` / `:end`) and rendered by
`mempalace rules --agent <identity>`. Re-render and **replace** the block rather than appending
a second one. They carry the canonical coordination protocol — the logstream inbox, the
watcher, delegation and patch handoff between the two agents.

## Capture hooks

Wired in `.claude/settings.local.json`, all four `mempalace hook run … --harness claude-code`:

| Event | Does |
|---|---|
| `SessionStart` | initializes session state; injects nothing (`_output({})`, by design) |
| `Stop` | transcript capture on a save interval |
| `SessionEnd` | final flush for sessions that never reach the interval |
| `PreCompact` | capture before context is compacted |

Capture runs **detached** — Claude Code allows SessionEnd about 1.5s and a cold `mempalace`
start alone exceeds that. Because `session-start` injects nothing, **recall is always the
agent's job**, per the protocol above.

`.claude/settings.local.json` is gitignored (`.gitignore:71`), so these hooks do not exist in a
fresh clone or worktree. Re-add them by hand there. Codex has an equivalent
`--harness codex` hook set if you want capture from that side too.

## State as of 2026-09-08

Set up in one pass after an audit found the palace had 9253 drawers from a single repo mining
run, an empty knowledge graph, an empty diary, no hooks, and no session had ever read from it.

- pruned 115 stale drawers and 9 closets (`mempalace_sync`)
- wrote `~/.mempalace/identity.txt`
- mined the 21 curated agent-memory files into the palace (102 drawers)
- seeded 8 knowledge-graph facts
- wrote the first `claude-code` diary entry

Known open issue: `mempalace wake-up`'s L1 "essential story" is still dominated by the ~7700
mined code chunks in room `apps`, so it emits arbitrary TypeScript rather than anything
essential. It does not reach sessions (the session-start hook injects nothing), but
`wake-up` is not worth reading by hand until the code corpus is separated into its own wing
or removed. `mempalace migrate-wings` does not do this — it only normalizes wing names.

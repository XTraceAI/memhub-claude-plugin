# MemHub for Claude Code

Auto-capture your Claude Code sessions into **MemHub team memory**. At the end of
each session, an agent hook reads the transcript and saves it through the
`memhub-staging` MCP server, which runs **tool-aware (agentic) extraction** of
facts, episodes, and artifacts.

## What's in here

This repo is a **marketplace** with two plugins:

```
.claude-plugin/marketplace.json     # makes the plugins installable
plugins/memhub/
├── .claude-plugin/plugin.json      # plugin manifest
├── .mcp.json                       # the memhub-staging MCP server (per-user OAuth)
├── hooks/hooks.json                # SessionEnd → agent hook → import_conversation
└── skills/                         # /memhub:* skills (also auto-invoked by Claude)
    ├── handoff-session/            # hand the current session to a teammate
    ├── import-session/             # import a past session, any size
    ├── save-artifact/              # store a file as a MemHub artifact
    └── search-memory/              # read-only team-memory recall
plugins/fleet/
├── .claude-plugin/plugin.json      # plugin manifest
├── hooks/hooks.json                # SessionStart/UserPromptSubmit/PostToolUse/SessionEnd
└── scripts/fleet_board.py          # one script, one subcommand per hook event
```

## Install

```text
/plugin marketplace add XTraceAI/memhub-claude-plugin
/plugin install memhub@memhub
```

Then authenticate the MCP server once (the hook can't run until it's connected):

```text
/mcp
```

Select `memhub-staging`, choose **Authenticate**, and approve in the browser.

## How it works

1. **SessionEnd** fires when a Claude Code session ends.
2. The **agent hook** (a subagent with MCP access) reads the session transcript
   `.jsonl` and calls `import_conversation`, passing the raw transcript records
   and the `session_id` as the `conversation_id`.
3. The MCP server auto-detects the Claude Code shape and routes to the
   **agentic** ingestion path: tool-bearing events are extracted with the
   `agentic` prompt variant (the agent is treated as a valid belief source;
   facts/episodes/artifacts land in your personal team-LTM).
4. Re-running the same session dedups (the `conversation_id` keys a deterministic
   re-import), so nothing is double-saved.

## Incremental flush on commit / PR (v0.2)

Besides the SessionEnd backstop, a `PostToolUse` hook watches for `git commit`,
`gh pr create`, and `gh pr merge` and flushes the transcript-so-far in the
background (async — never blocks your session). Commits are semantic work
boundaries: flushing there makes memory available **mid-session** (parallel
sessions see fresh decisions minutes after each commit), shapes episodes into
work-unit narratives, and survives sessions that never end cleanly. All
triggers share one `conversation_id` (= `session_id`) and one server-side
watermark, so the full transcript is re-sent but only the **delta** is ever
processed — total extraction cost is the same as a single end-of-session
import.

The flush hook authenticates with the plugin's own OAuth (same Auth0 client
as the `/mcp` connector, cached at `~/.config/memhub-plugin/`). A background
hook never opens a browser, so the cache must be seeded once by running any
memhub terminal script interactively — e.g. `/memhub:import-session` — or by
setting `$MEMHUB_TOKEN`. Until then the hook degrades silently (the
SessionEnd agent hook still captures everything at close).

## Skills (v0.5)

Four skills ship in `plugins/memhub/skills/` (the deprecated `commands/`
format is gone; invocation is unchanged). Each is both user-invocable as
`/memhub:<name>` and **model-invocable**: saying "save this spec to memhub" or
"what did we decide about X?" in plain language triggers the right skill.

- `/memhub:import-session <id-or-path> [title]` — terminal upload of a past
  session transcript; auto-chunks very large sessions.
- `/memhub:save-artifact <file> [name]` — terminal upload of a file as an
  artifact. Both upload skills exist so the model never re-emits file or
  transcript content token by token — a helper script ships the bytes.
- `/memhub:search-memory <query>` — read-only recall over facts, episodes,
  artifacts, and documents, with context-base / tag / time filters.
- `/memhub:handoff-session <teammate> [title]` — hand the current session to a
  teammate: creates a context base holding a composed handoff brief (goal,
  state, decisions, next steps, gotchas) plus the full session import, and
  shares it read-only via `share_context_base`. The teammate's agent picks it
  up by searching that context base.

## Fleet plugin (v0.1)

`plugins/fleet/` is a separate, local-only plugin for running **many Claude
Code agents in parallel git worktrees of one repo**. All worktrees share the
repo's common `.git` directory, so a single board file at
`$(git rev-parse --git-common-dir)/fleet-board.json` is visible to every
agent with no server and no auth. Hooks keep it current:

- **SessionStart** — registers the session (branch, worktree, session id),
  prunes stale/ghost entries, and injects a snapshot of the other active
  agents into context.
- **UserPromptSubmit** — heartbeats the entry, refreshes its one-line
  "working on" from your prompt, and injects only the *delta* of sibling
  changes since this agent last looked (joined / ended / committed /
  changed focus). No changes → no injection, no token cost.
- **PostToolUse** (git commits) — records the commit message and files
  touched on this agent's entry, so siblings get collision warnings before
  editing the same files.
- **SessionEnd** — marks the entry ended (siblings see it; pruned later).

Pairs with the memhub plugin: the board says *who is doing what right now*
(seconds, one line each); the flush hook already lands every session's
history in MemHub, so an agent that needs the *why* behind a sibling's
change searches team memory with the session id from the board entry.
Each board entry costs ~1 short line of injected context; everything fails
soft (not a git repo / hook error → silent no-op).

## Notes & trade-offs

- **Auth is per-user.** Nothing secret travels with the plugin — each person
  authenticates the MCP server themselves via `/mcp`. The first time the hook
  tries to use the server you'll see a trust/permission prompt; that's by design.
- **Cost.** The agent hook reads the full transcript and passes it to the tool
  at session end. For long sessions that's a non-trivial token cost once per
  session. (A future refinement: incremental capture via a transcript watermark.)
- **Requires** the backend `import_conversation` tool (MemHub-Backend branch
  `fm-feat/mcp-agentic-session-ingest`) to be deployed to staging.

## Configuration

To point at a different MemHub instance, edit `plugins/memhub/.mcp.json`
(`url` and `oauth.clientId`).

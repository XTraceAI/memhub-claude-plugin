# MemHub for Claude Code

Auto-capture your Claude Code sessions into **MemHub team memory**. At the end of
each session, an agent hook reads the transcript and saves it through the
`memhub-staging` MCP server, which runs **tool-aware (agentic) extraction** of
facts, episodes, and artifacts.

## What's in here

This repo is both a **marketplace** and a single **plugin**:

```
.claude-plugin/marketplace.json     # makes the plugin installable
plugins/memhub/
├── .claude-plugin/plugin.json      # plugin manifest
├── .mcp.json                       # the memhub-staging MCP server (per-user OAuth)
└── hooks/hooks.json                # SessionEnd → agent hook → import_conversation
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

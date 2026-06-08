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

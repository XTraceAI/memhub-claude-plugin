---
description: Import a specific Claude Code session into MemHub via a terminal upload (any size — the script ships the transcript, no token-by-token re-emit)
argument-hint: <session-id-or-path> [title...]
allowed-tools: Bash
---

Import a past Claude Code session into MemHub team memory on demand. A helper
script reads the transcript file and ships it to the `import_conversation` MCP
tool — **do NOT call the MCP tool yourself and do NOT read or paste transcript
content**; sessions can exceed a million tokens and the script handles any
size in one call. This is a terminal operation.

Arguments: `$ARGUMENTS`
- First token = a session id (e.g. `03374a1f-b074-4eb9-9900-...`) or a path to
  a `.jsonl` transcript (required). A bare id is resolved automatically under
  `~/.claude/projects/*/`.
- Remaining text = an optional conversation title.

Do exactly this:

1. Run the import via Bash — one command, substitute the real values:

   ```bash
   uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/import_session.py" \
     --session "<session-id-or-path>" [--title "<title>"] \
     [--context-base-id "<id>"]

   Pass `--context-base-id` when the user wants the session's memories in an
   isolated, shareable context base instead of raw workspace memory (find or
   create one via the memhub MCP's `list_context_bases` / `create_context_base`).
   NOTE: re-imports dedup per conversation_id GLOBALLY — to re-extract an
   already-imported session into a context base, pass a fresh
   `--conversation-id`.
   Very large transcripts are AUTO-CHUNKED (default threshold ~3.5MB): the
   script sends disjoint slices sequentially under one conversation_id and
   waits for each slice's extraction (the session gist folding forward)
   before the next — payloads beyond ~8MB fail server-side as one shot, so
   never disable chunking for huge sessions. This is slow but unattended;
   just let the command run.
   ```

2. Report back the returned `conversation_id`, `path` (should be `"agentic"`
   for Claude Code sessions), `messages_received`, and scope. Tell the user:
   - extraction runs in the background (allow several minutes for large
     sessions) — facts, episodes, artifacts, and the session **gist** land in
     `search_memory` as it completes;
   - re-importing the same session later is **incremental** (only new records
     are processed; the gist folds forward — nothing duplicates).

3. If the script prints an auth error, no setup is needed — the script runs the same
   OAuth flow as /mcp and will open the browser ONCE for approval (token is
   cached after that). If it can't find the session id, ask the user for the
   transcript path.

Never use curl or raw HTTP; never pass transcript content as tool arguments.

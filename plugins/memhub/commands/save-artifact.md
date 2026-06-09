---
description: Store a file as a MemHub artifact via a terminal upload (no token-by-token re-emit)
argument-hint: <file-path> [artifact name]
allowed-tools: Bash
---

Store an existing file as a MemHub artifact. The file's bytes are uploaded by a
helper script — **do NOT call the `save_artifact` MCP tool yourself and do NOT
paste/retype the file contents**; that would regenerate the whole document token
by token. This is a terminal operation, like `cat`-ing a file.

Arguments: `$ARGUMENTS`
- First token = the path to the file to store (required).
- Remaining text = the artifact name (optional; if omitted, use the file's base
  name as a readable title).

Do exactly this:

1. Resolve the file path (`$1`) and a name. If no name was given, derive a short
   Title-Case name from the filename.
2. Pick an `artifact_type` from the extension/content: `spec`, `design_doc`,
   `adr`, `runbook`, or `document` (default).
3. Run the upload via Bash — substitute the real values, keep it one command:

   ```bash
   uv run --with mcp python "${CLAUDE_PLUGIN_ROOT}/scripts/save_artifact.py" \
     --file "<path>" --name "<name>" --type "<type>"
   ```

   Optional flags when relevant: `--context-base-id <id>` to save into a context
   base, `--parent-id <id>` to version an existing artifact, `--rationale "..."`
   to note why this version supersedes the last, `--tags a,b`.
4. Report the returned `{id, action}` to the user. If it prints an auth error,
   tell them to run `memhub login` (the script reuses the memhub-cli token).

You only emit the short command with a path — the script reads the file and
ships it to `save_artifact`.

---
description: Use when the user asks what the fleet is doing, who else is working on this repo, or for fleet/agent status (e.g. "fleet status", "what are my other agents doing", "who's working on what right now", "show the fleet board"). Read-only — pretty-prints the shared fleet board for the current repo.
argument-hint: (none)
allowed-tools: Bash
---

Show the human a readable status of all Claude Code agents working in
worktrees of the current repo, from the shared fleet board. Read-only: never
write to the board.

Do exactly this:

1. Read the board and the current time in one command:

   ```bash
   BOARD="$(git rev-parse --git-common-dir 2>/dev/null)/fleet-board.json"
   date +%s && cat "$BOARD"
   ```

   - Not a git repo, or the file is missing/empty → tell the user there's no
     fleet board here yet (it appears once a session with the fleet plugin
     starts in this repo) and stop.

2. Render one line per agent from `agents`, active first, then ended, most
   recently updated first within each group. Compute ages from the epoch
   seconds you printed (`last_update`, `last_commit.at`). Per agent show:
   - branch (bold), status — `active` with "last seen Xm ago", or `ended Xm ago`;
     flag an active agent silent for over an hour as `stale?`
   - what it's working on (the `working_on` line, if set)
   - last commit: message, files (first few), and how long ago
   - worktree path and the first 8 chars of the session id

   A short markdown table or tight bullet list — whichever reads better for
   the number of agents. One agent (just this one) → say the fleet is only
   this session; no table needed.

3. Close with one summary line, e.g. "3 active across 3 worktrees; last
   commit 12m ago on `fm-fix/oauth-refresh`." If two agents' `last_commit`
   files or `working_on` lines clearly overlap, point out the potential
   collision — that's the most valuable thing this view can surface.

Plain English only — no raw JSON, no epoch numbers, no internal field names.

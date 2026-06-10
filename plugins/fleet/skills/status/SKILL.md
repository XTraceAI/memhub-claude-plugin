---
description: Use when the user asks what the fleet is doing, who else is working on this repo, or for fleet/agent status (e.g. "fleet status", "what are my other agents doing", "who's working on what right now", "show the fleet board"). Read-only — pretty-prints the shared fleet board for the current repo.
argument-hint: (none)
allowed-tools: Bash
---

Show the human a readable status of all Claude Code agents working in
worktrees of the current repo, from the shared fleet board. Read-only: never
write to the board.

Do exactly this:

1. Read the board, the current time, and this session's worktree in one
   command (`--path-format=absolute` matters — the relative form depends on
   the cwd the command happens to run in):

   ```bash
   BOARD="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)/fleet-board.json"
   date +%s && git rev-parse --path-format=absolute --show-toplevel && cat "$BOARD"
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
   the number of agents. Mark the entry whose `worktree` equals the toplevel
   you printed as "(this worktree)" — that is the closest the board gets to
   identifying the current session; never assume a row is this session by
   count alone. With exactly one entry: if it matches this worktree, say the
   fleet is just this session (no table); if it does NOT match, say another
   agent is active in a different worktree and this session isn't on the
   board (its fleet hooks haven't fired here — plugin not loaded, or no
   prompt yet).

3. Close with one summary line, e.g. "3 active across 3 worktrees; last
   commit 12m ago on `fm-fix/oauth-refresh`." If two agents' `last_commit`
   files or `working_on` lines clearly overlap, point out the potential
   collision — that's the most valuable thing this view can surface.

Plain English only — no raw JSON, no epoch numbers, no internal field names.

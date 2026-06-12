---
description: Use when the user wants to start a fleet — split a big task across parallel Claude Code agents in git worktrees (e.g. "/fleet:start <task>", "start a fleet for X", "parallelize this across worktrees", "spin up agents to work on X in parallel"). Decomposes the task, provisions worktrees with kickoff briefs, and launches a real session per stream.
argument-hint: <the big task> [--headless]
allowed-tools: Bash, Write, Read
---

Start a fleet: turn one complicated task into 2–4 parallel Claude Code
sessions, each in its own git worktree, coordinated by the fleet board the
plugin already maintains.

Arguments: `$ARGUMENTS` — the task, in natural language (ask if empty).
`--headless` anywhere in the arguments selects headless mode for all streams.

Do exactly this:

1. **Decompose.** Split the task into 2–4 workstreams that are genuinely
   independent — different files, different subsystems, no stream blocked on
   another's output. Independence is the whole game: the board softens
   collisions, it does not resolve merge conflicts. If the task is inherently
   sequential, SAY SO and offer to run it as one session instead — never
   manufacture fake parallelism.

2. **Confirm the split.** Show the user one compact list — stream name, scope,
   branch, what it must NOT touch — and get a yes/adjustment before creating
   anything. This is the one mandatory pause: a bad decomposition is the
   expensive failure mode.

3. **Provision** each confirmed stream:

   ```bash
   git fetch origin
   git worktree add ../<repo>-<slug> -b <branch-prefix>/<slug> origin/main
   ```

   Derive `<branch-prefix>` from the user's existing branch convention (look
   at `git log --oneline -10` author branches or recent local branches;
   default `feat`). Then Write the kickoff brief to
   `../<repo>-<slug>/.fleet-kickoff.md`: the stream's goal, concrete scope,
   explicit out-of-scope list naming the sibling streams' territory, relevant
   file/dir pointers, and how to finish (commit, push, open a PR vs leave for
   review — mirror what the user normally does). Add `.fleet-kickoff.md` once
   to `$(git rev-parse --path-format=absolute --git-common-dir)/info/exclude`
   so briefs never dirty any worktree.

4. **Launch** one session per stream via the helper — one call each:

   ```bash
   bash "${CLAUDE_PLUGIN_ROOT}/scripts/fleet_start_launch.sh" \
     "<absolute-worktree-path>" \
     "Read .fleet-kickoff.md in this directory — it is your kickoff brief. Follow it." \
     <tab|headless>
   ```

   - `tab` (default): opens an interactive tmux window / iTerm tab / Terminal
     window per stream. Opening windows on the user's screen is visible and
     loud — the step-2 confirmation covers it, but if the user seemed
     hesitant, re-ask before the first launch.
   - `headless`: detached `claude -p` per stream; output in each worktree's
     `.fleet-headless.log`. Use when the user said headless / fire-and-forget.
   - Each launched session registers itself on the fleet board through the
     plugin's own hooks — do not write to the board from here.

5. **Report** the fleet: one line per stream (branch, worktree path, mission),
   how to watch it (`/fleet:status` from any session, or the headless logs),
   and the reminder that streams branch from `origin/main` and merge back via
   normal PRs — flag any stream pair most likely to conflict at merge time.

Never launch more than 4 streams. Never reuse an existing worktree directory
(`git worktree add` fails on non-empty paths — pick a fresh name). If
`claude` is not on PATH in launched shells, tell the user to launch the tabs
manually with the printed commands instead of debugging their shell config.

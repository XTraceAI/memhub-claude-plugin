#!/bin/bash
# Launch one fleet session: a real `claude` session in WORKTREE seeded with
# PROMPT. Used by the /fleet:start skill — one call per workstream.
#
#   fleet_start_launch.sh <worktree> <prompt> [tab|headless]
#
# tab (default): open an interactive session in a new tmux window (when
#   inside tmux), iTerm tab, or Terminal window — whichever matches the
#   user's environment. The session registers on the fleet board via the
#   plugin's own hooks; nothing here touches the board.
# headless: run `claude -p` detached in the worktree; output lands in
#   .fleet-headless.log there.
#
# FLEET_DRY=1 prints the launch action instead of performing it.
set -euo pipefail

WT="$1"
PROMPT="$2"
MODE="${3:-tab}"

[ -d "$WT" ] || { echo "worktree not found: $WT" >&2; exit 1; }

# Headless sessions are non-interactive: they cannot answer permission
# prompts, so without pre-granted permissions a stream stalls right after
# reading its brief. Default: auto-accept file edits + git commands, which
# covers edit-commit-push workstreams while still gating everything else.
# Override with FLEET_HEADLESS_FLAGS (whitespace-split; e.g. set it to
# "--dangerously-skip-permissions" for fully autonomous streams — that
# bypasses ALL gating, so it's an explicit user choice, never the default).
if [ -n "${FLEET_HEADLESS_FLAGS:-}" ]; then
  # shellcheck disable=SC2206
  HEADLESS_FLAGS=( ${FLEET_HEADLESS_FLAGS} )
else
  HEADLESS_FLAGS=( --permission-mode acceptEdits --allowedTools 'Bash(git:*)' )
fi

if [ "${FLEET_DRY:-}" = "1" ]; then
  if [ "$MODE" = "headless" ]; then
    echo "DRY: mode=$MODE worktree=$WT flags=${HEADLESS_FLAGS[*]} prompt=${PROMPT:0:80}..."
  else
    echo "DRY: mode=$MODE worktree=$WT prompt=${PROMPT:0:80}..."
  fi
  exit 0
fi

if [ "$MODE" = "headless" ]; then
  # PROMPT must come before the flags: variadic options like --allowedTools
  # swallow a trailing positional as another value, and claude then dies
  # with "Input must be provided either through stdin or as a prompt
  # argument" (verified against claude CLI 2.x).
  (cd "$WT" && nohup claude -p "$PROMPT" "${HEADLESS_FLAGS[@]}" > .fleet-headless.log 2>&1 &)
  echo "headless session launched in $WT (log: .fleet-headless.log, flags: ${HEADLESS_FLAGS[*]})"
  exit 0
fi

LAUNCH_CMD="cd $(printf %q "$WT") && claude $(printf %q "$PROMPT")"

if [ -n "${TMUX:-}" ]; then
  tmux new-window -c "$WT" -n "$(basename "$WT")" "claude $(printf %q "$PROMPT")"
  echo "tmux window opened for $WT"
elif command -v osascript >/dev/null 2>&1 && [ "${TERM_PROGRAM:-}" = "iTerm.app" ]; then
  osascript - "$WT" "$PROMPT" <<'EOF'
on run argv
  set wt to item 1 of argv
  set p to item 2 of argv
  set launchCmd to "cd " & quoted form of wt & " && claude " & quoted form of p
  tell application "iTerm"
    activate
    try
      tell current window
        create tab with default profile
        tell current session to write text launchCmd
      end tell
    on error
      set newWindow to (create window with default profile)
      tell current session of newWindow to write text launchCmd
    end try
  end tell
end run
EOF
  echo "iTerm tab opened for $WT"
elif command -v osascript >/dev/null 2>&1 && [ "$(uname)" = "Darwin" ]; then
  osascript - "$WT" "$PROMPT" <<'EOF'
on run argv
  set wt to item 1 of argv
  set p to item 2 of argv
  tell application "Terminal"
    activate
    do script "cd " & quoted form of wt & " && claude " & quoted form of p
  end tell
end run
EOF
  echo "Terminal window opened for $WT"
  if [ "${TERM_PROGRAM:-}" = "vscode" ]; then
    echo "note: VS Code's integrated terminal can't be scripted — fleet tabs open in Terminal.app instead" >&2
  fi
elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v gnome-terminal >/dev/null 2>&1; then
  gnome-terminal -- bash -c "$LAUNCH_CMD; exec bash"
  echo "gnome-terminal window opened for $WT"
elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v konsole >/dev/null 2>&1; then
  nohup konsole -e bash -c "$LAUNCH_CMD; exec bash" >/dev/null 2>&1 &
  echo "konsole window opened for $WT"
elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v x-terminal-emulator >/dev/null 2>&1; then
  nohup x-terminal-emulator -e bash -c "$LAUNCH_CMD; exec bash" >/dev/null 2>&1 &
  echo "terminal window opened for $WT"
else
  # no scriptable terminal (e.g. headless Linux, SSH) — degrade gracefully
  # instead of dying on a missing osascript under set -e
  echo "no scriptable terminal available here — start this stream manually:" >&2
  echo "  $LAUNCH_CMD" >&2
  echo "(or re-run in headless mode)" >&2
  exit 0
fi

#!/usr/bin/env bash
# Executable proof of the PostToolUse flush hook's two-stage prefilter.
#
# Runs the EXACT command string from hooks.json (JSON-decoded, like the hook
# runtime does) against synthetic hook inputs. Documents two semantics that
# have repeatedly confused reviewers:
#
#   1. In a shell `case` pattern, quotes are QUOTING syntax, not matched
#      characters: *"gh pr"* matches any input containing the bare substring
#      `gh pr` (the quotes only protect the space). Hook JSON like
#      {"command": "gh pr create"} therefore DOES reach the flush script.
#   2. Stage 2 (flush_prefilter.py) decides on tool_input.command ALONE, so
#      stdout that merely mentions "git commit" / "gh pr" stays silent.
#
# Usage: bash plugins/memhub/scripts/test_flush_hook.sh   (from repo root)
set -u
cd "$(dirname "$0")/../../.." || exit 1
export CLAUDE_PLUGIN_ROOT="$PWD/plugins/memhub"
CMD=$(python3 -c "import json; print(json.load(open('plugins/memhub/hooks/hooks.json'))['hooks']['PostToolUse'][0]['hooks'][0]['command'])")
fail=0

run() { printf %s "$1" | bash -c "$CMD" 2>&1; }

# Reaches flush_session.py (which logs + skips on the synthetic input).
for cmd in "gh pr create --title x" "gh pr merge 5 --squash" \
           "git commit -m x" "git -C /repo commit -am fix" \
           "cd /x && git commit -m y"; do
  out=$(run "{\"tool_input\":{\"command\":\"$cmd\"}}")
  case "$out" in
    *"[memhub-flush]"*) echo "PASS  flush ran:   $cmd" ;;
    *) echo "FAIL  arm did not run: $cmd"; fail=1 ;;
  esac
done

# Must stay SILENT (no flush spawn).
silent() {
  out=$(run "$1")
  if [ -z "$out" ]; then echo "PASS  silent:      $2"; else echo "FAIL  fired on: $2 -> $out"; fail=1; fi
}
silent '{"tool_input":{"command":"ls -la"}}' "innocent command"
silent '{"tool_input":{"command":"cat notes.md"},"tool_response":{"stdout":"run gh pr create later; also git commit"}}' "stdout mention only"
silent '{"tool_input":{"command":"git log --oneline | grep commit"}}' "cross-pipe git…commit"
silent '{"tool_input":{"command":"echo git commit"}}' "echoed mention"

[ "$fail" -eq 0 ] && echo "ALL PASS" || echo "FAILURES PRESENT"
exit "$fail"

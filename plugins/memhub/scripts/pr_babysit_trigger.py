#!/usr/bin/env python3
"""PostToolUse(Bash) hook: after a successful `gh pr create`, inject context
telling Claude to start a self-paced loop running /memhub:pr-babysit on the
new PR. Emits nothing (hook is a no-op) unless the command was a PR creation
whose output contains a PR URL."""

import json
import re
import sys

PR_URL = re.compile(r"https://[^/\s\"\\]+/[^/\s\"\\]+/[^/\s\"\\]+/pull/\d+")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if "gh pr create" not in command:
        return

    # Only arm the loop if a PR URL actually came back — a failed
    # `gh pr create` produces no URL and should stay silent.
    response = payload.get("tool_response")
    blob = response if isinstance(response, str) else json.dumps(response or {})
    match = PR_URL.search(blob)
    if not match:
        return
    url = match.group(0)

    context = (
        f"A pull request was just created: {url} . MemHub PR-babysit policy: "
        "unless the user asked not to babysit PRs (in this session or in memory), "
        "start a self-paced loop NOW that babysits it — invoke the loop skill with "
        f'no interval and the prompt "/memhub:pr-babysit {url}". Each loop pass '
        "polls the PR's review bots (Cursor bugbot, OpenAI Codex) and CI, fixes "
        "real findings, pushes, and when the PR is clean saves the whole fixing "
        "process to the repo's MemHub context base and ends the loop. Tell the "
        "user the babysit loop is running and that saying 'stop the loop' ends it."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": context,
                }
            }
        )
    )


if __name__ == "__main__":
    main()

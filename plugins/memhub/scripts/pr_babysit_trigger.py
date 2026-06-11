#!/usr/bin/env python3
"""PostToolUse(Bash) hook: after a successful `gh pr create`, inject context
telling Claude to start a self-paced loop running /memhub:pr-babysit on the
new PR. Emits nothing (hook is a no-op) unless the command was a PR creation
whose output contains a PR URL."""

import json
import re
import sys

PR_URL = re.compile(r"https?://[^/\s\"\\]+/[^/\s\"\\]+/[^/\s\"\\]+/pull/\d+")
# Quoted segments are stripped before matching so a search pattern like
# grep "gh pr create" can never look like a PR creation.
QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")
# `gh ... pr create` only with `gh` at command position — start of string or
# after a separator (&&, ;, |, $(, backtick, newline), optionally preceded by
# env-var assignments — with flags allowed between tokens but never across a
# separator (`gh repo view && foo pr create` must not match).
GH_PR_CREATE = re.compile(
    r"(?:^|[;&|`\n]|\$\()\s*(?:\w+=\S*\s+)*gh\b[^|;&\n]*?\bpr\s+create\b"
)


def is_pr_create(command: str) -> bool:
    return bool(GH_PR_CREATE.search(QUOTED.sub(" ", command)))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict):
        return

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not is_pr_create(command):
        return

    # Only arm the loop if a PR URL actually came back — a failed
    # `gh pr create` produces no URL and should stay silent. Scan stdout
    # only when the response shape exposes it: gh prints the new PR's URL
    # there, and stderr/other fields could echo unrelated PR URLs.
    response = payload.get("tool_response")
    if isinstance(response, dict) and isinstance(response.get("stdout"), str):
        blob = response["stdout"]
    else:
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

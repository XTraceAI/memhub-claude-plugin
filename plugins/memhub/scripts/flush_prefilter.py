#!/usr/bin/env python3
"""Precise stage-2 prefilter for the commit/PR flush hook (stdlib only).

The hooks.json stage-1 shell gate is a crude substring test over the WHOLE
hook JSON — cheap, but it passes when e.g. a `cat`'s stdout merely contains
"git commit". This script reads the hook input and decides on
``tool_input.command`` ALONE: exit 0 (flush) only when the command actually
runs a commit / PR action, including flagged variants (`git -C <path>
commit`, `git -c k=v commit`) and compound commands (`cd x && git commit`).

Runs under the system python3 (no uv, no deps) so the expensive
`uv run ... flush_session.py` spawn only happens on true triggers.
"""
from __future__ import annotations

import json
import re
import sys

# A new shell segment (start, or after ; & | parens) beginning with `git`,
# then up to a few non-separator tokens (flags like -C <path>, -c k=v,
# --git-dir=x), then `commit`. Segment-bounded tokens ([^\s;&|()]+) stop the
# match from crossing pipes — `git log --oneline | grep commit` won't fire.
_GIT_COMMIT = re.compile(
    r"(?:^|[;&|()]\s*)git(?:\s+[^\s;&|()]+){0,4}?\s+commit\b"
)
_GH_PR = re.compile(r"(?:^|[;&|()]\s*)gh\s+pr\s+(?:create|merge)\b")


def should_flush(command: str) -> bool:
    return bool(_GIT_COMMIT.search(command) or _GH_PR.search(command))


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        command = str((hook_input.get("tool_input") or {}).get("command", ""))
    except Exception:  # noqa: BLE001 — malformed input → don't flush
        return 1
    return 0 if should_flush(command) else 1


if __name__ == "__main__":
    raise SystemExit(main())

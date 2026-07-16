#!/usr/bin/env python3
"""Codex `notify` handler — best-effort auto-capture of Codex sessions.

Wire it up in ``~/.codex/config.toml``::

    notify = ["python3", "/absolute/path/to/codex/codex_notify.py"]

Codex invokes the notify program with a single JSON argument describing an
event. On a turn/session-completion event this fires
``import_codex_session.py --session latest`` **detached** (fire-and-forget), so
Codex is never blocked. Re-importing is incremental — the server watermark
folds the session gist forward instead of duplicating — so it's safe to fire on
every turn.

This is deliberately conservative: unknown event shapes, parse failures, and
missing files all exit 0 silently. Auto-capture is a convenience; the manual
`import_codex_session.py` is always the reliable path.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# Events that mean "a turn finished, the rollout has new content worth banking".
_CAPTURE_EVENTS = {"agent-turn-complete", "turn-complete", "session-complete"}


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    try:
        event = json.loads(sys.argv[-1])
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict):
        return 0
    etype = event.get("type") or event.get("event")
    if etype not in _CAPTURE_EVENTS:
        return 0

    importer = Path(__file__).resolve().parent / "import_codex_session.py"
    if not importer.is_file():
        return 0

    # Detached, output discarded — never slow or crash the Codex session.
    try:
        subprocess.Popen(
            ["uv", "run", "--with", "mcp", "python", str(importer),
             "--session", "latest"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(importer.parent.parent),
            env={**os.environ},
        )
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

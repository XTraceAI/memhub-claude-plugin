#!/usr/bin/env python3
"""Import an OpenAI Codex session into MemHub team memory.

Reads a Codex *rollout* transcript (``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl``),
transforms it into the Claude Code record shape (see ``codex_to_claude``), and
hands it to the plugin's ``import_session.py`` — reusing its chunking, session
gist fold-forward, incremental-dedup, namespace resolution, and the SAME OAuth
the /mcp connector uses. The transform is what routes the session to the
tool-aware **agentic** ingestion path with no backend change.

Usage (mcp SDK pulled ephemerally by uv):

    uv run --with mcp python codex/import_codex_session.py --session latest
    uv run --with mcp python codex/import_codex_session.py \
        --session <rollout-path|session-id|latest> \
        [--agent-brain-id <id>] [--conversation-id <id>] [--title "..."] \
        [--url <mcp-url>]

``--session`` accepts a rollout path, a bare Codex session id (searched under
``~/.codex/sessions``), or ``latest`` for the most recently modified rollout.
The conversation id defaults to ``codex-<session-id>`` so re-imports are
incremental (the server watermark folds the gist forward instead of
duplicating).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_to_claude import load_rollout, rollout_to_claude_records  # noqa: E402

_SESSIONS = Path.home() / ".codex" / "sessions"
_IMPORT_SESSION = (Path(__file__).resolve().parent.parent
                   / "plugins" / "memhub" / "scripts" / "import_session.py")


def resolve_rollout(session: str) -> tuple[Path | None, str]:
    """Accept a rollout path, ``latest``, or a bare Codex session id."""
    p = Path(session).expanduser()
    if p.is_file():
        return p, ""
    if "/" in session and session != "latest":
        return None, f"rollout file not found: {p}"
    files = [Path(f) for f in glob.glob(str(_SESSIONS / "**" / "rollout-*.jsonl"),
                                        recursive=True)]
    if not files:
        return None, f"no Codex rollouts under {_SESSIONS}"
    if session == "latest":
        return max(files, key=lambda f: f.stat().st_mtime), ""
    # match a rollout whose filename ends with the session id
    sid = session.removesuffix(".jsonl")
    hits = [f for f in files if f.stem.endswith(sid)]
    if not hits:
        return None, f"no Codex rollout matching session id {sid!r} under {_SESSIONS}"
    return max(hits, key=lambda f: f.stat().st_size), ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a Codex session into MemHub.")
    ap.add_argument("--session", required=True,
                    help="rollout path, a bare Codex session id, or 'latest'")
    ap.add_argument("--conversation-id", default=None,
                    help="override (default: codex-<session-id>, for incremental dedup)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--agent-brain-id", default=None)
    ap.add_argument("--namespace", default=None,
                    help="repo scope for captured directives; default resolves "
                         "from the session's cwd via git remote, '' disables")
    ap.add_argument("--url", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="write the transformed transcript and print a summary, "
                         "but do not call the import tool")
    args = ap.parse_args()

    f, err = resolve_rollout(args.session)
    if f is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    rollout = load_rollout(f)
    if not rollout:
        print(f"ERROR: {f} has no valid records", file=sys.stderr)
        return 2
    records, meta = rollout_to_claude_records(rollout)
    if not records:
        print(f"ERROR: nothing to import from {f}", file=sys.stderr)
        return 2

    sid = meta.get("session_id") or f.stem
    conv_id = args.conversation_id or f"codex-{sid}"
    title = args.title or meta.get("title")

    n_tool = sum(1 for r in records
                 if isinstance(r["message"].get("content"), list)
                 and r["message"]["content"][0].get("type") == "tool_use")
    print(f"rollout         : {f}")
    print(f"codex session   : {sid}   (model {meta.get('model')})")
    print(f"records         : {len(records)}  ({n_tool} tool calls)")
    print(f"cwd             : {meta.get('cwd')}")
    print(f"conversation_id : {conv_id}")
    print(f"title           : {title}")
    print("-" * 56)

    # Materialise the Claude-shaped transcript for import_session.py. Named
    # codex-<sid>.jsonl so a bare run (no --conversation-id) still gets a
    # stable, codex-scoped id from the file stem.
    tmpdir = Path(tempfile.mkdtemp(prefix="memhub-codex-"))
    transcript = tmpdir / f"codex-{sid}.jsonl"
    transcript.write_text("".join(json.dumps(r) + "\n" for r in records))

    if args.dry_run:
        print(f"[dry-run] wrote {len(records)} records -> {transcript}")
        print("[dry-run] skipping import_conversation")
        return 0

    cmd = [
        "uv", "run", "--with", "mcp", "python", str(_IMPORT_SESSION),
        "--session", str(transcript),
        "--conversation-id", conv_id,
    ]
    if title:
        cmd += ["--title", title]
    if args.agent_brain_id:
        cmd += ["--agent-brain-id", args.agent_brain_id]
    if args.namespace is not None:
        cmd += ["--namespace", args.namespace]
    if args.url:
        cmd += ["--url", args.url]

    try:
        return subprocess.run(cmd).returncode
    finally:
        try:
            transcript.unlink()
            tmpdir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())

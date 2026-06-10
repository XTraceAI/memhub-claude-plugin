#!/usr/bin/env python3
"""Import a specific Claude Code session into MemHub — a terminal operation.

The transcript is read off disk and shipped straight to the
`import_conversation` MCP tool: the model never re-emits the content, so a
session of ANY size works (validated end-to-end at 2,305 records /
~1.4M tokens / 5.5MB in one call).

Mirrors the SessionEnd hook's contract exactly:
- raw transcript records passed AS-IS (the tool auto-detects the Claude Code
  shape and runs agentic, tool-aware extraction)
- `conversation_id` = the session id (file stem) by default, so re-imports of
  the same session are INCREMENTAL: the server-side watermark admits only
  records it hasn't seen, and the session gist folds forward instead of
  duplicating.

Auth = the SAME OAuth the /mcp connector uses (shared `_memhub_auth`):
$MEMHUB_TOKEN if set (CI escape hatch), else the cached plugin OAuth token,
else a one-time browser approval. No memhub-cli required.

Usage (mcp SDK pulled ephemerally by uv):
    uv run --with mcp python import_session.py --session <session-id-or-path>
        [--conversation-id <id>] [--title "..."] [--url <mcp-url>]

`--session` accepts either a path to a .jsonl transcript or a bare session id,
which is resolved by searching ~/.claude/projects/*/<id>.jsonl.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402


def load_transcript(path: Path) -> tuple[list[dict], int]:
    """Parse a JSONL transcript tolerantly.

    Returns ``(records, malformed_count)`` — malformed lines are skipped, not
    fatal, because real transcripts occasionally carry a truncated final line
    (interrupted write). The caller decides what to do when nothing parses.
    """
    records: list[dict] = []
    malformed = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1
    return records, malformed


def resolve_session_file(session: str) -> tuple[Path | None, str]:
    """Accept a path, or a bare session id searched under ~/.claude/projects.

    Returns ``(file, error_reason)`` — exactly one is set. A PATH-shaped
    argument (contains a separator) that doesn't exist is its own error;
    it must NOT fall through to the id glob, which would blame the
    projects-dir lookup for a plain file typo.

    Top-level session transcripts only — subagent/workflow .jsonl files live
    in subdirectories and are not sessions. If the same session id exists
    under several project dirs (relocated checkouts), prefer the largest
    file (the most complete transcript).
    """
    p = Path(session).expanduser()
    if p.is_file():
        return p, ""
    if "/" in session:
        return None, f"transcript file not found: {p}"
    sid = session.removesuffix(".jsonl")
    candidates = sorted(
        Path.home().glob(f".claude/projects/*/{sid}.jsonl"),
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    if not candidates:
        return None, (f"no session {sid!r} found under ~/.claude/projects/*/ "
                      "(pass a transcript path instead?)")
    return candidates[0], ""


def unwrap(result) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for b in getattr(result, "content", []) or []:
        t = getattr(b, "text", None)
        if t:
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return {"_raw": t}
    return {"_raw": str(result)}


async def main() -> int:
    ap = argparse.ArgumentParser(description="Import a Claude Code session into MemHub.")
    ap.add_argument("--session", required=True,
                    help="path to a .jsonl transcript, or a bare session id")
    ap.add_argument("--conversation-id", default=None,
                    help="override the conversation id (default: session id, for incremental dedup)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--context-base-id", default=None,
                    help="route the extracted facts/episodes into a context base "
                         "(isolated, shareable) instead of raw workspace memory")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    f, err = resolve_session_file(args.session)
    if f is None:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2

    records, malformed = load_transcript(f)
    if malformed:
        # Transcripts can carry a truncated final line (interrupted write) or
        # stray non-JSON noise; one bad line must not abort a 2,000-record
        # import. Skip-and-report, fail only if NOTHING is parseable.
        print(f"WARNING: skipped {malformed} malformed JSONL line(s) in {f}",
              file=sys.stderr)
    if not records:
        print(f"ERROR: {f} contains no valid JSONL records", file=sys.stderr)
        return 2

    conv_id = args.conversation_id or f.stem
    url, headers, auth = resolve_url_and_auth(args.url)
    size = f.stat().st_size
    print(f"session file    : {f}")
    print(f"records         : {len(records)}   ({size:,} bytes ≈ {size // 4:,} tokens)")
    print(f"conversation_id : {conv_id}")
    print(f"endpoint        : {url}")
    if args.context_base_id:
        print(f"context base    : {args.context_base_id}")
    print("-" * 56)

    call_args: dict = {
        "messages": records,
        "conversation_id": conv_id,
        "source_platform": "claude",
    }
    if args.title:
        call_args["title"] = args.title
    if args.context_base_id:
        call_args["context_base_id"] = args.context_base_id

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("import_conversation", arguments=call_args)
            print(json.dumps(unwrap(res), indent=2))
    print("-" * 56)
    print("Queued. Extraction runs in the background (minutes for large "
          "sessions); facts/episodes/artifacts + the session gist appear in "
          "search_memory as it completes. Re-running the same session later "
          "imports only NEW records (watermark) and folds the gist forward.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

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

Auth reuses the memhub-cli login (no credentials handled here): prefer
$MEMHUB_TOKEN, else `memhub token`, else `uvx memhub token`.

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
import os
import subprocess
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def default_url() -> str:
    """--url > env > the plugin connector's .mcp.json > staging default."""
    base = os.environ.get("MEMHUB_MCP_BASE_URL")
    if base:
        path = os.environ.get("MEMHUB_MCP_SERVER_PATH", "/mcp-server/mcp")
        return f"{base.rstrip('/')}{path}"
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    cfg = (Path(root) if root else Path(__file__).resolve().parents[1]) / ".mcp.json"
    try:
        servers = json.loads(cfg.read_text()).get("mcpServers", {})
        name = next((k for k in servers if k.lower().startswith("memhub")),
                    next(iter(servers)) if len(servers) == 1 else None)
        if name and servers[name].get("url"):
            return servers[name]["url"]
    except (OSError, json.JSONDecodeError):
        pass
    return "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"


def resolve_token() -> str:
    token = os.environ.get("MEMHUB_TOKEN", "").strip()
    if token:
        return token
    for cmd in (["memhub", "token"], ["uvx", "memhub", "token"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip().splitlines()[-1].strip()
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            print(f"`{' '.join(cmd)}` failed: {e}", file=sys.stderr)
    return ""


def resolve_session_file(session: str) -> Path | None:
    """Accept a path, or a bare session id searched under ~/.claude/projects.

    Top-level session transcripts only — subagent/workflow .jsonl files live
    in subdirectories and are not sessions. If the same session id exists
    under several project dirs (relocated checkouts), prefer the largest
    file (the most complete transcript).
    """
    p = Path(session).expanduser()
    if p.is_file():
        return p
    sid = session.removesuffix(".jsonl")
    candidates = sorted(
        Path.home().glob(f".claude/projects/*/{sid}.jsonl"),
        key=lambda f: f.stat().st_size,
        reverse=True,
    )
    return candidates[0] if candidates else None


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

    f = resolve_session_file(args.session)
    if f is None:
        print(f"ERROR: no transcript found for {args.session!r} "
              f"(looked under ~/.claude/projects/*/)", file=sys.stderr)
        return 2

    records = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    if not records:
        print(f"ERROR: {f} is empty", file=sys.stderr)
        return 2

    token = resolve_token()
    if not token:
        print("ERROR: no token. Run `memhub login` or set MEMHUB_TOKEN.", file=sys.stderr)
        return 2

    conv_id = args.conversation_id or f.stem
    url = args.url or default_url()
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

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (r, w, _):
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

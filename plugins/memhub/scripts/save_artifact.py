#!/usr/bin/env python3
"""Store an artifact from a FILE (or stdin) — a terminal operation.

The point: the artifact body is read off disk / the pipe and shipped straight
to the `save_artifact` MCP tool. The model never re-emits the content token by
token — it just runs this with a path, the same way it would `cat` a file.

Auth reuses the memhub-cli token (no credential handling here): prefer
$MEMHUB_TOKEN, else `memhub token` on PATH.

Run (mcp SDK pulled ephemerally by uv):
    uv run --with mcp python scripts/save_artifact.py \
        --file spec.md --name "Retry Policy Spec" --type spec \
        [--context-base-id <id>] [--parent-id <id>] [--rationale "..."] \
        [--tags a,b]

    # or pipe terminal output straight in:
    pytest -q | uv run --with mcp python scripts/save_artifact.py \
        --stdin --name "test run 2026-06-09" --type runbook

Endpoint resolution (so the script hits the SAME server the plugin connector
uses, by construction): --url > $MEMHUB_MCP_BASE_URL(+$MEMHUB_MCP_SERVER_PATH) >
the plugin's .mcp.json `mcpServers.*.url` > staging default.
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


def _mcp_json_url() -> str | None:
    """The endpoint the plugin's MCP connector uses — the source of truth.

    Reads ``<plugin_root>/.mcp.json`` (``$CLAUDE_PLUGIN_ROOT`` when installed,
    else the script's parent dir) and returns the server ``url``. Keyed by
    server *name*, so prefer a ``memhub*`` entry, then fall back to the sole
    entry. Returns None if the file/entry is absent or unreadable.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    cfg = (Path(root) if root else Path(__file__).resolve().parents[1]) / ".mcp.json"
    try:
        servers = json.loads(cfg.read_text()).get("mcpServers", {})
    except (OSError, json.JSONDecodeError):
        return None
    if not servers:
        return None
    name = next((k for k in servers if k.lower().startswith("memhub")),
                next(iter(servers)) if len(servers) == 1 else None)
    return servers.get(name, {}).get("url") if name else None


def default_url() -> str:
    # Explicit env override (matches memhub-cli's own config knobs).
    base = os.environ.get("MEMHUB_MCP_BASE_URL")
    if base:
        path = os.environ.get("MEMHUB_MCP_SERVER_PATH", "/mcp-server/mcp")
        return f"{base.rstrip('/')}{path}"
    # Otherwise follow the plugin connector's endpoint, then fall back to staging.
    return _mcp_json_url() or "https://api.staging.memhub.xtrace.ai/mcp-server/mcp"


def resolve_token() -> str:
    token = os.environ.get("MEMHUB_TOKEN", "").strip()
    if token:
        return token
    # The same token `memhub token` prints (memhub-cli OAuth cache). Try the
    # installed CLI, then `uvx` as a fallback for un-installed environments.
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


def unwrap(result) -> dict:
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw": text}
    return {"_raw": str(result)}


async def main() -> int:
    ap = argparse.ArgumentParser(description="Store a file/stdin as a MemHub artifact.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", type=Path, help="path to the artifact body")
    src.add_argument("--stdin", action="store_true", help="read body from stdin")
    ap.add_argument("--name", required=True, help="artifact title (re-using a name versions it)")
    ap.add_argument("--type", default="document", help="artifact_type (spec/design_doc/runbook/...)")
    ap.add_argument("--context-base-id", default=None)
    ap.add_argument("--parent-id", default=None, help="version an existing artifact by id")
    ap.add_argument("--rationale", default=None, help="why this version supersedes the last")
    ap.add_argument("--tags", default=None, help="comma-separated tags")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    content = sys.stdin.read() if args.stdin else args.file.read_text()
    if not content.strip():
        print("ERROR: artifact body is empty", file=sys.stderr)
        return 2

    token = resolve_token()
    if not token:
        print("ERROR: no token. Run `memhub login` or set MEMHUB_TOKEN.", file=sys.stderr)
        return 2

    call_args: dict = {"name": args.name, "content": content, "artifact_type": args.type}
    if args.context_base_id:
        call_args["context_base_id"] = args.context_base_id
    if args.parent_id:
        call_args["parent_id"] = args.parent_id
    if args.rationale:
        call_args["rationale"] = args.rationale
    if args.tags:
        call_args["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]

    url = args.url or default_url()
    src_desc = "stdin" if args.stdin else str(args.file)
    print(f"source   : {src_desc}  ({len(content):,} chars)")
    print(f"name     : {args.name}   type={args.type}")
    print(f"endpoint : {url}")
    print("-" * 56)

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool("save_artifact", arguments=call_args)
            out = unwrap(res)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

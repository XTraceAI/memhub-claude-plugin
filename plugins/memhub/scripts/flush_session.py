#!/usr/bin/env python3
"""Incremental session flush — fired by PostToolUse hooks on commit/PR events.

Reads the Claude Code hook input JSON from stdin (``session_id``,
``transcript_path``), sends the transcript-so-far to ``import_conversation``
with ``conversation_id = session_id`` — the SAME id the SessionEnd hook uses,
so every trigger feeds one conversation and one server-side watermark
(``agentic_seen_uuids``): the full transcript is re-sent, but only the DELTA
since the last flush is processed. Commits/PRs are semantic work boundaries,
so flushing here makes memory available mid-session (parallel sessions see
fresh decisions), shapes batch episodes into work-unit narratives, and gives
the gist's fold-forward an outcome-flavored cadence. SessionEnd remains the
backstop for sessions that never commit.

Discipline mirrors the SessionEnd hook: THIS SCRIPT NEVER FAILS LOUDLY —
any error exits 0 quietly (the hook is async fire-and-forget; memory capture
must never disturb the user's session).

Auth reuses the memhub-cli token ($MEMHUB_TOKEN -> `memhub token` ->
`uvx memhub token`). Endpoint: --url > $MEMHUB_MCP_BASE_URL(+_SERVER_PATH) >
the plugin's .mcp.json mcpServers.*.url > staging default.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path


def _log(msg: str) -> None:
    # Hook stdout is only shown in verbose/error views; keep one-liners.
    print(f"[memhub-flush] {msg}")


def default_url() -> str:
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
        url = servers.get(name, {}).get("url") if name else None
        if url:
            return url
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
        except Exception:  # noqa: BLE001 — silent-degrade discipline
            continue
    return ""


async def _flush(session_id: str, transcript_path: str) -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    records = []
    with open(transcript_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        _log("empty transcript; nothing to flush")
        return

    token = resolve_token()
    if not token:
        _log("no token (run `memhub login`); skipping flush")
        return

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(default_url(), headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool("import_conversation", arguments={
                "messages": records,
                "conversation_id": session_id,
                "source_platform": "claude",
            })
            texts = [t for t in (getattr(b, "text", None)
                                 for b in getattr(res, "content", []) or []) if t]
            # MCP signals tool failure via isError + a message in content,
            # NOT via an exception — without this check a bad token or
            # server error logs as success while memory never updates.
            if getattr(res, "isError", False):
                _log(f"flush FAILED: {(texts[0] if texts else 'no detail')[:200]}")
                return
            out = getattr(res, "structuredContent", None)
            if isinstance(out, dict) and "conversation_id" not in out \
                    and isinstance(out.get("result"), dict):
                out = out["result"]  # FastMCP wraps some returns in {"result": …}
            if not isinstance(out, dict):
                for text in texts:
                    try:
                        out = json.loads(text)
                        break
                    except json.JSONDecodeError:
                        continue
            if isinstance(out, dict) and "conversation_id" in out:
                _log(f"flushed {out.get('messages_received')} records "
                     f"(conv {str(out.get('conversation_id'))[:8]}, "
                     f"path={out.get('path')}) — server processes the delta")
            else:
                # Not an error per the protocol, but not the shape
                # import_conversation returns either — log what came back
                # instead of claiming success on an arbitrary body.
                _log("flush response unrecognized: "
                     f"{(texts[0] if texts else '')[:120]!r}")


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        session_id = hook_input.get("session_id")
        transcript_path = hook_input.get("transcript_path")
        if not session_id or not transcript_path or not Path(transcript_path).exists():
            _log("missing session_id/transcript_path; skipping")
            return 0
        cmd = str((hook_input.get("tool_input") or {}).get("command", ""))[:120]
        _log(f"trigger: {cmd!r}")
        asyncio.run(_flush(session_id, transcript_path))
    except Exception as e:  # noqa: BLE001 — never fail the hook
        _log(f"skipped ({type(e).__name__}: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

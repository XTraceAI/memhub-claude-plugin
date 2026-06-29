"""PreToolUse hook — recall situated directives for the in-flight tool call.

Fires before Edit / Write / Bash. Reads the hook JSON (``tool_name`` +
``tool_input``), asks MemHub's ``recall_directives`` tool which lessons /
procedures fire on the concrete identifiers in that call (file paths, commands,
symbols), and injects any hits back as ``additionalContext`` so the agent sees
them BEFORE it acts. This is the serving half of procedural memory — fire on the
symbols you're touching mid-task, not on the opening prompt.

**Retrieve-only + fail-open.** ``recall_directives`` is the deterministic symbol
tripwire (no LLM gate), so it's fast. The whole hook is best-effort: on a slow
call, an auth gap, or any error we emit nothing and exit 0 — a memory lookup
must NEVER block or break the agent's tool call. A tight internal timeout bounds
the wait; the hooks.json ``timeout`` is the hard backstop.

Invoked as: ``uv run --with mcp python directive_recall.py`` with the PreToolUse
hook JSON on stdin.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402

# Bound on the recall round-trip. Kept tight: this runs synchronously before the
# tool, so a hung server can't be allowed to stall the agent. Fail-open on hit.
_RECALL_TIMEOUT_S = 1.5
_MAX_DIRECTIVES = 5


def _log(msg: str) -> None:
    print(f"[memhub-directive] {msg}", file=sys.stderr)


def _render(items: list[dict]) -> str:
    """Plain-English context block from the recalled directives."""
    lines = ["## 📋 Relevant team directives for this action",
             "(situated lessons/procedures that fired on what you're touching — "
             "act on them)"]
    for d in items[:_MAX_DIRECTIVES]:
        kind = str(d.get("type", "directive")).upper()
        text = str(d.get("content", "")).strip()
        triggers = ", ".join(str(t) for t in (d.get("triggers") or [])[:4])
        lines.append(f"- **[{kind}]** {text}"
                     + (f"  _(triggers: {triggers})_" if triggers else ""))
    return "\n".join(lines)


async def _recall(tool: str, args: dict) -> list[dict]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url, headers, auth = resolve_url_and_auth(interactive=False)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            res = await session.call_tool("recall_directives", arguments={
                "tool": tool,
                "args": args,
            })
            if getattr(res, "isError", False):
                texts = [t for t in (getattr(b, "text", None)
                         for b in getattr(res, "content", []) or []) if t]
                _log(f"recall FAILED: {(texts[0] if texts else 'no detail')[:160]}")
                return []
            out = getattr(res, "structuredContent", None)
            if isinstance(out, dict) and isinstance(out.get("result"), dict) \
                    and "items" not in out:
                out = out["result"]  # FastMCP sometimes wraps in {"result": …}
            if not isinstance(out, dict):
                for b in getattr(res, "content", []) or []:
                    text = getattr(b, "text", None)
                    if text:
                        try:
                            out = json.loads(text)
                            break
                        except json.JSONDecodeError:
                            continue
            items = out.get("items") if isinstance(out, dict) else None
            return items if isinstance(items, list) else []


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
        tool = hook_input.get("tool_name") or ""
        args = hook_input.get("tool_input") or {}
        if not tool or not isinstance(args, dict):
            return 0
        items = asyncio.run(asyncio.wait_for(_recall(tool, args), _RECALL_TIMEOUT_S))
        if items:
            _log(f"{len(items)} directive(s) fired for {tool}")
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _render(items),
                }
            }))
    # BaseException (not Exception): anyio task groups can surface a
    # BaseExceptionGroup (e.g. auth cancelling siblings). This hook is
    # best-effort — never fail or block the tool call. Emit nothing, exit 0.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        _log(f"skipped ({type(e).__name__}: {str(e)[:120]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

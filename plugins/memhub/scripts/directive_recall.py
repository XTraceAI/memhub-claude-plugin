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

**Client-side precision gate.** The server tripwire matches loosely and has no
LLM gate, so it can surface directives that only broadly overlap the call — most
painfully a directive whose ``trigger_entities`` include the repo name, which
then fires on *every* command in the repo. ``_precision_filter`` re-imposes the
intended contract before injection: keep a directive only if one of its concrete
triggers literally appears in the handle we fired on (command / file_path),
after dropping always-on tokens (the repo name from ``cwd`` + generic filler).
Fail-open — unverifiable or error cases keep the items untouched.

Invoked as: ``uv run --with mcp python directive_recall.py`` with the PreToolUse
hook JSON on stdin.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402

# Bound on the recall round-trip. Kept tight: this runs synchronously before the
# tool, so a hung server can't be allowed to stall the agent. Fail-open on hit.
_RECALL_TIMEOUT_S = 1.5
_MAX_DIRECTIVES = 5

# The firing signal for a tool call is its identifying handle — the file path
# for an edit/write, the command for Bash — NOT the file body or diff hunks.
_ID_ARG_KEYS = ("file_path", "notebook_path", "command")
_MAX_ARG_CHARS = 500


def _log(msg: str) -> None:
    print(f"[memhub-directive] {msg}", file=sys.stderr)


def _recall_args(tool_input: dict) -> dict:
    """Trim tool_input to what the tripwire should fire on.

    Sending the whole ``tool_input`` ships large ``content`` / ``new_string``
    blobs to the server on every Edit/Write and lets symbols buried in the new
    content spuriously match directives. So prefer the identifying handles
    (``file_path`` / ``command``); for a tool we don't special-case, fall back to
    a size-capped copy so recall still has something concrete to fire on.
    """
    ids = {
        k: v for k in _ID_ARG_KEYS
        if isinstance(v := tool_input.get(k), str) and v
    }
    if ids:
        return ids
    return {
        k: (v[:_MAX_ARG_CHARS] if isinstance(v, str) else v)
        for k, v in tool_input.items()
    }


# --- client-side precision gate -------------------------------------------
# Always-on tokens that must never be a directive's sole anchor: English/keyword
# filler + (dynamically) the repo name from cwd. A trigger of just one of these
# would fire on ~every call, which is the exact noise this gate removes.
_GENERIC_TOKENS = frozenset({
    "true", "false", "none", "null", "self", "this", "that", "with",
    "from", "when", "into", "your", "code", "file", "path", "main",
    "test", "tests", "todo", "temp", "data",
})
_MIN_TOKEN_LEN = 4


def _repo_tokens(cwd: str) -> set[str]:
    """Always-on tokens derived from the working dir (the repo name + parts).

    A trigger equal to the repo (e.g. ``MemHub-Backend`` → ``memhub`` /
    ``backend``) matches essentially every call, so it can't stand alone.
    """
    base = Path(cwd).name.lower() if cwd else ""
    if not base:
        return set()
    toks = {base}
    toks.update(w for w in re.split(r"[^a-z0-9]+", base) if len(w) >= _MIN_TOKEN_LEN)
    return toks


def _trigger_tokens(trigger: str) -> set[str]:
    """Concrete, matchable tokens for one trigger entity.

    The full string, plus (for a path) its basename and extension-less stem,
    plus long identifier words. Short fragments are dropped so ``app`` / ``py``
    can't drive a spurious match.
    """
    t = trigger.strip().lower()
    if not t:
        return set()
    toks = {t}
    if "/" in t or "." in t:
        base = t.rsplit("/", 1)[-1]
        toks.add(base)
        if "." in base:
            toks.add(base.rsplit(".", 1)[0])
    toks.update(w for w in re.split(r"[^a-z0-9_]+", t) if len(w) >= 5)
    return {w for w in toks if len(w) >= _MIN_TOKEN_LEN}


def _precision_filter(items: list[dict], args: dict, cwd: str) -> list[dict]:
    """Keep only directives that concretely match the handle we fired on.

    An item survives when it declares no triggers (unverifiable → trusted) or
    when at least one of its non-generic trigger tokens is a substring of the
    call's identifying handle (command / file_path). Fail-open: any error
    returns ``items`` unchanged, so the gate can never suppress the feature.
    """
    try:
        haystack = " ".join(
            v.lower() for v in args.values() if isinstance(v, str)
        )
        if not haystack:
            return items
        blocked = _GENERIC_TOKENS | _repo_tokens(cwd)
        kept: list[dict] = []
        for d in items:
            triggers = d.get("triggers")
            if not isinstance(triggers, list) or not triggers:
                kept.append(d)  # no declared triggers → can't verify → keep
                continue
            tokens = {
                tok
                for t in triggers if isinstance(t, str)
                for tok in _trigger_tokens(t)
                if tok not in blocked
            }
            if any(tok in haystack for tok in tokens):
                kept.append(d)
        return kept
    except Exception:  # noqa: BLE001 — the gate must never break the hook
        return items


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
        recall_args = _recall_args(args)
        items = asyncio.run(
            asyncio.wait_for(_recall(tool, recall_args), _RECALL_TIMEOUT_S)
        )
        # Re-impose the symbol-tripwire contract the loose server match doesn't:
        # only surface directives whose triggers concretely hit this call.
        items = _precision_filter(items, recall_args, hook_input.get("cwd") or "")
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

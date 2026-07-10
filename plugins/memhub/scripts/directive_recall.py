"""PreToolUse hook — recall situated directives for the in-flight tool call.

Fires before Edit / Write / Bash. Reads the hook JSON (``tool_name`` +
``tool_input``), asks MemHub's ``recall_directives`` tool which lessons /
procedures fire on the concrete identifiers in that call (file paths, commands,
symbols), and injects any hits back as ``additionalContext`` so the agent sees
them BEFORE it acts. This is the serving half of procedural memory — fire on the
symbols you're touching mid-task, not on the opening prompt.

**Server funnel + fail-open.** The server runs the full v4 precision funnel
(symbol tripwire → contextual match semantics → LLM relevance gate, fail-open
past its 0.8s budget). The whole hook is best-effort: on a slow call, an auth
gap, or any error we emit nothing and exit 0 — a memory lookup must NEVER block
or break the agent's tool call. A tight internal timeout bounds the wait; the
hooks.json ``timeout`` is the hard backstop.

**Session already_fired.** A directive injects at most once per session: the
ids of directives actually INJECTED (not merely recalled — a gate-dropped
candidate keeps its chance at its real moment) persist in a per-session state
file and are (a) deduped client-side, which works against any server version,
and (b) sent to the server so its funnel can spend the budget on fresh
candidates. Repeats measured as 76% of all injection noise.

**Repo scope.** The repo name (git remote basename, else cwd basename) is sent
as ``repo``: the server scopes recall to directives learned there (legacy
unscoped rows still pass) and discounts the repo's own name as a trigger.

**Client-side precision gate.** ``_precision_filter`` re-imposes the concrete
trigger-in-handle contract before injection — transitional belt-and-braces for
servers predating the match-semantics funnel; fail-open.

Invoked as: ``uv run --with mcp python directive_recall.py`` with the PreToolUse
hook JSON on stdin.
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _memhub_auth import resolve_url_and_auth  # noqa: E402

# Bound on the recall round-trip. This runs synchronously before the tool, so a
# hung server can't be allowed to stall the agent; the server's own LLM-gate
# budget (0.8s, fail-open) fits inside with headroom. Fail-open on hit.
_RECALL_TIMEOUT_S = 2.5
_MAX_DIRECTIVES = 5

# The firing signal for a tool call is its identifying handle — the file path
# for an edit/write, the command for Bash — NOT the file body or diff hunks.
_ID_ARG_KEYS = ("file_path", "notebook_path", "command")
_MAX_ARG_CHARS = 500

# Session already_fired state: one small JSON list per session id, pruned by
# age so the directory can't grow unbounded across months of sessions.
_STATE_DIR = Path.home() / ".claude" / ".memhub" / "directive_fired"
_STATE_MAX_AGE_S = 7 * 24 * 3600
_MAX_FIRED_SENT = 1024


def _log(msg: str) -> None:
    print(f"[memhub-directive] {msg}", file=sys.stderr)


# --- session already_fired state -------------------------------------------

def _state_path(session_id: str) -> Path | None:
    sid = re.sub(r"[^A-Za-z0-9_-]", "", session_id or "")
    return (_STATE_DIR / f"{sid}.json") if sid else None


def _load_fired(session_id: str) -> list[str]:
    """Ids injected earlier this session (empty on any problem — a lost state
    file only means a directive may fire once more, never a broken hook)."""
    path = _state_path(session_id)
    if not path:
        return []
    try:
        ids = json.loads(path.read_text())
        return [str(i) for i in ids if str(i).strip()] if isinstance(ids, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_fired(session_id: str, ids: list[str]) -> None:
    """Persist the injected-id list; opportunistically prune stale sessions."""
    path = _state_path(session_id)
    if not path:
        return
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ids[-_MAX_FIRED_SENT:]))
        cutoff = time.time() - _STATE_MAX_AGE_S
        for old in _STATE_DIR.glob("*.json"):
            if old != path and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
    except OSError:
        pass  # state is an optimization, never worth failing the hook


def _repo_name(cwd: str) -> str:
    """The repo this session works in: git remote basename (stable across
    worktrees like ``xmem-directive-golden``), else the cwd basename."""
    if not cwd:
        return ""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=0.5,
        )
        url = out.stdout.strip()
        if out.returncode == 0 and url:
            return url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(cwd).name


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
    for d in items:
        kind = str(d.get("type", "directive")).upper()
        text = str(d.get("content", "")).strip()
        triggers = ", ".join(str(t) for t in (d.get("triggers") or [])[:4])
        # Provenance the agent can weight instead of re-verifying: when the
        # directive was last confirmed and how often it has been observed.
        prov = []
        if d.get("as_of"):
            prov.append(f"as of {d['as_of']}")
        if isinstance(d.get("seen"), int) and d["seen"] > 1:
            prov.append(f"seen {d['seen']}×")
        suffix = ""
        if triggers or prov:
            suffix = "  _(" + "; ".join(
                p for p in (f"triggers: {triggers}" if triggers else "", *prov) if p
            ) + ")_"
        lines.append(f"- **[{kind}]** {text}{suffix}")
    return "\n".join(lines)


async def _recall(tool: str, args: dict, repo: str, fired: list[str]) -> list[dict]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url, headers, auth = resolve_url_and_auth(interactive=False)
    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            arguments: dict = {"tool": tool, "args": args}
            if repo:
                arguments["repo"] = repo
            if fired:
                arguments["already_fired"] = fired[-_MAX_FIRED_SENT:]
            res = await session.call_tool("recall_directives", arguments=arguments)
            if getattr(res, "isError", False) and (repo or fired):
                # Rolling-upgrade compat: a server predating the repo /
                # already_fired params rejects unknown arguments. Retry once
                # legacy-shaped — client-side dedup still covers repeats.
                texts = [t for t in (getattr(b, "text", None)
                         for b in getattr(res, "content", []) or []) if t]
                detail = (texts[0] if texts else "")[:200]
                if re.search(r"unexpected|repo|already_fired|validation", detail, re.I):
                    _log("server predates repo/already_fired; retrying legacy")
                    res = await session.call_tool("recall_directives", arguments={
                        "tool": tool, "args": args,
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
        cwd = hook_input.get("cwd") or ""
        session_id = str(hook_input.get("session_id") or "")
        fired = _load_fired(session_id)
        recall_args = _recall_args(args)
        items = asyncio.run(
            asyncio.wait_for(
                _recall(tool, recall_args, _repo_name(cwd), fired),
                _RECALL_TIMEOUT_S,
            )
        )
        # Belt-and-braces dedup for servers predating already_fired — repeats
        # were 76% of all injection noise, so this must not depend on the
        # server version.
        fired_set = set(fired)
        items = [d for d in items if str(d.get("id") or "") not in fired_set]
        # Re-impose the symbol-tripwire contract for servers predating the
        # match-semantics funnel: only triggers that concretely hit this call.
        items = _precision_filter(items, recall_args, cwd)
        items = items[:_MAX_DIRECTIVES]
        if items:
            _log(f"{len(items)} directive(s) fired for {tool}")
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": _render(items),
                }
            }))
            # Record INJECTIONS only, and only after a successful emit — a
            # recalled-but-not-shown directive keeps its chance at its real
            # moment later in the session.
            new_ids = [str(d["id"]) for d in items if str(d.get("id") or "").strip()]
            if new_ids and session_id:
                _save_fired(session_id, fired + new_ids)
    # BaseException (not Exception): anyio task groups can surface a
    # BaseExceptionGroup (e.g. auth cancelling siblings). This hook is
    # best-effort — never fail or block the tool call. Emit nothing, exit 0.
    except BaseException as e:  # noqa: BLE001 — never fail the hook
        _log(f"skipped ({type(e).__name__}: {str(e)[:120]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

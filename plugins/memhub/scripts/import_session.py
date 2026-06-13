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


def _slices(records: list[dict], chunk_bytes: int) -> list[list[dict]]:
    """Split records into consecutive disjoint slices under chunk_bytes each
    (single oversized records still go through alone). Disjointness matters:
    each slice is its own incremental import, so no record is ever extracted
    twice regardless of watermark timing."""
    out: list[list[dict]] = []
    cur: list[dict] = []
    size = 0
    for rec in records:
        b = len(json.dumps(rec, separators=(",", ":")))
        if cur and size + b > chunk_bytes:
            out.append(cur)
            cur, size = [], 0
        cur.append(rec)
        size += b
    if cur:
        out.append(cur)
    return out


async def _gist_hash(session, context_base_id: str | None) -> str | None:
    """Content hash of the session gist (episode starting '## GOAL'), or None."""
    import hashlib
    args = {"query": "GOAL INTENT OUTCOME ROUTE RESUME STATE next step",
            "memory_type": "episodes", "top_k": 5}
    if context_base_id:
        args["context_base_id"] = context_base_id
    try:
        res = await session.call_tool("search_memory", arguments=args)
        d = unwrap(res)
        for it in d.get("items", []):
            c = str(it.get("content", "")).lstrip()
            if c.startswith("## GOAL"):
                return hashlib.sha256(c.encode()).hexdigest()
    except Exception:  # noqa: BLE001
        pass
    return None


async def _wait_gist_change(session, context_base_id, prev_hash, timeout=1800):
    """Block until the gist appears (prev None) or its content changes
    (fold-forward happened) — the end-of-slice extraction signal. On timeout,
    warn and proceed (the next slice still imports safely; worst case the
    gist upserts race and one fold is lost to last-writer-wins)."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        await asyncio.sleep(20)
        h = await _gist_hash(session, context_base_id)
        if h is not None and h != prev_hash:
            print("  slice extraction complete (gist updated)")
            return h
    print("  WARNING: slice wait timed out; continuing with next slice")
    return prev_hash


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


def call_error(result, payload: dict) -> str | None:
    """The server-side failure text of a tool call, or None on success.

    Tool exceptions arrive as ``CallToolResult.isError`` with the message in
    the content blocks — ``unwrap`` can't distinguish that from a successful
    payload, so callers must check this BEFORE trusting the dict.
    """
    if getattr(result, "isError", False):
        return str(payload.get("_raw") or payload.get("error")
                   or json.dumps(payload))
    return None


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
    ap.add_argument("--chunk-bytes", type=int, default=3_500_000,
                    help="transcripts larger than this are sent as sequential "
                         "disjoint slices under the same conversation_id "
                         "(server extracts each incrementally; the session "
                         "gist folds forward per slice). 0 disables chunking.")
    ap.add_argument("--slice-timeout", type=int, default=1800,
                    help="max seconds to wait for a slice's extraction "
                         "(detected via the session gist appearing/changing) "
                         "before sending the next slice anyway")
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

    slices = _slices(records, args.chunk_bytes) if args.chunk_bytes else [records]
    size = f.stat().st_size
    print(f"session file    : {f}")
    print(f"records         : {len(records)}   ({size:,} bytes ≈ {size // 4:,} tokens)")
    print(f"conversation_id : {conv_id}")
    print(f"endpoint        : {url}")
    if args.context_base_id:
        print(f"context base    : {args.context_base_id}")
    print("-" * 56)

    if len(slices) > 1:
        print(f"chunked import : {len(slices)} slices "
              f"(payload exceeds {args.chunk_bytes:,} bytes; slices are "
              "disjoint and sent sequentially — the gist folds forward "
              "after each)")

    async with streamablehttp_client(url, headers=headers, auth=auth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            prev_gist_hash = await _gist_hash(s, args.context_base_id)
            for i, sl in enumerate(slices, 1):
                call_args: dict = {
                    "messages": sl,
                    "conversation_id": conv_id,
                    "source_platform": "claude",
                }
                if args.title:
                    call_args["title"] = args.title
                if args.context_base_id:
                    call_args["context_base_id"] = args.context_base_id
                if len(slices) > 1:
                    print(f"--- slice {i}/{len(slices)}: {len(sl)} records ---")
                res = await s.call_tool("import_conversation", arguments=call_args)
                payload = unwrap(res)
                print(json.dumps(payload, indent=2))
                err = call_error(res, payload)
                if err:
                    # No success epilogue — a headless caller (the pr-babysit
                    # loop) must see this as a failed save, not "Queued".
                    label = (f"slice {i}/{len(slices)}" if len(slices) > 1
                             else "import")
                    print(f"ERROR: {label} failed: {err}", file=sys.stderr)
                    if i > 1:
                        print(f"NOTE: slices 1..{i - 1} were already queued; "
                              "re-running after fixing the error is safe "
                              "(the server watermark skips them).",
                              file=sys.stderr)
                    return 1
                if i < len(slices):
                    print(f"waiting for slice {i} extraction "
                          "(gist appear/fold-forward) before next slice ...")
                    prev_gist_hash = await _wait_gist_change(
                        s, args.context_base_id, prev_gist_hash,
                        timeout=args.slice_timeout,
                    )
    print("-" * 56)
    print("Queued. Extraction runs in the background (minutes for large "
          "sessions); facts/episodes/artifacts + the session gist appear in "
          "search_memory as it completes. Re-running the same session later "
          "imports only NEW records (watermark) and folds the gist forward.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

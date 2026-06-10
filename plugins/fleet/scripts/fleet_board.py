#!/usr/bin/env python3
"""Fleet board: shared awareness across parallel Claude Code agents in git
worktrees of the same repo.

One JSON board per repo lives at ``<git-common-dir>/fleet-board.json`` —
worktrees share the common .git dir, so every agent sees the same file with
no server involved. Each hook event maps to a subcommand:

    session-start   register this session, prune stale entries, inject the
                    current fleet snapshot into context
    prompt          heartbeat + refresh this agent's "working on" line, then
                    inject any sibling changes since last look
    post-tool       record the latest commit (message + files) on this
                    agent's entry; invoked only for git-commit Bash calls
    session-end     mark this agent's entry ended

All subcommands read the standard hook-input JSON on stdin and exit 0 in
every failure mode — a coordination aid must never break a session.
"""

import json
import os
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


def _lock(f):
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_EX)
    elif msvcrt:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)


def _unlock(f):
    try:
        if fcntl:
            fcntl.flock(f, fcntl.LOCK_UN)
        elif msvcrt:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass

ACTIVE_STALE_SECS = 12 * 3600   # active entry with no heartbeat for this long → prune
ENDED_KEEP_SECS = 3600          # ended entries linger this long so siblings see the exit
WORKING_ON_MAX = 120
MAX_COMMIT_FILES = 8


def now():
    return int(time.time())


def ago(ts: int):
    d = max(0, now() - ts)
    if d < 90:
        return f"{d}s ago"
    if d < 5400:
        return f"{d // 60}m ago"
    return f"{d // 3600}h ago"


def git(cwd: str, *args: str):
    try:
        out = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def board_path(cwd: str):
    common = git(cwd, "rev-parse", "--git-common-dir")
    if not common:
        return None
    if not os.path.isabs(common):
        common = os.path.abspath(os.path.join(cwd, common))
    return os.path.join(common, "fleet-board.json")


class Board:
    """flock-guarded read-modify-write of the board file."""

    def __init__(self, path: str):
        self.path = path
        self.lock_path = path + ".lock"

    def __enter__(self):
        self.lock = open(self.lock_path, "w")
        _lock(self.lock)
        try:
            with open(self.path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {"version": 1, "agents": {}}
        if not isinstance(self.data.get("agents"), dict):
            self.data = {"version": 1, "agents": {}}
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.data, f, indent=1)
                os.replace(tmp, self.path)
        finally:
            _unlock(self.lock)
            self.lock.close()
        return False

    def prune(self):
        agents = self.data["agents"]
        for sid in list(agents):
            e = agents[sid]
            last = e.get("last_update", 0)
            dead = (
                (e.get("status") == "ended" and now() - last > ENDED_KEEP_SECS)
                or (e.get("status") == "active" and now() - last > ACTIVE_STALE_SECS)
                or not os.path.isdir(e.get("worktree", ""))
            )
            if dead:
                del agents[sid]

    def others(self, sid: str):
        return [e for k, e in sorted(self.data["agents"].items()) if k != sid]


def entry_line(e: dict):
    bits = [f"`{e.get('branch', '?')}`"]
    if e.get("status") == "ended":
        bits.append(f"(ended {ago(e.get('last_update', 0))})")
    else:
        bits.append(f"(active, last update {ago(e.get('last_update', 0))})")
    if e.get("working_on"):
        bits.append(f"— working on: {e['working_on']}")
    c = e.get("last_commit")
    if c:
        files = ", ".join(c.get("files", [])[:3])
        more = len(c.get("files", [])) - 3
        if more > 0:
            files += f" +{more} more"
        bits.append(f'— last commit {ago(c.get("at", 0))}: "{c.get("message", "")}"'
                    + (f" [{files}]" if files else ""))
    bits.append(f"— worktree {e.get('worktree', '?')} — session {e.get('session_id', '?')[:8]}")
    return "- " + " ".join(bits)


def snapshot_seen(others: list):
    return {
        e["session_id"]: {
            "lu": e.get("last_update", 0),
            "st": e.get("status"),
            "wo": e.get("working_on"),
            "ch": (e.get("last_commit") or {}).get("hash"),
        }
        for e in others
    }


def delta_lines(others: list, seen: dict):
    lines = []
    for e in others:
        sid = e["session_id"]
        old = seen.get(sid)
        branch = f"`{e.get('branch', '?')}`"
        if old is None:
            if e.get("status") == "active":
                wo = f" — working on: {e['working_on']}" if e.get("working_on") else ""
                c = e.get("last_commit") or {}
                cm = f' — last commit: "{c["message"]}"' if c.get("message") else ""
                lines.append(f"- {branch} joined the fleet{wo}{cm}")
            continue
        if e.get("status") == "ended" and old.get("st") == "active":
            lines.append(f"- {branch} ended its session")
            continue
        if e.get("status") == "active" and old.get("st") == "ended":
            wo = f" — working on: {e['working_on']}" if e.get("working_on") else ""
            c = e.get("last_commit") or {}
            cm = (f' — last commit: "{c["message"]}"'
                  if c.get("message") and c.get("hash") != old.get("ch") else "")
            lines.append(f"- {branch} rejoined the fleet{wo}{cm}")
            continue
        c = e.get("last_commit") or {}
        if c.get("hash") and c.get("hash") != old.get("ch"):
            files = ", ".join(c.get("files", [])[:MAX_COMMIT_FILES])
            lines.append(f'- {branch} committed "{c.get("message", "")}"'
                         + (f" touching {files}" if files else ""))
        elif e.get("working_on") and e.get("working_on") != old.get("wo"):
            lines.append(f"- {branch} is now working on: {e['working_on']}")
    return lines


def context_out(event: str, text: str):
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": text}
    }))


def derive_working_on(prompt: str):
    p = " ".join(prompt.split())
    if len(p) < 20 or p.startswith("/") or p.startswith("!"):
        return None
    return p[:WORKING_ON_MAX] + ("…" if len(p) > WORKING_ON_MAX else "")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        hook = json.load(sys.stdin)
    except ValueError:
        hook = {}
    cwd = hook.get("cwd") or os.getcwd()
    sid = hook.get("session_id")
    path = board_path(cwd)
    if not path or not sid:
        return 0
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "?"

    if cmd == "session-start":
        with Board(path) as b:
            b.prune()
            others = [e for e in b.others(sid) if e.get("status") == "active"]
            # merge on resume/reconnect — a re-fired SessionStart must not wipe
            # this agent's working_on / last_commit / seen state off the board
            me = b.data["agents"].get(sid) or {
                "session_id": sid,
                "started": now(),
                "working_on": None,
                "last_commit": None,
                "seen": snapshot_seen(others),
            }
            me.update({
                "branch": branch,
                "worktree": cwd,
                "last_update": now(),
                "status": "active",
            })
            b.data["agents"][sid] = me
        if others:
            repo = os.path.basename(os.path.dirname(path)).removesuffix(".git") or \
                os.path.basename(os.path.dirname(os.path.dirname(path)))
            lines = "\n".join(entry_line(e) for e in others)
            context_out("SessionStart",
                        f"Fleet board — {len(others)} other agent(s) active on this repo "
                        f"({repo}), each in its own worktree:\n{lines}\n"
                        "Coordinate by awareness: if you are about to change a file a "
                        "sibling recently committed to or is working on, build on their "
                        "work instead of diverging, and tell the user about the overlap. "
                        "Fleet updates will be injected as they happen.")

    elif cmd == "prompt":
        text = None
        with Board(path) as b:
            me = b.data["agents"].setdefault(sid, {
                "session_id": sid, "branch": branch, "worktree": cwd,
                "started": now(), "status": "active",
                "working_on": None, "last_commit": None, "seen": {},
            })
            me["last_update"] = now()
            me["status"] = "active"
            wo = derive_working_on(hook.get("prompt", "") or "")
            if wo:
                me["working_on"] = wo
            others = b.others(sid)
            lines = delta_lines(others, me.get("seen", {}))
            me["seen"] = snapshot_seen(others)
            if lines:
                text = "Fleet update:\n" + "\n".join(lines)
        if text:
            context_out("UserPromptSubmit", text)

    elif cmd == "post-tool":
        # the hooks.json prefilter matches the whole hook payload, which can
        # carry "git"+"commit" in output text (a pull, a log mention) — require
        # `commit` as git's actual subcommand (first non-option token), so
        # `git diff main commit`, `feature/commit`, `commit-tree` don't pass
        bash_cmd = (hook.get("tool_input") or {}).get("command") or ""
        m = re.search(
            r"\bgit\s+"
            r"(?:-C\s+(\"[^\"]+\"|'[^']+'|\S+)\s+|-c\s+\S+\s+|--?[\w=./-]+\s+)*"
            r"commit(?![\w/-])",
            bash_cmd)
        if not m:
            return 0
        git_cwd = (m.group(1) or "").strip("\"'") or cwd
        if git_cwd != cwd:
            # a -C path must still be THIS board's repo — never attach a
            # foreign repo's HEAD to this session's entry
            here = git(cwd, "rev-parse", "--git-common-dir")
            there = git(git_cwd, "rev-parse", "--git-common-dir")
            if not here or not there:
                return 0
            if not os.path.isabs(here):
                here = os.path.join(cwd, here)
            if not os.path.isabs(there):
                there = os.path.join(git_cwd, there)
            if os.path.realpath(here) != os.path.realpath(there):
                return 0
        # and only record HEAD if it was created within the last two minutes
        ct = git(git_cwd, "log", "-1", "--pretty=%ct")
        if not ct or not ct.isdigit() or now() - int(ct) > 120:
            return 0
        head = git(git_cwd, "rev-parse", "HEAD")
        msg = git(git_cwd, "log", "-1", "--pretty=%s")
        if not head or msg is None:
            return 0
        raw = git(git_cwd, "log", "-1", "--name-only", "--pretty=format:") or ""
        files = [f for f in raw.splitlines() if f][:MAX_COMMIT_FILES]
        with Board(path) as b:
            me = b.data["agents"].get(sid)
            # keyed by hash so a re-fired hook can't re-announce the same HEAD
            if me is not None and (me.get("last_commit") or {}).get("hash") != head:
                me["last_update"] = now()
                me["branch"] = git(git_cwd, "rev-parse", "--abbrev-ref", "HEAD") or branch
                me["last_commit"] = {"hash": head, "message": msg,
                                     "files": files, "at": int(ct)}

    elif cmd == "session-end":
        with Board(path) as b:
            me = b.data["agents"].get(sid)
            if me is not None:
                me["status"] = "ended"
                me["last_update"] = now()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)

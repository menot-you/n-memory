"""Vendor sessions that nSPAWN did not start: their rows, their liveness and the
reasons a home is not listed (`nspawn thread list|show`).

This module works on data only and launches nothing. The vendor reads are
`claude_sessions.mjs` (the Claude Agent SDK) and the Codex app-server's
`thread/list` and `thread/read`, both under `Supervisor.threads`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

#: The bytes a Codex rollout's tail read covers. Its `token_count` totals are
#: cumulative, so the last one in the tail is the thread's total.
ROLLOUT_TAIL_BYTES: Final = 256 * 1024
TITLE_CHARACTERS: Final = 200
PROC: Final = Path("/proc")

AGY_REASON: Final = (
    "AGY has no session list, its adapter cannot load a session, and NOTT "
    "cannot show its earlier conversation"
)
DEFAULT_CODEX_REASON: Final = (
    "no roster account uses ~/.codex. Codex resumes a thread only from its own "
    "home. Add this home as an account to continue it."
)
NO_TIME_REASON: Final = "not read: the list spent its time on other homes"


def unlisted(provider: str, home: str | None, reason: str) -> dict[str, Any]:
    return {"provider": provider, "home": home, "reason": reason}


def proc_start(pid: int) -> str | None:
    """Field 22 of `/proc/<pid>/stat`, the start time in clock ticks."""
    try:
        stat = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    return stat[stat.rindex(")") + 2 :].split()[19]


def claude_live(home: Path) -> dict[str, bool] | None:
    """Each session a live Claude Code process holds, by its pid record
    (`<home>/sessions/<pid>.json`) and that process's start ticks. None when
    this host has no `/proc`."""
    if not PROC.is_dir():
        return None
    live: dict[str, bool] = {}
    for record in sorted((home / "sessions").glob("*.json")):
        if not record.is_file():
            continue  # a FIFO would block the read past the verb's deadline
        try:
            data = json.loads(record.read_text())
        except OSError, ValueError:
            continue
        if not isinstance(data, dict):
            continue
        pid, session, start = (
            data.get("pid"),
            data.get("sessionId"),
            data.get("procStart"),
        )
        if (
            isinstance(pid, int)
            and isinstance(session, str)
            and start is not None
            and proc_start(pid) == str(start)
        ):
            live[session] = True
    return live


def codex_live(home: Path, thread_id: str) -> bool | None:
    """Whether a process holds the thread's writer lock: a FLOCK on the lock
    file's inode in `/proc/locks`. None when this host has no `/proc`.

    The inode alone: on btrfs `stat` gives the subvolume's device, and
    `/proc/locks` the superblock's, so the two device numbers differ."""
    if not (PROC / "locks").is_file():
        return None
    try:
        inode = str((home / "thread-writer-locks" / f"{thread_id}.lock").stat().st_ino)
    except OSError:
        return False
    try:
        locks = (PROC / "locks").read_text()
    except OSError:
        return None
    return any(
        len(fields) > 5 and fields[1] == "FLOCK" and fields[5].split(":")[-1] == inode
        for fields in (line.split() for line in locks.splitlines())
    )


def rollout_tokens(path: object) -> dict[str, int] | None:
    """The last `token_count` total in a Codex rollout's tail, or None."""
    if not isinstance(path, str) or not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - ROLLOUT_TAIL_BYTES))
            tail = stream.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            event = json.loads(line)
            payload = event["payload"]
            total = (
                payload["info"]["total_token_usage"]
                if payload["type"] == "token_count"
                else None
            )
        except ValueError, KeyError, TypeError:
            continue
        if isinstance(total, dict):
            figures = [
                total.get(key, 0)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "total_tokens",
                )
            ]
            if all(isinstance(value, int) for value in figures):
                return dict(
                    zip(
                        ("input", "output", "cacheRead", "cacheWrite", "total"), figures
                    )
                )
    return None


def claude_row(
    entry: Mapping[str, Any], home: Path, live: Mapping[str, bool] | None
) -> dict[str, Any]:
    session = entry["sessionId"]
    return {
        "provider": "claude",
        "home": str(home),
        "route": None,
        "sessionId": session,
        "cwd": entry["cwd"],
        "title": str(entry.get("title") or "")[:TITLE_CHARACTERS],
        "updatedAt": entry.get("updatedAt"),
        "model": entry.get("model"),
        "tokens": entry.get("tokens"),
        "live": None if live is None else live.get(session, False),
        "continuable": True,
        "reason": None,
    }


def codex_row(
    thread: Mapping[str, Any], home: Path, route: str | None, reason: str | None
) -> dict[str, Any]:
    ident, updated = thread["id"], thread.get("updatedAt")
    title = thread.get("name") or thread.get("preview") or ""
    return {
        "provider": "codex",
        "home": str(home),
        "route": route,
        "sessionId": ident,
        "cwd": thread.get("cwd"),
        "title": str(title)[:TITLE_CHARACTERS],
        # Codex counts seconds and Claude milliseconds: both rows count milliseconds.
        "updatedAt": updated * 1000 if isinstance(updated, int) else None,
        "model": thread.get("model"),
        "tokens": rollout_tokens(thread.get("path")),
        "live": codex_live(home, ident),
        "continuable": reason is None,
        "reason": reason,
    }

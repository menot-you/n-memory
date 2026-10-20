"""Capture context metadata sidecar (v25).

Captures environment provenance: harness, model, cwd, session_id, host, owner.
"""

from __future__ import annotations

import os
import pwd
import socket
from dataclasses import dataclass
from typing import Any

from .renamed import env

CAPTURE_CONTEXT_EXTENSION_KEY = "nmemory:capture-context"


def client_key(ppid: int | None = None) -> str:
    """Name the CLIENT SESSION from the process tree, asking the client nothing.

    `session_id` depends on the client telling us, and most of the fleet does
    not: measured 2026-09-13, 88,5% of `read_traffic` rows carried `unknown`
    because `NOTT_NMEMORY_SESSION_ID` was exported nowhere then (the node host
    now exports it to each canvas node's server, and `server.capture_context`
    reads it first), the
    `CLAUDE_CODE_SESSION_ID` lane is Claude Code's alone, and the Codex MCP
    client sent no `_meta` (it does by 0.160.1, measured 2026-10-07, and
    `server._named_session` reads it). Every nMEMORY server is SPAWNED BY its
    client, so the parent process is the nearest thing to a session the server
    can see WITHOUT being told.

    How far that goes, measured rather than assumed — 15 live servers on this
    host: 5 under `codex`, 3 under `claude`, 4 under `tailscaled`, 2 under
    `fish`, 1 under `bash`. Only the first eight have the client itself as
    parent. So the honest claim is narrow: the key is always UNIQUE and STABLE,
    and always better than `unknown`; it counts SESSIONS only where the client
    spawns the server directly, and counts SERVERS where a per-server wrapper
    (a `fish -c`, an ssh child) sits in between. An earlier draft of this
    docstring said flatly that the parents "are codex and claude" — true of the
    eight I had looked at, false of the seven I had not. Independent review
    caught it.

    The parent's START TIME is half the key and is not decoration: PIDs recycle,
    and this host's counter wrapped past 4,000,000 back to ~1,000,000 during the
    campaign that added this. A bare ppid fuses two unrelated sessions the same
    way a bare pgid once made a gate run accuse another campaign's live
    processes of being its own leftovers.

    `unknown` for an orphan, never a borrowed identity: a server whose client
    died is reparented, and answering `1-<boot>` would file every orphan in the
    fleet under one shared session — worse than `unknown`, because it looks real.
    """
    parent = os.getppid() if ppid is None else ppid
    if parent <= 1:
        return "unknown"
    try:
        with open(f"/proc/{parent}/stat", "rb") as handle:
            raw = handle.read().decode()
        started = starttime_from_stat(raw)
    except OSError, IndexError, UnicodeDecodeError, ValueError:
        return "unknown"
    return f"{parent}-{started}"


def starttime_from_stat(raw: str) -> int:
    """Field 22 of `/proc/<pid>/stat`, parsed the only way that is safe.

    Split on whitespace and field 22 is wrong, because field 2 is the executable
    name in parentheses and the kernel does NOT escape it: a process named
    `(we ird)` shifts every later field. Every correct parse therefore starts
    after the LAST ')', never the first. Its own function so the pathological
    name is testable — no real parent on this host has one, so a fixture built
    from `os.getppid()` cannot tell the two parses apart.
    """
    return int(raw[raw.rindex(")") + 2 :].split()[19])


@dataclass(frozen=True)
class CaptureContext:
    harness: str = "unknown"
    model: str = "unknown"
    cwd: str = "unknown"
    session_id: str = "unknown"
    host: str = "unknown"
    owner: str = "unknown"

    def to_dict(self) -> dict[str, str]:
        return {
            "harness": self.harness,
            "model": self.model,
            "cwd": self.cwd,
            "session_id": self.session_id,
            "host": self.host,
            "owner": self.owner,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CaptureContext:
        return cls(
            harness=data.get("harness", "unknown") or "unknown",
            model=data.get("model", "unknown") or "unknown",
            cwd=data.get("cwd", "unknown") or "unknown",
            session_id=data.get("session_id", "unknown") or "unknown",
            host=data.get("host", "unknown") or "unknown",
            owner=data.get("owner", "unknown") or "unknown",
        )

    @classmethod
    def detect(cls, override_meta: dict[str, Any] | None = None) -> CaptureContext:
        """Derive capture context from current process environment + optional _meta."""
        host = socket.gethostname() or "unknown"
        try:
            owner = pwd.getpwuid(os.getuid()).pw_name
        except KeyError, AttributeError, OSError:
            owner = os.environ.get("USER", "unknown")

        cwd = os.getcwd()
        harness = env("NOTT_NMEMORY_HARNESS") or "unknown"
        model = env("NOTT_NMEMORY_MODEL") or "unknown"
        session_id = env("NOTT_NMEMORY_SESSION_ID") or "unknown"

        ctx = cls(
            harness=harness,
            model=model,
            cwd=cwd,
            session_id=session_id,
            host=host,
            owner=owner,
        )

        if override_meta:
            ext = override_meta.get(CAPTURE_CONTEXT_EXTENSION_KEY, override_meta)
            if isinstance(ext, dict):
                return cls(
                    harness=ext.get("harness") or ctx.harness,
                    model=ext.get("model") or ctx.model,
                    cwd=ext.get("cwd") or ctx.cwd,
                    session_id=ext.get("session_id") or ctx.session_id,
                    host=ext.get("host") or ctx.host,
                    owner=ext.get("owner") or ctx.owner,
                )

        return ctx

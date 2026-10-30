"""Vendor sessions that nSPAWN did not start: their rows, their liveness and the
reasons a home is not listed (`nspawn thread list|show`).

This module launches nothing. The vendor reads are `claude_sessions.mjs` (the
Claude Agent SDK) and the Codex app-server's `thread/list` and `thread/read`,
both under `Supervisor.threads`. A host with the journal writer's unit reads its
Claude sessions from the journal instead of the SDK. The session
journal (`nspawn.journal`) finds its Claude homes and liveness here too.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from .owner_roster import Provider, Roster, claude_config_dir

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


def claude_homes(roster: Roster) -> list[tuple[str | None, Path]]:
    """Each Claude home on this host, once: the inherited one (`~/.claude`, or
    `CLAUDE_CONFIG_DIR`) first, then each roster account's own home. The alias
    is the account that declares the home, or None for the inherited one."""
    homes: list[tuple[str | None, Path]] = [(None, claude_config_dir())]
    for provider in roster.providers:
        if provider.name == "claude" and provider.kind == "claude":
            for alias, account in provider.accounts.items():
                if account.home is None:
                    continue
                home = claude_config_dir(account)
                known = [item.resolve() for _, item in homes]
                if home.resolve() not in known:
                    homes.append((alias, home))
                else:
                    at = known.index(home.resolve())
                    if homes[at][0] is None:
                        homes[at] = (alias, homes[at][1])
    return homes


def codex_homes(roster: Roster) -> list[tuple[Provider, str | None, Path]]:
    """Each Codex home to read: the `codex` provider's accounts, then the default
    `~/.codex` when it exists and no account uses it (alias None). The roster
    loader refuses a Codex account without a home."""
    provider = next(
        (p for p in roster.providers if p.name == "codex" and p.kind == "codex"), None
    )
    if provider is None:
        return []
    homes: list[tuple[Provider, str | None, Path]] = [
        (provider, alias, account.home.expanduser())
        for alias, account in sorted(provider.accounts.items())
        if account.home is not None
    ]
    default = Path.home() / ".codex"
    if default.is_dir() and default.resolve() not in {
        home.resolve() for _, _, home in homes
    }:
        homes.append((provider, None, default))
    return homes


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


def journal_installed() -> bool:
    """The installer has placed the journal writer's user unit on this host."""
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return (config / "systemd/user/nspawn-session-serve.service").is_file()


def journal_messages(
    rows: Sequence[dict[str, Any]], limit: int, budget: int
) -> dict[str, Any]:
    """Map active journal rows to the existing Claude message response."""
    messages: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    key: object = None
    for row in rows:
        kind = row.get("type")
        if kind not in ("message", "reasoning", "tool_call", "tool_result"):
            continue
        if kind == "message" and not row.get("text") and not row.get("media"):
            continue
        role = (
            "user"
            if kind == "tool_result" or row.get("role") == "owner"
            else "assistant"
        )
        ident = row.get("uuid", (row.get("src"), role))
        if current is None or key != ident:
            current = {
                "type": role,
                "message": {"role": role, "content": []},
                "timestamp": row.get("time"),
            }
            current.update(
                (name, row[name])
                for name in ("uuid", "parentUuid", "logicalParentUuid")
                if name in row
            )
            if row.get("mid"):
                current["message"]["id"] = row["mid"]
            messages.append(current)
            key = ident
        blocks = current["message"]["content"]
        if kind == "message":
            if row.get("text"):
                blocks.append({"type": "text", "text": row["text"]})
            for media in row.get("media", []):
                blocks.append(
                    {"type": "text", "text": f"[{media['kind']}: {media['mime']}]"}
                )
        elif kind == "reasoning":
            blocks.append(
                {"type": "thinking", "thinking": row["text"]}
                if "text" in row
                else {"type": "redacted_thinking", "data": "[redacted]"}
            )
        elif kind == "tool_call":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": row["id"],
                    "name": row["name"],
                    "input": row["input"],
                }
            )
        else:
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": row["id"],
                    "content": row["output"],
                    "is_error": not row["ok"],
                }
            )
    for message in messages:
        content = message["message"]["content"]
        if (
            message["type"] == "user"
            and len(content) == 1
            and content[0].get("type") == "text"
        ):
            message["message"]["content"] = content[0]["text"]
    kept: list[dict[str, Any]] = []
    size = 0
    for message in reversed(messages):
        length = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
        if len(kept) == limit or length > 4 * 1024 * 1024 or size + length > budget:
            break
        kept.append(message)
        size += length
    kept.reverse()
    while kept:
        first = kept[0]
        content = first["message"]["content"]
        if first["type"] == "user" and (
            isinstance(content, str)
            or not any(block.get("type") == "tool_result" for block in content)
        ):
            break
        kept.pop(0)
    return {"messages": kept, "omitted": len(messages) - len(kept)}


def _journaled(
    connection: Any, homes: Sequence[Path] | None, ident: str | None = None
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each journaled Claude session of `homes` (None: any home), or only the
    copies of session `ident`, with the metadata of its `session` row, as
    stored."""
    from .journal import show

    wanted = None if homes is None else {home.resolve() for home in homes}
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for session in show.sessions(
        connection, None, None if ident is None else ("claude", ident)
    ):
        if session["vendor"] != "claude":
            continue
        head = show.rows_after(connection, "claude", session["id"], -1, 1)
        start = head[0] if head and head[0].get("type") == "session" else {}
        home = start.get("source", {}).get("home")
        if home is None or (wanted is not None and Path(home).resolve() not in wanted):
            continue
        metadata = start.get("metadata", {})
        found.append(
            (
                {
                    **session,
                    "home": home,
                    "path": start.get("source", {}).get("path", ""),
                    "sessionId": metadata.get("vendor_id", session["id"]),
                },
                metadata,
            )
        )
    return found


def journal_history(
    ident: str, home: Path, message_limit: int, message_budget: int
) -> dict[str, Any]:
    """`thread show --messages --claude-home` from the shared reader: the
    session's active conversation as the SDK reader's message response. Only a
    journaled home answers; the verb never translates a source."""
    from .journal import show

    connection = show.open_readonly()
    if connection is None:
        return {"messages": None, "reason": "The session is not in the journal."}
    try:
        for session, metadata in _journaled(connection, [home], ident):
            if session["sessionId"] != ident:
                continue
            if metadata.get("import_state") != "done":
                return {"messages": None, "reason": "The session is still importing."}
            try:
                active = show.active_rows(connection, "claude", session["id"])
            except show.ChainIncomplete as error:
                return {"messages": None, "reason": str(error)}
            return journal_messages(active, message_limit, message_budget)
    finally:
        connection.close()
    return {"messages": None, "reason": "The session is not in the journal."}


def journal_claude(
    roster: Roster,
    ident: str | None,
    known: Callable[[list[str]], set[str]],
    list_budget: int,
) -> list[dict[str, Any]]:
    """The Claude rows of `thread list` (`ident` None) or `thread show` from the
    shared reader, over the same homes and filters as the SDK reader: no node
    copy, no session without a cwd, no SDK or subagent session in a list. A
    list leaves out the sessions in `known` before any limit."""
    from .journal import show

    connection = show.open_readonly()
    if connection is None:
        return []
    try:
        listed = [
            (session, metadata)
            for session, metadata in _journaled(
                connection, [home for _, home in claude_homes(roster)], ident
            )
            if (
                session["sessionId"] == ident
                or ident is None
                and not metadata.get("owner_node")
                and session.get("cwd")
                and not str(metadata.get("entrypoint", "")).startswith("sdk")
                and "/subagents/" not in session["path"]
            )
            # Claude Code deletes old transcripts: a session whose file is gone
            # cannot be continued, and the SDK reader never listed it.
            and (Path(session["home"]) / session["path"]).is_file()
        ]
    finally:
        connection.close()
    listed.sort(key=lambda item: item[1].get("modified") or 0, reverse=True)
    mine = (
        known([session["sessionId"] for session, _ in listed])
        if ident is None
        else set()
    )
    rows: list[dict[str, Any]] = []
    spent = 0
    for session, metadata in listed:
        if session["sessionId"] in mine:
            continue
        total = session["total"]
        fits = spent + metadata.get("size", 0) <= list_budget
        if fits:
            spent += metadata.get("size", 0)
        tokens = None
        if ident is None and fits and total["calls"]:
            tokens = {
                "input": total["input"],
                "output": total["output"],
                "cacheRead": total["cache_read"],
                "cacheWrite": total["cache_write"],
                "total": total["input"]
                + total["output"]
                + total["cache_read"]
                + total["cache_write"],
            }
        status = session.get("status")
        rows.append(
            {
                "provider": "claude",
                "home": session["home"],
                "route": None,
                "sessionId": session["sessionId"],
                "cwd": session["cwd"],
                "title": str(metadata.get("title") or session.get("title") or "")[
                    :TITLE_CHARACTERS
                ],
                "updatedAt": metadata.get("modified"),
                "model": session["model"] if fits else None,
                "tokens": tokens,
                "live": None if status is None else status in ("live", "waiting"),
                "continuable": True,
                "reason": None,
                "journalId": session["id"],
                "parent": metadata.get("parent"),
                "parentVendor": metadata.get("parent_vendor"),
                "parentEvidence": metadata.get("parent_evidence"),
                "account": metadata.get("account", session.get("account")),
                "effort": metadata.get("effort"),
                "requestedEffort": metadata.get("requested_effort"),
                "sentEffort": metadata.get("sent_effort"),
                "status": status,
                "importState": metadata.get("import_state"),
            }
        )
    return rows

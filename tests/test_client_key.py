"""Read traffic names the CLIENT SESSION without asking the client to tell us.

`read_traffic.session_id` was blind to most of the fleet, measured: 88,5% of its
rows carried `unknown`, because `CaptureContext.detect` reads `NOTT_NMEMORY_SESSION_ID`
(exported nowhere in this repository) and `server.py` reads
`CLAUDE_CODE_SESSION_ID` (only Claude Code sets it). The remaining lane is the
JSON-RPC `_meta` extension, and the Codex MCP client did not send it then (it
does by 0.160.1: the Codex test below). Split by
harness over one day: `codex-mcp-client/0.154.0` wrote 550 capsules, 100%
anonymous; `claude-code/*` wrote 190, 0% anonymous.

So stop asking the client. Every nMEMORY server is spawned BY its client, so the
parent process is the nearest session identity the server can see unaided.

Measured on the live host rather than assumed, 15 servers: 5 under `codex`,
3 under `claude`, 4 under `tailscaled`, 2 under `fish`, 1 under `bash`. Only
those eight have the client itself as parent. The key is always unique, stable
and better than `unknown`; it counts SESSIONS where the client spawns the server
directly, and SERVERS where a per-server wrapper sits in between. The first
draft of this file claimed the parents "are codex and claude" — true of the
sample, false of the fleet, and caught by independent review.

The key is `(ppid, that parent's start time)` and the second half is not
decoration. PIDs recycle: this host's counter wrapped past 4,000,000 back to
~1,000,000 during the campaign that wrote this file, and a bare pgid was enough
to make one gate run accuse another campaign's live processes of being its own
leftovers. A bare ppid would fuse two unrelated sessions the same way.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from stdio_peer import wire
from storefile import fresh_store_path

from nmemory.capture import client_key, starttime_from_stat
from nmemory.database import Database
from nmemory.server import MemoryServer
from nmemory.store import Store


def _parent_starttime_ticks(pid: int) -> int:
    with open(f"/proc/{pid}/stat", "rb") as fh:
        raw = fh.read().decode()
    # The comm field is parenthesised and may itself contain spaces and
    # parens, so every robust parse starts after the LAST ')'.
    return int(raw[raw.rindex(")") + 2 :].split()[19])


def test_the_key_names_this_process_real_parent_and_its_start_time() -> None:
    key = client_key()
    assert key != "unknown", "a process with a live parent always has a key"
    ppid, ticks = key.split("-")
    assert int(ppid) == os.getppid()
    assert int(ticks) == _parent_starttime_ticks(os.getppid()), (
        "the start time is the half that survives PID reuse; without it two "
        "sessions that happen to reuse a pid become one"
    )


def test_the_key_is_stable_for_the_life_of_the_process() -> None:
    assert client_key() == client_key() == client_key()


def test_an_orphan_has_no_key_rather_than_a_wrong_one() -> None:
    """A server whose client died is reparented, and must not borrow init's identity.

    Measured on the live host while writing this: one of the running nMEMORY
    servers had been reparented and its `ppid` read back as unusable. Answering
    `1-<boot>` there would file every orphan in the fleet under one shared
    session — worse than `unknown`, because it looks like a real one.
    """
    assert client_key(ppid=0) == "unknown"
    assert client_key(ppid=1) == "unknown"
    assert client_key(ppid=2**31) == "unknown", "a pid that cannot be read"


def test_one_session_two_servers_share_a_key_and_a_second_session_does_not() -> None:
    """The property that makes this count SESSIONS where the client spawns directly.

    Measured on the live host: pids 684385 and 729250 are two nMEMORY servers
    with the same parent, a single `codex` process. They are one session, and a
    key derived from the server's OWN pid would have called them two.

    Where it does NOT hold, named so nobody reads more into it: a per-server
    wrapper (`fish -c`, an ssh child) is its own parent per server, so there the
    key counts servers. Measured the same day: 7 of 15 live servers sit behind
    such a wrapper. Unique and stable either way — just not always one per
    session.
    """
    mine = client_key()
    same_parent_again = client_key(ppid=os.getppid())
    assert same_parent_again == mine

    # Our own parent's parent is a different process, hence a different session.
    with open(f"/proc/{os.getppid()}/stat", "rb") as handle:
        grandparent = int(handle.read().decode().rsplit(")", 1)[1].split()[1])
    if grandparent > 1:
        assert client_key(ppid=grandparent) != mine


def test_a_read_files_itself_under_the_client_key() -> None:
    url = fresh_store_path("clientkey")
    pg = Database(url)
    server = MemoryServer(store=Store.open(url))

    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "A capsule to orient over.",
            "kind": "fact",
            "source": "test_client_key",
            "anchor": "1",
        },
    )
    assert not response["result"]["isError"], response

    response = server.call_tool("nmemory_context_show", {"query": "capsule"})
    assert not response["result"]["isError"], response

    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT surface, session_id, client_key FROM read_traffic ORDER BY seq"
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["context"]
    assert rows[0][2] == client_key(), (
        "the row names the session the SERVER can see, whatever the client chose "
        "to send"
    )
    assert rows[0][2] != "unknown"


def test_the_key_survives_a_session_that_sends_no_session_id() -> None:
    """The case of a client that names no session (Codex before it sent
    `_meta.sessionId`), which is the whole reason this exists.

    `session_id` stays `unknown` because nothing told us; `client_key` does not,
    because nobody had to.
    """
    url = fresh_store_path("clientkey_anon")
    pg = Database(url)
    store = Store.open(url)

    store.db.record_read_traffic(
        surface="context",
        session_id=None,
        terms="anonymous client",
        served_ids=["cap-1"],
        project_id="pynott",
        project_prefix=None,
        outcome=None,
        now=datetime.now(UTC),
    )

    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT session_id, client_key FROM read_traffic")
        session_id, key = cur.fetchone()
    assert session_id == "unknown"
    assert key == client_key() != "unknown"


CODEX_SESSION = "01a115f0-1e79-7c41-b52e-df1d831db27c"
CLAUDE_SESSION = "4f3c2a10-claude-env-session"


def _ingest(rid: int, content: str, meta: dict[str, Any]) -> dict[str, Any]:
    arguments = {
        "content": content,
        "kind": "fact",
        "source": "test_client_key",
        "anchor": "https://example.invalid/named-session",
    }
    params = {"name": "nmemory_capsule_ingest", "arguments": arguments, "_meta": meta}
    return {"id": rid, "method": "tools/call", "params": params}


def _read(rid: int, meta: dict[str, Any]) -> dict[str, Any]:
    params = {
        "name": "nmemory_context_show",
        "arguments": {"query": "fact"},
        "_meta": meta,
    }
    return {"id": rid, "method": "tools/call", "params": params}


def _filed_session(store: Store, answer: str) -> str:
    outcome = json.loads(json.loads(answer)["result"]["content"][0]["text"])
    outcome = outcome["outcomes"][0]
    assert outcome["status"] == "captured", outcome
    context = store.capsules.get_capture_context(outcome["id"])
    assert context is not None, outcome
    return context.session_id


def _read_sessions(url: Path) -> list[str]:
    with Database(url).writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT session_id FROM read_traffic ORDER BY seq")
        return [row[0] for row in cur.fetchall()]


def test_a_call_files_its_capture_and_read_under_the_session_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex names its session in each call's `_meta`, Claude Code in the env.

    Measured 2026-10-07 with a probe server. Under `codex exec` 0.160.1 the
    server's environment carries only HOME, LANG, LOGNAME, PATH, SHELL, TERM,
    TMPDIR and USER, `initialize` names `codex-mcp-client` and its version, and
    every `tools/call` carries `_meta.sessionId` (= `threadId`, the id Codex
    printed for the session); the live store held 6,725 Codex captures, all
    filed under `unknown`. Under `claude -p` 2.1.292 the environment carries
    `CLAUDE_CODE_SESSION_ID` and `_meta` only `claudecode/toolUseId` and
    `progressToken`. The named session wins over a conflicting environment, and
    a call naming none keeps the environment's. The route is the SDK door.
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE_SESSION)
    url = fresh_store_path("named_session")
    store = Store.open(url)
    codex = {"progressToken": 1, "sessionId": CODEX_SESSION, "threadId": CODEX_SESSION}
    claude = {"claudecode/toolUseId": "toolu_01probe", "progressToken": 2}
    answers = wire(
        MemoryServer(store=store),
        [
            _ingest(1, "Codex wrote this fact about the probe server.", codex),
            _read(2, codex),
            _ingest(3, "Claude wrote another fact, on a different subject.", claude),
            _read(4, claude),
        ],
    )

    assert _filed_session(store, answers[0]) == CODEX_SESSION
    assert _filed_session(store, answers[2]) == CLAUDE_SESSION
    assert _read_sessions(url) == [CODEX_SESSION, CLAUDE_SESSION]


@pytest.mark.parametrize("named", ["", "   ", 42])
def test_a_session_named_as_no_session_is_refused_not_dropped(
    named: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `sessionId` the call supplies but that names no session is refused.

    Read as absence, it would file the call under `unknown` or under another
    session (the environment's, the transport's) without a word. Neither
    client measured sends one: Codex sends a UUID, Claude Code no such key.
    """
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CLAUDE_SESSION)
    url = fresh_store_path("malformed_session")
    store = Store.open(url)
    meta = {"progressToken": 1, "sessionId": named}
    answers = wire(
        MemoryServer(store=store),
        [_ingest(1, "A fact that must not be filed.", meta), _read(2, meta)],
    )

    assert [json.loads(a)["error"]["code"] for a in answers] == [-32602, -32602]
    assert store.capsules.list() == []
    assert _read_sessions(url) == []


def test_a_parent_named_with_parens_does_not_shift_every_later_field() -> None:
    """The mutation a real parent cannot produce, so the fixture builds one.

    `/proc/<pid>/stat` field 2 is the executable name in parentheses and the
    kernel does not escape it. Parsing from the FIRST ')' instead of the last
    reads a process named `(we ird)` off by two fields and returns a start time
    that is not one. No parent on this host is named that way, so every test
    built from `os.getppid()` passes under either parse — measured: that mutant
    survived six of them.
    """
    # After the last ')' the split index 0 is field 3 (state), so field 22
    # lands at index 19: state, ppid, then seventeen fillers, then starttime.
    fill = " ".join(str(i) for i in range(1, 18))  # fields 5..21
    raw = f"4242 (we (ir)d proc) S 1 {fill} 987654 " + " ".join(["0"] * 30)
    assert starttime_from_stat(raw) == 987654

    # And the honest ordinary case still parses.
    plain = f"4242 (bash) S 1 {fill} 555 " + " ".join(["0"] * 30)
    assert starttime_from_stat(plain) == 555

"""`nmemory serve`: the MCP Streamable HTTP door, driven through a real socket.

The child is the real CLI on a loopback port the kernel picks, over a store the
`nott-database` plugin made disposable. HOME is the test's own directory, so the
default key path (`~/.nott/secrets/memory.key`) is exercised without a flag.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from storefile import default_store_path
from workspace_root import workspace_path

# The tree's package, also under mutmut: the copy's modules load mutmut's
# configuration from the working directory, and this child starts elsewhere.
_MEMBER_ROOT = str(workspace_path("mcps/nmemory"))
_SERVING = re.compile(r"nmemory: serving (http://127\.0\.0\.1:\d+/mcp)")


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "PYTHONPATH": _MEMBER_ROOT,
        "NOTT_DATABASE_DIR": str(default_store_path().parent),
        "NOTT_NMEMORY_CONFIG": str(home / "no-memory.toml"),
        "NOTT_NMEMORY_CURATOR": "0",
    }


def _argv(project: str | None) -> list[str]:
    scope = ["--project", project] if project else []
    return [
        sys.executable,
        "-m",
        "nmemory",
        "mcp",
        "serve",
        *scope,
        "--http",
        "--listen",
        "127.0.0.1:0",
    ]


def _start(
    home: Path, project: str | None = "test_http"
) -> tuple[subprocess.Popen[str], str]:
    # stderr goes to a file, never a pipe: a pipe nobody drains fills and
    # blocks the server; the announcement is awaited with a deadline.
    log = home / "serve.stderr"
    with log.open("w") as sink:
        proc = subprocess.Popen(
            _argv(project),
            env=_env(home),
            cwd=home,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sink,
            text=True,
        )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        found = _SERVING.search(log.read_text())
        if found:
            return proc, found.group(1)
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.kill()
    proc.wait(timeout=10)
    raise AssertionError(
        f"serve did not announce its address: {log.read_text()[-2000:]}"
    )


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, str, Path]]:
    proc, url = _start(tmp_path)
    key_file = tmp_path / ".nott" / "secrets" / "memory.key"
    try:
        yield url, key_file.read_text().strip(), key_file
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _post(url: str, key: str | None, body: object) -> tuple[int, dict[str, str], bytes]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    return _send(req)


def _send(req: urllib.request.Request) -> tuple[int, dict[str, str], bytes]:
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return (
                resp.status,
                {k.lower(): v for k, v in resp.headers.items()},
                resp.read(),
            )
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def _rpc(
    url: str, key: str, method: str, params: dict[str, Any], rid: int, session: str
) -> dict[str, Any]:
    headers, reply = _call(
        url,
        key,
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
        session,
    )
    assert headers["content-type"] == "application/json"
    assert reply["id"] == rid
    return reply


def test_first_run_creates_a_private_key(served: tuple[str, str, Path]) -> None:
    _, key, key_file = served
    assert re.fullmatch(r"nott_mem_[0-9A-Za-z]{32}", key)
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_second_run_reuses_the_key(tmp_path: Path) -> None:
    first, _ = _start(tmp_path)
    first.terminate()
    first.wait(timeout=10)
    key_file = tmp_path / ".nott" / "secrets" / "memory.key"
    before = key_file.read_bytes()
    second, _ = _start(tmp_path)
    second.terminate()
    second.wait(timeout=10)
    assert key_file.read_bytes() == before


def test_a_key_others_can_read_is_refused(tmp_path: Path) -> None:
    key_file = tmp_path / ".nott" / "secrets" / "memory.key"
    key_file.parent.mkdir(parents=True)
    key_file.write_text("x" * 43)
    key_file.chmod(0o644)
    proc = subprocess.run(
        _argv(None),
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode != 0
    assert "0600" in proc.stderr


def test_initialize_list_and_round_trip(served: tuple[str, str, Path]) -> None:
    url, key, _ = served
    session = _init(url, key, "claude-code")
    tools = _rpc(url, key, "tools/list", {}, 2, session)["result"]["tools"]
    assert {"nmemory_context_show", "nmemory_capsule_ingest"} <= {
        t["name"] for t in tools
    }

    marker = "http-door-round-trip-7f3a"
    ingest = _rpc(
        url,
        key,
        "tools/call",
        {
            "name": "nmemory_capsule_ingest",
            "arguments": {
                "content": f"fact {marker}",
                "source": "test",
                "anchor": "https://example.invalid/http-door",
                "kind": "fact",
            },
        },
        3,
        session,
    )
    outcome = json.loads(ingest["result"]["content"][0]["text"])["outcomes"][0]
    assert outcome["status"] == "captured", outcome
    found = _rpc(
        url,
        key,
        "tools/call",
        {
            "name": "nmemory_context_show",
            "arguments": {
                "query": marker,
                "sources": ["memory"],
            },
        },
        4,
        session,
    )
    results = json.loads(found["result"]["content"][0]["text"])["results"]
    assert [r["id"] for r in results] == [outcome["id"]], results


def test_notification_is_accepted_without_a_body(served: tuple[str, str, Path]) -> None:
    url, key, _ = served
    session = _init(url, key, "claude-code")
    req = urllib.request.Request(
        url,
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {key}",
            "Mcp-Session-Id": session,
        },
        method="POST",
    )
    status, _, body = _send(req)
    assert (status, body) == (202, b"")


def test_get_has_no_event_stream(served: tuple[str, str, Path]) -> None:
    url, key, _ = served
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    status, _, _ = _send(req)
    assert status >= 400


def test_missing_or_wrong_bearer_is_refused(served: tuple[str, str, Path]) -> None:
    url, _, _ = served
    ping = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert _post(url, None, ping)[0] == 401
    assert _post(url, "not-the-key", ping)[0] == 401


def test_concurrent_calls_all_answer(served: tuple[str, str, Path]) -> None:
    url, key, _ = served
    session = _init(url, key, "claude-code")

    def call(i: int) -> dict[str, Any]:
        return _rpc(
            url,
            key,
            "tools/call",
            {
                "name": "nmemory_context_show",
                "arguments": {
                    "query": f"concurrent {i}",
                    "sources": ["memory"],
                },
            },
            100 + i,
            session,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        replies = list(pool.map(call, range(8)))
    assert all("result" in r and not r["result"].get("isError") for r in replies)


def _call(
    url: str, key: str, body: dict[str, Any], session: str | None
) -> tuple[dict[str, str], dict[str, Any]]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {key}",
    }
    if session is not None:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")
    status, got, raw = _send(req)
    assert status == 200, (status, raw)
    return got, json.loads(raw)


def test_each_client_session_is_its_own_capture_session(
    served: tuple[str, str, Path],
) -> None:
    """One daemon serves many clients: captures name the MCP session, not the daemon."""
    url, key, _ = served
    sessions = []
    for i, client in enumerate(("claude-code", "codex")):
        got, _ = _call(
            url,
            key,
            {
                "jsonrpc": "2.0",
                "id": i,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": client, "version": "1"},
                },
            },
            None,
        )
        sessions.append(got["mcp-session-id"])
    assert len(set(sessions)) == 2
    # Codex names its own session in each call's `_meta`; that wins over the
    # transport's. Claude Code names none here and keeps the transport's.
    named = "01a115f0-1e79-7c41-b52e-df1d831db27c"

    for i, session in enumerate(sessions):
        marker = f"sesscapture{i}xc41e"
        params: dict[str, Any] = {
            "name": "nmemory_capsule_ingest",
            "arguments": {
                "content": f"fact {marker}",
                "source": "test",
                "anchor": "https://example.invalid/http-door",
                "kind": "fact",
            },
        }
        if i == 1:
            params["_meta"] = {"progressToken": 1, "sessionId": named}
        _, ingested = _call(
            url,
            key,
            {"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": params},
            session,
        )
        outcome = json.loads(ingested["result"]["content"][0]["text"])["outcomes"][0]
        assert outcome["status"] == "captured", outcome
        _, found = _call(
            url,
            key,
            {
                "jsonrpc": "2.0",
                "id": 20 + i,
                "method": "tools/call",
                "params": {
                    "name": "nmemory_capsule_retrieve",
                    "arguments": {"terms": [marker], "lane": "term"},
                },
            },
            session,
        )
        text = found["result"]["content"][0]["text"]
        contexts = [r["capture_context"] for r in json.loads(text)["results"]]
        assert contexts and contexts[0]["session_id"] == (session, named)[i], contexts
        assert contexts[0]["harness"].startswith(("claude-code", "codex")[i]), contexts


def _init(url: str, key: str, client: str) -> str:
    got, _ = _call(
        url,
        key,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": client, "version": "1"},
            },
        },
        None,
    )
    return got["mcp-session-id"]


def test_a_client_keeps_its_own_default_project(tmp_path: Path) -> None:
    """Without --project the scope derives from the client, per session."""
    proc, url = _start(tmp_path, project=None)
    key = (tmp_path / ".nott" / "secrets" / "memory.key").read_text().strip()
    try:
        alpha = _init(url, key, "alpha-client")
        _init(url, key, "beta-client")
        marker = "ownprojectx9d2"
        _, ingested = _call(
            url,
            key,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "nmemory_capsule_ingest",
                    "arguments": {
                        "content": f"fact {marker}",
                        "source": "test",
                        "anchor": "https://example.invalid/http-door",
                        "kind": "fact",
                    },
                },
            },
            alpha,
        )
        text = ingested["result"]["content"][0]["text"]
        assert json.loads(text)["outcomes"][0]["status"] == "captured", text
        _, found = _call(
            url,
            key,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "nmemory_capsule_retrieve",
                    "arguments": {"terms": [marker], "lane": "term"},
                },
            },
            alpha,
        )
        recalled = json.loads(found["result"]["content"][0]["text"])
        assert recalled["scope"]["project_id"] == "alpha-client", recalled
        assert recalled.get("results"), recalled
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_an_unknown_session_must_reinitialize(served: tuple[str, str, Path]) -> None:
    url, key, _ = served
    req = urllib.request.Request(
        url,
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
        {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "Mcp-Session-Id": "not-issued-by-this-daemon",
        },
        method="POST",
    )
    assert _send(req)[0] == 404


def test_read_only_over_http_lists_only_the_observational_tool(tmp_path: Path) -> None:
    log = tmp_path / "serve.stderr"
    with log.open("w") as sink:
        proc = subprocess.Popen(
            [*_argv("test_http"), "--read-only"],
            env=_env(tmp_path),
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sink,
            text=True,
        )
    try:
        deadline = time.monotonic() + 30
        while not (found := _SERVING.search(log.read_text())):
            assert time.monotonic() < deadline and proc.poll() is None, log.read_text()
            time.sleep(0.05)
        key = (tmp_path / ".nott" / "secrets" / "memory.key").read_text().strip()
        session = _init(found.group(1), key, "claude-code")
        tools = _rpc(found.group(1), key, "tools/list", {}, 1, session)["result"][
            "tools"
        ]
        assert [t["name"] for t in tools] == ["nmemory_context_show"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)

"""Share the nSPAWN stdio client and its disposable configuration."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import nott_database
import pytest

from nspawn.registry import create_store, store_path

MEMBER_ROOT = Path(__file__).resolve().parents[2]
FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"


class RpcTimeout(RuntimeError):
    """The stdio port did not respond within the judge's limit."""


class McpStdio:
    """Real JSON-RPC client for a ``python -m nspawn.main`` process."""

    def __init__(
        self, environment: dict[str, str], prefix: tuple[str, ...] = ()
    ) -> None:
        self.process = subprocess.Popen(
            [*prefix, sys.executable, "-m", "nspawn.main"],
            cwd=MEMBER_ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=True,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("the MCP subprocess did not open its stdio pipes")
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._buffer = bytearray()
        self._next_id = 1
        self.notifications: list[dict[str, Any]] = []

    def _receive(self, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        descriptor = self._stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(descriptor, selectors.EVENT_READ)
        try:
            while True:
                newline = self._buffer.find(b"\n")
                if newline >= 0:
                    raw = bytes(self._buffer[:newline])
                    del self._buffer[: newline + 1]
                    if not raw.strip():
                        continue
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise RuntimeError(
                            "the MCP door answered a frame that is not an object"
                        )
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RpcTimeout("the MCP door did not answer within 10 seconds")
                if not selector.select(remaining):
                    raise RpcTimeout("the MCP door did not answer within 10 seconds")
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    raise RuntimeError(
                        f"the MCP door closed before the answer; rc={self.process.poll()}"
                    )
                self._buffer.extend(chunk)
        finally:
            selector.close()

    def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        frame: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            frame["params"] = params
        self._stdin.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        self._stdin.flush()
        while True:
            answer = self._receive()
            if answer.get("id") == request_id:
                return answer
            self.notifications.append(answer)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        self._stdin.write(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        self._stdin.flush()

    def handshake(self) -> dict[str, Any]:
        initialized = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nspawn-live-judge", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return initialized

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self.process.poll() is None:
            self._stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()


@pytest.fixture(autouse=True)
def registry_file() -> Path:
    """This test's own registry file, in the directory the nott-database plugin
    names; the served processes inherit `NOTT_DATABASE_DIR` and open it."""
    path = store_path()
    create_store(path)
    return path


def store_rows(statement: str, params: tuple[object, ...] = ()) -> list[Any]:
    """Rows of this test's registry file, read on a connection of their own."""
    conn = nott_database.connect(store_path(), readonly=True)
    try:
        return nott_database.query(conn, statement, params)
    finally:
        conn.close()


@pytest.fixture
def live_environment(tmp_path: Path) -> dict[str, str]:
    """Own every configuration path; inherit only this test's store directory."""
    home = tmp_path / "home"
    home.mkdir()
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    credential = secrets / "fixture.token"
    credential.write_text("disposable-fake-credential-never-sent-to-a-vendor")
    credential.chmod(0o600)
    # ONE file carries [store], [defaults] and [[provider]]: under the single
    # config law the file that NOTT_NSPAWN_CONFIG names is also the roster, so
    # a store-only config here would resolve an EMPTY fleet and the compat
    # variable below would never be consulted.
    spawn_config = tmp_path / "spawn.toml"
    spawn_config.write_text(
        f"[store]\nrun_root = {json.dumps(str(tmp_path / 'runs'))}\n"
        f"[defaults]\nplugins = [{json.dumps(str(plugin))}]\n"
        '[[provider]]\nname = "fixture"\nkind = "claude"\n'
        'model = "fixture-model"\n'
        f"secrets = {json.dumps(str(secrets))}\n"
    )
    spawn_config.chmod(0o600)
    # nMEMORY's store is a file in this test's NOTT_DATABASE_DIR (P2); its config
    # path is private and absent, so the host's live memory.toml is never read.
    memory_config = tmp_path / "memory.toml"
    (plugin / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": sys.executable,
                        "args": ["-m", "nmemory", "mcp", "serve"],
                    }
                }
            }
        )
    )
    environment = {
        "NOTT_DATABASE_DIR": os.environ["NOTT_DATABASE_DIR"],
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": str(MEMBER_ROOT),
        "NOTT_NSPAWN_CONFIG": str(spawn_config),
        "NOTT_NSPAWN_ROSTER": str(spawn_config),
        "NOTT_NSPAWN_CLAUDE_BIN": str(FAKE_CLAUDE),
        "NOTT_NMEMORY_CONFIG": str(memory_config),
        "NOTT_OTLP_ENDPOINT": "http://127.0.0.1:9",
    }
    return environment


@pytest.fixture
def mcp_client(live_environment: dict[str, str]) -> Iterator[McpStdio]:
    client = McpStdio(live_environment)
    try:
        yield client
    finally:
        client.close()

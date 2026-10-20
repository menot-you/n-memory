"""The stdio door answers `initialize` and `tools/list` WITHOUT opening the store.

Measured 2026-09-05, two symptoms with one cause: the binary opened the store
in `main()`, before any message. (1) The release installer, running
on a service account without a store, failed: "the built binary did not
list its tools over its own stdio door" — the child refused and returned
nothing, when `tools/list` is schema-only. (2) Eight simultaneous startups
touched the store concurrently.

The cure: store opens on FIRST `tools/call`, through a memoized accessor; without
a store file that call returns typed error `-32002`, never a dead process.

Every child gets `NOTT_DATABASE_DIR`: without it the store is the account's live
directory (from the passwd home, not `$HOME`), which a test never opens.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from nott_database import DatabaseError
from stdio_peer import exchange
from storefile import default_store_path
from workspace_root import workspace_path

from nmemory import cli
from nmemory.config import ConfigError
from nmemory.server import MemoryServer
from nmemory.store import Store

# The tree's package, also under mutmut: the copy's modules load mutmut's
# configuration from the working directory, and this child starts elsewhere.
_MEMBER_ROOT = str(workspace_path("mcps/nmemory"))


def _speak(
    env: dict[str, str],
    cwd: Path,
    messages: list[dict[str, Any]],
    *argv: str,
) -> list[dict[str, Any]]:
    """Talks JSON-RPC to a real `python -m nmemory` in a CONTROLLED environment.

    `stdio_peer.exchange` answers each request before closing stdin.
    """
    child_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(cwd),
        "PYTHONPATH": _MEMBER_ROOT,
        # conftest fence: a real `tools/call` exports a span, and the
        # default is the owner collector. `env -i` does not inherit it; the suite rule
        # mandates passing it.
        **env,
    }
    answers, errors, code = exchange(
        [sys.executable, "-m", "nmemory", "mcp", "serve", *argv],
        [{"id": i, **m} for i, m in enumerate(messages, start=1)],
        deadline_s=120,
        env=child_env,
        cwd=cwd,
    )
    assert len(answers) == len(messages), (
        f"{len(answers)} answers for {len(messages)} messages "
        f"(exit {code}); stderr:\n{errors}"
    )
    return answers


def _assert_catalog(answers: list[dict[str, Any]]) -> None:
    assert "result" in answers[0], answers[0]
    tools = answers[1]["result"]["tools"]
    assert tools, answers[1]
    assert all("inputSchema" in t for t in tools), [t.get("name") for t in tools]


def test_tools_list_needs_no_store(tmp_path: Path) -> None:
    init, listed, called = _speak(
        {"NOTT_DATABASE_DIR": str(tmp_path / "no-store")},
        tmp_path,
        [
            {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"method": "tools/list", "params": {}},
            {
                "method": "tools/call",
                "params": {
                    "name": "nmemory_context_show",
                    "arguments": {"query": "lazy"},
                },
            },
        ],
    )
    _assert_catalog([init, listed])
    assert called["error"]["code"] == -32002, called
    assert "missing" in called["error"]["message"], called


def test_first_tool_call_opens_the_store(tmp_path: Path) -> None:
    init, listed, called = _speak(
        {"NOTT_DATABASE_DIR": str(default_store_path().parent)},
        tmp_path,
        [
            {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"method": "tools/list", "params": {}},
            {
                "method": "tools/call",
                "params": {
                    "name": "nmemory_context_show",
                    "arguments": {"query": "lazy"},
                },
            },
        ],
    )
    _assert_catalog([init, listed])
    assert "result" in called, called
    assert called["result"].get("isError") is not True, called


def test_read_only_open_on_a_missing_store_answers_and_survives(
    tmp_path: Path,
) -> None:
    """A read-only door on a missing store: the first tool call is `-32002`,
    never a dead process, and the door keeps serving the catalog.
    """
    init, called, listed = _speak(
        {"NOTT_DATABASE_DIR": str(tmp_path / "no-store")},
        tmp_path,
        [
            {"method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {
                "method": "tools/call",
                "params": {
                    "name": "nmemory_context_show",
                    "arguments": {"query": "lazy"},
                },
            },
            {"method": "tools/list", "params": {}},
        ],
        "--read-only",
    )
    assert "result" in init, init
    assert called["error"]["code"] == -32002, called
    _assert_catalog([init, listed])


def test_server_rejects_both_store_and_open_store() -> None:
    fake_store = cast(Store, object())
    with pytest.raises(ValueError, match="store and open_store are mutually exclusive"):
        MemoryServer(store=fake_store, open_store=lambda: fake_store)


def test_server_without_store_source_refuses_tool_call_with_32002() -> None:
    server = MemoryServer()
    with pytest.raises(
        ConfigError, match="this server was built without a store source"
    ):
        _ = server.store

    called = server.call_tool("nmemory_context_show", {"query": "lazy"})
    assert called["error"]["code"] == -32002
    assert (
        "store unavailable: this server was built without a store source"
        in called["error"]["message"]
    )


def test_server_with_failing_store_open_refuses_tool_call_with_32002() -> None:
    def _fail_open() -> Store:
        raise DatabaseError("the store file is missing")

    server = MemoryServer(open_store=_fail_open)
    called = server.call_tool("nmemory_context_show", {"query": "lazy"})
    assert called["error"]["code"] == -32002
    assert "store unavailable: the store file is missing" in called["error"]["message"]


def test_server_memoised_open_is_not_repeated_on_second_call() -> None:
    real_store = Store.open_disposable_test_store()
    open_count = 0

    def _open() -> Store:
        nonlocal open_count
        open_count += 1
        return real_store

    server = MemoryServer(open_store=_open)
    assert open_count == 0

    first = server.call_tool("nmemory_context_show", {"query": "lazy"})
    assert "result" in first, first
    assert open_count == 1

    second = server.call_tool("nmemory_context_show", {"query": "lazy"})
    assert "result" in second, second
    assert open_count == 1
    assert server.store is real_store


def test_cli_mutable_server_spawns_services_on_first_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_store = Store.open_disposable_test_store()
    embedding_worker_spawned: list[Store] = []

    def fake_open_store(
        args: argparse.Namespace,
        *,
        read_only: bool = False,
        path: object = None,
    ) -> Store:
        assert read_only is False
        return real_store

    def fake_spawn_embedding(store: Store) -> tuple[Mock, Mock]:
        embedding_worker_spawned.append(store)
        return Mock(), Mock()

    def run_server(instance: MemoryServer) -> None:
        assert not instance.read_only
        assert len(embedding_worker_spawned) == 0

        res = instance.call_tool("nmemory_context_show", {"query": "lazy"})
        assert "result" in res, res
        assert len(embedding_worker_spawned) == 1
        assert embedding_worker_spawned[0] is real_store

    monkeypatch.setattr(sys, "argv", ["nmemory", "mcp", "serve"])
    monkeypatch.setattr(cli, "open_store_from_args", fake_open_store)
    monkeypatch.setattr(cli, "spawn_embedding_worker", fake_spawn_embedding)
    monkeypatch.setattr("nmemory.server.run_stdio_server", run_server)

    cli.main()

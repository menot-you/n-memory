"""The standalone review server is observational by construction."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import pytest

from nmemory import cli, context, server
from nmemory.server import MemoryServer
from nmemory.store import Store


class _ReadBoundary:
    def __init__(self) -> None:
        self.entries = 0

    @contextmanager
    def tool_boundary(self, *, defer_traffic: bool = False) -> Iterator[None]:
        self.entries += 1
        yield


class _ReadStore:
    def __init__(self) -> None:
        self.db = _ReadBoundary()


def test_cli_read_only_uses_only_the_observational_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_store = cast(Store, object())
    seen: list[bool] = []

    def open_store(
        args: argparse.Namespace,
        *,
        read_only: bool = False,
        read_census: bool = False,
        path: object = None,
    ) -> Store:
        seen.append(read_only)
        return fake_store

    def fail_mutable_service(*args: object, **kwargs: object) -> None:
        pytest.fail("read-only startup reached a mutable background service")

    def run_server(instance: MemoryServer) -> None:
        # Opening is lazy (2026-09-05): the REAL server, constructed
        # by `main()`, only opens the store on the first tool. It is that call — not
        # a stub running the opener — that proves the read-only path reaches
        # no mutable service.
        assert instance.read_only
        instance.call_tool("nmemory_context_show", {"query": "x"})

    monkeypatch.setattr(sys, "argv", ["nmemory", "mcp", "serve", "--read-only"])
    monkeypatch.setattr(cli, "open_store_from_args", open_store)
    monkeypatch.setattr(cli, "spawn_embedding_worker", fail_mutable_service)
    monkeypatch.setattr("nmemory.server.run_stdio_server", run_server)

    cli.main()

    assert seen == [True]


def test_cli_read_only_refuses_subcommands_before_opening_store(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_open(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid read-only CLI reached the store")

    monkeypatch.setattr(sys, "argv", ["nmemory", "digest", "show", "--read-only"])
    monkeypatch.setattr(cli, "open_store_from_args", fail_open)

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "unrecognized arguments: --read-only" in capsys.readouterr().err


def test_read_only_server_exposes_and_executes_only_observational_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_embedding(*args: object, **kwargs: object) -> None:
        pytest.fail("read-only server constructed an embedding client")

    seen: list[dict[str, object]] = []

    def execute_context(**kwargs: object) -> dict[str, object]:
        seen.append(kwargs)
        return {
            "label": "ADVISORY_NOT_AUTHORITY",
            "results": [],
            "semantic": {
                "detail": "semantic embeddings are not configured",
                "status": "unavailable_lexical_only",
            },
        }

    monkeypatch.setattr(server, "EmbeddingClient", no_embedding)
    monkeypatch.setattr(context, "execute_context", execute_context)
    backing = _ReadStore()
    instance = MemoryServer(cast(Store, backing), read_only=True)

    tools = instance.get_tool_definitions()
    assert [tool["name"] for tool in tools] == ["nmemory_context_show"]
    schema = tools[0]["inputSchema"]
    assert isinstance(schema, dict)
    defs = schema["$defs"]
    assert isinstance(defs, dict)
    source_def = defs["ContextSourceParam"]
    assert isinstance(source_def, dict)
    choices = source_def["oneOf"]
    assert isinstance(choices, list)
    assert {choice["const"] for choice in choices} == set(
        context.OBSERVATIONAL_CONTEXT_SOURCES
    )

    denied = instance.call_tool("nmemory_capsule_purge", {})
    assert denied["error"]["code"] == -32602
    assert backing.db.entries == 0

    result = instance.call_tool("nmemory_context_show", {"query": "known capsule"})
    assert "result" in result
    assert backing.db.entries == 1
    assert len(seen) == 1
    assert seen[0]["sources"] == list(context.OBSERVATIONAL_CONTEXT_SOURCES)
    assert seen[0]["embeddings"] is None
    payload = json.loads(result["result"]["content"][0]["text"])
    assert payload["semantic"] == {
        "detail": "semantic embeddings are disabled by read-only mode",
        "status": "unavailable_lexical_only",
    }

    refused = instance.call_tool(
        "nmemory_context_show", {"query": "known capsule", "sources": ["codegraph"]}
    )
    assert refused["result"]["isError"] is True
    assert len(seen) == 1


def test_normal_server_keeps_the_complete_catalog() -> None:
    instance = MemoryServer(
        cast(Store, _ReadStore()),
        embeddings=cast(Any, object()),
    )

    tools = instance.get_tool_definitions()

    assert len(tools) == len(MemoryServer.SUPPORTED_TOOLS) == 27
    assert "nmemory_board_show" in {tool["name"] for tool in tools}
    assert "nmemory_work_list" in {tool["name"] for tool in tools}
    context_tool = next(
        tool for tool in tools if tool["name"] == "nmemory_context_show"
    )
    choices = context_tool["inputSchema"]["$defs"]["ContextSourceParam"]["oneOf"]
    assert {choice["const"] for choice in choices} == set(context.ALL_CONTEXT_SOURCES)

"""Compare and capture through two real MCP stdio writers on one private store."""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from nott_socket.mcp import McpClient, McpClientError, StdioServer

from nmemory.database import create_store, store_path

PROJECT = "objective-revision"


@contextmanager
def client(cwd: Path) -> Iterator[McpClient]:
    server = StdioServer(
        sys.executable,
        ("-m", "nmemory.cli", "mcp", "serve", "--project", PROJECT),
        {"NOTT_NMEMORY_CURATOR": "off"},
    )
    with McpClient(server, cwd, server_name="nmemory") as memory:
        yield memory


def capture(content: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "doc",
        "content": content,
        "source": "nott-app:objectives",
        "anchor": "objective:private-session",
        "project_id": PROJECT,
        "hints": False,
        **extra,
    }


def test_revision_null_requires_absence_and_omission_keeps_ingest(
    tmp_path: Path,
) -> None:
    create_store(store_path())
    with client(tmp_path) as memory:
        first = memory.call(
            "nmemory_capsule_ingest", capture("First checklist", expected_revision=None)
        )
        assert first["captured"] == 1, first
        stale = memory.call(
            "nmemory_capsule_ingest",
            capture("Conflicting checklist", expected_revision=None),
        )
        assert stale["rejected"] == 1, stale
        assert "revision" in stale["outcomes"][0]["error"], stale
        compatible = memory.call("nmemory_capsule_ingest", capture("Ordinary capture"))
        assert compatible["captured"] == 1, compatible


def test_concurrent_writers_accept_one_successor_and_no_partial_revision(
    tmp_path: Path,
) -> None:
    create_store(store_path())
    with client(tmp_path) as seed:
        previous = seed.call("nmemory_capsule_ingest", capture("Initial checklist"))[
            "outcomes"
        ][0]["id"]
    ready = threading.Barrier(2)

    def write(content: str) -> dict[str, Any]:
        with client(tmp_path) as memory:
            ready.wait(timeout=20)
            try:
                return memory.call(
                    "nmemory_capsule_ingest",
                    capture(content, expected_revision=previous, supersedes=previous),
                )
            except McpClientError as error:
                return {"transportError": str(error)}

    with ThreadPoolExecutor(max_workers=2) as writers:
        results = list(writers.map(write, ("Edited checklist", "Removed checklist")))
    assert sum(result.get("captured", 0) for result in results) == 1, results
    refused = next(result for result in results if result.get("rejected") == 1)
    assert "revision" in refused["outcomes"][0]["error"], refused
    accepted = next(result for result in results if result.get("captured") == 1)
    with client(tmp_path) as memory:
        listed = memory.call(
            "nmemory_capsule_list", {"project_id": PROJECT, "kind": "doc"}
        )
        assert len(listed["entries"]) == 2, listed
        shown = memory.call(
            "nmemory_capsule_show", {"id": accepted["outcomes"][0]["id"]}
        )
        assert any(
            edge["kind"] == "supersedes" and edge["to"] == previous
            for edge in shown["relations"]
        )


@pytest.mark.parametrize("field", ["project_id", "source", "anchor"])
def test_revision_is_bound_to_the_exact_provenance(tmp_path: Path, field: str) -> None:
    create_store(store_path())
    with client(tmp_path) as memory:
        previous = memory.call("nmemory_capsule_ingest", capture("Original checklist"))[
            "outcomes"
        ][0]["id"]
        params = capture(
            "Changed checklist", expected_revision=previous, supersedes=previous
        )
        params[field] = "other"
        refused = memory.call("nmemory_capsule_ingest", params)
        assert refused["rejected"] == 1, refused
        assert "revision" in refused["outcomes"][0]["error"], refused


def test_conditional_capture_refuses_content_from_another_provenance(
    tmp_path: Path,
) -> None:
    create_store(store_path())
    with client(tmp_path) as memory:
        memory.call("nmemory_capsule_ingest", capture("Identical stored text"))
        refused = memory.call(
            "nmemory_capsule_ingest",
            capture(
                "Identical stored text",
                anchor="objective:other",
                expected_revision=None,
            ),
        )
        assert refused["rejected"] == 1, refused
        listed = memory.call(
            "nmemory_capsule_list", {"project_id": PROJECT, "kind": "doc"}
        )
        assert len(listed["entries"]) == 1, listed

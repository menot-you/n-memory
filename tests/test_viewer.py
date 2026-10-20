"""`nmemory mcp serve --viewer`: reads that count nothing and start no writer.

A viewer child serves the page's four reads. Each one must leave the read
clocks (read_traffic, usage), the receipts and the recall-miss ledger as they
were, because those rows change recall for every agent. The control runs the
same calls through a normal child and moves the counts, so the test is not empty.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from nott_socket.mcp import McpClient, McpRefused, StdioServer
from storefile import default_store_path

from nmemory import cli
from nmemory.queries.census import last_read_among
from nmemory.server import MemoryServer
from nmemory.store import Store

HMAC = {"NOTT_NMEMORY_HMAC_KEY": "viewer-local-" * 4}
COUNTED = ("read_traffic", "usage", "recall_receipts", "recall_misses")


def child(tmp_path: Path, *flags: str) -> McpClient:
    server = StdioServer(
        sys.executable,
        ("-m", "nmemory", "mcp", "serve", "--project", "p", *flags),
        HMAC,
    )
    return McpClient(server, tmp_path, deadline_s=60, server_name="nmemory")


def item(text: str, **extra: Any) -> dict[str, Any]:
    return {
        "kind": "decision",
        "content": text,
        "source": "session:viewer-test",
        "anchor": "test_viewer.py",
        "proof_hint": "fixture: the test writes this capsule itself",
        "confidence": 0.9,
        "project_id": "p",
        **extra,
    }


@pytest.fixture
def filled(tmp_path: Path) -> dict[str, Any]:
    path = default_store_path()
    with child(tmp_path) as normal:
        answer = normal.call(
            "nmemory_capsule_ingest",
            {
                "items": [
                    item("Viewerprobe alpha decision keeps the page honest."),
                    item("Viewerprobe beta decision keeps the page honest."),
                    item(
                        "Lapsedonly decision whose validity has ended.",
                        valid_from="2020-01-01T00:00:00Z",
                        valid_to="2020-02-01T00:00:00Z",
                    ),
                ]
            },
        )
        ids = [o["id"] for o in answer["outcomes"]]
        normal.call(
            "nmemory_outcome_record",
            {"description": "Viewerprobe held.", "actor": "test", "capsule_id": ids[0]},
        )
    return {"path": path, "ids": ids}


def counts(path: Path, ids: list[str]) -> tuple[dict[str, int], dict[str, Any]]:
    store = Store.open(path)
    found: dict[str, int] = {}
    with store.db.connection() as conn, conn.cursor() as cur:
        for table in COUNTED:
            cur.execute(f"SELECT count(*) FROM {table}")
            row = cur.fetchone()
            assert row is not None
            found[table] = int(row[0])
    return found, dict(last_read_among(store.db, ids))


def page_reads(client: McpClient, ids: list[str]) -> dict[str, Any]:
    """The four page verbs, with each of the three retrieve outcomes."""
    client.call("nmemory_capsule_list", {"kind": "decision", "limit": 10})
    client.call("nmemory_capsule_show", {"id": ids[0]})
    client.call("nmemory_capsule_show", {"ids": ids})
    client.call("nmemory_outcome_list", {"limit": 10})
    outcomes = {
        "grounded": client.call("nmemory_capsule_retrieve", {"terms": ["viewerprobe"]}),
        "missing_evidence": client.call(
            "nmemory_capsule_retrieve", {"terms": ["lapsedonly"]}
        ),
        "abstain": client.call("nmemory_capsule_retrieve", {"terms": ["zzzunmatched"]}),
    }
    for expected, answer in outcomes.items():
        assert answer["outcome"] == expected, answer
    return outcomes


def test_viewer_reads_count_nothing(tmp_path: Path, filled: dict[str, Any]) -> None:
    path, ids = filled["path"], filled["ids"]
    before = counts(path, ids)
    with child(tmp_path, "--viewer") as viewer:
        page_reads(viewer, ids)
    assert counts(path, ids) == before

    with child(tmp_path) as normal:
        page_reads(normal, ids)
    after, read = counts(path, ids)
    assert all(after[table] > before[0][table] for table in COUNTED), (before, after)
    assert read != before[1]


def test_viewer_grounded_results_equal_a_normal_search(
    tmp_path: Path, filled: dict[str, Any]
) -> None:
    def results(client: McpClient) -> list[tuple[Any, ...]]:
        answer = client.call("nmemory_capsule_retrieve", {"terms": ["viewerprobe"]})
        return [
            (r["id"], r["headline"], r["instruction_taint"], r["relevance"])
            for r in answer["results"]
        ]

    with child(tmp_path, "--viewer") as viewer:
        seen = results(viewer)
    with child(tmp_path) as normal:
        assert results(normal) == seen
    assert len(seen) == 2


@pytest.mark.parametrize("tool", ["nmemory_capsule_ingest", "nmemory_digest_show"])
def test_viewer_refuses_every_other_tool(
    tmp_path: Path, filled: dict[str, Any], tool: str
) -> None:
    with (
        child(tmp_path, "--viewer") as viewer,
        pytest.raises(McpRefused, match=f'unknown tool "{tool}"'),
    ):
        viewer.call(tool, {})


def test_viewer_and_read_only_exclude_each_other(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def served(instance: MemoryServer) -> None:
        raise AssertionError("a server started with both flags")

    monkeypatch.setattr(
        sys, "argv", ["nmemory", "mcp", "serve", "--viewer", "--read-only"]
    )
    monkeypatch.setattr("nmemory.server.run_stdio_server", served)
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


class _Started(Exception):
    """A background writer was started."""


@pytest.mark.parametrize("viewer", [True, False])
def test_viewer_starts_no_background_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, viewer: bool
) -> None:
    from nmemory import curator

    def open_store(args: argparse.Namespace, **kwargs: object) -> Store:
        return cast(Store, object())

    def started(*args: object, **kwargs: object) -> None:
        raise _Started

    def run_server(instance: MemoryServer) -> None:
        assert instance.viewer is viewer
        instance.store  # noqa: B018 - the store, and its writers, open lazily

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")
    flags = ["--viewer"] if viewer else []
    monkeypatch.setattr(
        sys, "argv", ["nmemory", "mcp", "serve", "--project", "p", *flags]
    )
    monkeypatch.setattr(cli, "open_store_from_args", open_store)
    monkeypatch.setattr(cli, "spawn_embedding_worker", started)
    monkeypatch.setattr(curator, "spawn_curator_worker", started)
    monkeypatch.setattr("nmemory.server.run_stdio_server", run_server)
    if viewer:
        cli.main()
    else:
        with pytest.raises(_Started):
            cli.main()


def test_viewer_curator_is_reached_without_the_embedding_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The control for the curator sentinel: without --viewer it is reached too."""
    from nmemory import curator

    reached: list[str] = []

    def started(name: str) -> tuple[Mock, Mock]:
        reached.append(name)
        return Mock(), Mock()

    def open_store(args: argparse.Namespace, **kwargs: object) -> Store:
        return cast(Store, object())

    def run_server(instance: MemoryServer) -> None:
        instance.store  # noqa: B018 - the lazy open is the call under test

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")
    monkeypatch.setattr(sys, "argv", ["nmemory", "mcp", "serve", "--project", "p"])
    monkeypatch.setattr(cli, "open_store_from_args", open_store)
    monkeypatch.setattr(
        cli, "spawn_embedding_worker", lambda *a, **k: started("embedding")
    )
    monkeypatch.setattr(
        curator, "spawn_curator_worker", lambda *a, **k: started("curator")
    )
    monkeypatch.setattr("nmemory.server.run_stdio_server", run_server)
    cli.main()
    assert reached == ["embedding", "curator"]

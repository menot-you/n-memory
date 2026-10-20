"""nmemory_outcome_list: the bounded, newest-first outcome read, and show's headline."""

from __future__ import annotations

import json
from typing import Any

import pytest
from storefile import default_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store


def ok(response: dict[str, Any]) -> dict[str, Any]:
    assert "result" in response and not response["result"]["isError"], response
    payload: dict[str, Any] = json.loads(response["result"]["content"][0]["text"])
    return payload


@pytest.fixture
def server() -> MemoryServer:
    return MemoryServer(store=Store.open(default_store_path()))


def capsule(server: MemoryServer, project: str, text: str) -> str:
    answer = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "kind": "decision",
                "content": text,
                "source": "session:outcome-list-test",
                "anchor": "test_outcome_list.py",
                "proof_hint": "fixture: the test writes this capsule itself",
                "confidence": 0.9,
                "project_id": project,
            },
        )
    )
    return str(answer["outcomes"][0]["id"])


def record(server: MemoryServer, text: str, capsule_id: str | None) -> str:
    args: dict[str, Any] = {"description": text, "actor": "test"}
    if capsule_id is not None:
        args["capsule_id"] = capsule_id
    return str(ok(server.call_tool("nmemory_outcome_record", args))["recorded"]["id"])


def outcome_count(server: MemoryServer) -> int:
    with server.store.db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outcomes")
        row = cur.fetchone()
        assert row is not None
        return int(row[0])


@pytest.fixture
def filled(server: MemoryServer) -> list[str]:
    in_p = capsule(server, "p", "The parent project keeps its outcome rows.")
    in_sub = capsule(server, "p/sub", "The child project keeps its outcome rows.")
    in_q = capsule(server, "q", "The other project keeps its outcome rows.")
    return [
        record(server, "First outcome on the parent project.", in_p),
        record(server, "Second outcome on the child project.", in_sub),
        record(server, "Third outcome on the other project.", in_q),
        record(server, "Fourth outcome bound to no capsule.", None),
    ]


def test_limit_returns_the_newest_first(
    server: MemoryServer, filled: list[str]
) -> None:
    answer = ok(server.call_tool("nmemory_outcome_list", {"limit": 2}))
    assert [o["id"] for o in answer["outcomes"]] == [filled[3], filled[2]]
    assert answer["returned"] == 2
    assert answer["label"] == "ADVISORY_NOT_AUTHORITY"
    assert answer["framing"] == "DATA"
    assert answer["outcomes"][0] == {
        "id": filled[3],
        "description": "Fourth outcome bound to no capsule.",
        "actor": "test",
        "at": answer["outcomes"][0]["at"],
    }


def test_default_limit_reads_every_row_here(
    server: MemoryServer, filled: list[str]
) -> None:
    answer = ok(server.call_tool("nmemory_outcome_list", {}))
    assert [o["id"] for o in answer["outcomes"]] == list(reversed(filled))


def test_project_prefix_keeps_the_subtree_only(
    server: MemoryServer, filled: list[str]
) -> None:
    answer = ok(server.call_tool("nmemory_outcome_list", {"project_prefix": "p"}))
    assert [o["id"] for o in answer["outcomes"]] == [filled[1], filled[0]]


@pytest.mark.parametrize("limit", [0, 501])
def test_limit_out_of_range_is_refused(server: MemoryServer, limit: int) -> None:
    answer = server.call_tool("nmemory_outcome_list", {"limit": limit})
    assert answer["error"]["code"] == -32602
    assert "limit" in answer["error"]["message"]


def test_unknown_key_is_refused(server: MemoryServer) -> None:
    answer = server.call_tool("nmemory_outcome_list", {"capsule_id": "cap-1"})
    assert answer["result"]["isError"] is True
    assert "unknown field `capsule_id`" in answer["result"]["content"][0]["text"]


def test_list_writes_no_outcome(server: MemoryServer, filled: list[str]) -> None:
    before = outcome_count(server)
    ok(server.call_tool("nmemory_outcome_list", {"limit": 500}))
    assert outcome_count(server) == before == len(filled)


def test_show_headline_equals_list_headline(server: MemoryServer) -> None:
    cid = capsule(server, "p", "A headline line.\n\nThe body text follows here.")
    shown = ok(server.call_tool("nmemory_capsule_show", {"id": cid}))
    listed = ok(server.call_tool("nmemory_capsule_list", {"project_id": "p"}))
    (row,) = [row for row in listed["entries"] if row["id"] == cid]
    assert shown["headline"] == row["headline"] == "A headline line.\u2026"
    batch = ok(server.call_tool("nmemory_capsule_show", {"ids": [cid]}))
    assert batch["results"][0]["value"]["headline"] == row["headline"]

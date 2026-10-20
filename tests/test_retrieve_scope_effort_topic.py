"""Scope fence tests for effort_id and topic_id in nmemory_capsule_retrieve (U0e, U0f)."""

from __future__ import annotations

import json
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = fresh_store_path("efftop")
    return MemoryServer(store=Store.open(url))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_effort_id_fences_to_epic_and_members() -> None:
    """effort_id fences recall to the epic ∪ part_of members."""
    server = fresh_server()

    # 1. Ingest epic
    res_epic = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Epic de infraestrutura alvo_effort",
                        "source": "test:epic",
                        "anchor": "epic.py:1",
                        "project_id": "pynott",
                        "kind": "epic",
                    }
                ]
            },
        )
    )
    epic_id = res_epic["outcomes"][0]["id"]

    # 2. Ingest task 1 (epic member)
    res_t1 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Task 1 de infraestrutura alvo_effort",
                        "source": "test:t1",
                        "anchor": "t1.py:1",
                        "project_id": "pynott",
                        "kind": "task",
                    }
                ]
            },
        )
    )
    t1_id = res_t1["outcomes"][0]["id"]
    ok(
        server.call_tool(
            "nmemory_relation_add",
            {"kind": "part_of", "from": t1_id, "to": epic_id},
        )
    )

    # 3. Ingest task 2 (outside epic)
    res_t2 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Task 2 isolada alvo_effort",
                        "source": "test:t2",
                        "anchor": "t2.py:1",
                        "project_id": "pynott",
                        "kind": "task",
                    }
                ]
            },
        )
    )
    t2_id = res_t2["outcomes"][0]["id"]

    # Ordinary recall now serves the epic AND both tasks: `task` left the fence
    # on measurement (the most-served kind in the live store; 43% of its read
    # capsules are read across days), while `dod` stays fenced. Before that this
    # assertion read `== 1` with `kind_excluded == 2`, which is what fencing a
    # fifth of everything the store serves actually looks like at the boundary.
    res_all = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_effort"], "project_id": "pynott"},
        )
    )
    assert len(res_all["results"]) == 3
    assert res_all["excluded"].get("kind_excluded", 0) == 0

    # Retrieve with effort_id=epic_id -> only epic and task 1 return
    res_effort = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_effort"], "project_id": "pynott", "effort_id": epic_id},
        )
    )
    assert res_effort["outcome"] == "grounded", res_effort
    assert len(res_effort["results"]) == 2
    ids = [r["id"] for r in res_effort["results"]]
    assert epic_id in ids
    assert t1_id in ids
    assert t2_id not in ids

    assert "effort" in res_effort
    assert res_effort["effort"]["epic_id"] == epic_id
    assert res_effort["effort"]["member_total"] == 1


def test_topic_id_fences_across_projects() -> None:
    """topic_id fences recall to topic ∪ items with about relation to the topic."""
    server = fresh_server()

    # 1. Ingest topic doc
    res_topic = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Documento sobre arquitetura alvo_topic",
                        "source": "test:doc",
                        "anchor": "doc.py:1",
                        "project_id": "pynott",
                        "kind": "doc",
                    }
                ]
            },
        )
    )
    topic_id = res_topic["outcomes"][0]["id"]

    # 2. Ingest fact linked to topic
    res_f1 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Facto sobre arquitetura alvo_topic",
                        "kind": "fact",
                        "source": "test:f1",
                        "anchor": "f1.py:1",
                        "project_id": "outro_projeto",
                    }
                ]
            },
        )
    )
    f1_id = res_f1["outcomes"][0]["id"]
    ok(
        server.call_tool(
            "nmemory_relation_add",
            {"kind": "about", "from": f1_id, "to": topic_id},
        )
    )

    # 3. Ingest unlinked fact
    res_f2 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Facto desvinculado alvo_topic",
                        "kind": "fact",
                        "source": "test:f2",
                        "anchor": "f2.py:1",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )
    f2_id = res_f2["outcomes"][0]["id"]

    # Retrieve with topic_id -> returns topic and f1 (even with f1 from another project), but not f2
    res_top = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_topic"], "topic_id": topic_id},
        )
    )
    assert res_top["outcome"] == "grounded", res_top
    assert len(res_top["results"]) == 2
    ids = [r["id"] for r in res_top["results"]]
    assert topic_id in ids
    assert f1_id in ids
    assert f2_id not in ids
    assert "topic" in res_top
    assert res_top["topic"]["topic_id"] == topic_id
    assert res_top["topic"]["member_total"] == 1


def test_naming_a_topic_bypasses_the_kind_fence() -> None:
    """An explicit id set IS the caller asking for those capsules.

    `retrieve.py` skips the kind fence when `effort_set` or `topic_set` is not
    None, and RULEBOOK-level behaviour depends on it. Nothing else exercises it
    now: `effort_set` is built only from `part_of`, which the domain restricts to
    `task -> plan|epic`, and `task` left the fence — so no capsule can be both in
    an effort set AND fenced. The TOPIC path still can, because `about` accepts a
    `dod`, and `dod` is the kind still fenced.
    """
    server = fresh_server()
    res_topic = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": "bypassprobe topic",
                "kind": "doc",
                "source": "test_scope",
                "anchor": "bypass_topic",
                "project_id": "pynott",
            },
        )
    )
    topic_id = res_topic["outcomes"][0]["id"]
    res_dod = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": "bypassprobe dod",
                "kind": "dod",
                "source": "test_scope",
                "anchor": "bypass_dod",
                "project_id": "pynott",
            },
        )
    )
    dod_id = res_dod["outcomes"][0]["id"]
    ok(
        server.call_tool(
            "nmemory_relation_add", {"kind": "about", "from": dod_id, "to": topic_id}
        )
    )

    plain = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["bypassprobe"], "project_id": "pynott"},
        )
    )
    assert dod_id not in [r["id"] for r in plain["results"]], "fenced by kind"
    assert plain["excluded"]["kind_excluded"] == 1

    scoped = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["bypassprobe"], "project_id": "pynott", "topic_id": topic_id},
        )
    )
    assert dod_id in [r["id"] for r in scoped["results"]], (
        "naming the topic bypasses the kind fence"
    )
    assert scoped["excluded"].get("kind_excluded", 0) == 0

"""Tests for belief time known_at fence in nmemory_capsule_retrieve (U0c)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = fresh_store_path("knat")
    return MemoryServer(store=Store.open(url))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_known_at_rewinds_eligibility_for_superseded_capsule() -> None:
    """Belief rewinding scenario with known_at:

    1. Ingest cap1 (content 'versao 1 alvo_known') at time T1 (2026-01-10T10:00:00Z).
    2. Later, at time T2 (2026-02-10T10:00:00Z), ingest cap2 ('versao 2 alvo_known')
       and relate cap2 --supersedes--> cap1 at time T2.

    Today (time T3 = 2026-03-01T00:00:00Z):
    - Retrieve without known_at: cap1 is excluded as superseded, only cap2 returns.
    - Retrieve with known_at=T1.5 (2026-01-20T00:00:00Z):
      - At that instant T1.5, cap1 was NOT superseded yet and cap2 did NOT exist.
      - cap1 must return (grounded).
      - cap2 must be excluded as future_dated (did not exist in store at T1.5).
    """
    server = fresh_server()
    t1 = datetime.fromisoformat("2026-01-10T10:00:00+00:00")
    t2 = datetime.fromisoformat("2026-02-10T10:00:00+00:00")
    t3 = datetime.fromisoformat("2026-03-01T00:00:00+00:00")

    # Ingestion of cap1 at T1
    res1 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "versao 1 alvo_known informacao antiga",
                        "kind": "fact",
                        "source": "test:v1",
                        "anchor": "v1.py:1",
                        "project_id": "pynott",
                    }
                ]
            },
            now=t1,
        )
    )
    cid1 = res1["outcomes"][0]["id"]

    # Ingestion of cap2 at T2
    res2 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "versao 2 alvo_known informacao nova",
                        "kind": "fact",
                        "source": "test:v2",
                        "anchor": "v2.py:1",
                        "project_id": "pynott",
                    }
                ]
            },
            now=t2,
        )
    )
    cid2 = res2["outcomes"][0]["id"]

    # Relates cap2 supersedes cap1 at T2
    ok(
        server.call_tool(
            "nmemory_relation_add",
            {
                "kind": "supersedes",
                "from": cid2,
                "to": cid1,
            },
            now=t2,
        )
    )

    # 1. Query in the present (without known_at, at T3): cap2 returns, cap1 does not
    res_present = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_known"], "project_id": "pynott"},
            now=t3,
        )
    )
    assert res_present["outcome"] == "grounded", res_present
    returned_ids = [r["id"] for r in res_present["results"]]
    assert cid2 in returned_ids
    assert cid1 not in returned_ids

    # 2. Query in the past with known_at="2026-01-20T00:00:00Z" (between T1 and T2)
    # cap1 must return because at T1.5 it was not superseded; cap2 must not return
    res_past = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["alvo_known"],
                "project_id": "pynott",
                "known_at": "2026-01-20T00:00:00Z",
            },
            now=t3,
        )
    )
    assert res_past["outcome"] == "grounded", res_past
    returned_past_ids = [r["id"] for r in res_past["results"]]
    assert cid1 in returned_past_ids
    assert cid2 not in returned_past_ids

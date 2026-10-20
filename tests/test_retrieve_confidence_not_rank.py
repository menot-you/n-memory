"""The writer's confidence does not reorder recall (relevant-capsule-outranked).

Audit 2026-10-16, mode `relevant-capsule-outranked`: in a one-lane recall the
RRF score of term rank 1 is only 1.6% above rank 2, while confidence spans
0.6 (the ingest default, used when the writer omits it) to 1.0. An older
capsule written with confidence 0.95 and a weaker term match therefore
outranked the newer owner decision that matched the query best."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store

OLD = datetime.fromisoformat("2026-09-20T10:00:00+00:00")
NEW = datetime.fromisoformat("2026-09-21T18:00:00+00:00")
ASK = datetime.fromisoformat("2026-09-21T18:20:00+00:00")


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def ingest(server: MemoryServer, item: dict[str, Any], now: datetime) -> str:
    base = {"source": "test:rank", "anchor": "rank.py:1", "project_id": "pynott"}
    res = ok(
        server.call_tool("nmemory_capsule_ingest", {"items": [base | item]}, now=now)
    )
    cid: str = res["outcomes"][0]["id"]
    return cid


def store_with_outranked_decision() -> tuple[MemoryServer, str, str]:
    """An older continuation at confidence 0.95 and a newer owner decision at the default."""
    server = MemoryServer(store=Store.open(fresh_store_path("conf")))
    old = ingest(
        server,
        {
            "content": "Continue the clover goal and run the final review battery.",
            "kind": "task",
            "confidence": 0.95,
            "authority_class": "user-stated",
        },
        OLD,
    )
    new = ingest(
        server,
        {
            "content": "Owner correction: clover closure cancels the mutation battery.",
            "kind": "decision",
            "authority_class": "user-stated",
        },
        NEW,
    )
    return server, old, new


def test_retrieve_ranks_the_better_match_above_a_higher_confidence() -> None:
    server, old, new = store_with_outranked_decision()
    payload = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["clover", "mutation", "battery"],
                "lane": "term",
                "project_id": "pynott",
            },
            now=ASK,
        )
    )
    ids = [r["id"] for r in payload["results"]]
    assert ids == [new, old], ids


def test_context_ranks_the_better_match_above_a_higher_confidence() -> None:
    server, old, new = store_with_outranked_decision()
    payload = ok(
        server.call_tool(
            "nmemory_context_show",
            {
                "query": "clover mutation battery",
                "sources": ["memory"],
                "project_id": "pynott",
            },
            now=ASK,
        )
    )
    ids = [r["id"] for r in payload["results"]]
    assert ids == [new, old], ids

"""Tests for time_window fence in nmemory_capsule_retrieve (U0b)."""

from __future__ import annotations

import pytest
from mcp_helpers import fresh_server, ok
from storefile import fresh_store_path, put_embedding

from nmemory.embedding import EmbeddingClient
from nmemory.server import MemoryServer
from nmemory.space import DIMENSION
from nmemory.store import Store


@pytest.mark.parametrize("lane", ["term", "vector", "fused"])
def test_time_window_uses_capture_time_without_declared_event_time(lane: str) -> None:
    """Use capture time only when no event time is declared."""
    store = Store.open(fresh_store_path("tw"))
    vector = [1.0] + [0.0] * (DIMENSION - 1)
    client = EmbeddingClient(custom_hook=lambda text: list(vector))
    server = MemoryServer(store=store, embeddings=client)
    inside = "2026-03-15T12:00:00Z"
    outside = "2025-06-01T00:00:00Z"
    captures = [
        {"valid_from": inside},
        {"valid_from": outside},
        {"valid_from": outside, "event_at": inside},
        {"valid_from": inside, "event_at": outside},
    ]
    ids = []
    for index, times in enumerate(captures):
        result = ok(
            server.call_tool(
                "nmemory_capsule_ingest",
                {
                    "items": [
                        {
                            "content": f"Capture {index} windowtarget.",
                            "kind": "fact",
                            "source": "test:tw",
                            "anchor": f"tw.py:{index + 1}",
                            "project_id": "pynott",
                            **times,
                        }
                    ]
                },
            )
        )
        row = result["outcomes"][0]
        assert row["status"] == "captured", row
        cid = row["id"]
        ids.append(cid)
        put_embedding(store.db, cid, vector, client.space_fingerprint.as_str())

    request = {"terms": ["windowtarget"], "project_id": "pynott", "lane": lane}
    res_all = ok(server.call_tool("nmemory_capsule_retrieve", request))
    assert res_all["outcome"] == "grounded", res_all
    assert {row["id"] for row in res_all["results"]} == set(ids)

    res_tw = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {**request, "time_window": {"from": inside, "to": inside}},
        )
    )
    assert res_tw["outcome"] == "grounded", res_tw
    assert {row["id"] for row in res_tw["results"]} == {ids[0], ids[2]}
    assert res_tw["excluded"] == {"outside_time_window": 2}

    res_none = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                **request,
                "time_window": {
                    "from": "2024-01-01T00:00:00Z",
                    "to": "2024-12-31T23:59:59Z",
                },
            },
        )
    )
    assert res_none["outcome"] == "missing_evidence", res_none
    assert res_none["excluded"] == {"outside_time_window": 4}


def test_time_window_empty_bounds_refused() -> None:
    """Empty time_window (without 'from' and without 'to') must be rejected."""
    server = fresh_server()
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {
            "terms": ["qualquer"],
            "project_id": "pynott",
            "time_window": {},
        },
    )
    assert ("error" in res) or (res.get("result", {}).get("isError") is True), res

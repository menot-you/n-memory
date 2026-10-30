"""Tests for weight_blend in nmemory_capsule_retrieve (U0h, U0i)."""

from __future__ import annotations

from datetime import UTC, datetime

from mcp_helpers import fresh_server, ok

from nmemory.capsule import format_rfc3339


def test_weight_blend_re_ranks_by_feedback_weight() -> None:
    """weight_blend re-orders results favoring capsules with positive feedback."""
    server = fresh_server()
    now = datetime.now(UTC)

    # Ingest 2 capsules with same common term
    res1 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Capsula A alvo_blend texto adicional de preenchimento.",
                        "kind": "fact",
                        "source": "test:a",
                        "anchor": "a.py:1",
                        "project_id": "pynott",
                    }
                ]
            },
            now=now,
        )
    )
    cid_a = res1["outcomes"][0]["id"]

    res2 = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Capsula B alvo_blend texto adicional de preenchimento.",
                        "kind": "fact",
                        "source": "test:b",
                        "anchor": "b.py:1",
                        "project_id": "pynott",
                    }
                ]
            },
            now=now,
        )
    )
    cid_b = res2["outcomes"][0]["id"]

    # Assign high feedback weight to B and low to A in store. No product code
    # writes feedback_weights (retrieve only reads it), so the rows are seeded
    # here: the weights the retired EMA writer produced from a first score of 1.0
    # for B and 0.1 for A (0.7 * 1.0 + 0.3 * score).
    with server.store.db.writing() as conn, conn.cursor() as cur:
        for cid, weight in (
            (cid_b, 0.7 * 1.0 + 0.3 * 1.0),
            (cid_a, 0.7 * 1.0 + 0.3 * 0.1),
        ):
            cur.execute(
                "INSERT INTO feedback_weights (capsule_id, weight, at) VALUES (?, ?, ?)",
                (cid, weight, format_rfc3339(now)),
            )

    # 1. Without blend (weight_blend=0.0 or None) -> base order (seq A before B)
    res_base = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_blend"], "project_id": "pynott"},
            now=now,
        )
    )
    assert res_base["outcome"] == "grounded", res_base
    ids_base = [r["id"] for r in res_base["results"]]
    assert ids_base == [cid_a, cid_b]

    # 2. With weight_blend=1.0 -> B (weight=1.0) moves ahead of A (weight=0.1)
    res_blended = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_blend"], "project_id": "pynott", "weight_blend": 1.0},
            now=now,
        )
    )
    assert res_blended["outcome"] == "grounded", res_blended
    ids_blended = [r["id"] for r in res_blended["results"]]
    assert ids_blended == [cid_b, cid_a]

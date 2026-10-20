"""Total branch coverage and edge case tests for the retrieve patch."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from storefile import fresh_store_path, sample_capsule_id

from nmemory.capture import CaptureContext
from nmemory.ingest import IngestDefaults, IngestRequest, effective_defaults, ingest
from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = fresh_store_path("pedge")
    # Keyless servers refuse nmemory_capsule_purge, and the gate env has no
    # NOTT_NMEMORY_HMAC_KEY (see tests/test_forget.py for the measurement).
    return MemoryServer(store=Store.open(url), hmac_env_key=b"k" * 32)


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def err(res: dict[str, Any]) -> dict[str, Any]:
    assert ("error" in res) or (
        "result" in res and res["result"].get("isError") is True
    ), res
    return res


def test_validation_errors_for_retrieve_parameters() -> None:
    """Covers all typed refusals of invalid parameters in server.py."""
    server = fresh_server()

    # 1. weight_blend out of 0..1
    r_wb1 = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "weight_blend": -0.5}
    )
    assert err(r_wb1)["error"]["code"] == -32602
    r_wb2 = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "weight_blend": 1.5}
    )
    assert err(r_wb2)["error"]["code"] == -32602

    # 3. invalid known_at
    r_ka = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "known_at": "not-a-date"}
    )
    assert err(r_ka)["error"]["code"] == -32602

    # 4. unresolvable effort_id
    r_eff1 = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["a"], "effort_id": sample_capsule_id(99999)},
    )
    assert err(r_eff1)["error"]["code"] == -32602

    # 5. unresolvable topic_id
    r_top1 = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["a"], "topic_id": sample_capsule_id(99999)},
    )
    assert err(r_top1)["error"]["code"] == -32602


def test_effort_id_and_topic_id_precondition_failures() -> None:
    """Covers effort_id preconditions (tombstoned, not epic, 0 members) and topic_id (tombstoned)."""
    server = fresh_server()

    # Ingest fact that is NOT epic
    r_fact = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "kind": "task",
                        "content": "fato comum alvo_pedge",
                        "source": "t",
                        "anchor": "a:1",
                    }
                ]
            },
        )
    )
    fact_id = r_fact["outcomes"][0]["id"]

    # effort_id pointing to fact (not epic) -> error
    r_eff_not_epic = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "effort_id": fact_id}
    )
    assert err(r_eff_not_epic)["error"]["code"] == -32602
    # The EXACT message, not just the prefix: the product has two sub-messages in
    # the same branch --- `got <kind>` and `got none` --- and only equality separates them.
    assert err(r_eff_not_epic)["error"]["message"] == (
        "effort_id must name a capsule classified kind=epic (got task)"
    )

    # The OTHER sub-message: a capsule WITHOUT any classification. Since the
    # `kind` door closed, only internal writers produce it, so it is seeded via
    # `ingest()`. Without this case, `got none` would lack a test.
    sem_classe = ingest(
        server.store,
        IngestRequest(
            content="capsula sem classe alvo_pedge", source="t", anchor="a:9"
        ),
        IngestDefaults(
            project_id=effective_defaults(
                server.project_id, server.client_name, None
            ).project_id
        ),
        datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
        # Explicit `context=`: without it `append` falls back to `CaptureContext.detect()`
        # and records the machine's REAL host and cwd. It is the rule that the other two
        # seeds in this campaign already follow.
        context=CaptureContext(
            harness="pytest",
            model="none",
            cwd="/tmp",
            session_id="sess-pedge",
            host="t",
            owner="t",
        ),
    ).id
    r_eff_sem_classe = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "effort_id": sem_classe}
    )
    assert err(r_eff_sem_classe)["error"]["message"] == (
        "effort_id must name a capsule classified kind=epic (got none)"
    )

    # Ingest epic WITHOUT part_of members
    r_epic = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "epic sem membros alvo_pedge",
                        "source": "t",
                        "anchor": "a:1",
                        "kind": "epic",
                    }
                ]
            },
        )
    )
    epic_id = r_epic["outcomes"][0]["id"]

    # effort_id without members -> error
    r_eff_no_m = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "effort_id": epic_id}
    )
    assert err(r_eff_no_m)["error"]["code"] == -32602
    assert "has no part_of members" in err(r_eff_no_m)["error"]["message"]

    # Ingest topic and purge (tombstone)
    r_top = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "topic para purgar alvo_pedge",
                        "kind": "task",
                        "source": "t",
                        "anchor": "a:1",
                    }
                ]
            },
        )
    )
    top_id = r_top["outcomes"][0]["id"]
    ok(
        server.call_tool(
            "nmemory_capsule_purge", {"id": top_id, "mode": "purged", "reason": "teste"}
        )
    )

    # tombstoned topic_id -> error
    r_top_tomb = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "topic_id": top_id}
    )
    assert err(r_top_tomb)["error"]["code"] == -32602
    assert "is tombstoned" in err(r_top_tomb)["error"]["message"]

    # tombstoned effort_id -> error
    ok(
        server.call_tool(
            "nmemory_capsule_purge",
            {"id": epic_id, "mode": "purged", "reason": "teste"},
        )
    )
    r_eff_tomb = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "effort_id": epic_id}
    )
    assert err(r_eff_tomb)["error"]["code"] == -32602
    assert "is tombstoned" in err(r_eff_tomb)["error"]["message"]


def test_lane_routing_and_vector_k() -> None:
    """Covers lane selection (term, vector, fused) and vector_k."""
    server = fresh_server()
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "conteudo para testar lanes alvo_lane",
                        "kind": "task",
                        "source": "t",
                        "anchor": "a:1",
                        "project_id": "pynott",
                    }
                ]
            },
        )
    )

    # 1. lane='term'
    r_term = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_lane"], "project_id": "pynott", "lane": "term"},
        )
    )
    assert r_term["outcome"] == "grounded"

    # 2. lane='fused'
    r_fused = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["alvo_lane"],
                "project_id": "pynott",
                "lane": "fused",
                "vector_k": 5,
            },
        )
    )
    assert r_fused["outcome"] == "grounded"

    # 3. lane='vector' without configured embedding -> abstain or fallback
    r_vec = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_lane"], "project_id": "pynott", "lane": "vector"},
        )
    )
    assert r_vec["outcome"] in ("grounded", "abstain")

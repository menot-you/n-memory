"""Targeted tests for 100% patch coverage in retrieve.py and server.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from storefile import fresh_store_path, sample_capsule_id

from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = fresh_store_path("p100")
    return MemoryServer(store=Store.open(url))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def is_error_response(res: dict[str, Any]) -> bool:
    """Error on ANY channel — deliberately coarse, and therefore insufficient.

    Serves the branches in this file, which only ask "did it refuse?". The
    EXACT assertion — in-band TYPE failure versus VALUE failure in `-32602` —
    lives in `test_retrieve_trust_boundary.py`, because flattening both channels
    into one predicate loses the distinction documented by nMEMORY itself (q88).
    """
    return "error" in res or res.get("result", {}).get("isError") is True


def test_server_error_branches_complete() -> None:
    """Tests all error branches of the nmemory_capsule_retrieve handler."""
    server = fresh_server()

    # 1. invalid / valid min_confidence
    res_mc = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "min_confidence": 0.8}
    )
    assert "result" in res_mc

    # 2. time_window with invalid format (not dict)
    res_tw_not_dict = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "time_window": "invalid"}
    )
    assert is_error_response(res_tw_not_dict)

    # 3. non-existent effort_id (CapsuleNotFound)
    res_eff_nf = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["a"], "effort_id": sample_capsule_id(99999)},
    )
    assert is_error_response(res_eff_nf)

    # 4. non-existent topic_id (CapsuleNotFound)
    res_top_nf = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["a"], "topic_id": sample_capsule_id(99999)},
    )
    assert is_error_response(res_top_nf)

    # 5. session_id not string
    res_sess_not_str = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "session_id": 12345}
    )
    assert is_error_response(res_sess_not_str)

    # 6. invalid known_at
    res_ka_err = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["a"], "known_at": "invalid_date_format"}
    )
    assert is_error_response(res_ka_err)


def test_time_window_open_bounds() -> None:
    """Tests time_window with only 'from' or only 'to'."""
    server = fresh_server()
    now = datetime.now(UTC)
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "evento 2026 alvo_open_tw",
                        "kind": "task",
                        "source": "s",
                        "anchor": "a:1",
                        "event_at": "2026-05-01T00:00:00Z",
                    }
                ]
            },
            now=now,
        )
    )

    # Only 'from'
    r_from = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["alvo_open_tw"],
                "time_window": {"from": "2026-01-01T00:00:00Z"},
            },
            now=now,
        )
    )
    assert len(r_from["results"]) == 1

    # Only 'to'
    r_to = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_open_tw"], "time_window": {"to": "2026-12-31T23:59:59Z"}},
            now=now,
        )
    )
    assert len(r_to["results"]) == 1

    # 'to' in the past (outside)
    r_past = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_open_tw"], "time_window": {"to": "2025-01-01T00:00:00Z"}},
            now=now,
        )
    )
    assert r_past["outcome"] == "missing_evidence"


def test_grounded_results_with_effort_and_topic_roles() -> None:
    """Ensures construction of grounded results with roles is executed."""
    server = fresh_server()
    now = datetime.now(UTC)

    # Epic ingestion
    r_epic = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "epic completo alvo_grounded_roles",
                        "source": "s",
                        "anchor": "a:1",
                        "kind": "epic",
                    }
                ]
            },
            now=now,
        )
    )
    epic_id = r_epic["outcomes"][0]["id"]

    # Ingestion of member task of the epic
    r_task = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "task membro alvo_grounded_roles",
                        "source": "s",
                        "anchor": "a:1",
                        "kind": "task",
                    }
                ]
            },
            now=now,
        )
    )
    task_id = r_task["outcomes"][0]["id"]
    ok(
        server.call_tool(
            "nmemory_relation_add",
            {"kind": "part_of", "from": task_id, "to": epic_id},
            now=now,
        )
    )

    # Topic doc ingestion
    r_doc = ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "doc topico alvo_grounded_roles",
                        "source": "s",
                        "anchor": "a:1",
                        "kind": "doc",
                    }
                ]
            },
            now=now,
        )
    )
    doc_id = r_doc["outcomes"][0]["id"]
    ok(
        server.call_tool(
            "nmemory_relation_add",
            {"kind": "about", "from": task_id, "to": doc_id},
            now=now,
        )
    )

    # Retrieve with effort_id
    r_eff = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_grounded_roles"], "effort_id": epic_id},
            now=now,
        )
    )
    assert r_eff["outcome"] == "grounded"
    assert "effort" in r_eff
    for item in r_eff["results"]:
        assert item["effort_role"] in ("epic", "member")

    # Retrieve with topic_id
    r_top = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_grounded_roles"], "topic_id": doc_id},
            now=now,
        )
    )
    assert r_top["outcome"] == "grounded"
    assert "topic" in r_top
    for item in r_top["results"]:
        assert item["topic_role"] in ("topic", "member")

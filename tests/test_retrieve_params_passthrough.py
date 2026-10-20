"""Pass-through tests and real application of nmemory_capsule_retrieve parameters."""

from __future__ import annotations

import json
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = fresh_store_path("rthr")
    return MemoryServer(store=Store.open(url))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload


def test_token_budget_passthrough_allows_larger_result_set() -> None:
    """Initial RED: token_budget=100_000 sent via MCP must be honored by engine.

    We ingest 10 large capsules (each ~800 chars), totaling ~8000 chars
    (>2000 tokens). With fixed default of 1500 tokens, only ~5 capsules fit.
    With token_budget=100_000 declared by caller, all 10 must return.
    With token_budget=0, zero capsules must return.
    """
    server = fresh_server()
    items = []
    for i in range(10):
        body = f"Item {i} alvo_passthrough " + (
            "palavra_longa_para_gastar_tokens " * 30
        )
        items.append(
            {
                "content": body,
                "kind": "fact",
                "source": f"test:budget:{i}",
                "anchor": f"file_{i}.py:1",
                "project_id": "pynott",
            }
        )
    ok(server.call_tool("nmemory_capsule_ingest", {"items": items}))

    # Call with large token_budget
    res = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["alvo_passthrough"],
                "project_id": "pynott",
                "limit": 10,
                "token_budget": 100_000,
            },
        )
    )
    assert res["outcome"] == "grounded", res
    assert len(res["results"]) == 10, (
        f"Esperado 10 resultados, recebido {len(res['results'])}"
    )

    # Call with token_budget=0
    res_zero = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["alvo_passthrough"],
                "project_id": "pynott",
                "limit": 10,
                "token_budget": 0,
            },
        )
    )
    assert res_zero["outcome"] in ("grounded", "missing_evidence"), res_zero
    assert len(res_zero["results"]) == 0, (
        f"Esperado 0 resultados, recebido {len(res_zero['results'])}"
    )


def test_include_staged_passthrough() -> None:
    """Staged capsule (proposal) NEVER serves recall as grounded (Lock 10, b2).

    The fence is `retrieve.py` "Staged fence": `review_state in ("proposed",
    "rejected") and not params.staged` counts `exclusions["staged"]` and skips. The
    honest outcome for a query that MATCHES a staged capsule is
    `missing_evidence` WITH exclusion counted — not `abstain` (nothing matched,
    false) and never `grounded` (proposal became ground truth, the worst outcome).
    This test is the fence falsifier: neutralizing it in `retrieve.py` must
    turn red HERE, for the right reason (outcome grounded / staged not counted).
    """
    server = fresh_server()
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Staged proposal that must not appear by default.",
                        "kind": "fact",
                        "source": "test:staged",
                        "anchor": "staged.py:1",
                        "project_id": "pynott",
                        "staged": True,
                    }
                ]
            },
        )
    )

    # 1. Default (include_staged absent / False) -> missing_evidence, staged counted,
    #    and NO results: the proposal matched but cannot provide grounding.
    res_default = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["staged"], "project_id": "pynott"},
        )
    )
    assert res_default["outcome"] == "missing_evidence", res_default
    assert res_default["excluded"].get("staged") == 1, res_default
    assert len(res_default.get("results", [])) == 0

    # 2. With include_staged=True -> grounded, result returned
    res_staged = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["staged"], "project_id": "pynott", "include_staged": True},
        )
    )
    assert res_staged["outcome"] == "grounded", res_staged
    assert len(res_staged["results"]) == 1

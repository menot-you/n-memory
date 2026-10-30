"""Tests for session_id fence in nmemory_capsule_retrieve (U0d)."""

from __future__ import annotations

from mcp_helpers import fresh_server, ok


def test_session_id_fence_filters_by_capsule_session() -> None:
    """session_id filters capsules linked to the specified session:

    We ingest 2 capsules with common term 'alvo_sessao':
    1. cap1 linked to session 'sess-1'
    2. cap2 linked to session 'sess-2'
    """
    server = fresh_server()
    # Opens sess-1 and ingests cap1
    ok(server.call_tool("nmemory_session_start", {}))
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Content of session one alvo_sessao",
                        "kind": "fact",
                        "source": "test:sess1",
                        "anchor": "sess1.py:1",
                        "project_id": "pynott",
                        "session_id": "sess-1",
                    }
                ]
            },
        )
    )

    # Opens sess-2 and ingests cap2
    ok(server.call_tool("nmemory_session_start", {}))
    ok(
        server.call_tool(
            "nmemory_capsule_ingest",
            {
                "items": [
                    {
                        "content": "Content of session two alvo_sessao",
                        "kind": "fact",
                        "source": "test:sess2",
                        "anchor": "sess2.py:1",
                        "project_id": "pynott",
                        "session_id": "sess-2",
                    }
                ]
            },
        )
    )

    # 1. Without session_id -> both return
    res_all = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {"terms": ["alvo_sessao"], "project_id": "pynott"},
        )
    )
    assert res_all["outcome"] == "grounded", res_all
    assert len(res_all["results"]) == 2

    # 2. With session_id="sess-1" -> only cap1 returns
    res_sess1 = ok(
        server.call_tool(
            "nmemory_capsule_retrieve",
            {
                "terms": ["alvo_sessao"],
                "project_id": "pynott",
                "session_id": "sess-1",
            },
        )
    )
    assert res_sess1["outcome"] == "grounded", res_sess1
    assert len(res_sess1["results"]) == 1
    assert "session one" in res_sess1["results"][0]["headline"]


def test_session_id_whitespace_only_refused() -> None:
    """session_id with only whitespace must be rejected with error -32602."""
    server = fresh_server()
    res = server.call_tool(
        "nmemory_capsule_retrieve",
        {
            "terms": ["qualquer"],
            "project_id": "pynott",
            "session_id": "   ",
        },
    )
    assert ("error" in res) or (res.get("result", {}).get("isError") is True), res

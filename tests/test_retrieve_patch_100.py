"""Targeted tests for 100% patch coverage in retrieve.py and server.py."""

from __future__ import annotations

from datetime import UTC, datetime

from mcp_helpers import fresh_server, ok


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

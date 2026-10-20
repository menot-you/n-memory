"""`nmemory_digest_show` cannot open a connection per capsule.

Measured by owner on 2026-08-28 against real store (N=2516): `postgres.connection`
opens a NEW connection per method (`postgres.py`), and `_tool_nmemory_digest_show` called
`get_tier`/`get_usage`/`get_pin`/`get_classification`/`get_handle` once PER
capsule — ~8N+22 = 20,150 connections, 114 s in `connect()` alone.

The falsifier here is not wall-clock time: on a shared box that is
flaky. It is connection COUNT, which is deterministic. If digest cost still
scales with population, doubling the store doubles the count; if collapsed into aggregates,
the count is the SAME in both sizes.
"""

from __future__ import annotations

from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store

SMALL = 40
LARGE = 80


def _lane_database_url() -> str:
    """A single owner creates the run database and records the name for cleanup."""
    return fresh_store_path("digestconn")


def _fresh_server() -> MemoryServer:
    lane_url = _lane_database_url()
    return MemoryServer(store=Store.open(lane_url))


def _seed(server: MemoryServer, count: int) -> None:
    items = [
        {
            "content": f"Capsula numero {i} da medicao de conexoes do digest.",
            "source": "session:test-digest-connections",
            "anchor": f"PLAN.md:{i}",
            "confidence": 0.9,
            "project_id": "nmemory",
        }
        for i in range(1, count + 1)
    ]
    res = server.call_tool("nmemory_capsule_ingest", {"items": items})
    assert "result" in res and res["result"]["isError"] is False, res


def _digest_connections(server: MemoryServer) -> int:
    """Counts the `connection()` calls that ONE nmemory_digest_show invocation makes."""
    postgres = server.store.db
    original = postgres.connection
    opened = 0

    def counting() -> Any:
        nonlocal opened
        opened += 1
        return original()

    postgres.connection = counting  # type: ignore[method-assign]
    try:
        res = server.call_tool("nmemory_digest_show", {})
    finally:
        postgres.connection = original  # type: ignore[method-assign]
    assert "result" in res and res["result"]["isError"] is False, res
    return opened


def test_digest_cost_does_not_scale_with_the_store() -> None:
    small_server = _fresh_server()
    _seed(small_server, SMALL)
    small = _digest_connections(small_server)

    large_server = _fresh_server()
    _seed(large_server, LARGE)
    large = _digest_connections(large_server)

    assert large == small, (
        f"the digest opened {small} connections with {SMALL} capsules and {large} with "
        f"{LARGE}: the cost still scales with the population, so a real store "
        "pays one connection per capsule"
    )
    assert small <= 20, (
        f"the digest opened {small} connections with {SMALL} capsules — the ceiling is the "
        "number of aggregated SECTIONS it reads, never the population"
    )

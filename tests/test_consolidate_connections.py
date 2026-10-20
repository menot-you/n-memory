"""`nmemory_capsule_consolidate` does not pay connections per capsule.

DEBT 9 for bootstrap proved the pattern; the manual consolidator had the SAME N+1
via another path: `_tool_nmemory_capsule_consolidate` queried usage/is_superseded/
tier/pin per capsule (4 N+1 queries). Measured by the reviewer of #434 on the live store:
~4.2 s at 11.7k capsules, growing with read_traffic.

The falsifier is NOT wall-clock time — it is the connection COUNT, equal between
two store sizes (the idiom of test_bootstrap_connections.py, this time for the
consolidator).
"""

from __future__ import annotations

from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store

SMALL = 20
LARGE = 60


def _fresh_server() -> MemoryServer:
    url = fresh_store_path("consolidateconn")
    return MemoryServer(store=Store.open(url))


def _seed(server: MemoryServer, count: int) -> None:
    items = [
        {
            "content": f"Capsula numero {i} da medicao de conexoes do consolidador.",
            "kind": "fact",
            "source": "session:consolidate-conn",
            "anchor": f"CONS:{i}",
            "confidence": 0.9,
        }
        for i in range(1, count + 1)
    ]
    res = server.call_tool("nmemory_capsule_ingest", {"items": items})
    assert "result" in res and res["result"]["isError"] is False, res


def _consolidate_connections(server: MemoryServer) -> int:
    postgres = server.store.db
    original = postgres.connection
    opened = 0

    def counting() -> Any:
        nonlocal opened
        opened += 1
        return original()

    postgres.connection = counting  # type: ignore[method-assign]
    try:
        res = server.call_tool("nmemory_capsule_consolidate", {"apply_tiers": False})
    finally:
        postgres.connection = original  # type: ignore[method-assign]
    assert "result" in res and res["result"]["isError"] is False, res
    return opened


def test_consolidate_cost_does_not_scale_with_the_store() -> None:
    # The ORDER is load-bearing: reset_disposable_database of _fresh_server drops
    # the previous store, so the small one is measured in full BEFORE the large one is born.
    small_server = _fresh_server()
    _seed(small_server, SMALL)
    small = _consolidate_connections(small_server)

    large_server = _fresh_server()
    _seed(large_server, LARGE)
    large = _consolidate_connections(large_server)

    assert large == small, (
        f"uma leitura em batch paga constante: small={small}, large={large}"
    )

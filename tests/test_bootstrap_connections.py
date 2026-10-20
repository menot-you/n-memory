"""DEBT 9. `nmemory_project_bootstrap` cannot open a connection per capsule.

The PLAN of the nmemory-escrita campaign forbids N+1 in ALL CAPS for this unit,
and the prohibition was verified BY READING — nobody had put it behind a
judge. U6 built exactly this judge for its fifteen literals
(`test_digest_connections.py`), measured by the owner on 2026-08-28 against the
real store: `postgres.connection` opens a NEW connection per method, and a handler querying
tier/usage/handle once per capsule paid ~8N connections — 114 s in
`connect()` alone with N=2516.

The falsifier is NOT wall-clock time: on a shared box that is flaky. It is
connection COUNT, deterministic. If cost still scales with population,
doubling the store doubles the count; if collapsed into aggregates, count is the SAME
in both sizes.

TWO paths, because there are two: `nmemory_project_bootstrap {}` and `nmemory_project_bootstrap
{terms: [...]}`, which adds an FTS scan of the corpus. The second is what
line 7 of DEBT claims costs a second full scan — this count tells whether it
also costs a connection per capsule, which is another and worse thing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store

SMALL = 40
LARGE = 80

# The ceiling is generous on purpose: the judge that matters is EQUALITY between the two
# sizes. A tight ceiling converts an honest refactor into red and
# teaches touching the number instead of the cost.
CEILING = 30


def _fresh_server() -> MemoryServer:
    url = fresh_store_path("bootstrapconn")
    return MemoryServer(store=Store.open(url))


def _seed(server: MemoryServer, count: int) -> None:
    kinds = ("constraint", "decision", "failure_pattern", "task")
    items = [
        {
            "content": f"Capsula numero {i} da medicao de conexoes do bootstrap.",
            "kind": kinds[i % len(kinds)],
            "source": "session:test-bootstrap-connections",
            "anchor": f"PLAN.md:{i}",
            "confidence": 0.9,
            "project_id": "nott",
        }
        for i in range(1, count + 1)
    ]
    res = server.call_tool("nmemory_capsule_ingest", {"items": items})
    assert "result" in res and res["result"]["isError"] is False, res


def _bootstrap_connections(server: MemoryServer, args: dict[str, Any]) -> int:
    postgres = server.store.db
    original = postgres.connection
    opened = 0

    def counting() -> Any:
        nonlocal opened
        opened += 1
        return original()

    postgres.connection = counting  # type: ignore[method-assign]
    try:
        res = server.call_tool("nmemory_project_bootstrap", args)
    finally:
        postgres.connection = original  # type: ignore[method-assign]
    assert "result" in res and res["result"]["isError"] is False, res
    return opened


SHAPES = (
    ("bare", {"project_prefix": "nott"}),
    ("with terms", {"project_prefix": "nott", "terms": ["capsula"]}),
)


def test_bootstrap_cost_does_not_scale_with_the_store() -> None:
    # The ORDER is load-bearing and the first version got it wrong: `_fresh_server()`
    # ALWAYS resolves the same lane database and calls `reset_disposable_database()`,
    # so creating both servers before measuring causes the second to delete the store
    # of the first — and both counts come out of the SAME size, equal by
    # construction. The small one is measured in full, only then is the large born.
    small_server = _fresh_server()
    _seed(small_server, SMALL)
    smalls = {
        label: _bootstrap_connections(small_server, args) for label, args in SHAPES
    }

    large_server = _fresh_server()
    _seed(large_server, LARGE)
    larges = {
        label: _bootstrap_connections(large_server, args) for label, args in SHAPES
    }

    for label, _ in SHAPES:
        small, large = smalls[label], larges[label]
        assert large == small, (
            f"{label}: bootstrap opened {small} connections with {SMALL} capsules and "
            f"{large} with {LARGE} — the cost scales with the population, so a real "
            "store pays one connection per capsule"
        )
        assert small <= CEILING, (
            f"{label}: {small} connections with {SMALL} capsules — the ceiling is the number "
            "of aggregates the handler reads, never the population"
        )


def test_the_quarantine_and_falsified_fences_cost_no_extra_connection() -> None:
    """The fence in line 2 of DEBT reads relations and sidecars ALREADY in memory.

    `store.is_falsified(cid)` exists and is per-id: using it in the handler would have been
    the obvious way and would have added one connection PER candidate CAPSULE — the
    very N+1 that line 9 forbids, introduced by the fix for another line.
    This count dies if anyone goes back there.

    This is NOT a duplicate of the test above, and an independent review already read it as such.
    The mutation that ONLY this kills: a per-id query triggered only for the
    FENCED SUBSET. The test above seeds no capsules in
    quarantine, so that mutant leaves its count intact. MEASURED
    2026-09-13: with the mutant, the test above passes at 8 connections in both
    sizes and this one turns red, 8 versus 27. Before trimming this, apply that
    mutant — owner law is to measure the mutation the guard answers, never
    to count lines.
    """
    server = _fresh_server()
    _seed(server, SMALL)
    before = _bootstrap_connections(server, {"project_prefix": "nott"})

    ids = [f"cap-{i}" for i in range(1, SMALL // 2)]
    for cid in ids:
        server.store.tiers.set_tier(cid, "quarantined", datetime.now(UTC))

    after = _bootstrap_connections(server, {"project_prefix": "nott"})
    assert after == before, (
        f"{before} connections without quarantined capsules and {after} with "
        f"{len(ids)}: the fence is asking the store once per capsule"
    )

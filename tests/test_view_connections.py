"""`nmemory capsule export` cannot open a connection per capsule.

The sibling of this file, `test_digest_connections.py`, closed the same defect in
`nmemory_digest_show`. Both VIEWS carried their version of it: export asked for
classification, tier, verdict, anchor, and handle one capsule at a time, plus one
handle per tombstone; visual asked for handle and tier. Each question calls
`postgres.connection`.

The falsifier is connection COUNT, never wall-clock time: on a shared box
the second is flaky and the first is deterministic. If cost still
scales with population, doubling the store doubles the count.

The count alone is not enough: a `view_sidecars` returning `{}` would be
constant AND would wipe handle, tier, verdict, and anchor from markdown, because each
field has a default. That is why the second test reads the columns back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from storefile import fresh_store_path

from nmemory.commands.capsule import export_markdown
from nmemory.server import MemoryServer
from nmemory.store import Store

SMALL = 40
LARGE = 80


def _fresh_server() -> MemoryServer:
    lane_url = fresh_store_path("viewconn")
    return MemoryServer(store=Store.open(lane_url), hmac_env_key=b"k" * 32)


def _seed(server: MemoryServer, count: int) -> None:
    items = [
        {
            "content": f"Capsule number {i} of the view connection measurement.",
            "kind": "fact",
            "source": "session:test-view-connections",
            "anchor": f"PLAN.md:{i}",
            "confidence": 0.9,
            "project_id": "nmemory",
        }
        for i in range(1, count + 1)
    ]
    res = server.call_tool("nmemory_capsule_ingest", {"items": items})
    assert "result" in res and res["result"]["isError"] is False, res
    # One tombstone PER SIZE: its handle is the reason `view_sidecars`
    # does not filter `canonical_json IS NOT NULL`, and without it the tombstone
    # population would be zero on both sides and the axis would not be measured.
    victim = server.store.capsules.list()[0].id
    res = server.call_tool(
        "nmemory_capsule_purge",
        {"id": victim, "mode": "redacted", "reason": "semear uma tombstone"},
    )
    assert "result" in res and res["result"]["isError"] is False, res


def _connections(server: MemoryServer) -> int:
    """Counts the `connection()` calls that ONE export makes."""
    postgres = server.store.db
    original = postgres.connection
    opened = 0

    def counting() -> Any:
        nonlocal opened
        opened += 1
        return original()

    postgres.connection = counting  # type: ignore[method-assign]
    try:
        export_markdown(server.store)
    finally:
        postgres.connection = original  # type: ignore[method-assign]
    return opened


def test_export_cost_does_not_scale_with_the_store() -> None:
    small_server = _fresh_server()
    _seed(small_server, SMALL)
    large_server = _fresh_server()
    _seed(large_server, LARGE)

    small = _connections(small_server)
    large = _connections(large_server)
    assert large == small, (
        f"export opened {small} connections with {SMALL} capsules and {large} with "
        f"{LARGE}: the cost still scales with the population, so a real "
        "store pays several connections per capsule"
    )
    assert small <= 20, (
        f"export opened {small} connections with {SMALL} capsules — the ceiling is the "
        "number of aggregated reads, never the population"
    )


def test_export_still_carries_every_sidecar_column() -> None:
    """Handle, tier, verdict, and anchor survive batching.

    Each has a default in the handler, so an empty batch would pass unnoticed
    by the cost test: the four columns would drop from markdown silently.
    """
    server = _fresh_server()
    _seed(server, 3)
    live = server.store.capsules.list()
    subject = live[0].id

    now = datetime.now(UTC)
    server.store.tiers.set_tier(subject, "archived", now)
    server.store.reviews.append_review_event(
        subject, "ratified", "prova das colunas", "test", now
    )
    server.store.anchors.reanchor(subject, "DECISIONS.md:7", "prova das colunas", now)

    md = export_markdown(server.store, no_stamp=True)

    assert f"handle {server.store.capsules.get_handle(subject)}" in md, (
        "the capsule handle vanished from the export"
    )
    assert "tier archived" in md, "the tier is no longer read"
    assert "reanchored to DECISIONS.md:7" in md, "the re-pointed anchor vanished"
    assert "ratified" in md, "the review verdict vanished"
    # The seeded tombstone has its own handle, coming from the SAME query.
    tomb = server.store.capsules.list_tombstones()[0]
    assert f"handle {server.store.capsules.get_handle(tomb.capsule_id)}" in md, (
        "o handle da tombstone sumiu: a consulta voltou a filtrar as vivas"
    )

"""tier_events: a tier move is an event, not an overwrite (U5 nmemory-curator).

`nmemory.tier_events` existed in the schema with zero writers since the port:
`set_tier` upserted the single `tiers` row and the previous tier was destroyed
— `get_tier(as_of=antes)` read the OVERWRITTEN row, so a capsule archived and
restored looked ACTIVE its whole life, and the «undo registado» of LOCK 2
(nmemory-alcance) existed nowhere. Every move now appends an event, history
reads back through the events, and a legacy `tiers` row with no event is
backfilled as `reconstructed = 1` by `migrate()`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from storefile import fresh_store_path

from nmemory.consolidate import Tier
from nmemory.database import Database
from nmemory.server import MemoryServer
from nmemory.store import Store

SESSION = "sess-tier-events"
T0 = datetime(2026, 9, 15, tzinfo=UTC)


def _fresh() -> tuple[Database, Store, MemoryServer]:
    url = fresh_store_path("tier_events")
    pg = Database(url)
    store = Store.open(url)
    return pg, store, MemoryServer(store=store)


def _seed(server: MemoryServer, pg: Database) -> str:
    from nmemory.capture import CaptureContext

    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "tier events observation",
            "kind": "fact",
            "source": "test_tier_events",
            "anchor": "0",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
        },
        context=CaptureContext.detect({"session_id": SESSION, "harness": "pytest/1"}),
    )
    assert not response["result"]["isError"], response
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM capsules ORDER BY seq")
        return str(cur.fetchone()[0])


def _events(pg: Database) -> list[tuple[str, str, int]]:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT tier, at, reconstructed FROM tier_events ORDER BY seq")
        return [(str(r[0]), str(r[1]), int(r[2])) for r in cur.fetchall()]


def test_every_move_appends_and_history_reads_back() -> None:
    pg, store, server = _fresh()
    cid = _seed(server, pg)
    t1 = T0
    t2 = T0 + timedelta(days=1)
    t3 = T0 + timedelta(days=2)

    store.tiers.set_tier(cid, Tier.ARCHIVED, t2)
    assert store.tiers.get_tier(cid) is Tier.ARCHIVED
    assert store.tiers.get_tier(cid, as_of=t1) is Tier.ACTIVE, (
        "before the move the tier is what it was — not what it became"
    )
    assert store.tiers.get_tier(cid, as_of=t2) is Tier.ARCHIVED

    store.tiers.set_tier(cid, Tier.ACTIVE, t3)
    assert store.tiers.get_tier(cid) is Tier.ACTIVE
    assert store.tiers.get_tier(cid, as_of=t2) is Tier.ARCHIVED, (
        "the restore does not rewrite the past"
    )
    assert store.tiers.get_tier(cid, as_of=t1) is Tier.ACTIVE

    events = _events(pg)
    assert [e[0] for e in events] == ["archived", "active"], (
        "two moves, two live events, in order"
    )
    assert all(e[2] == 0 for e in events), "live moves are never reconstructed"

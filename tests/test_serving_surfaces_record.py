"""Every surface that SERVES is counted; only a question is a recall.

U1 of nmemory-curator. The store measured 2026-09-15: `nmemory_capsule_show`,
`nmemory_capsule_list` and `nmemory_digest_show` served capsules every day and left no row in
`nmemory.read_traffic`, so a capsule read by handle was «never recalled»
forever. The census is blind to exactly the surfaces an agent uses on purpose.

Two rules, pinned separately so neither can smuggle the other:

1. COUNTED. `get`, `list` and `digest` leave a row naming themselves, the
   session, and what they served — including reads that found nothing.

2. STILL NOT RECALL. A row from those surfaces must never reach
   `nmemory.usage` nor `digest_sidecars.recall_count`: the ranking counters are
   an ALLOWLIST (`RANKING_SURFACES`), and `archive_deadline` reads them. A
   denylist would absorb each new surface silently and nothing the digest ever
   showed could archive again. `context` stays the one read_traffic surface
   that ranks, alongside `retrieve` in `usage`.
"""

from __future__ import annotations

from datetime import UTC, datetime

from storefile import fresh_store_path, sample_capsule_id

from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.queries.census import write_read_census
from nmemory.queries.sidecars import digest_sidecars
from nmemory.server import MemoryServer
from nmemory.store import Store

SESSION = "sess-serving-surfaces"


def _fresh() -> tuple[Database, MemoryServer]:
    url = fresh_store_path("serving_surfaces")
    pg = Database(url)
    return pg, MemoryServer(store=Store.open(url))


def _context() -> CaptureContext:
    return CaptureContext.detect({"session_id": SESSION, "harness": "pytest/1"})


def _seed(
    server: MemoryServer, kinds: tuple[str, ...] = ("decision", "constraint")
) -> None:
    for index, kind in enumerate(kinds):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Serving surface observation {index}",
                "kind": kind,
                "source": "test_serving_surfaces",
                "anchor": str(index),
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            },
            context=_context(),
        )
        assert not response["result"]["isError"], response


def _capsule_ids(pg: Database) -> list[str]:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM capsules ORDER BY seq")
        return [str(r[0]) for r in cur.fetchall()]


def _traffic(pg: Database) -> list[tuple[str, str, int, str | None]]:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT surface, session_id, served_count, outcome "
            "FROM read_traffic ORDER BY seq"
        )
        return [(str(r[0]), str(r[1]), int(r[2]), r[3]) for r in cur.fetchall()]


def _usage_total(pg: Database) -> int:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(sum(recall_count), 0) FROM usage")
        return int(cur.fetchone()[0])


def test_get_by_handle_is_counted_and_stays_out_of_the_ranking() -> None:
    pg, server = _fresh()
    _seed(server, kinds=("decision",))
    (cid,) = _capsule_ids(pg)

    response = server.call_tool("nmemory_capsule_show", {"id": cid}, context=_context())
    assert not response["result"]["isError"], response

    rows = _traffic(pg)
    assert rows == [("get", SESSION, 1, None)], (
        "a get by handle names itself, the session, and the capsule served"
    )
    assert _usage_total(pg) == 0, "serving by handle is arrival, never recall"


def test_a_get_that_found_nothing_is_still_counted() -> None:
    pg, server = _fresh()
    _seed(server, kinds=("decision",))

    response = server.call_tool(
        "nmemory_capsule_show", {"id": sample_capsule_id(999999)}, context=_context()
    )
    assert not response["result"]["isError"], response

    rows = _traffic(pg)
    assert rows == [("get", SESSION, 0, "not_found")], (
        "the ask is the interesting row, with its outcome"
    )


def test_batch_get_counts_only_what_it_served() -> None:
    pg, server = _fresh()
    _seed(server)
    first, second = _capsule_ids(pg)[:2]

    response = server.call_tool(
        "nmemory_capsule_show",
        {"ids": [first, second, sample_capsule_id(999999)]},
        context=_context(),
    )
    assert not response["result"]["isError"], response

    rows = _traffic(pg)
    assert rows == [("get", SESSION, 2, "batch")], (
        "two found ids are served; the missing ref is not"
    )


def test_list_is_counted() -> None:
    pg, server = _fresh()
    _seed(server)

    response = server.call_tool("nmemory_capsule_list", {}, context=_context())
    assert not response["result"]["isError"], response

    rows = _traffic(pg)
    assert rows == [("list", SESSION, 2, None)]


def test_digest_is_counted() -> None:
    pg, server = _fresh()
    _seed(server)

    response = server.call_tool(
        "nmemory_digest_show", {"headlines": 5}, context=_context()
    )
    assert not response["result"]["isError"], response

    rows = _traffic(pg)
    assert len(rows) == 1
    assert rows[0][0] == "digest"
    assert rows[0][1] == SESSION
    assert rows[0][2] >= 2, "newest carries both capsules; most_recalled none yet"


def test_serving_surfaces_never_become_recall() -> None:
    """The allowlist guard: every new surface must opt OUT of the ranking.

    `archive_deadline` fires on `recall_count == 0`; if `digest_sidecars` ever
    counted a `get`/`list`/`digest` row, nothing the digest itself displayed
    could archive again — the exact silent absorb a denylist causes.
    """
    pg, server = _fresh()
    _seed(server)
    first, second = _capsule_ids(pg)[:2]

    for tool, arguments in (
        ("nmemory_capsule_show", {"id": first}),
        ("nmemory_capsule_show", {"ids": [first, second]}),
        ("nmemory_capsule_list", {}),
        ("nmemory_digest_show", {"headlines": 5}),
    ):
        response = server.call_tool(tool, arguments, context=_context())
        assert not response["result"]["isError"], (tool, response)

    assert _usage_total(pg) == 0
    store = Store.open(pg.path, allow_live_store=True)
    sidecars = digest_sidecars(store.db)
    assert all(sc.recall_count == 0 for sc in sidecars.values()), (
        "get/list/digest rows must not reach the ranking counters"
    )

    # Positive control: the one read_traffic surface that ranks is `context`.
    # `nmemory.usage` stays retrieve-only by design — the ranking surface shows
    # up in `digest_sidecars.recall_count`, which is what `archive_deadline`
    # reads.
    response = server.call_tool(
        "nmemory_context_show",
        {"query": "serving surface observation"},
        context=_context(),
    )
    assert not response["result"]["isError"], response
    assert _usage_total(pg) == 0, "usage stays retrieve-only"
    assert any(sc.recall_count > 0 for sc in digest_sidecars(store.db).values()), (
        "the counter is not broken — the allowlist is the only gate"
    )


def test_the_census_counts_arrival_on_every_surface() -> None:
    """U1 changes the census's role: it counts ARRIVAL, ranking counts asking.

    `write_read_census` kept `NOT IN ('retrieve','bootstrap')` — the #434 law
    for recall. With every serving surface now leaving rows, the census reads
    the union of ALL of them: a capsule the agent read by handle is no longer
    «never recalled». `digest_sidecars` keeps the allowlist (test above).
    """
    pg, server = _fresh()
    _seed(server, kinds=("decision",))
    (cid,) = _capsule_ids(pg)

    response = server.call_tool("nmemory_capsule_show", {"id": cid}, context=_context())
    assert not response["result"]["isError"], response

    store = Store.open(pg.path, allow_live_store=True)
    since = datetime.now(UTC).replace(year=2020)
    census = write_read_census(store.db, since)
    assert census["decision"] == (1, 0), (
        "the get row clears the capsule in the census of arrival"
    )

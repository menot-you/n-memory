"""Every read surface is counted, and counting never turns a reader into a writer.

Measured on the live store 2026-09-13, and the reason this file exists:
`store.record_recall` is called from exactly one place — `retrieve.py` — so
`nmemory.usage`, the table the control metric "% ever recalled" reads, can only
see `nmemory_capsule_retrieve`. `nmemory_context_show` and `nmemory_project_bootstrap` recorded nothing,
and the tool contract makes `nmemory_context_show` mandatory before any other tool in
every session. The metric was blind to the dominant read path, and no amount of
waiting fixes a blind metric.

THREE THINGS PULL AGAINST EACH OTHER HERE, so all three are pinned:

1. Countable. Every surface leaves a row naming itself and the session that
   asked — including reads that returned nothing, which are the most interesting
   rows in the table.

2. Still a reader. The first version of this recorded through
   `store.record_receipt`, which is `@writes` over a native table. Independent
   review measured what that cost: every orientation call took
   `SELECT ... FOR UPDATE` on the `canonical_generation` singleton and recomputed
   the whole-corpus digest, and under a concurrent writer the orientation answer
   was computed and then DISCARDED as `isError`. The dispatcher states the rule
   it broke, verbatim: «A tool that only reads never takes the lock nor pays the digest.»
   `test_counting_never_moves_the_canonical_generation` is that measurement made
   permanent.

3. Not recall. The nmemory-escrita PLAN ratified, verbatim: «Appearing in bootstrap
   does NOT increment `usage`. It is an orientation package, not evidence delivery;
   counting recall for a capsule that the agent only glimpsed inflates
   exactly the U0 census.» Counting orientation must not make it count as
   recall, or the census improves by measuring more of itself.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest
from nott_database import DatabaseError
from stdio_peer import wire
from storefile import fresh_store_path

from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.server import MemoryServer
from nmemory.store import Store

SESSION = "sess-countable-probe"


def _fresh() -> tuple[Database, MemoryServer]:
    url = fresh_store_path("read_surfaces")
    pg = Database(url)
    return pg, MemoryServer(store=Store.open(url))


def _context() -> CaptureContext:
    return CaptureContext.detect({"session_id": SESSION, "harness": "pytest/1"})


def _seed(server: MemoryServer) -> None:
    for index, kind in enumerate(("constraint", "decision", "failure_pattern", "task")):
        response = server.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"Countable read surface observation {index}",
                "kind": kind,
                "source": "test_read_surfaces",
                "anchor": str(index),
                "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
            },
            context=_context(),
        )
        assert not response["result"]["isError"], response


def _traffic(pg: Database) -> list[tuple[str, str, int, str | None]]:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT surface, session_id, served_count, outcome "
            "FROM read_traffic ORDER BY seq"
        )
        return [(str(r[0]), str(r[1]), int(r[2]), r[3]) for r in cur.fetchall()]


def _generation(pg: Database) -> int:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM generation")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def _usage_total(pg: Database) -> int:
    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT COALESCE(sum(recall_count), 0) FROM usage")
        row = cur.fetchone()
        return int(row[0]) if row else 0


def test_every_read_surface_leaves_a_row_naming_itself_and_its_session() -> None:
    pg, server = _fresh()
    _seed(server)
    assert _traffic(pg) == [], "ingest is not a read"

    for tool, arguments in (
        ("nmemory_capsule_retrieve", {"terms": ["countable"]}),
        ("nmemory_context_show", {"query": "countable read surface"}),
        ("nmemory_project_bootstrap", {}),
    ):
        response = server.call_tool(tool, arguments, context=_context())
        assert not response["result"]["isError"], (tool, response)

    rows = _traffic(pg)
    assert [r[0] for r in rows] == ["retrieve", "context", "bootstrap"]
    assert [r[1] for r in rows] == [SESSION] * 3, (
        "every row carries the session that asked, on every surface"
    )
    assert rows[0][3] == "grounded", "retrieve records its outcome, not just its hits"


def test_a_read_that_found_nothing_is_still_counted() -> None:
    """The asymmetry that would have skewed the table by construction.

    `retrieve()` returns early on `abstain` and on `missing_evidence`, long
    before its own receipt — so counting retrieve inside it would have recorded
    only the grounded half, while the orientation surfaces recorded their empty
    reads too. `SELECT surface, count(*) ... GROUP BY 1` is the first query
    anyone runs against this table, and it would have been wrong the first time.
    """
    pg, server = _fresh()
    _seed(server)

    response = server.call_tool(
        "nmemory_capsule_retrieve",
        {"terms": ["zzzznothingmatchesthis"]},
        context=_context(),
    )
    assert not response["result"]["isError"], response

    rows = _traffic(pg)
    assert len(rows) == 1, "a retrieve that matched nothing is still a read"
    assert rows[0][0] == "retrieve"
    assert rows[0][2] == 0, "it served nothing, and says so"
    assert rows[0][3] in {"abstain", "missing_evidence"}


def test_counting_never_moves_the_canonical_generation() -> None:
    """The blocker this design exists to avoid, made permanent.

    Independent review measured the first attempt: `nmemory_context_show` advanced the
    generation 301->302 and `nmemory_project_bootstrap` 302->303, because the receipt went
    through a `@writes` method over a native table. Under a concurrent writer
    holding the singleton, both tools returned `isError` and threw away an
    orientation answer they had already computed — on the one tool the contract
    makes mandatory at the start of every session.

    This test dies if anyone routes read counting back through the canonical
    write boundary.
    """
    pg, server = _fresh()
    _seed(server)

    # ORIENTATION only. `nmemory_capsule_retrieve` is an opt-in tool that has always been
    # a canonical writer — it records a receipt through `@writes` — so including
    # it here would assert something false and hide the thing under test.
    before = _generation(pg)
    for tool, arguments in (
        ("nmemory_context_show", {"query": "countable read surface"}),
        ("nmemory_project_bootstrap", {}),
    ):
        response = server.call_tool(tool, arguments, context=_context())
        assert not response["result"]["isError"], (tool, response)
    assert _generation(pg) == before, (
        "a tool that only reads never takes the lock nor pays the digest "
        "(server.py dispatcher, verbatim); counting must not change that"
    )
    assert len(_traffic(pg)) == 2, "and both were still counted"

    # Not vacuous: a real write still moves it, so the assertion above measures
    # the read path rather than a generation counter that never moves at all.
    response = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "content": "a write that should move the generation",
            "kind": "fact",
            "source": "test_read_surfaces",
            "anchor": "gen",
            "proof_hint": "fixture: a porta de captura exige prova re-obtenível",
        },
        context=_context(),
    )
    assert not response["result"]["isError"], response
    assert _generation(pg) > before


def test_orientation_is_counted_but_never_counts_as_recall() -> None:
    """The ratified decision's falsifier — it dies if blindness is 'fixed' with record_recall."""
    pg, server = _fresh()
    _seed(server)

    before = _usage_total(pg)
    for tool, arguments in (
        ("nmemory_context_show", {"query": "countable read surface"}),
        ("nmemory_project_bootstrap", {}),
    ):
        response = server.call_tool(tool, arguments, context=_context())
        assert not response["result"]["isError"], (tool, response)
    assert _usage_total(pg) == before, (
        "orientation is not evidence delivery: bootstrap and context must leave "
        "usage.recall_count untouched (nmemory-escrita PLAN, U3, ratified)"
    )
    assert len(_traffic(pg)) == 2, "counted, though — that is the whole point"

    response = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["countable"]}, context=_context()
    )
    assert not response["result"]["isError"], response
    assert _usage_total(pg) > before, "retrieve still counts as recall"


def test_concurrent_counting_loses_no_rows() -> None:
    """The seq race, pinned. Found by review, not by the first version of this file.

    `read_traffic` is written WITHOUT the `canonical_generation` lock — that is
    the whole design. So the `MAX(seq) + 1` idiom the canonical tables use safely
    (they are serialised by that lock) would let two concurrent readers pick the
    same seq; one INSERT dies on the primary key and the best-effort `except`
    swallows the loss. Counting would drop rows exactly when traffic is highest,
    which is the one moment it matters. The database allocates the id instead.
    """
    import threading

    pg, server = _fresh()
    _seed(server)
    store = server.store

    workers = 12
    barrier = threading.Barrier(workers)

    def hammer(index: int) -> None:
        barrier.wait()
        store.db.record_read_traffic(
            surface="context",
            session_id=f"sess-{index}",
            terms="concurrent",
            served_ids=["cap-1"],
            project_id="pynott",
            project_prefix=None,
            outcome=None,
            now=datetime.now(UTC),
        )

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = _traffic(pg)
    assert len(rows) == workers, (
        f"every concurrent read must be counted; {workers - len(rows)} rows lost"
    )
    assert len({r[1] for r in rows}) == workers, "and each one is its own session"


def test_a_failed_counting_connection_is_swallowed_not_raised() -> None:
    """The catch that could not catch, pinned.

    `_open` maps every connect failure to `DatabaseError`, which is NOT a
    `sqlite3.Error`. The first version
    of `record_read_traffic` caught only the latter, so a bookkeeping connection
    failure escaped — and in the handler it would have discarded an orientation
    answer the caller already had in hand, which is the exact blocker this whole
    design exists to avoid, one layer down. Review measured it.

    The seam is `record_read_traffic` itself, not the handler: breaking
    `_open` from outside also breaks the real read (the dispatcher's
    boundary is opened through it), which would confound the two failures into
    one and prove nothing about either.
    """
    pg, server = _fresh()
    _seed(server)

    original = pg._open

    def refuse() -> object:
        raise DatabaseError("bookkeeping connection refused")

    server.store.db._open = refuse  # type: ignore[method-assign]
    try:
        # Must return, not raise. A raise here becomes `isError` upstream.
        server.store.db.record_read_traffic(
            surface="context",
            session_id=SESSION,
            terms="counting is best effort",
            served_ids=["cap-1"],
            project_id="pynott",
            project_prefix=None,
            outcome=None,
            now=datetime.now(UTC),
        )
    finally:
        server.store.db._open = original  # type: ignore[method-assign]

    assert _traffic(pg) == [], "nothing was counted — that is the accepted cost"

    # And the counting works again the moment the store does: the swallow is not
    # a permanent no-op hiding a broken writer.
    response = server.call_tool(
        "nmemory_context_show", {"query": "countable read surface"}, context=_context()
    )
    assert not response["result"]["isError"], response
    assert len(_traffic(pg)) == 1


def test_retrieve_counts_under_the_project_it_actually_searched() -> None:
    """The fix round 3 found shipping without a falsifier.

    When the caller sends no `project_id`, the handler resolves one from the
    defaults and the search runs fenced to it. Recording the raw argument would
    file the row against no project at all, so any per-project reading of read
    traffic would silently disagree with the retrieval it describes.
    """
    pg, server = _fresh()
    _seed(server)

    response = server.call_tool(
        "nmemory_capsule_retrieve", {"terms": ["countable"]}, context=_context()
    )
    assert not response["result"]["isError"], response

    with pg.writing() as conn, conn.cursor() as cur:
        cur.execute("SELECT project_id FROM read_traffic WHERE surface = 'retrieve'")
        row = cur.fetchone()
    assert row is not None and row[0], (
        "the row names the project the search was fenced to, not the empty "
        "argument the caller omitted"
    )


def test_the_stdio_answer_is_on_the_wire_before_its_read_traffic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recording a read never delays its answer: the row is written after the frame.

    Written inside the boundary's exit, the row waited for the store's write lock
    (up to the busy timeout) while the caller's answer was still unsent.
    """
    pg, server = _fresh()
    _seed(server)
    server.defer_read_traffic = True
    release = threading.Event()
    real = server.flush_read_traffic

    def held() -> None:
        release.wait(30)
        real()

    monkeypatch.setattr(server, "flush_read_traffic", held)
    frame = {
        "id": 9,
        "method": "tools/call",
        "params": {"name": "nmemory_context_show", "arguments": {"query": "countable"}},
    }
    [answer] = wire(server, [frame])
    assert '"id":9' in answer and len(_traffic(pg)) == 0
    release.set()
    end = time.monotonic() + 30
    while not _traffic(pg) and time.monotonic() < end:
        time.sleep(0.05)
    assert [r[0] for r in _traffic(pg)] == ["context"]

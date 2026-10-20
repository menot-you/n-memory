"""The inbox's `served` fence answers from a table, not from all read traffic.

Measured on a copy of the live store on 2026-10-04: `inbox_candidates` took
7.7 s on every `nmemory_context_show`, because its `served` CTE unpacked all
1.80 M `read_traffic` rows (31.8 M ids, 29.5 M of them from `list`) to learn
which of 17,975 ids had ever been served. `served` keeps that answer per id,
maintained by triggers in the same transaction as the traffic row.

The definition does not change: an id is served while a non-revoked
`read_traffic` row lists it, or `usage` holds it. `ORACLE_SERVED` is the CTE
the inbox ran before, kept here as the definition every case is checked against.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import nott_database
from storefile import fresh_store_path

from nmemory.capture import CaptureContext
from nmemory.database import Database
from nmemory.queries.inbox import inbox_candidates
from nmemory.server import MemoryServer
from nmemory.store import Store

T0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

#: The inbox's `served` CTE before this change: the definition.
ORACLE_SERVED = """
    SELECT DISTINCT trim(sid.value) AS capsule_id
    FROM read_traffic, json_each(read_traffic.served_ids) AS sid
    WHERE revoked_at IS NULL
    UNION SELECT capsule_id FROM usage
"""


def _fresh(count: int) -> tuple[Store, MemoryServer, list[str]]:
    """A writer server that wrote `count` knowledge capsules, and the store a
    reader opens: the self-delivery fence keeps a writer's own capsules out."""
    url = fresh_store_path("inbox_served")
    writer = MemoryServer(store=Store.open(url), project_id="curator")
    ids = []
    for index in range(count):
        response = writer.call_tool(
            "nmemory_capsule_ingest",
            {
                "content": f"served fence observation {index:02d}",
                "source": "test_inbox_served",
                "anchor": f"served fence {index:02d}",
                "proof_hint": "fixture: the capture door requires a re-obtainable proof",
                "kind": "fact",
            },
            context=CaptureContext.detect(
                {"session_id": "sess-writer", "harness": "pytest/1"}
            ),
            now=T0,
        )
        assert not response["result"]["isError"], response
        body = json.loads(response["result"]["content"][0]["text"])
        ids.append(str(body["outcomes"][0]["id"]))
    return Store.open(url), writer, ids


def _inbox(store: Store) -> list[str]:
    """The whole queue: both ends wider than the population."""
    rows = inbox_candidates(
        store.db, (), "curator", subtree=False, oldest=50, newest=50
    )
    return [row.id for row in rows]


def _oracle_inbox(store: Store, ids: list[str]) -> list[str]:
    with store.db.connection() as conn, conn.cursor() as cur:
        served = {str(r[0]) for r in cur.execute(ORACLE_SERVED).fetchall()}
    return [cid for cid in ids if cid not in served]


def _read(db: Database, surface: str, served_ids: list[str]) -> None:
    db.record_read_traffic(
        surface=surface,
        session_id="sess-reader",
        terms="served fence",
        served_ids=served_ids,
        project_id="curator",
        project_prefix=None,
        outcome=None,
        now=T0,
    )


def _witness(db: Database, reads: list[tuple[str, str]], at: datetime) -> None:
    """Transcript-witnessed rows (`origin` not `live`) as the retired backfill
    wrote them: witnessing a revoked row again restores it."""
    with db.writing() as conn:
        for source_ref, capsule_id in reads:
            conn.execute(
                "INSERT INTO read_traffic (surface, session_id, terms, served_ids,"
                " served_count, at, origin, source_ref) VALUES ('digest', 'sess-t',"
                " '', json_array(?), 1, ?, 'transcript:t', ?)"
                " ON CONFLICT (origin, source_ref) WHERE origin <> 'live'"
                " DO UPDATE SET revoked_at = NULL",
                (capsule_id, at.isoformat(), source_ref),
            )


def _revoke(db: Database) -> int:
    """Mark the transcript origin's rows revoked, never deleting them."""
    with db.writing() as conn:
        return conn.execute(
            "UPDATE read_traffic SET revoked_at = ?"
            " WHERE origin = 'transcript:t' AND revoked_at IS NULL",
            (T0.isoformat(),),
        ).rowcount


def _inbox_steps(store: Store) -> tuple[list[str], int]:
    """The inbox and the SQLite VM instructions it took, on the boundary's
    connection: work the database did, not wall time."""
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    with store.db.tool_boundary(), store.db.connection() as handle:
        handle.conn.set_progress_handler(tick, 1)
        try:
            ids = _inbox(store)
        finally:
            handle.conn.set_progress_handler(None, 0)
    return ids, steps


def test_the_inbox_does_not_read_the_traffic_it_already_counted() -> None:
    """2,000 more `list` rows (50,000 ids) leave the inbox's work flat.

    The CTE visited every id of every row on every call: the work grew by
    at least one VM instruction per id the traffic added. The bound is that
    per-id cost, so the case fails for any form that reads the traffic again,
    however fast the machine.
    """
    store, _writer, ids = _fresh(6)
    _read(store.db, "get", [ids[0]])
    _read(store.db, "context", [ids[1], "ctx-0001"])
    expected = [ids[2], ids[3], ids[4], ids[5]]
    small, small_steps = _inbox_steps(store)
    assert small == expected == _oracle_inbox(store, ids)

    listed = [ids[0], ids[1]] + [f"ctx-{n:04d}" for n in range(23)]
    with store.db.tool_boundary():
        for _ in range(2000):
            _read(store.db, "list", listed)
    added = 2000 * len(listed)
    big, big_steps = _inbox_steps(store)

    assert big == expected == _oracle_inbox(store, ids), "the same capsules wait"
    assert big_steps - small_steps < added, (
        f"the inbox did {big_steps - small_steps} more VM instructions for "
        f"{added} more served ids: it reads the traffic again"
    )


def test_every_traffic_write_moves_the_inbox_as_the_definition_does() -> None:
    """Live rows, revoked and restored transcript rows, `usage`, and the raw edits a
    build that never heard of `served` could make: after each, the inbox is
    the one `ORACLE_SERVED` gives, and the literal list says which that is."""
    store, _writer, ids = _fresh(8)
    db = store.db
    _read(db, "get", [ids[0]])
    _read(db, "list", [f" {ids[1]} ", ids[2], ids[2]])
    early = T0 - timedelta(days=1)
    reads = [("toolu_1", ids[3]), ("toolu_2", ids[4])]
    _witness(db, reads, early)
    _read(db, "context", [ids[4]])

    def check(expected: list[str]) -> None:
        assert _inbox(store) == expected == _oracle_inbox(store, ids)

    check([ids[5], ids[6], ids[7]])

    assert _revoke(db) == 2
    check([ids[3], ids[5], ids[6], ids[7]])  # ids[4] keeps its live read

    _witness(db, reads, early)
    check([ids[5], ids[6], ids[7]])

    store.traffic.record_recall(ids[5], T0)
    check([ids[6], ids[7]])

    with db.writing() as conn:
        conn.execute(
            "UPDATE read_traffic SET served_ids = json_array(?) WHERE surface = 'get'",
            (ids[6],),
        )
        conn.execute("DELETE FROM read_traffic WHERE surface = 'list'")
        conn.execute(
            "INSERT INTO read_traffic (surface, session_id, terms, served_ids,"
            " served_count, at) VALUES ('get', 'sess-raw', '', json_array(NULL, 7, ?),"
            " 3, ?)",
            (ids[7], T0.isoformat()),
        )
    check([ids[0], ids[1], ids[2]])

    # The deleted row listed ids[2] twice: insert and delete must count the
    # same way, or one more read leaves it at zero and the inbox serves it.
    _read(db, "get", [ids[2]])
    check([ids[0], ids[1]])


def test_a_store_from_before_the_table_gets_it_when_opened_for_writing() -> None:
    """The member's additive step: a writable open adds the table, its
    triggers and its counts once; the version stays 1, so a build that
    predates it still opens the store, and its writes keep the table exact
    through the triggers. A read-only open adds nothing, and its inbox
    still answers, from the rows."""
    store, _writer, ids = _fresh(4)
    _read(store.db, "get", [ids[0]])
    _read(store.db, "list", [ids[2], ids[2]])
    _witness(store.db, [("toolu_1", ids[1])], T0)
    assert _revoke(store.db) == 1
    before = _inbox(store)
    assert before == [ids[1], ids[3]] == _oracle_inbox(store, ids)
    path = store.db.path

    def drop() -> None:
        with Database(path).writing() as conn:
            for trigger in ("insert", "delete", "update"):
                conn.execute(f"DROP TRIGGER read_traffic_served_{trigger}")
            conn.execute("DROP TABLE served")

    def tables() -> set[str]:
        conn = nott_database.connect(path, readonly=True)
        try:
            rows = nott_database.query(conn, "SELECT name FROM sqlite_master")
        finally:
            conn.close()
        return {str(r[0]) for r in rows}

    drop()
    assert _inbox(Store.open(path, read_only=True)) == before
    assert "served" not in tables(), "a read-only open never writes"

    reopened = Store.open(path)
    with reopened.db.connection() as conn, conn.cursor() as cur:
        counts = cur.execute("SELECT capsule_id, n FROM served ORDER BY 1").fetchall()
        version = nott_database.user_version(conn.conn)
    assert sorted(counts) == sorted([(ids[0], 1), (ids[2], 2)])
    assert version == 1, "a rollback reopens the store with no step"
    assert {
        "read_traffic_served_insert",
        "read_traffic_served_delete",
        "read_traffic_served_update",
    } <= tables()
    assert _inbox(reopened) == before

    _read(reopened.db, "get", [ids[3]])
    assert _inbox(reopened) == [ids[1]] == _oracle_inbox(reopened, ids)

    # The list row counted ids[2] twice; the delete trigger must take back two.
    with reopened.db.writing() as conn:
        conn.execute("DELETE FROM read_traffic WHERE surface = 'list'")
    assert _inbox(reopened) == [ids[1], ids[2]] == _oracle_inbox(reopened, ids)
    _read(reopened.db, "get", [ids[2]])
    assert _inbox(reopened) == [ids[1]] == _oracle_inbox(reopened, ids)

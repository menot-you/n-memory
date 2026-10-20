"""Read surfaces retain their meaning without per-capsule database work."""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    sha256_hex,
)
from nmemory.commands import anchor as anchor_command
from nmemory.queries.census import delivery_census, write_read_census
from nmemory.store import Store

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)

TRAFFIC_EXPANSION = re.compile(r"json_each\(((?:\w+\.)?served_ids)\)")


def seed(store: Store, count: int, start: int = 0) -> list[str]:
    ids = []
    for number in range(start, start + count):
        content = f"Memory cost observation {number}: preserve result meaning."
        capsule = Capsule(
            content=content,
            provenance=Provenance(
                source="test:read-cost",
                anchor=f"code/item.py:{number + 1}",
                source_hash=sha256_hex(content.encode()),
            ),
            confidence=0.9,
            freshness=Freshness(valid_from=NOW),
            scope=Scope(project_id="read-cost"),
            authority_class=AuthorityClass.USER_STATED,
            instruction_taint=number == 1,
        )
        cid, _ = store.append(capsule, NOW)
        if number % 2 == 0:
            store.classifications.upsert(cid, "fact", "project", NOW.isoformat())
        ids.append(cid)
    return ids


def test_anchor_list_database_opens_do_not_grow_per_capsule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = Store.open_disposable_test_store()
    ids = seed(store, 12)
    store.anchors.reanchor(ids[0], "code/effective.py:7", "test correction", NOW)
    monkeypatch.setattr(anchor_command, "open_store_from_args", lambda args: store)
    original = store.db.connection
    opened = 0

    def connection() -> Any:
        nonlocal opened
        opened += 1
        return original()

    monkeypatch.setattr(store.db, "connection", connection)

    def listing() -> tuple[list[dict[str, Any]], int]:
        before = opened
        anchor_command.cmd_anchor_list([])
        return json.loads(capsys.readouterr().out), opened - before

    first, small = listing()
    assert [row["id"] for row in first] == ids
    assert first[0]["anchorEffective"] == "code/effective.py:7"
    assert first[0]["kind"] == "fact"
    assert first[1]["kind"] is None
    assert first[1]["instructionTaint"] is True
    assert "headline" not in first[1]
    seed(store, 12, start=12)
    second, large = listing()
    assert len(second) == 24
    assert first == second[:12]
    assert large == small, (
        f"anchor list opened {small} connections for 12 capsules and {large} for 24"
    )


def test_write_read_census_does_not_rescan_expanded_traffic_for_each_capsule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = Store.open_disposable_test_store()
    original = store.db._open
    operations = 0

    def opened() -> Any:
        conn = original()

        def count() -> int:
            nonlocal operations
            operations += 1
            return 0

        conn.set_progress_handler(count, 1)
        return conn

    def populate(count: int, start: int) -> None:
        ids = seed(store, count, start=start)
        for n in range(count):
            store.db.record_read_traffic(
                surface="digest",
                session_id=f"session-{start}-{n}",
                terms="cost",
                served_ids=ids,
                project_id="read-cost",
                project_prefix=None,
                outcome=None,
                now=NOW + timedelta(minutes=1),
            )

    populate(40, 0)
    monkeypatch.setattr(store.db, "_open", opened)
    first = write_read_census(store.db, NOW - timedelta(days=1), rewrites=False)
    small = operations
    assert first == {"fact": (20, 0), "unclassified": (20, 0)}
    monkeypatch.setattr(store.db, "_open", original)
    populate(40, 40)
    monkeypatch.setattr(store.db, "_open", opened)
    operations = 0
    second = write_read_census(store.db, NOW - timedelta(days=1), rewrites=False)
    large = operations
    assert second == {"fact": (40, 0), "unclassified": (40, 0)}
    print(f"Measured census: {small} SQLite operations at 40 capsules; {large} at 80")
    # Doubling both capsule and traffic populations must stay below quadratic
    # growth: one expanded traffic pass and indexed joins replace N full scans.
    assert large < 3 * small, (
        f"census executed {small} SQLite operations for 40 rows and {large} for 80"
    )


@pytest.mark.parametrize("aggregate", [True, False])
def test_digest_collects_both_arrival_partitions_in_one_traffic_scan(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    aggregate: bool,
) -> None:
    from nmemory.commands import digest as digest_command
    from nmemory.database import Cursor, _adapt_params
    from nmemory.relation import RelationKind

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return NOW

    monkeypatch.setattr(digest_command, "datetime", Clock)
    store = Store.open_disposable_test_store()
    ids = seed(store, 12)
    store.add_relation(RelationKind.SUPERSEDES, from_id=ids[-1], to_id=ids[0], now=NOW)
    store.db.record_read_traffic(
        surface="list",
        session_id="arrival-proof",
        terms="read",
        served_ids=[ids[0]],
        project_id="read-cost",
        project_prefix=None,
        outcome=None,
        now=NOW + timedelta(minutes=1),
    )
    if not aggregate:
        with store.db.writing() as conn:
            conn.execute("UPDATE last_read_state SET valid = 0")
    monkeypatch.setattr(
        digest_command, "open_store_from_args", lambda args, **kwargs: store
    )
    monkeypatch.setattr(
        digest_command,
        "datetime",
        SimpleNamespace(now=lambda tz: NOW + timedelta(hours=1)),
    )
    original = Cursor.execute
    traffic_scans = 0
    plans: list[str] = []

    def execute(self: Cursor, sql: str, params: Any = ()) -> Cursor:
        nonlocal traffic_scans
        if "read_traffic" in sql and "capsules c" in sql:
            traffic_scans += 1
        if "is_rewrite" in sql:
            with closing(
                self._cur.connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, _adapt_params(params)
                )
            ) as plan:
                plans.extend(str(row[3]) for row in plan.fetchall())
        return original(self, sql, params)

    monkeypatch.setattr(Cursor, "execute", execute)
    digest_command.cmd_digest_show([])
    output = capsys.readouterr().out
    assert output == (
        "nMEMORY Digest: 12 capsules stored in SQLite · 0 ever recalled\n"
        "nMEMORY write→read, last 7 days (project all):\n"
        "  11 capsules written · 10 never recalled (91%)\n"
        "  1 rewrites (each supersedes an older capsule) · 1 never served\n"
        "  written/never recalled: unclassified 5/5 · fact 6/5\n"
        "nMEMORY delivery, last 7 days (project all):\n"
        "  undelivered 0 · no session 11 · read by question 0 · "
        "delivered 1 · retired unread 0\n"
    )
    assert "SCAN rw LEFT-JOIN" not in plans, (
        "Each capsule must use a lookup in the rewrite set",
        plans,
    )
    assert any("SEARCH r USING" in step and "from_id=?" in step for step in plans)
    # Arrival uses the aggregate or one shared scan for both partitions.
    assert traffic_scans == (2 if aggregate else 3), (
        f"digest expanded read traffic {traffic_scans} times; arrival partitions should share one scan"
    )


@pytest.mark.parametrize("query", ["arrival", "delivery"])
def test_census_expands_each_distinct_traffic_array_once(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    from nmemory.database import Cursor, _adapt_params

    store = Store.open_disposable_test_store()
    ids = seed(store, 4)
    for surface, served in (("context", ids[:2]), ("list", ids[2:])):
        for number in range(3):
            store.db.record_read_traffic(
                surface=surface,
                session_id=f"repeat-{number}",
                terms="cost",
                served_ids=served,
                project_id="read-cost",
                project_prefix=None,
                outcome=None,
                now=NOW + timedelta(minutes=1),
            )
    if query == "arrival":
        with store.db.writing() as conn:
            conn.execute("UPDATE last_read_state SET valid = 0")
    plans: list[str] = []
    original = Cursor.execute

    def execute(self: Cursor, sql: str, params: Any = ()) -> Cursor:
        if "json_each" in sql and "read_traffic" in sql:
            with closing(
                self._cur.connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, _adapt_params(params)
                )
            ) as plan:
                plans.extend(str(row[3]) for row in plan.fetchall())
        return original(self, sql, params)

    monkeypatch.setattr(Cursor, "execute", execute)
    if query == "arrival":
        assert write_read_census(store.db, NOW - timedelta(days=1)) == {
            "fact": (2, 0),
            "unclassified": (2, 0),
        }
    else:
        counts = delivery_census(store.db, NOW - timedelta(days=1)).total()
        assert counts.asked == 2
        assert counts.delivered == 2
        assert counts.written == 4
        assert "SCAN d LEFT-JOIN" not in plans, (
            "Each capsule must use a lookup in the delivery set",
            plans,
        )
    expansions = [i for i, step in enumerate(plans) if "SCAN sid VIRTUAL" in step]
    reductions = [
        i
        for i, step in enumerate(plans)
        if step in ("CO-ROUTINE traffic", "MATERIALIZE traffic")
    ]
    assert expansions
    assert len(reductions) == len(expansions), (
        "Traffic arrays must be distinct before JSON expansion",
        plans,
    )
    assert all(
        reduce < expand
        and any(
            step in ("USE TEMP B-TREE FOR DISTINCT", "USE TEMP B-TREE FOR GROUP BY")
            for step in plans[reduce:expand]
        )
        for reduce, expand in zip(reductions, expansions, strict=True)
    )


@pytest.mark.parametrize("query", ["arrival", "delivery", "sidecars"])
def test_digest_paths_expand_repeated_arrays_once(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    from nmemory.database import Cursor
    from nmemory.queries.sidecars import digest_sidecars

    store = Store.open_disposable_test_store()
    ids = seed(store, 4)
    served = [ids[0], ids[0], f" {ids[1]} ", ids[2]]
    for surface in ("context", "list"):
        for number in range(20):
            store.db.record_read_traffic(
                surface=surface,
                session_id=f"repeat-{number}",
                terms="cost",
                served_ids=served,
                project_id="read-cost",
                project_prefix=None,
                outcome=None,
                now=NOW + timedelta(minutes=number),
            )
    with store.db.writing() as conn:
        conn.execute("UPDATE last_read_state SET valid = 0")
    expansions: list[str] = []
    original = Cursor.execute

    def count_expansion(value: str) -> str:
        expansions.append(value)
        return value

    def execute(self: Cursor, sql: str, params: Any = ()) -> Cursor:
        if TRAFFIC_EXPANSION.search(sql):
            self._cur.connection.create_function("count_expansion", 1, count_expansion)
            sql = TRAFFIC_EXPANSION.sub(r"json_each(count_expansion(\1))", sql)
        return original(self, sql, params)

    monkeypatch.setattr(Cursor, "execute", execute)
    if query == "arrival":
        assert write_read_census(store.db, NOW - timedelta(days=1)) == {
            "fact": (2, 0),
            "unclassified": (2, 2),
        }
    elif query == "delivery":
        counts = delivery_census(store.db, NOW - timedelta(days=1)).total()
        assert counts.asked == 3
        assert counts.delivered == 0
        assert counts.undelivered == 1
    else:
        rows = digest_sidecars(store.db)
        assert [rows[cid].recall_count for cid in ids] == [40, 0, 20, 0]
        assert rows[ids[0]].last_recalled_at == NOW + timedelta(minutes=19)
    assert len(expansions) == 1, (query, len(expansions))


@pytest.mark.parametrize(
    "state",
    [
        "complete",
        "revoked",
        "deleted",
        "incomplete",
        "missing",
        "building",
        "altered",
        "negative",
    ],
)
def test_arrival_uses_only_a_complete_last_read_aggregate(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    from nmemory.database import Cursor

    store = Store.open_disposable_test_store()
    ids = seed(store, 4)
    store.db.record_read_traffic(
        surface="list",
        session_id="arrival-aggregate",
        terms="cost",
        served_ids=[ids[0], ids[0], f" {ids[1]} "],
        project_id="read-cost",
        project_prefix=None,
        outcome=None,
        now=NOW,
    )
    store.traffic.record_recall(ids[2], NOW)
    with store.db.writing() as conn:
        match state:
            case "revoked":
                conn.execute(
                    "UPDATE read_traffic SET revoked_at = ?", (NOW.isoformat(),)
                )
            case "deleted":
                conn.execute("DELETE FROM read_traffic")
            case "incomplete":
                conn.execute("UPDATE last_read_state SET covered_seq = 0")
            case "missing":
                conn.execute("DROP TABLE last_read")
            case "building":
                conn.execute("UPDATE last_read_state SET valid = 2")
            case "altered":
                conn.execute("DROP TRIGGER read_traffic_last_read_delete")
            case "negative":
                conn.execute("UPDATE read_traffic SET seq = -1")
                conn.execute("UPDATE last_read_state SET covered_seq = 0, valid = 1")
    store = Store.open(store.db.path, read_only=True)
    scans = 0
    original = Cursor.execute

    def execute(self: Cursor, sql: str, params: Any = ()) -> Cursor:
        nonlocal scans
        if TRAFFIC_EXPANSION.search(sql):
            scans += 1
        return original(self, sql, params)

    monkeypatch.setattr(Cursor, "execute", execute)
    assert write_read_census(store.db, NOW - timedelta(days=1)) == {
        "fact": (2, 1 if state in ("revoked", "deleted") else 0),
        "unclassified": (2, 2),
    }
    assert scans == (0 if state == "complete" else 1), (
        "Arrival must use the complete aggregate and scan only when coverage is unsafe",
        state,
        scans,
    )

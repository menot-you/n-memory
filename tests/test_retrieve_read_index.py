"""The unread clock reads one aggregate per candidate capsule."""

from __future__ import annotations

import json
import random
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import nott_database
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nmemory import database
from nmemory.database import Database, from_us
from nmemory.queries.census import last_read_among
from nmemory.store import Store

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
ORACLE = """
SELECT capsule_id, max(ts(at)) FROM (
    SELECT capsule_id, last_recalled_at AS at FROM usage
    WHERE capsule_id IN (SELECT value FROM json_each(?))
    UNION ALL
    SELECT sid.value, rt.at
    FROM read_traffic rt, json_each(rt.served_ids) sid
    WHERE rt.revoked_at IS NULL
      AND sid.value IN (SELECT value FROM json_each(?))
) GROUP BY capsule_id
"""


def _read(store: Store, ids: list[str], at: datetime = NOW) -> None:
    store.db.record_read_traffic(
        surface="list",
        session_id="sess-read-index",
        terms="read index",
        served_ids=ids,
        project_id=None,
        project_prefix=None,
        outcome=None,
        now=at,
    )


def _oracle(db: Database, ids: list[str]) -> dict[str, datetime]:
    with db.connection() as conn, conn.cursor() as cur:
        rows = cur.execute(ORACLE, (ids, ids)).fetchall()
    return {str(cid): from_us(int(at)) for cid, at in rows}


def _steps(store: Store, ids: list[str]) -> tuple[dict[str, datetime], int]:
    steps = 0

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    with store.db.tool_boundary(), store.db.connection() as handle:
        handle.conn.set_progress_handler(tick, 1)
        try:
            result = last_read_among(store.db, ids)
        finally:
            handle.conn.set_progress_handler(None, 0)
    return result, steps


def test_unrelated_traffic_does_not_add_work_to_the_unread_clock() -> None:
    store = Store.open_disposable_test_store()
    ids = ["cap-target", "cap-absent"]
    _read(store, [ids[0]])
    small, small_steps = _steps(store, ids)
    assert small == {ids[0]: NOW} == _oracle(store.db, ids)

    unrelated = [f"ctx-{n}" for n in range(25)]
    with store.db.tool_boundary():
        for _ in range(200):
            _read(store, unrelated)
    big, big_steps = _steps(store, ids)
    assert big == small == _oracle(store.db, ids)
    added_ids = 200 * len(unrelated)
    assert big_steps - small_steps < added_ids, (
        f"the unread clock used {big_steps - small_steps} more VM instructions "
        f"for {added_ids} unrelated IDs"
    )
    # A valid aggregate reads the seq bounds by index: no step per traffic row.
    assert big_steps - small_steps < 200, (
        f"the unread clock used {big_steps - small_steps} more VM instructions "
        "for 200 unrelated traffic rows"
    )


def test_aggregate_keeps_the_time_and_revocation_rules() -> None:
    store = Store.open_disposable_test_store()
    ids = ["cap-a", "cap-b", "cap-c", "7", " cap-a ", "cap-none"]
    earlier = NOW - timedelta(microseconds=1)
    _read(store, ["cap-a", "cap-a", "cap-b"], earlier)
    _read(store, ["cap-a", "cap-c"], NOW)
    store.traffic.record_recall("cap-b", NOW + timedelta(days=1))

    def check() -> dict[str, datetime]:
        actual = last_read_among(store.db, ids)
        assert actual == _oracle(store.db, ids)
        return actual

    assert check() == {"cap-a": NOW, "cap-b": NOW + timedelta(days=1), "cap-c": NOW}
    with store.db.writing() as conn:
        conn.execute(
            "UPDATE read_traffic SET revoked_at = ? WHERE seq = 2", (NOW.isoformat(),)
        )
    assert check() == {"cap-a": earlier, "cap-b": NOW + timedelta(days=1)}
    # Invalidated data must use the old scan until a writable open rebuilds it.
    store = Store.open(store.db.path)
    assert check() == {"cap-a": earlier, "cap-b": NOW + timedelta(days=1)}
    with store.db.writing() as conn:
        conn.execute("UPDATE read_traffic SET revoked_at = NULL WHERE seq = 2")
        conn.execute(
            "UPDATE read_traffic SET served_ids = ?, at = ? WHERE seq = 2",
            (
                json.dumps(["cap-c", " cap-a ", 7, None]),
                "2026-10-16T14:00:00.000001+02:00",
            ),
        )
    expected = {
        "cap-a": earlier,
        "cap-b": NOW + timedelta(days=1),
        "cap-c": NOW + timedelta(microseconds=1),
        " cap-a ": NOW + timedelta(microseconds=1),
    }
    assert check() == expected
    with store.db.writing() as conn:
        conn.execute("DELETE FROM read_traffic WHERE seq = 2")
    assert check() == {"cap-a": earlier, "cap-b": NOW + timedelta(days=1)}
    assert last_read_among(store.db, []) == {}


def test_old_stores_keep_read_only_recall_and_get_the_aggregate_on_writable_open() -> (
    None
):
    store = Store.open_disposable_test_store()
    _read(store, ["cap-a", "cap-b"])
    _read(store, ["cap-a"], NOW - timedelta(days=1))
    _read(store, ["cap-a", "cap-revoked"], NOW + timedelta(days=1))
    with store.db.writing() as conn:
        conn.execute("UPDATE read_traffic SET revoked_at = ? WHERE seq = 3", (NOW,))
        # Remove only objects introduced by this change.
        for name in (
            "read_traffic_last_read_insert",
            "read_traffic_last_read_update",
            "read_traffic_last_read_delete",
        ):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute("DROP TABLE IF EXISTS last_read")
        conn.execute("DROP TABLE IF EXISTS last_read_state")
        conn.execute(
            "UPDATE read_traffic SET at = '2026-10-16t12:00:00Z' WHERE seq = 1"
        )

    def has_aggregate() -> bool:
        with store.db.connection() as conn, conn.cursor() as cur:
            return (
                cur.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'last_read'"
                ).fetchone()
                is not None
            )

    reader = Store.open(store.db.path, read_only=True)
    assert last_read_among(reader.db, ["cap-a"]) == {"cap-a": NOW}
    assert not has_aggregate()
    reopened = Store.open(store.db.path)
    assert has_aggregate(), "a writable open must add the read aggregate"
    assert last_read_among(reopened.db, ["cap-a"]) == {"cap-a": NOW}
    assert last_read_among(reopened.db, ["cap-revoked"]) == {}
    with reopened.db.connection() as conn:
        assert nott_database.user_version(conn.conn) == 1
    _read(reopened, ["cap-b"], NOW + timedelta(days=1))
    assert last_read_among(reader.db, ["cap-b"]) == {"cap-b": NOW + timedelta(days=1)}
    # A second writable open must not rebuild an aggregate that already exists.
    with reopened.db.connection() as conn, conn.cursor() as cur:
        before = cur.execute("SELECT * FROM last_read ORDER BY capsule_id").fetchall()
    again = Store.open(store.db.path)
    with again.db.connection() as conn, conn.cursor() as cur:
        assert (
            cur.execute("SELECT * FROM last_read ORDER BY capsule_id").fetchall()
            == before
        )


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-16T12:00:00Z",
        "2026-10-16T12:00:00.000001Z",
        "2026-10-16T11:59:59.999999Z",
        "2026-10-16T14:00:00.000001+02:00",
        "2026-10-16T09:00:00.5-03:00",
        "2026-10-16 12:00:00+00",
        "2026-10-16 12:00:00+0000",
        "2026-10-16T12:00:00",
        "2026-10-16T12:00:00z",
        "2026-10-16t12:00:00Z",
        "2026-10-16t09:00:00.5-03:00",
        " 2026-10-16T12:00:00.000249Z ",
        "1969-12-31T23:59:59.999999Z",
        "2026-10-16T12:00:00+15:00",
        "2026-10-16T12:00:00+02:30:15.5",
        "\t2026-10-16T12:00:00Z\t",
        "2026-10-16T12:00:00,123456789Z",
    ],
)
def test_old_writer_and_backfill_match_the_scan(text: str) -> None:
    store = Store.open_disposable_test_store()
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO read_traffic "
            "(surface, session_id, terms, served_ids, served_count, at) "
            "VALUES ('list', 'test', '', '[\"cap-time\"]', 1, ?)",
            (text,),
        )
    with store.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("PRAGMA trusted_schema").fetchone() == (0,)
    assert last_read_among(store.db, ["cap-time"]) == _oracle(store.db, ["cap-time"])
    reopened = Store.open(store.db.path)
    assert last_read_among(reopened.db, ["cap-time"]) == _oracle(store.db, ["cap-time"])


def test_traffic_rollback_also_restores_the_read_aggregate() -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-a"])
    with pytest.raises(RuntimeError, match="rollback"), store.db.writing() as conn:
        conn.execute("UPDATE read_traffic SET revoked_at = ?", (NOW,))
        raise RuntimeError("rollback")
    assert last_read_among(store.db, ["cap-a"]) == {"cap-a": NOW}


def _raw_read(store: Store, text: str, ids: list[object]) -> None:
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO read_traffic "
            "(surface, session_id, terms, served_ids, served_count, at) "
            "VALUES ('list', 'test', '', ?, ?, ?)",
            (json.dumps(ids), len(ids), text),
        )


def test_large_offset_does_not_drop_the_latest_read() -> None:
    store = Store.open_disposable_test_store()
    _raw_read(store, "2020-01-01T00:00:00Z", ["cap-time"])
    _raw_read(store, "2026-10-16T12:00:00+15:00", ["cap-time"])
    reopened = Store.open(store.db.path)
    assert last_read_among(reopened.db, ["cap-time"]) == _oracle(store.db, ["cap-time"])


@pytest.mark.parametrize("operation", ["revoke", "delete", "time", "ids"])
def test_mutation_invalidates_without_scanning_traffic(operation: str) -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-a"])
    _read(store, ["cap-a"], NOW + timedelta(days=1))
    with store.db.writing() as conn:
        statements = {
            "revoke": "UPDATE read_traffic SET revoked_at = at WHERE seq = 2",
            "delete": "DELETE FROM read_traffic WHERE seq = 2",
            "time": "UPDATE read_traffic SET at = '2020-01-01T00:00:00Z' WHERE seq = 2",
            "ids": "UPDATE read_traffic SET served_ids = '[]' WHERE seq = 2",
        }
        conn.execute(statements[operation])
    with store.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT valid FROM last_read_state").fetchone() == (0,)
        triggers = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name LIKE 'read_traffic_last_read_%'"
        ).fetchall()
        assert len(triggers) == 2
        assert all("SELECT" not in sql.upper() for (sql,) in triggers)
    assert last_read_among(store.db, ["cap-a"]) == _oracle(store.db, ["cap-a"])
    reopened = Store.open(store.db.path)
    assert last_read_among(reopened.db, ["cap-a"]) == _oracle(store.db, ["cap-a"])
    with reopened.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT valid FROM last_read_state").fetchone() == (1,)


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE last_read",
        "DROP TABLE last_read_state",
        "DELETE FROM last_read_state",
        "DROP TRIGGER read_traffic_last_read_update",
        "DROP TRIGGER read_traffic_last_read_delete",
        "DROP TABLE last_read; CREATE TABLE last_read (capsule_id TEXT, at INTEGER)",
        (
            "DROP TRIGGER read_traffic_last_read_update; "
            "CREATE TRIGGER read_traffic_last_read_update AFTER UPDATE ON read_traffic "
            "BEGIN UPDATE last_read_state SET valid = 1; END"
        ),
    ],
)
def test_partial_structure_uses_the_scan_and_is_rebuilt(damage: str) -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-a"])
    with store.db.writing() as conn:
        conn.execute("UPDATE read_traffic SET at = '2020-01-01T00:00:00Z'")
        conn.conn.executescript(damage)
    reader = Store.open(store.db.path, read_only=True)
    assert last_read_among(reader.db, ["cap-a"]) == _oracle(reader.db, ["cap-a"])
    reopened = Store.open(store.db.path)
    assert last_read_among(reopened.db, ["cap-a"]) == _oracle(reader.db, ["cap-a"])


@pytest.mark.parametrize("seed", range(12))
def test_random_timestamp_rows_match_the_old_scan(seed: int) -> None:
    rng = random.Random(seed)
    store = Store.open_disposable_test_store()
    ids = ["cap-a", "cap-b", " cap-a ", "7", "absent"]
    formats = [
        "2026-10-16T12:00:00Z",
        "2026-10-16T12:00:00+15:00",
        "2026-10-16T12:00:00-23:59",
        "2026-10-16T12:00:00+02:30:15.25",
        "2026-10-16T12:00:00.123456Z",
        "2026-10-16T12:00:00.123456789Z",
        "2026-10-16t12:00:00,123456789z",
        "\t 2026-10-16T12:00:00Z\n",
        "1969-12-31T23:59:59.999999Z",
    ]
    for _ in range(35):
        served: list[object] = rng.choices(ids[:-1] + [7, None], k=rng.randrange(5))
        _raw_read(store, rng.choice(formats), served)
    assert last_read_among(store.db, ids) == _oracle(store.db, ids)
    reopened = Store.open(store.db.path)
    assert last_read_among(reopened.db, ids) == _oracle(store.db, ids)
    _read(reopened, ["cap-b"], NOW + timedelta(days=2))
    assert last_read_among(reopened.db, ids) == _oracle(store.db, ids)


def test_bad_timestamp_keeps_the_old_candidate_scoped_refusal() -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-good"])
    _raw_read(store, "not-a-time", ["cap-bad"])
    for db in (store.db, Store.open(store.db.path).db):
        assert last_read_among(db, ["cap-good"]) == _oracle(db, ["cap-good"])
        with pytest.raises(
            nott_database.OperationalError, match="user-defined function"
        ):
            _oracle(db, ["cap-bad"])
        with pytest.raises(
            nott_database.OperationalError, match="user-defined function"
        ):
            last_read_among(db, ["cap-bad"])


@pytest.mark.parametrize("mutate", [False, True])
def test_backfill_releases_the_lock_and_never_serves_partial_data(
    monkeypatch: pytest.MonkeyPatch,
    mutate: bool,
) -> None:
    store = Store.open_disposable_test_store()
    ids = [f"cap-{n}" for n in range(10)]
    for cid in ids:
        _read(store, [cid])
    with store.db.writing() as conn:
        conn.execute("UPDATE last_read_state SET valid = 0")
    monkeypatch.setattr(database, "LAST_READ_CHUNK_SIZE", 3)
    write = nott_database.write
    checkpoints: list[int] = []

    @contextmanager
    def observe(conn: nott_database.Connection) -> Iterator[nott_database.Connection]:
        with write(conn) as active:
            yield active
        state = nott_database.query(
            conn, "SELECT covered_seq, valid FROM last_read_state"
        )[0]
        if state[1] != 1 and 0 < state[0] < 10 and state[0] not in checkpoints:
            checkpoints.append(state[0])
            # A second connection can write between chunks without losing its read.
            _read(store, ["cap-concurrent"])
            if mutate and state[0] == 3:
                with store.db.writing() as handle:
                    handle.execute(
                        "UPDATE read_traffic SET at = '2020-01-01T00:00:00Z' "
                        "WHERE seq = 1"
                    )
            assert last_read_among(store.db, ids) == _oracle(store.db, ids)

    monkeypatch.setattr(nott_database, "write", observe)
    reopened = Store.open(store.db.path)
    assert checkpoints == [3, 6, 9]
    assert last_read_among(reopened.db, ids + ["cap-concurrent"]) == _oracle(
        store.db, ids + ["cap-concurrent"]
    )


@pytest.mark.parametrize("only_read", [False, True])
def test_upgrade_replaces_the_sql_time_aggregate(only_read: bool) -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-time"], NOW - timedelta(days=3000))
    with store.db.writing() as conn:
        conn.execute("DROP TRIGGER read_traffic_last_read_update")
        conn.execute("DROP TRIGGER read_traffic_last_read_delete")
        conn.execute("DROP TABLE last_read_state")
        conn.execute(
            "CREATE TRIGGER read_traffic_last_read_insert AFTER INSERT ON read_traffic "
            "BEGIN SELECT 1; END"
        )
        if only_read:
            conn.execute("DELETE FROM read_traffic")
    _raw_read(store, "2026-10-16T12:00:00+15:00", ["cap-time"])
    reopened = Store.open(store.db.path)
    assert last_read_among(reopened.db, ["cap-time"]) == _oracle(store.db, ["cap-time"])


def test_read_and_aggregate_commit_together() -> None:
    store = Store.open_disposable_test_store()
    with store.db.writing() as conn:
        conn.execute(
            "CREATE TRIGGER reject_aggregate BEFORE INSERT ON last_read "
            "BEGIN SELECT RAISE(ABORT, 'test refusal'); END"
        )
    _read(store, ["cap-a"])
    with store.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT count(*) FROM read_traffic").fetchone() == (0,)
        assert cur.execute("SELECT covered_seq FROM last_read_state").fetchone() == (0,)
    assert last_read_among(store.db, ["cap-a"]) == {}


def _assert_scan_parity(db: Database, ids: list[str]) -> None:
    try:
        expected = _oracle(db, ids)
    except (
        nott_database.DatabaseError,
        nott_database.Error,
        TypeError,
        OverflowError,
    ) as exc:
        with pytest.raises(type(exc)):
            last_read_among(db, ids)
    else:
        assert last_read_among(db, ids) == expected


@pytest.mark.parametrize("seq", [0, -5])
def test_rows_below_seq_one_keep_scan_results(seq: int) -> None:
    store = Store.open_disposable_test_store()
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO read_traffic "
            "(seq, surface, session_id, terms, served_ids, served_count, at) "
            "VALUES (?, 'list', 'test', '', ?, 1, ?)",
            (seq, json.dumps(["cap-a"]), NOW.isoformat()),
        )
    _raw_read(store, NOW.isoformat(), ["cap-b"])
    reopened = Store.open(store.db.path)
    assert _oracle(reopened.db, ["cap-a", "cap-b"]) == {"cap-a": NOW, "cap-b": NOW}
    _assert_scan_parity(reopened.db, ["cap-a", "cap-b"])


def test_extreme_aggregate_with_a_newer_tail_matches_the_scan() -> None:
    store = Store.open_disposable_test_store()
    _raw_read(store, "0001-01-01T00:00:00+15:00", ["cap-a"])
    reopened = Store.open(store.db.path)
    _raw_read(reopened, NOW.isoformat(), ["cap-a"])
    assert _oracle(reopened.db, ["cap-a"]) == {"cap-a": NOW}
    _assert_scan_parity(reopened.db, ["cap-a"])


@pytest.mark.parametrize("served", ['{"cap-a":"cap-b"}', "null"])
def test_json_shapes_do_not_change_backfill_results(served: str) -> None:
    store = Store.open_disposable_test_store()
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO read_traffic "
            "(surface, session_id, terms, served_ids, served_count, at) "
            "VALUES ('list', 'test', '', ?, 1, ?)",
            (served, NOW.isoformat()),
        )
    reopened = Store.open(store.db.path)
    _assert_scan_parity(reopened.db, ["cap-a", "cap-b"])


def test_blob_time_does_not_block_backfill_or_other_candidates() -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-good"])
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO read_traffic "
            "(surface, session_id, terms, served_ids, served_count, at) "
            "VALUES ('list', 'test', '', '[\"cap-bad\"]', 1, ?)",
            (b"2026-10-16T12:00:00Z",),
        )
    reopened = Store.open(store.db.path)
    for ids in (["cap-good"], ["cap-bad"]):
        _assert_scan_parity(reopened.db, ids)


@pytest.mark.parametrize("cid", ["cap-\u0000tail", "cap-\U0001f600", "\ud800"])
def test_json_string_values_keep_scan_results(cid: str) -> None:
    store = Store.open_disposable_test_store()
    _raw_read(store, NOW.isoformat(), ["cap-a", cid])
    _assert_scan_parity(store.db, ["cap-a"])
    reopened = Store.open(store.db.path)
    _assert_scan_parity(reopened.db, ["cap-a"])
    _assert_scan_parity(reopened.db, [cid])


def test_invalid_utf8_time_keeps_candidate_scoped_refusal() -> None:
    store = Store.open_disposable_test_store()
    _read(store, ["cap-good"])
    with store.db.writing() as conn:
        conn.execute(
            "INSERT INTO read_traffic "
            "(surface, session_id, terms, served_ids, served_count, at) "
            "VALUES ('list', 'test', '', '[\"cap-bad\"]', 1, CAST(? AS TEXT))",
            (b"\xff",),
        )
    _assert_scan_parity(store.db, ["cap-good"])
    reopened = Store.open(store.db.path)
    _assert_scan_parity(reopened.db, ["cap-good"])
    _assert_scan_parity(reopened.db, ["cap-bad"])


@pytest.mark.parametrize(
    "served,at",
    [
        ('{"cap-a":"cap-b"}', NOW.isoformat()),
        ('["cap-a",7]', NOW.isoformat()),
        ('["cap-a"]', b"2026-10-16T12:00:00Z"),
        ('["cap-a"]', "0001-01-01T00:00:00+15:00"),
    ],
)
def test_insert_maintenance_keeps_domain_refusal_until_a_full_check(
    served: str,
    at: str | bytes,
) -> None:
    store = Store.open_disposable_test_store()
    store.db._write_traffic(
        [("list", "test", None, None, None, "", served, 1, None, at)]
    )
    with store.db.connection() as conn, conn.cursor() as cur:
        valid, reason = cur.execute(
            "SELECT valid, reason FROM last_read_state"
        ).fetchone()
    assert valid == 0 and reason
    _read(store, ["cap-good"])
    with store.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT valid, reason FROM last_read_state").fetchone() == (
            valid,
            reason,
        )
    _assert_scan_parity(store.db, ["cap-a", "cap-b", "cap-good"])
    reopened = Store.open(store.db.path)
    with reopened.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT valid FROM last_read_state").fetchone() == (0,)
    with reopened.db.writing() as conn:
        conn.execute("DELETE FROM read_traffic WHERE seq = 1")
    repaired = Store.open(store.db.path)
    with repaired.db.connection() as conn, conn.cursor() as cur:
        assert cur.execute("SELECT valid, reason FROM last_read_state").fetchone() == (
            1,
            None,
        )
    _assert_scan_parity(repaired.db, ["cap-good"])


_SERVED = st.one_of(
    st.lists(st.sampled_from(["cap-a", "cap-b", " cap-a ", "7", ""]), max_size=4).map(
        json.dumps
    ),
    st.sampled_from(
        [
            '{"cap-a":"cap-b"}',
            "null",
            '"cap-a"',
            "7",
            "true",
            '["cap-a",7,null,true,{},["cap-b"]]',
            '["cap-a","\\ud800"]',
            "[]",
            "{bad json",
        ]
    ),
    st.just(b'["cap-a"]'),
)
_TIMES = st.one_of(
    st.datetimes(timezones=st.just(UTC)).map(datetime.isoformat),
    st.sampled_from(
        [
            None,
            b"2026-10-16T12:00:00Z",
            "not-a-time",
            "",
            "0001-01-01T00:00:00+15:00",
            "9999-12-31T23:59:59-15:00",
            "0001-01-01T15:00:00+15:00",
            "9999-12-31T08:59:59-15:00",
        ]
    ),
)


@settings(max_examples=100, deadline=None)
@given(rows=st.lists(st.tuples(_SERVED, _TIMES, st.booleans()), min_size=1, max_size=8))
def test_generated_traffic_matches_scan_results_and_exception_types(rows) -> None:
    # Allow legacy NULL times and malformed JSON in this test store only.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            database,
            "SCHEMA",
            database.SCHEMA.replace(
                "outcome TEXT, at TEXT NOT NULL", "outcome TEXT, at TEXT"
            ),
        )
        store = Store.open_disposable_test_store()
    with store.db.writing() as conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        for event in ("insert", "update", "delete"):
            conn.execute(f"DROP TRIGGER read_traffic_served_{event}")
        for served, at, revoked in rows:
            conn.execute(
                "INSERT INTO read_traffic "
                "(surface, session_id, terms, served_ids, served_count, at, revoked_at) "
                "VALUES ('list', 'test', '', ?, 1, ?, ?)",
                (served, at, NOW.isoformat() if revoked else None),
            )
    for db in (store.db, Store.open(store.db.path, read_only=True).db):
        for ids in (["cap-a"], ["cap-b", "7", " cap-a "], ["absent"]):
            _assert_scan_parity(db, ids)
    reopened = Store.open(store.db.path)
    _read(reopened, ["cap-b"])
    for ids in (["cap-a"], ["cap-b", "7", " cap-a "], ["absent"]):
        _assert_scan_parity(reopened.db, ids)

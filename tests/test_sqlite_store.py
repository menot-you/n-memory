"""The SQLite store's own promises (P2): transactions, fences, time, elections.

Each test here names one property the PostgreSQL store held by construction and
the SQLite port has to hold by its own code:

- a write nobody declared fails (`query_only`), it is never silently committed;
- a tool call that wrote and then failed leaves nothing, not even a generation;
- project fences are exact and case-sensitive, and `_` or GLOB metacharacters in
  a project name are literal (`LIKE` read `_` as a wildcard and ignored case);
- `ts()` reads every instant form the store holds, so queries order by time and
  not by text;
- one embedding worker and one curator per store, and a survivor takes over
  when the owner dies (≤ 1 s and ≤ `CURATOR_POLL_S` ≤ 60 s); a curator without
  credentials never holds the lock.
"""

from __future__ import annotations

import contextlib
import math
import os
import random
import signal
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import nott_database
import pytest
from child_env import child_env
from nott_database import BusyError, RefusedError, mapped
from storefile import fresh_store_path, put_embedding

from nmemory import curator
from nmemory.capsule import (
    AuthorityClass,
    Capsule,
    Freshness,
    Provenance,
    Scope,
    format_rfc3339,
    sha256_hex,
)
from nmemory.database import Database, ts
from nmemory.repositories.capsules import ListFilter
from nmemory.space import DIMENSION, SpaceFingerprint
from nmemory.store import Store

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
CHILD = Path(__file__).with_name("election_child.py")


def _capsule(project: str, text: str | None = None) -> Capsule:
    content = text or f"sqlite fence probe for project {project}"
    return Capsule(
        content=content,
        provenance=Provenance(
            source="test:sqlite-store",
            anchor=f"tests/test_sqlite_store.py:{project}",
            source_hash=sha256_hex(content.encode("utf-8")),
        ),
        confidence=0.9,
        freshness=Freshness(valid_from=NOW),
        scope=Scope(project_id=project),
        authority_class=AuthorityClass.USER_STATED,
        instruction_taint=False,
    )


def _projects_of(store: Store, filt: ListFilter) -> set[str]:
    return {c.capsule.scope.project_id for c in store.capsules.list(filt)}


# -- transactions ---------------------------------------------------------------


def test_an_undeclared_write_is_refused_inside_and_outside_a_boundary() -> None:
    store = Store.open(fresh_store_path())
    with (
        store.db.tool_boundary(),
        store.db.connection() as handle,
        pytest.raises(RefusedError),
    ):
        handle.execute("UPDATE generation SET value = 99")
    with store.db.connection() as handle, pytest.raises(RefusedError):
        handle.execute("UPDATE generation SET value = 99")
    assert store.db.get_generation() == 1


def test_a_declared_write_outside_a_boundary_is_refused() -> None:
    store = Store.open(fresh_store_path())
    with pytest.raises(nott_database.DatabaseError):
        store.db.begin_write()


def test_write_then_failure_leaves_nothing() -> None:
    store = Store.open(fresh_store_path())
    before = store.db.get_generation()
    with (
        pytest.raises(RuntimeError, match="after the write"),
        store.db.tool_boundary(),
    ):
        store.append(_capsule("rollback"), NOW)
        raise RuntimeError("after the write")
    assert store.capsules.list(ListFilter()) == []
    assert store.db.get_generation() == before


def test_one_boundary_is_one_generation() -> None:
    store = Store.open(fresh_store_path())
    with store.db.tool_boundary():
        store.append(_capsule("gen", "first capsule"), NOW)
        store.append(_capsule("gen", "second capsule"), NOW)
    assert store.db.get_generation() == 2


def test_a_declared_write_that_changes_nothing_is_not_a_generation() -> None:
    """The generation counts changed canonical state, not declared intent.

    Every server's curator loop runs `expire_staged_proposals` and
    `reject_unsupported_evidence` each 60 s, and the curator verdict paths and
    hygiene's re-check declare a write and then find nothing to do. Bumping on
    each of them kept the generation moving on an idle store, and the elected
    embedding worker re-ran its full-store sync under the write lock forever.
    """
    store = Store.open(fresh_store_path())
    store.append(_capsule("noop"), NOW)
    before = store.db.get_generation()
    # The curator verdict and hygiene shape: declare, re-check, leave.
    with store.db.tool_boundary():
        store.db.begin_write()
    assert store.expire_staged_proposals(None, subtree=False, now=NOW, actor="t") == []
    assert (
        store.reject_unsupported_evidence(None, subtree=False, now=NOW, actor="t") == []
    )
    # A write that matches no row is still no change.
    with store.db.tool_boundary(), store.db.connection() as handle:
        store.db.begin_write()
        handle.execute("DELETE FROM tombstones WHERE capsule_id = 'absent'")
    assert store.db.get_generation() == before
    store.append(_capsule("noop", "a real write after the no-ops"), NOW)
    assert store.db.get_generation() == before + 1


def test_read_only_store_cannot_begin_a_write() -> None:
    path = fresh_store_path()
    store = Store.open(path, read_only=True)
    with store.db.tool_boundary(), pytest.raises(RefusedError):
        store.db.begin_write()


def test_a_read_only_handle_refuses_a_write_even_without_query_only() -> None:
    """The file handle refuses, not the `read_only` belief."""
    conn = Database(fresh_store_path(), read_only=True)._open()
    try:
        conn.execute("PRAGMA query_only = OFF")
        with pytest.raises(RefusedError), mapped():
            conn.execute("DELETE FROM generation WHERE 0")
    finally:
        conn.close()


@pytest.mark.parametrize("read_only", [True, False])
def test_every_store_connection_maps_the_file(read_only: bool) -> None:
    """The exact vector scan reads the file through mmap, not one pread per page.

    Measured cold on a 13.6k x 3072 store: the dominant project's fence p50 fell
    from 922 ms to 385 ms with the file mapped.
    """
    db = Database(fresh_store_path(), read_only=read_only)
    with db.connection() as handle:
        mapped = nott_database.query(handle.conn, "PRAGMA mmap_size")[0][0]
    assert mapped >= 1 << 30


def test_a_call_reads_one_snapshot_and_never_blocks_a_writer() -> None:
    """Totals and rows of one read-only call agree under a concurrent writer."""
    path = fresh_store_path()
    store, other = Store.open(path), Store.open(path)
    store.append(_capsule("snap", "first snapshot capsule"), NOW)
    with store.db.tool_boundary():
        before = len(store.capsules.list(ListFilter()))
        other.append(_capsule("snap", "a concurrent writer's capsule"), NOW)
        assert len(store.capsules.list(ListFilter())) == before
        # Declaring a write ends the snapshot: the writer re-reads current state.
        store.db.begin_write()
        assert len(store.capsules.list(ListFilter())) == before + 1
    assert len(store.capsules.list(ListFilter())) == before + 1


def test_deferred_read_traffic_waits_for_the_flush() -> None:
    store = Store.open(fresh_store_path())
    probe = Database(store.db.path)

    def traffic() -> int:
        with probe.connection() as handle:
            return int(
                handle.execute("SELECT count(*) FROM read_traffic").fetchone()[0]
            )

    with store.db.tool_boundary(defer_traffic=True):
        store.db.record_traffic((*_TRAFFIC_ROW, format_rfc3339(NOW)))
    assert traffic() == 0
    store.db.flush_traffic()
    assert traffic() == 1
    store.db.flush_traffic()
    assert traffic() == 1


# -- fences ---------------------------------------------------------------------

FENCE_PROJECTS = (
    "Raven",
    "raven",
    "Raven/child",
    "ravenx",
    "test_probe",
    "testXprobe",
    "test_probe/a",
    "testXprobe/a",
    "a[b]*?",
    "a[b]*?/c",
    "ab/c",
    "a[b]xx/c",
)


@pytest.fixture
def fenced_store() -> Store:
    store = Store.open(fresh_store_path())
    for project in FENCE_PROJECTS:
        store.append(_capsule(project), NOW)
    return store


@pytest.mark.parametrize(
    ("project_id", "expected"),
    [
        ("Raven", {"Raven"}),
        ("raven", {"raven"}),
        ("test_probe", {"test_probe"}),
        ("a[b]*?", {"a[b]*?"}),
    ],
)
def test_exact_project_fence_is_case_sensitive_and_literal(
    fenced_store: Store, project_id: str, expected: set[str]
) -> None:
    assert _projects_of(fenced_store, ListFilter(project_id=project_id)) == expected


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("Raven", {"Raven", "Raven/child"}),
        ("raven", {"raven"}),
        ("test_probe", {"test_probe", "test_probe/a"}),
        ("a[b]*?", {"a[b]*?", "a[b]*?/c"}),
    ],
)
def test_prefix_fence_is_the_exact_id_or_its_subtree(
    fenced_store: Store, prefix: str, expected: set[str]
) -> None:
    assert _projects_of(fenced_store, ListFilter(project_prefix=prefix)) == expected


def test_vector_fence_matches_the_list_fence(fenced_store: Store) -> None:
    space = SpaceFingerprint.current().as_str()
    unit = [1.0] + [0.0] * (DIMENSION - 1)
    ids = {
        c.capsule.scope.project_id: c.id
        for c in fenced_store.capsules.list(ListFilter())
    }
    for cid in ids.values():
        put_embedding(fenced_store.db, cid, unit, space=space)
    by_id = {v: k for k, v in ids.items()}

    def projects(**fence: str) -> set[str]:
        hits = fenced_store.embeddings.search_cosine(
            unit, 50, space_fingerprint=space, **fence
        )
        return {by_id[cid] for cid, _ in hits}

    assert projects(project_id="Raven") == {"Raven"}
    assert projects(project_prefix="Raven") == {"Raven", "Raven/child"}
    assert projects(project_prefix="test_probe") == {"test_probe", "test_probe/a"}
    assert projects(project_prefix="a[b]*?") == {"a[b]*?", "a[b]*?/c"}


def test_vector_search_refuses_a_negative_limit(fenced_store: Store) -> None:
    with pytest.raises(ValueError, match="limit"):
        fenced_store.embeddings.search_cosine([1.0] * DIMENSION, -1)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    return dot / math.sqrt(math.fsum(x * x for x in a) * math.fsum(y * y for y in b))


@pytest.fixture
def ranked_store() -> tuple[Store, str, dict[str, list[float]]]:
    """Capsules in and out of the `rank` subtree, with exact duplicate vectors (ties)."""
    store = Store.open(fresh_store_path())
    space = SpaceFingerprint.current().as_str()
    rng = random.Random(11)
    vectors: dict[str, list[float]] = {}
    projects = ("rank", "rank/child", "rankx", "other")
    for n in range(36):
        project = projects[n % len(projects)]
        cid, _ = store.append(
            _capsule(project, f"ranked capsule {n} of {project}"), NOW
        )
        # Each `rank/child` capsule repeats its `rank` sibling's vector: an exact
        # tie inside the fence, which breaks by capsule id.
        vector = (
            list(vectors.values())[-1]
            if project == "rank/child"
            else [
                float(struct.unpack("f", struct.pack("f", rng.gauss(0, 1)))[0])
                for _ in range(DIMENSION)
            ]
        )
        put_embedding(store.db, cid, vector, space=space)
        vectors[cid] = vector
    return store, space, vectors


@pytest.mark.parametrize("limit", [1, 5, 10, 50])
def test_vector_top_k_is_the_exact_cosine_ranking(
    ranked_store: tuple[Store, str, dict[str, list[float]]], limit: int
) -> None:
    """Top-k, order and ties are the brute-force cosine ranking inside the fence."""
    store, space, vectors = ranked_store
    fenced = {c.id for c in store.capsules.list(ListFilter(project_prefix="rank"))}
    for query in list(vectors.values())[:6]:
        expected = sorted(
            fenced, key=lambda cid: (1.0 - _cosine(vectors[cid], query), cid)
        )[:limit]
        hits = store.embeddings.search_cosine(
            query, limit, project_prefix="rank", space_fingerprint=space
        )
        assert [cid for cid, _ in hits] == expected
        for cid, sim in hits:
            assert sim == pytest.approx(_cosine(vectors[cid], query), abs=1e-5)


def test_vector_space_check_reads_a_covering_index(
    ranked_store: tuple[Store, str, dict[str, list[float]]],
) -> None:
    """The superseded-space count never walks the embedding rows themselves.

    Walking the clustered table read every leaf of the 12 KiB rows: 48 ms warm
    on the 13.5k-row store, against 1.4 ms over the narrow capsule index.
    """
    store, space, vectors = ranked_store
    statements: list[str] = []
    with store.db.tool_boundary(), store.db.connection() as handle:
        handle.conn.set_trace_callback(statements.append)
        store.embeddings.search_cosine(
            next(iter(vectors.values())),
            5,
            project_prefix="rank",
            space_fingerprint=space,
        )
        handle.conn.set_trace_callback(None)
        (check,) = [s for s in statements if "count(*)" in s]
        plan = [
            row[3] for row in handle.execute(f"EXPLAIN QUERY PLAN {check}").fetchall()
        ]
    assert any("COVERING INDEX" in step for step in plan), plan


# -- time -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "2026-09-24T12:00:00Z",
        "2026-09-24T12:00:00.000000Z",
        "2026-09-24T12:00:00+00:00",
        "2026-09-24T09:00:00-03:00",
        "2026-09-24T12:00:00.0Z",
        "2026-09-24 12:00:00+00",
        "2026-09-24 12:00:00+00:00",
        "2026-09-24T12:00:00",
        "2026-09-24T12:00:00z",
    ],
)
def test_ts_reads_every_stored_form_as_the_same_instant(text: str) -> None:
    assert ts(text) == int(NOW.timestamp()) * 1_000_000


def test_ts_keeps_microseconds_and_orders_by_time_not_text() -> None:
    earlier = "2026-09-24T12:00:00Z"
    later = "2026-09-24T12:00:00.5Z"
    assert earlier > later  # text order is wrong: 'Z' sorts after '.'
    assert ts(earlier) < ts(later)  # type: ignore[operator]
    assert ts("2026-09-24T12:00:00.000001Z") == ts("2026-09-24T12:00:00Z") + 1  # type: ignore[operator]
    assert ts(None) is None


def test_ts_is_available_in_sql() -> None:
    store = Store.open(fresh_store_path())
    with store.db.connection() as handle:
        row = handle.execute(
            "SELECT ts(?) - ts(?)", ("2026-09-24T12:00:01Z", "2026-09-24 12:00:00+00")
        ).fetchone()
    assert row[0] == 1_000_000


# -- read traffic -------------------------------------------------------------------


# `served_ids` as the server passes it: a list, bound as a JSON array.
_TRAFFIC_ROW = ("retrieve", "s", None, "p", None, "probe", ["cap-1"], 1, "grounded")


@pytest.mark.parametrize("fails", [False, True])
def test_a_tool_call_writes_its_read_traffic_on_its_own_connection(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    """One connection per call: the traffic row no longer opens and closes a second.

    Each open ran WAL recovery as the first connection and each close
    checkpointed as the last: ~2 x 50 ms of a warm `nmemory_capsule_retrieve`. The row
    still lands in its own transaction, after the call's, even when the call
    raised.
    """
    store = Store.open(fresh_store_path())
    opened = 0
    real = store.db._open

    def counting() -> nott_database.Connection:
        nonlocal opened
        opened += 1
        return real()

    monkeypatch.setattr(store.db, "_open", counting)
    with contextlib.suppress(RuntimeError), store.db.tool_boundary():
        store.append(_capsule("traffic"), NOW)
        store.db.record_traffic((*_TRAFFIC_ROW, format_rfc3339(NOW)))
        if fails:
            raise RuntimeError("the call failed after reading")
    assert opened == 1
    with store.db.connection() as handle:
        traffic = handle.execute("SELECT count(*) FROM read_traffic").fetchone()[0]
    assert traffic == 1
    assert len(store.capsules.list(ListFilter())) == (0 if fails else 1)


# -- elections across processes ---------------------------------------------------


def _lock_file(store_file: Path, role: str) -> Path:
    return store_file.parent / f"nmemory.{role}.lock"


def _held(lock: Path) -> bool:
    """Whether another process holds `lock` (probed without waiting)."""
    try:
        with nott_database.lock(lock, wait=0):
            return False
    except BusyError:
        return True


def _wait_until(predicate: object, limit: float) -> float | None:
    started = time.monotonic()
    while time.monotonic() - started < limit:
        if predicate():  # type: ignore[operator]
            return time.monotonic() - started
        time.sleep(0.02)
    return None


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[str]]]:
    spawned: list[subprocess.Popen[str]] = []
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def _spawn(
    spawned: list[subprocess.Popen[str]], role: str, store_file: Path
) -> subprocess.Popen[str]:
    env = child_env(dict(os.environ, NOTT_NMEMORY_CURATOR_POLL_S="0.5"))
    child = subprocess.Popen(
        [sys.executable, str(CHILD), role, str(store_file)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    spawned.append(child)
    assert child.stdout is not None
    assert child.stdout.readline().strip() == "ready"
    return child


def _survivor_takes_over(
    children: list[subprocess.Popen[str]], role: str, lock_role: str, limit: float
) -> float:
    store_file = fresh_store_path()
    lock = _lock_file(store_file, lock_role)
    first = _spawn(children, role, store_file)
    assert _wait_until(lambda: _held(lock), 10.0) is not None
    _spawn(children, role, store_file)
    time.sleep(0.3)
    assert _held(lock)
    os.kill(first.pid, signal.SIGKILL)
    first.wait(timeout=10)
    taken = _wait_until(lambda: _held(lock), limit + 5.0)
    assert taken is not None, f"no survivor took the {lock_role} lock"
    return taken


def test_embedding_survivor_takes_over_within_one_second(
    children: list[subprocess.Popen[str]],
) -> None:
    elapsed = _survivor_takes_over(children, "embedding", "embedding", 1.0)
    assert elapsed <= 1.0, elapsed


def test_curator_survivor_takes_over_within_its_poll(
    children: list[subprocess.Popen[str]],
) -> None:
    assert curator.CURATOR_POLL_S <= 60.0
    elapsed = _survivor_takes_over(children, "curator", "curator", 0.5)
    # The child's poll is 0.5 s; one poll plus scheduling slack.
    assert elapsed <= 1.5, elapsed


def test_a_curator_without_credentials_never_holds_the_lock(
    children: list[subprocess.Popen[str]],
) -> None:
    store_file = fresh_store_path()
    lock = _lock_file(store_file, "curator")
    _spawn(children, "curator-unconfigured", store_file)
    assert _wait_until(lambda: _held(lock), 2.0) is None


def test_one_owner_per_store_in_one_process() -> None:
    path = fresh_store_path()
    a, b = Database(path), Database(path)
    with a.embedding_worker_owner() as first, b.embedding_worker_owner() as second:
        assert (first, second) == (True, False)
    with a.curator_owner() as first, b.curator_owner() as second:
        assert (first, second) == (True, False)
        assert a.curator_lock_alive()

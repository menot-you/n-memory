"""The refusals and transaction rules of `nott_database`, each on a real SQLite file."""

from __future__ import annotations

import gc
import os
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import nott_database as db

pytest_plugins = ["pytester"]

DDL = (
    "CREATE TABLE t(id INTEGER PRIMARY KEY, v TEXT NOT NULL); PRAGMA user_version = 7;"
)


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "store" / "s.sqlite3"
    db.create(path, DDL)
    return path


def rows(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in db.query(conn, "SELECT v FROM t ORDER BY id")]


# -- create -----------------------------------------------------------------


def test_create_makes_private_directory_and_file(store: Path) -> None:
    assert store.parent.stat().st_mode & 0o777 == 0o700
    assert store.stat().st_mode & 0o777 == 0o600
    conn = db.connect(store)
    assert db.user_version(conn) == 7
    assert db.query(conn, "PRAGMA journal_mode") == [("wal",)]


def test_create_refuses_an_existing_file(store: Path) -> None:
    with pytest.raises(db.RefusedError, match="exists"):
        db.create(store, DDL)


def test_create_removes_the_file_when_the_ddl_fails(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        db.create(path, "CREATE TABLE broken(")
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_connect_never_creates(tmp_path: Path) -> None:
    with pytest.raises(db.RefusedError, match="missing"):
        db.connect(tmp_path / "absent.sqlite3")
    assert list(tmp_path.iterdir()) == []


# -- refusals ---------------------------------------------------------------


def test_refuses_a_symlinked_file(store: Path) -> None:
    link = store.parent / "link.sqlite3"
    link.symlink_to(store)
    with pytest.raises(db.RefusedError, match="is a symlink"):
        db.connect(link)


def test_refuses_a_symlinked_directory(store: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(store.parent)
    with pytest.raises(db.RefusedError, match="is a symlink"):
        db.connect(alias / store.name)


def test_refuses_a_root_owned_directory() -> None:
    if os.geteuid() == 0:
        pytest.skip("root owns the stand-in directory")
    with pytest.raises(db.RefusedError, match="owned by uid 0"):
        db.connect(Path("/") / "s.sqlite3")


def test_refuses_a_foreign_owned_file(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uid = os.geteuid()
    real = os.lstat

    def lstat(path: str | os.PathLike[str]) -> os.stat_result:
        st = real(path)
        if Path(path) == store:
            fields = list(st)
            fields[4] = uid + 1
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(db.os, "lstat", lstat)
    with pytest.raises(db.RefusedError, match=f"owned by uid {uid + 1}"):
        db.connect(store)


def test_refuses_a_wider_directory(store: Path) -> None:
    store.parent.chmod(0o750)
    with pytest.raises(db.RefusedError, match="mode 0o750"):
        db.connect(store)


def test_refuses_a_wider_file(store: Path) -> None:
    store.chmod(0o640)
    with pytest.raises(db.RefusedError, match="mode 0o640"):
        db.connect(store)


def test_refuses_a_directory_that_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("")
    with pytest.raises(db.RefusedError, match="not a directory"):
        db.connect(tmp_path / "f" / "s.sqlite3")


def test_refuses_a_file_that_is_a_directory(tmp_path: Path) -> None:
    (tmp_path / "d.sqlite3").mkdir(mode=0o700)
    with pytest.raises(db.RefusedError, match="not a regular file"):
        db.connect(tmp_path / "d.sqlite3")


def test_refuses_a_file_swapped_during_open(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = store.parent / "other.sqlite3"
    db.create(other, DDL)
    real = sqlite3.connect

    def swapping(*args: object, **kwargs: object) -> sqlite3.Connection:
        os.replace(other, store)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db.sqlite3, "connect", swapping)
    with pytest.raises(db.RefusedError, match="changed while it was opened"):
        db.connect(store)


def test_refuses_a_symlink_swapped_in_during_open(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The link resolves to the same inode, so only lstat sees the swap.
    moved = store.with_name("moved.sqlite3")
    real = sqlite3.connect

    def swapping(*args: object, **kwargs: object) -> sqlite3.Connection:
        os.replace(store, moved)
        store.symlink_to(moved)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db.sqlite3, "connect", swapping)
    with pytest.raises(db.RefusedError, match="changed while it was opened"):
        db.connect(store)


def test_refuses_a_hard_link_to_the_live_store(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live-store guard knows directories; a hard link carries the live file's
    # inode into any other private directory.
    monkeypatch.setattr(db, "live_directory", lambda: store.parent)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.link(store, elsewhere / "s.sqlite3")
    with pytest.raises(db.RefusedError, match="hard link"):
        db.connect(elsewhere / "s.sqlite3")


def test_planted_trigger_calling_load_extension_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    db.create(
        path,
        DDL + "CREATE TRIGGER planted AFTER INSERT ON t BEGIN "
        "SELECT load_extension('/nonexistent/evil'); END;",
    )
    conn = db.connect(path)
    assert db.query(conn, "PRAGMA trusted_schema") == [(0,)]
    with (
        pytest.raises(db.RefusedError, match="unsafe use of load_extension"),
        db.write(conn),
    ):
        conn.execute("INSERT INTO t(v) VALUES ('x')")
    assert rows(conn) == []


def test_extension_sql_function_stays_off_while_vec_loads(store: Path) -> None:
    conn = db.connect(store)
    db.load_vec(conn)
    assert db.query(conn, "SELECT vec_version()")[0][0].startswith("v")
    vector = struct.pack("3f", 1.0, 0.0, 0.0)
    assert db.query(conn, "SELECT vec_distance_cosine(?, ?)", (vector, vector)) == [
        (0.0,)
    ]
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        db.query(conn, "SELECT load_extension('/nonexistent/evil')")


def test_defensive_mode_blocks_writable_schema(store: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        conn.execute("PRAGMA writable_schema = ON")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("UPDATE sqlite_schema SET sql = 'x' WHERE name = 't'")


def test_opening_pragmas(store: Path) -> None:
    conn = db.connect(store)
    got = {
        name: db.query(conn, f"PRAGMA {name}")[0][0]
        for name in (
            "busy_timeout",
            "foreign_keys",
            "trusted_schema",
            "synchronous",
            "query_only",
            "journal_mode",
        )
    }
    assert got == {
        "busy_timeout": 10000,
        "foreign_keys": 1,
        "trusted_schema": 0,
        "synchronous": 2,
        "query_only": 1,
        "journal_mode": "wal",
    }
    assert conn.autocommit is True
    assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE) is True
    assert conn.getconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION) is False
    assert not conn.in_transaction


@pytest.mark.parametrize("readonly", [False, True])
def test_full_flush_pragmas_on_darwin_only(store: Path, readonly: bool) -> None:
    # macOS fsync(2) stops at the drive cache; SQLite asks for F_FULLFSYNC only when told.
    conn = db.connect(store, readonly=readonly)
    want = 1 if sys.platform == "darwin" else 0
    assert db.query(conn, "PRAGMA fullfsync")[0][0] == want
    assert db.query(conn, "PRAGMA checkpoint_fullfsync")[0][0] == want


def test_readonly_open_refuses_writes(store: Path) -> None:
    conn = db.connect(store, readonly=True)
    assert rows(conn) == []
    with pytest.raises(db.RefusedError), db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('x')")
    assert db.query(conn, "PRAGMA query_only") == [(1,)]


# -- live store -------------------------------------------------------------


_MEMORY = Path("/dev/shm")
_ON_MEMORY = _MEMORY.is_dir() and os.access(_MEMORY, os.W_OK)


def test_pytest_plugin_points_the_directory_at_the_test(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Per test, and not under the basetemp: `journal.sock` in it must fit AF_UNIX's
    104-byte path (108 on Linux) however deep pytest nests its temporary tree. On a
    host with a writable `/dev/shm` it is there, so commits skip the disk fsync."""
    configured = db.directory()
    assert configured.is_relative_to(_MEMORY if _ON_MEMORY else Path("/var/tmp"))
    assert configured != db.live_directory()
    assert not configured.is_relative_to(tmp_path_factory.getbasetemp())
    assert len(os.fsencode(configured.resolve() / "journal.sock")) < 104


def test_pytest_plugin_directory_is_private_and_removed_even_on_failure() -> None:
    from nott_database import pytest_plugin

    with pytest.raises(RuntimeError), pytest_plugin.private_directory() as folder:
        parent = folder.parent
        assert not folder.exists()
        assert parent.stat().st_mode & 0o777 == 0o700
        folder.mkdir()
        (folder / "journal.sock").write_text("")
        raise RuntimeError("the test failed")
    assert not parent.exists()


@pytest.mark.parametrize("ambient", [None, "false"])
def test_pytest_plugin_disables_telemetry_before_collection_and_in_children(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ambient: str | None
) -> None:
    if ambient is None:
        monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    else:
        monkeypatch.setenv("OTEL_SDK_DISABLED", ambient)
    pytester.makepyfile(
        """
        import os
        import subprocess
        import sys

        assert os.environ.get("OTEL_SDK_DISABLED") == "true"

        def test_child():
            result = subprocess.run(
                [sys.executable, "-c",
                 "import os; assert os.environ['OTEL_SDK_DISABLED'] == 'true'"],
                check=False, capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, result.stderr
        """
    )
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=1)


@pytest.mark.parametrize("marker", db.TEST_MARKERS)
def test_directory_refuses_to_default_under_a_test_marker(
    monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    # What a test's child inherits once it lost the variable: no fallback to live.
    monkeypatch.delenv("NOTT_DATABASE_DIR")
    for other in db.TEST_MARKERS:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(marker, "1")
    with pytest.raises(
        db.RefusedError, match=f"NOTT_DATABASE_DIR is unset under a test \\({marker}"
    ):
        db.directory()


def _production(monkeypatch: pytest.MonkeyPatch) -> None:
    """No directory and no test marker: what a production entry point sees."""
    monkeypatch.delenv("NOTT_DATABASE_DIR")
    for marker in db.TEST_MARKERS:
        monkeypatch.delenv(marker, raising=False)


@pytest.mark.skipif(not _ON_MEMORY, reason="no writable /dev/shm on this host")
def test_pytest_plugin_removes_the_memory_directory_after_the_test(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every commit is an fsync on disk (synchronous = FULL): 200 inserts took 2.0 s
    on btrfs and 0.03 s on tmpfs. The store lives in memory, and leaves nothing there."""
    pytester.makepyfile(
        """
        import os
        from pathlib import Path

        def test_open():
            folder = Path(os.environ["NOTT_DATABASE_DIR"])
            folder.mkdir(mode=0o700, parents=True)
            (folder / "store.db").write_bytes(b"x")
            Path(os.environ["PROBE"]).write_text(str(folder))
        """
    )
    probe = pytester.path / "probe"
    monkeypatch.setenv("PROBE", str(probe))
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=1)
    folder = Path(probe.read_text())
    assert folder.is_relative_to(_MEMORY)
    assert not folder.parent.exists()


def test_directory_defaults_to_the_passwd_home(monkeypatch: pytest.MonkeyPatch) -> None:
    import pwd

    _production(monkeypatch)
    monkeypatch.setenv("HOME", "/nonexistent")
    home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
    assert db.directory() == db.live_directory() == home / ".nott/state/database"


def test_directory_refuses_a_relative_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTT_DATABASE_DIR", "relative/dir")
    with pytest.raises(db.RefusedError, match="absolute"):
        db.directory()


def test_live_store_is_refused_under_pytest_even_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # What `cli.main` does with no directory set: the default, and the production flag.
    _production(monkeypatch)
    monkeypatch.setattr(db, "live_directory", lambda: tmp_path / "live")
    path = db.directory() / "nmemory.sqlite3"
    for call in (
        lambda: db.connect(path, allow_live_store=True),
        lambda: db.create(path, DDL, allow_live_store=True),
        lambda: db.lock(
            db.directory() / "x.lock", wait=0, allow_live_store=True
        ).__enter__(),
    ):
        with pytest.raises(db.RefusedError, match="under pytest"):
            call()


def test_live_identity_is_not_by_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "live"
    live.mkdir(mode=0o700)
    (tmp_path / "x").mkdir()
    monkeypatch.setattr(db, "live_directory", lambda: tmp_path / "x" / ".." / "live")
    db.create(tmp_path / "elsewhere" / "s.sqlite3", DDL)
    with pytest.raises(db.RefusedError, match="under pytest"):
        db.create(live / "s.sqlite3", DDL, allow_live_store=True)
    assert list(live.iterdir()) == []


def test_live_store_needs_the_production_flag_outside_pytest(tmp_path: Path) -> None:
    code = (
        "import sys, nott_database as db\n"
        "from pathlib import Path\n"
        "db.live_directory = lambda: Path(sys.argv[1])\n"
        "assert 'pytest' not in sys.modules\n"
        "try:\n"
        "    db.connect(db.live_directory() / 'nmemory.sqlite3')\n"
        "except db.RefusedError as e:\n"
        "    print(e)\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in (db.DIRECTORY_VARIABLE, *db.TEST_MARKERS)
    }
    env["HOME"] = str(tmp_path)
    out = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "live")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "allow_live_store" in out.stdout


# -- transactions -----------------------------------------------------------


def test_idle_connection_holds_no_transaction(store: Path) -> None:
    conn = db.connect(store)
    rows(conn)
    assert not conn.in_transaction


def test_write_outside_write_is_refused(store: Path) -> None:
    conn = db.connect(store)
    with pytest.raises(db.RefusedError, match="readonly"), db.mapped():
        conn.execute("INSERT INTO t(v) VALUES ('x')")


def test_write_commits_and_restores_query_only(store: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        assert db.query(conn, "PRAGMA query_only") == [(0,)]
        conn.execute("INSERT INTO t(v) VALUES ('a')")
    assert db.query(conn, "PRAGMA query_only") == [(1,)]
    assert rows(db.connect(store)) == ["a"]


def test_write_rolls_back_on_error(store: Path) -> None:
    conn = db.connect(store)
    with pytest.raises(RuntimeError), db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('a')")
        raise RuntimeError
    assert rows(conn) == []
    assert db.query(conn, "PRAGMA query_only") == [(1,)]
    assert not conn.in_transaction


def test_nested_write_rolls_back_alone(store: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('outer')")
        with pytest.raises(RuntimeError), db.write(conn):
            conn.execute("INSERT INTO t(v) VALUES ('inner')")
            raise RuntimeError
        with db.write(conn):
            conn.execute("INSERT INTO t(v) VALUES ('kept')")
        assert db.query(conn, "PRAGMA query_only") == [(0,)]
    assert rows(db.connect(store)) == ["outer", "kept"]


def test_outer_rollback_takes_the_released_savepoint(store: Path) -> None:
    conn = db.connect(store)
    with pytest.raises(RuntimeError), db.write(conn):
        with db.write(conn):
            conn.execute("INSERT INTO t(v) VALUES ('inner')")
        raise RuntimeError
    assert rows(conn) == []


def test_begin_and_finish_halves(store: Path) -> None:
    conn = db.connect(store)
    db.begin(conn)
    conn.execute("INSERT INTO t(v) VALUES ('a')")
    db.finish(conn, False)
    db.begin(conn)
    conn.execute("INSERT INTO t(v) VALUES ('b')")
    db.finish(conn, True)
    assert rows(conn) == ["b"]
    with pytest.raises(db.DatabaseError, match="without begin"):
        db.finish(conn, True)


def test_write_after_a_read(store: Path) -> None:
    reader = db.connect(store)
    other = db.connect(store)
    assert rows(reader) == []
    with db.write(other):
        other.execute("INSERT INTO t(v) VALUES ('other')")
    # A deferred transaction that read before the other commit would now fail with
    # SQLITE_BUSY_SNAPSHOT; an idle autocommit connection starts fresh.
    with db.write(reader):
        reader.execute("INSERT INTO t(v) VALUES ('mine')")
    assert rows(reader) == ["other", "mine"]


def test_unconsumed_query_leaves_no_read_transaction(store: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        conn.executemany("INSERT INTO t(v) VALUES (?)", [("a",), ("b",)])
    assert db.query(conn, "SELECT v FROM t LIMIT 1") == [("a",)]
    assert not conn.in_transaction
    other = db.connect(store)
    with db.write(other):
        other.execute("INSERT INTO t(v) VALUES ('c')")
    assert db.query(conn, "PRAGMA wal_checkpoint(TRUNCATE)")[0][0] == 0


def test_second_connection_in_the_same_thread_fails_at_once(store: Path) -> None:
    first = db.connect(store)
    second = db.connect(store)
    with db.write(first):
        started = time.monotonic()
        with pytest.raises(db.BusyError, match="same thread"):
            db.begin(second)
        assert time.monotonic() - started < 1.0
        assert db.query(second, "PRAGMA query_only") == [(1,)]
    with db.write(second):
        second.execute("INSERT INTO t(v) VALUES ('after')")
    assert rows(second) == ["after"]


def test_closing_a_holder_releases_the_same_thread_claim(store: Path) -> None:
    first = db.connect(store)
    db.begin(first)
    first.close()
    second = db.connect(store)
    with db.write(second):
        second.execute("INSERT INTO t(v) VALUES ('x')")
    assert rows(second) == ["x"]


def test_a_shared_connection_is_usable_from_another_thread_only_when_asked(
    store: Path,
) -> None:
    """The nSPAWN supervisor shares one connection under its own lock."""
    confined = db.connect(store)
    shared = db.connect(store, check_same_thread=False)
    seen: list[object] = []

    def other() -> None:
        for conn in (confined, shared):
            try:
                seen.append(db.query(conn, "SELECT 1")[0][0])
            except sqlite3.ProgrammingError:
                seen.append("refused")

    thread = threading.Thread(target=other)
    thread.start()
    thread.join(5)
    assert seen == ["refused", 1]


def test_another_thread_waits_for_the_holder(store: Path) -> None:
    first = db.connect(store)
    done: list[str] = []

    def other() -> None:
        conn = db.connect(store)
        with db.write(conn):
            conn.execute("INSERT INTO t(v) VALUES ('second')")
        done.append("ok")

    with db.write(first):
        first.execute("INSERT INTO t(v) VALUES ('first')")
        thread = threading.Thread(target=other)
        thread.start()
        time.sleep(0.2)
        assert done == []
    thread.join(5)
    assert done == ["ok"]
    assert rows(first) == ["first", "second"]


def test_another_process_is_busy_after_the_timeout(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 100)
    holder = db.connect(store)
    code = (
        "import sys, nott_database as db\n"
        "db.BUSY_TIMEOUT_MS = 100\n"
        "c = db.connect(sys.argv[1])\n"
        "try:\n"
        "    db.begin(c)\n"
        "except db.BusyError:\n"
        "    print('busy')\n"
    )
    with db.write(holder):
        out = subprocess.run(
            [sys.executable, "-c", code, str(store)],
            capture_output=True,
            text=True,
            check=True,
        )
    assert out.stdout.strip() == "busy"


def test_mapping() -> None:
    def error(code: int, message: str) -> sqlite3.Error:
        e = sqlite3.OperationalError(message)
        e.sqlite_errorcode = code
        return e

    assert isinstance(db.translate(error(5, "locked")), db.BusyError)
    assert isinstance(db.translate(error(517, "busy snapshot")), db.BusyError)
    assert isinstance(db.translate(error(6, "table locked")), db.BusyError)
    assert isinstance(db.translate(error(8, "readonly")), db.RefusedError)
    assert isinstance(db.translate(error(23, "auth")), db.RefusedError)
    assert isinstance(db.translate(error(1, "unsafe use of x()")), db.RefusedError)
    assert isinstance(db.translate(error(11, "corrupt")), db.SchemaError)
    assert isinstance(db.translate(error(26, "not a database")), db.SchemaError)
    full = error(sqlite3.SQLITE_FULL, "database or disk is full")
    assert db.translate(full) is full
    assert isinstance(db.translate(full), db.Error)
    plain = error(1, "no such table: x")
    assert db.translate(plain) is plain
    assert (
        db.translate(sqlite3.ProgrammingError("closed")).__class__
        is sqlite3.ProgrammingError
    )


def test_corrupt_file_is_a_schema_error(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.write(fd, b"not a database, " * 512)
    os.close(fd)
    with pytest.raises(db.SchemaError):
        db.connect(path)


# -- snapshot ---------------------------------------------------------------


def test_snapshot_under_query_only(store: Path, tmp_path: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('a')")
    assert db.query(conn, "PRAGMA query_only") == [(1,)]
    dest = tmp_path / "snap.sqlite3"
    db.snapshot(conn, dest)
    assert dest.stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["snap.sqlite3", "store"]
    copy = sqlite3.connect(f"{dest.as_uri()}?mode=ro", uri=True)
    assert copy.execute("SELECT v FROM t").fetchall() == [("a",)]
    assert copy.execute("PRAGMA user_version").fetchone() == (7,)
    assert copy.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    copy.close()


def test_snapshot_from_a_read_only_handle_is_one_private_checked_file(
    store: Path, tmp_path: Path
) -> None:
    """Every caller passes `readonly=True`; the copy is 0600, a rollback-journal file
    with no sidecar beside it, and passes `integrity_check`."""
    out = tmp_path / "out"
    out.mkdir(mode=0o700)
    conn = db.connect(store, readonly=True)
    db.snapshot(conn, out / "snap.sqlite3")
    assert db.query(conn, "PRAGMA query_only") == [(1,)]
    conn.close()
    dest = out / "snap.sqlite3"
    assert dest.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in out.iterdir()] == ["snap.sqlite3"]
    assert dest.read_bytes()[18:20] == b"\x01\x01"  # not WAL
    copy = sqlite3.connect(f"{dest.as_uri()}?mode=ro", uri=True)
    assert copy.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    copy.close()


def test_snapshot_group_read(store: Path, tmp_path: Path) -> None:
    dest = tmp_path / "snap.sqlite3"
    db.snapshot(db.connect(store), dest, group_read=True)
    assert dest.stat().st_mode & 0o777 == 0o640


def test_group_read_snapshot_into_a_group_readable_directory(
    store: Path, tmp_path: Path
) -> None:
    # Plan section 10: another account's snapshots land in a 0750 directory whose
    # ACL names the owner, and are widened to 0640 after the check.
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o750)
    shared.chmod(0o750)
    dest = shared / "snap.sqlite3"
    db.snapshot(db.connect(store), dest, group_read=True)
    assert dest.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in shared.iterdir()) == ["snap.sqlite3"]
    with pytest.raises(db.RefusedError, match="wider than private"):
        db.snapshot(db.connect(store), shared / "private.sqlite3")


@pytest.mark.parametrize("mode", [0o770, 0o751, 0o754])
def test_group_read_snapshot_refuses_group_write_or_others(
    store: Path, tmp_path: Path, mode: int
) -> None:
    wide = tmp_path / "wide"
    wide.mkdir()
    wide.chmod(mode)
    with pytest.raises(db.RefusedError, match="wider than"):
        db.snapshot(db.connect(store), wide / "snap.sqlite3", group_read=True)
    assert list(wide.iterdir()) == []


def test_snapshot_refuses_an_existing_destination(store: Path, tmp_path: Path) -> None:
    dest = tmp_path / "snap.sqlite3"
    dest.symlink_to(store)
    with pytest.raises(db.RefusedError, match="exists"):
        db.snapshot(db.connect(store), dest)
    assert dest.is_symlink()


def test_snapshot_failing_its_check_leaves_nothing(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "_integrity", lambda conn: ["row 1 missing"])
    dest = tmp_path / "snap.sqlite3"
    with pytest.raises(db.SchemaError, match="row 1 missing"):
        db.snapshot(db.connect(store), dest)
    assert not dest.exists()


def test_snapshot_into_the_live_directory_is_refused(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    live = tmp_path / "live"
    live.mkdir(mode=0o700)
    source = db.connect(store)
    monkeypatch.setattr(db, "live_directory", lambda: live)
    with pytest.raises(db.RefusedError, match="under pytest"):
        db.snapshot(source, live / "snap.sqlite3")
    assert list(live.iterdir()) == []


def test_remove_in_the_live_directory_is_refused_before_anything_goes(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-09-26 20:13: a test helper run by hand in an nSPAWN child, whose
    # NOTT_DATABASE_DIR is the live directory, deleted the live nmemory store and
    # only then reached the guard that refuses creating a store there.
    monkeypatch.setattr(db, "live_directory", lambda: store.parent)
    with pytest.raises(db.RefusedError, match="live store"):
        db.remove(store)
    assert store.is_file()


def test_remove_deletes_a_store_and_its_sidecars(store: Path) -> None:
    sidecars = [
        Path(f"{store}-{end}") for end in ("wal", "shm", "journal", "write.lock")
    ]
    for sidecar in sidecars:
        sidecar.touch()
    db.remove(store)
    assert [p for p in [store, *sidecars] if p.exists()] == []


def test_snapshot_refuses_a_wider_directory(store: Path, tmp_path: Path) -> None:
    wide = tmp_path / "wide"
    wide.mkdir()
    wide.chmod(0o755)
    with pytest.raises(db.RefusedError, match="wider than private"):
        db.snapshot(db.connect(store), wide / "snap.sqlite3")
    assert list(wide.iterdir()) == []


def test_snapshot_opens_its_target_hardened(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []
    real = db._integrity

    def spy(conn: sqlite3.Connection) -> list[str]:
        seen.append(
            (
                db.query(conn, "PRAGMA trusted_schema")[0][0],
                conn.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE),
                conn.getconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION),
            )
        )
        return real(conn)

    monkeypatch.setattr(db, "_integrity", spy)
    db.snapshot(db.connect(store), tmp_path / "snap.sqlite3")
    assert seen == [(0, True, False)]


def test_snapshot_refuses_an_open_write(store: Path, tmp_path: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('uncommitted')")
        with pytest.raises(db.RefusedError, match="open transaction"):
            db.snapshot(conn, tmp_path / "snap.sqlite3")
    assert not (tmp_path / "snap.sqlite3").exists()


# -- lock -------------------------------------------------------------------


def test_lock_excludes_and_times_out(tmp_path: Path) -> None:
    path = tmp_path / "election.lock"
    with db.lock(path, wait=0):
        assert path.stat().st_mode & 0o777 == 0o600
        started = time.monotonic()
        with pytest.raises(db.BusyError, match="held"), db.lock(path, wait=0.2):
            pass
        assert 0.2 <= time.monotonic() - started < 1.0
    with db.lock(path, wait=0):
        pass


def test_lock_passes_to_a_waiter_when_released(tmp_path: Path) -> None:
    path = tmp_path / "election.lock"
    got: list[float] = []

    def waiter() -> None:
        with db.lock(path, wait=5):
            got.append(time.monotonic())

    with db.lock(path, wait=0):
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.2)
        released = time.monotonic()
    thread.join(5)
    assert got and got[0] - released < 0.5


def test_lock_refuses_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real.lock").write_text("")
    (tmp_path / "x.lock").symlink_to(tmp_path / "real.lock")
    with (
        pytest.raises(db.RefusedError, match="is a symlink"),
        db.lock(tmp_path / "x.lock", wait=0),
    ):
        pass


def test_lock_refuses_a_wider_file(tmp_path: Path) -> None:
    path = tmp_path / "x.lock"
    path.write_text("")
    path.chmod(0o644)
    with pytest.raises(db.RefusedError, match="mode 0o644"), db.lock(path, wait=0):
        pass


def test_live_store_needs_the_production_flag_in_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _production(monkeypatch)
    monkeypatch.setattr(db, "live_directory", lambda: tmp_path / "live")
    monkeypatch.delitem(sys.modules, "pytest")
    with pytest.raises(db.RefusedError, match="allow_live_store"):
        db.connect(db.live_directory() / "nmemory.sqlite3")


def test_refuses_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(db.RefusedError, match="nope is missing"):
        db.connect(tmp_path / "nope" / "s.sqlite3")


# Each refusal a person can cure names its cure in the same message (2026-10-01:
# a fresh NOTT_DATABASE_DIR met two refusals in a row, neither saying what to run).


def test_a_wide_directory_names_its_chmod(tmp_path: Path) -> None:
    folder = tmp_path / "wide"
    folder.mkdir()
    folder.chmod(0o755)
    with pytest.raises(db.RefusedError) as refused:
        db.connect(folder / "nmemory.sqlite3")
    assert str(refused.value) == (
        f"{folder} has mode 0o755, wider than private; run `chmod 700 {folder}`"
    )


def test_a_wide_file_names_its_chmod(store: Path) -> None:
    store.chmod(0o644)
    with pytest.raises(db.RefusedError) as refused:
        db.connect(store)
    assert str(refused.value) == (
        f"{store} has mode 0o644, wider than private; run `chmod 600 {store}`"
    )


@pytest.mark.parametrize(
    ("name", "creator"),
    [
        ("nmemory.sqlite3", "nmemory database create"),
        ("nspawn.sqlite3", "nspawn database create"),
        ("brake.sqlite3", "nott brake off"),
    ],
)
def test_a_missing_store_names_its_creation_command(
    tmp_path: Path, name: str, creator: str
) -> None:
    folder = tmp_path / "fresh"
    folder.mkdir(mode=0o700)
    with pytest.raises(db.RefusedError) as refused:
        db.connect(folder / name)
    assert str(refused.value) == (
        f"{folder / name} is missing; create it with `{creator}`"
    )
    # The same command makes the directory too (`create` does the mkdir).
    with pytest.raises(db.RefusedError) as refused:
        db.connect(tmp_path / "absent" / name)
    assert str(refused.value) == (
        f"{tmp_path / 'absent'} is missing; create it with `{creator}`"
    )


def test_a_missing_unknown_store_names_the_generic_verb(tmp_path: Path) -> None:
    with pytest.raises(db.RefusedError) as refused:
        db.connect(tmp_path / "s.sqlite3")
    assert str(refused.value) == (
        f"{tmp_path / 's.sqlite3'} is missing;"
        " create it with its owner's `database create` command"
    )


def test_busy_begin_restores_query_only(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "BUSY_TIMEOUT_MS", 50)
    waiter = db.connect(store)
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        conn = db.connect(store)
        with db.write(conn):
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    holding.wait(5)
    try:
        with pytest.raises(db.BusyError):
            db.begin(waiter)
        assert waiter.depth == 0
        assert db.query(waiter, "PRAGMA query_only") == [(1,)]
    finally:
        release.set()
        thread.join(5)


def test_failed_commit_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "s.sqlite3"
    db.create(
        path,
        "CREATE TABLE p(id INTEGER PRIMARY KEY);"
        "CREATE TABLE c(p INTEGER REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);",
    )
    conn = db.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), db.write(conn):
        conn.execute("INSERT INTO c(p) VALUES (42)")
    assert not conn.in_transaction
    assert db.query(conn, "SELECT count(*) FROM c") == [(0,)]
    assert db.query(conn, "PRAGMA query_only") == [(1,)]
    with db.write(conn):
        conn.execute("INSERT INTO p(id) VALUES (1)")


# -- fair admission ---------------------------------------------------------

HOG = (
    "import sys, time, nott_database as db\n"
    "c = db.connect(sys.argv[1])\n"
    "until = time.monotonic() + float(sys.argv[2])\n"
    "print('ready', flush=True)\n"
    "while time.monotonic() < until:\n"
    "    with db.write(c):\n"
    "        c.execute(\"INSERT INTO t(v) VALUES ('hog')\")\n"
    "        time.sleep(0.005)\n"
)


def test_a_waiter_is_admitted_while_others_retake_the_lock(store: Path) -> None:
    # Eight processes commit back to back and ask again at once. SQLite's busy
    # handler sleeps and polls, so the one that just committed wins again and a
    # waiter can starve for the whole busy timeout; admission must be bounded.
    hogs = [
        subprocess.Popen(
            [sys.executable, "-c", HOG, str(store), "4"],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    try:
        for hog in hogs:
            assert hog.stdout is not None
            assert hog.stdout.readline() == "ready\n"
        time.sleep(0.2)
        conn = db.connect(store)
        waits = []
        for _ in range(10):
            started = time.monotonic()
            with db.write(conn):
                waits.append(time.monotonic() - started)
                conn.execute("INSERT INTO t(v) VALUES ('waiter')")
        assert max(waits) < 1.0, waits
    finally:
        for hog in hogs:
            hog.wait(10)
            assert hog.stdout is not None
            hog.stdout.close()
    assert all(hog.returncode == 0 for hog in hogs)


def test_a_nested_write_does_not_wait_for_admission(store: Path) -> None:
    conn = db.connect(store)
    with db.write(conn):
        started = time.monotonic()
        with db.write(conn):
            conn.execute("INSERT INTO t(v) VALUES ('inner')")
        assert time.monotonic() - started < 0.5
    assert rows(conn) == ["inner"]


def test_admission_wait_is_the_busy_timeout(store: Path) -> None:
    holder = db.connect(store)
    code = (
        "import sys, time, nott_database as db\n"
        "db.BUSY_TIMEOUT_MS = 300\n"
        "c = db.connect(sys.argv[1])\n"
        "t = time.monotonic()\n"
        "try:\n"
        "    db.begin(c)\n"
        "except db.BusyError:\n"
        "    print('busy', round(time.monotonic() - t, 2))\n"
        "    print(db.query(c, 'PRAGMA query_only')[0][0], c.depth)\n"
    )
    with db.write(holder):
        out = subprocess.run(
            [sys.executable, "-c", code, str(store)],
            capture_output=True,
            text=True,
            check=True,
        )
    word, waited, state = out.stdout.split(maxsplit=2)
    assert word == "busy"
    assert 0.25 <= float(waited) < 1.0
    assert state.split() == ["1", "0"]
    with db.write(db.connect(store)):
        pass


def test_a_zero_busy_timeout_refuses_admission_at_once(store: Path) -> None:
    # A caller that sets busy_timeout = 0 asks for a refusal, never a wait; the
    # admission flock must honour it, keep it across writes and leave no waiter.
    conn = db.connect(store)
    conn.execute("PRAGMA busy_timeout = 0")
    with db.write(conn):
        pass
    assert db.query(conn, "PRAGMA busy_timeout") == [(0,)]
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        holder = db.connect(store)
        try:
            with db.write(holder):
                holding.set()
                release.wait(5)
        finally:
            holder.close()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert holding.wait(5)
        started = time.monotonic()
        with pytest.raises(db.BusyError):
            db.begin(conn)
        assert time.monotonic() - started < 0.5
        assert conn.depth == 0
        assert db.query(conn, "PRAGMA busy_timeout") == [(0,)]
    finally:
        release.set()
        thread.join(5)
    # The refusal left nothing queued on the lock that could refuse the next writer.
    with db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('after')")
    assert rows(conn) == ["after"]


def test_a_dropped_writer_releases_its_admission_and_claim(store: Path) -> None:
    conn = db.connect(store)
    db.begin(conn)
    del conn
    gc.collect()
    other = db.connect(store)
    other.execute("PRAGMA busy_timeout = 500")
    with db.write(other):
        other.execute("INSERT INTO t(v) VALUES ('after')")
    assert rows(other) == ["after"]


def test_a_write_finished_from_another_thread_releases_the_claim(
    store: Path,
) -> None:
    shared = db.connect(store, check_same_thread=False)
    db.begin(shared)
    thread = threading.Thread(target=db.finish, args=(shared, True))
    thread.start()
    thread.join(5)
    other = db.connect(store)
    with db.write(other):
        other.execute("INSERT INTO t(v) VALUES ('after')")
    assert rows(other) == ["after"]


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_retrying_a_held_write_leaves_no_waiters_behind(store: Path) -> None:
    # A short busy_timeout: every refused begin() must not
    # leave another thread and descriptor queued in the kernel.
    holding, release = threading.Event(), threading.Event()

    def hold() -> None:
        holder = db.connect(store)
        try:
            with db.write(holder):
                holding.set()
                release.wait(10)
        finally:
            holder.close()

    thread = threading.Thread(target=hold)
    thread.start()
    conn = db.connect(store)
    conn.execute("PRAGMA busy_timeout = 5")
    try:
        assert holding.wait(5)
        threads, fds = threading.active_count(), _open_fds()
        for _ in range(100):
            with pytest.raises(db.BusyError):
                db.begin(conn)
        assert threading.active_count() <= threads + 1
        assert _open_fds() <= fds + 1
    finally:
        release.set()
        thread.join(5)
    conn.execute("PRAGMA busy_timeout = 2000")
    with db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('after')")
    assert rows(conn) == ["after"]


def _interrupt_after(monkeypatch: pytest.MonkeyPatch, sql: str) -> None:
    real = db.Connection.execute

    def interrupting(self: db.Connection, text: str, *args: object) -> object:
        result = real(self, text, *args)  # type: ignore[arg-type]
        if text == sql:
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(db.Connection, "execute", interrupting)


def _no_write_left(conn: db.Connection, store: Path) -> None:
    assert not conn.in_transaction
    assert conn.depth == 0
    assert db.query(conn, "PRAGMA query_only") == [(1,)]
    other = db.connect(store)
    other.execute("PRAGMA busy_timeout = 500")
    with db.write(other):
        other.execute("INSERT INTO t(v) VALUES ('after')")
    other.close()


def test_an_interrupt_after_begin_immediate_leaves_no_transaction(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(store)
    _interrupt_after(monkeypatch, "BEGIN IMMEDIATE")
    with pytest.raises(KeyboardInterrupt):
        db.begin(conn)
    monkeypatch.undo()
    _no_write_left(conn, store)


def test_an_interrupt_before_commit_leaves_no_transaction(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = db.connect(store)
    db.begin(conn)
    conn.execute("INSERT INTO t(v) VALUES ('lost')")
    real = db._release_admission
    calls: list[None] = []

    def interrupted(c: db.Connection) -> None:
        # The interrupt lands once, right after admission is handed on.
        real(c)
        calls.append(None)
        if len(calls) == 1:
            raise KeyboardInterrupt

    monkeypatch.setattr(db, "_release_admission", interrupted)
    with pytest.raises(KeyboardInterrupt):
        db.finish(conn, True)
    monkeypatch.undo()
    _no_write_left(conn, store)
    assert rows(conn) == ["after"]


# -- the caller's row factory -------------------------------------------------


def _dict_row(cursor: sqlite3.Cursor, row: tuple[object, ...]) -> dict[str, object]:
    return {column[0]: value for column, value in zip(cursor.description, row)}


@pytest.mark.parametrize("factory", [_dict_row, sqlite3.Row], ids=["dict", "Row"])
def test_internal_reads_ignore_the_callers_row_factory(
    store: Path, tmp_path: Path, factory: object
) -> None:
    conn = db.connect(store)
    conn.row_factory = factory  # type: ignore[assignment]
    with db.write(conn):
        conn.execute("INSERT INTO t(v) VALUES ('a')")
        with db.write(conn):
            conn.execute("INSERT INTO t(v) VALUES ('b')")
    db.begin(conn)
    conn.execute("INSERT INTO t(v) VALUES ('c')")
    db.finish(conn, True)
    assert db.user_version(conn) == 7
    db.snapshot(conn, tmp_path / "snap.sqlite3")
    with db.lock(tmp_path / "x.lock", wait=0):
        pass
    conn.row_factory = None
    assert rows(conn) == ["a", "b", "c"]
    assert db.query(conn, "PRAGMA query_only") == [(1,)]


def test_raw_errors_are_the_sqlite3_classes() -> None:
    assert (db.Error, db.IntegrityError, db.OperationalError) == (
        sqlite3.Error,
        sqlite3.IntegrityError,
        sqlite3.OperationalError,
    )


def test_row_type_is_the_sqlite3_class() -> None:
    assert db.Row is sqlite3.Row

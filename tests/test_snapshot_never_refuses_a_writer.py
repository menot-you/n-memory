"""The hourly snapshot never refuses a writer (2026-10-03: every nMEMORY write in the
snapshot's window failed after 10 s on the `-write.lock` the in-place VACUUM held)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import nott_database
import pytest
from nott_database import BusyError
from storefile import default_store_path

from nmemory import cli
from nmemory.commands.database import cmd_database_snapshot


def test_a_snapshot_runs_while_a_writer_holds_the_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The snapshot neither takes nor waits for the writers' admission lock."""
    path = default_store_path()
    monkeypatch.setattr(nott_database, "BUSY_TIMEOUT_MS", 0)
    dest = tmp_path / "copy.sqlite3"
    with nott_database.lock(Path(f"{path}-write.lock"), wait=0):  # a writer mid-write
        cmd_database_snapshot([str(dest)])
    assert dest.stat().st_size > 0


def test_a_writer_commits_while_the_snapshot_copies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A write issued from inside the copy (a progress handler on every connection the
    snapshot opens, armed once its VACUUM statement starts) is admitted at once and
    commits; it is not refused."""
    path = default_store_path()
    monkeypatch.setattr(nott_database, "BUSY_TIMEOUT_MS", 0)
    writer = nott_database.connect(path)
    with nott_database.write(writer):
        writer.execute("CREATE TABLE probe_writes (n INTEGER)")
    outcomes: list[str] = []
    copying: list[str] = []
    real_connect = nott_database.connect

    def write_once() -> int:
        if copying and not outcomes:
            try:
                with nott_database.write(writer):
                    writer.execute("INSERT INTO probe_writes VALUES (1)")
                outcomes.append("committed")
            except BusyError as error:
                outcomes.append(f"refused: {error}")
        return 0

    def opened(target: Path, **options: bool) -> nott_database.Connection:
        conn = real_connect(target, **options)
        conn.set_trace_callback(lambda sql: "VACUUM" in sql and copying.append(sql))
        conn.set_progress_handler(write_once, 10)
        return conn

    monkeypatch.setattr(nott_database, "connect", opened)
    try:
        cmd_database_snapshot([str(tmp_path / "copy.sqlite3")])
    finally:
        writer.close()
    assert outcomes == ["committed"]


def test_a_snapshot_leaves_the_store_and_its_wal_unchanged(tmp_path: Path) -> None:
    """The copy is read from the store, never written back into it: the main file and
    the WAL a live writer keeps open hold the same bytes before and after."""
    path = default_store_path()
    writer = nott_database.connect(path)
    try:
        with nott_database.write(writer):
            writer.execute("CREATE TABLE probe_writes (n INTEGER)")
            writer.executemany(
                "INSERT INTO probe_writes VALUES (?)", [(n,) for n in range(500)]
            )
        wal = Path(f"{path}-wal")
        before = (path.read_bytes(), wal.read_bytes())
        assert before[1], "the fixture needs a WAL with frames in it"
        cmd_database_snapshot([str(tmp_path / "copy.sqlite3")])
        assert (path.read_bytes(), wal.read_bytes()) == before
    finally:
        writer.close()


def test_a_snapshot_with_no_other_connection_keeps_the_retained_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With nobody else on the store, the last close of a read-write handle checkpoints
    the WAL into the store and deletes it under an exclusive lock (716 MB live). The
    verb opens no such handle: the store and its WAL keep their bytes, and it runs
    while a writer holds the admission."""
    path = default_store_path()
    wal = Path(f"{path}-wal")
    writer = nott_database.connect(path)
    with nott_database.write(writer):
        writer.execute("CREATE TABLE probe_writes (n INTEGER)")
        writer.executemany(
            "INSERT INTO probe_writes VALUES (?)", [(n,) for n in range(500)]
        )
    retained = (path.read_bytes(), wal.read_bytes())
    writer.close()  # its own last close checkpoints and deletes the WAL, so put both
    path.write_bytes(retained[0])  # files back as a crashed writer leaves them
    fd = os.open(wal, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.write(fd, retained[1])
    os.close(fd)
    monkeypatch.setattr(nott_database, "BUSY_TIMEOUT_MS", 0)
    dest = tmp_path / "copy.sqlite3"
    monkeypatch.setattr(sys, "argv", ["nmemory", "database", "snapshot", str(dest)])
    with nott_database.lock(Path(f"{path}-write.lock"), wait=0):
        try:
            cli.main()
            code = 0
        except SystemExit as exit_:
            code = int(exit_.code or 0)
    assert code == 0
    assert wal.stat().st_size == len(retained[1])
    assert (path.read_bytes(), wal.read_bytes()) == retained
    copy = nott_database.connect(dest, readonly=True)
    try:
        assert nott_database.query(copy, "SELECT count(*) FROM probe_writes") == [
            (500,)
        ]
    finally:
        copy.close()

"""Keep database writes and cleanup inside the open disposable directory."""

from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

import nott_database as db


def test_descriptor_keeps_sqlite_files_and_locks_after_an_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder, outside = tmp_path / "store", tmp_path / "account"
    folder.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    for suffix in ("", "-wal", "-shm", "-journal", "-write.lock"):
        (outside / f"s.sqlite3{suffix}").write_bytes(b"account sentinel")
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        path = db.directory_bind(descriptor) / "s.sqlite3"
        db.create(path, "CREATE TABLE t(v);")
        with closing(db.connect(path)) as conn:
            assert db.query(conn, "PRAGMA database_list")[0][2] == str(path)
            folder.rename(tmp_path / "reserved")
            folder.symlink_to(outside, target_is_directory=True)
            with db.write(conn):
                conn.execute("INSERT INTO t VALUES (7)")
            assert {p.name for p in (tmp_path / "reserved").iterdir()} == {
                "s.sqlite3",
                "s.sqlite3-wal",
                "s.sqlite3-shm",
                "s.sqlite3-write.lock",
            }
        with closing(db.connect(path)) as reopened:
            assert db.query(reopened, "SELECT v FROM t") == [(7,)]
            with db.lock(path.parent / "worker.lock", wait=0):
                assert (tmp_path / "reserved/worker.lock").is_file()
        db.remove(path)
        assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
        assert sorted(p.name for p in (tmp_path / "reserved").iterdir()) == [
            "worker.lock"
        ]
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("failure", ["schema", "open"])
def test_descriptor_creation_failure_cleans_only_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    folder, outside = tmp_path / "store", tmp_path / "account"
    folder.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    for suffix in ("", "-wal", "-shm", "-journal", "-write.lock"):
        (outside / f"s.sqlite3{suffix}").write_bytes(b"keep")
    before = {p.name: p.read_bytes() for p in outside.iterdir()}
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    original = db.connect

    def connect(path: Path, **kwargs: Any) -> db.Connection:
        folder.rename(tmp_path / "reserved")
        folder.symlink_to(outside, target_is_directory=True)
        if failure == "open":
            raise db.RefusedError("The test interrupts the open.")
        return original(path, **kwargs)

    try:
        path = db.directory_bind(descriptor) / "s.sqlite3"
        monkeypatch.setattr(db, "connect", connect)
        with pytest.raises((db.Error, db.RefusedError)):
            db.create(path, "invalid SQL")
        assert list((tmp_path / "reserved").iterdir()) == []
        assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
    finally:
        os.close(descriptor)


def test_descriptor_keeps_live_store_and_mode_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "store"
    folder.mkdir(mode=0o700)
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        folder.chmod(0o750)
        with pytest.raises(db.RefusedError, match="wider than private"):
            db.directory_bind(descriptor)
        folder.chmod(0o700)
        monkeypatch.setattr(db, "live_directory", lambda: folder)
        with pytest.raises(db.RefusedError, match="live store"):
            db.directory_bind(descriptor)
    finally:
        os.close(descriptor)

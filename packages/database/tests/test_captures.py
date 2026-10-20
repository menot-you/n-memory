"""The capture root: PNG files named by their sha256 beside the store directory."""

from __future__ import annotations

import hashlib
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nott_database as db
from nott_database import captures

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def root() -> Path:
    return db.directory().parent / "captures"


def test_keep_read_and_clear_round_trip_in_a_private_root() -> None:
    ident = captures.keep(PNG)
    assert ident == hashlib.sha256(PNG).hexdigest()
    assert captures.root() == root()
    assert stat.S_IMODE(os.lstat(root()).st_mode) == 0o700
    path = root() / f"{ident}.png"
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    assert path.read_bytes() == PNG
    assert sorted(p.name for p in root().iterdir()) == [f"{ident}.png"]
    assert captures.keep(PNG) == ident
    assert captures.read(ident, 0, 10) == (PNG[:10], len(PNG))
    assert captures.read(ident, 1000, 100) == (PNG[1000:], len(PNG))
    assert captures.read(ident, len(PNG), 100) == (b"", len(PNG))
    with pytest.raises(captures.RefusedError, match="past the end"):
        captures.read(ident, len(PNG) + 1, 1)
    (root() / "notes.txt").write_text("kept")
    assert captures.clear() == 1
    assert sorted(p.name for p in root().iterdir()) == ["notes.txt"]
    with pytest.raises(captures.Missing):
        captures.read(ident, 0, 1)


def test_clear_without_a_root_removes_nothing() -> None:
    assert not root().exists()
    assert captures.clear() == 0


def test_a_full_cache_refuses_new_captures_without_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(captures, "COUNT", 2, raising=False)
    first = captures.keep(PNG)
    second = captures.keep(PNG + b"second")
    assert captures.keep(PNG) == first
    with pytest.raises(captures.RefusedError, match="cache is full"):
        captures.keep(PNG + b"third")
    assert captures.read(first, 0, len(PNG))[0] == PNG
    assert captures.read(second, 0, len(PNG) + 6)[0] == PNG + b"second"
    assert captures.clear() == 2
    captures.keep(PNG + b"third")


def test_concurrent_writers_cannot_exceed_the_cache_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(captures, "COUNT", 2)

    def keep(index: int) -> bool:
        try:
            captures.keep(PNG + bytes([index]))
        except captures.RefusedError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        kept = list(pool.map(keep, range(8)))
    assert sum(kept) == 2
    assert len(list(captures.root().iterdir())) == 2


def test_interrupted_writes_use_cache_capacity_until_explicit_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(captures, "COUNT", 1)
    folder = captures.root()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / ".capture-interrupted").write_bytes(PNG)
    with pytest.raises(captures.RefusedError, match="cache is full"):
        captures.keep(PNG)
    assert captures.clear() == 0
    assert captures.keep(PNG)


@pytest.mark.parametrize("size", [0, captures.LIMIT + 1])
def test_keep_refuses_empty_or_oversized_bytes(size: int) -> None:
    with pytest.raises(captures.RefusedError, match="bytes"):
        captures.keep(b"x" * size)
    assert not (root() / f"{hashlib.sha256(b'x' * size).hexdigest()}.png").exists()


@pytest.mark.parametrize("ident", ["A" * 64, "a" * 63, "../x", "", "a" * 64 + "/"])
def test_a_malformed_id_is_refused(ident: str) -> None:
    captures.keep(PNG)
    with pytest.raises(captures.RefusedError, match="64 lowercase hex"):
        captures.read(ident, 0, 1)


def test_a_symlinked_file_is_refused_and_keep_replaces_only_the_link(
    tmp_path: Path,
) -> None:
    ident = captures.keep(PNG)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"secret")
    path = root() / f"{ident}.png"
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(captures.RefusedError, match="link"):
        captures.read(ident, 0, 1)
    assert captures.keep(PNG) == ident
    assert outside.read_bytes() == b"secret"
    assert not path.is_symlink() and path.read_bytes() == PNG
    assert captures.clear() == 1
    assert outside.read_bytes() == b"secret"


def test_a_hard_linked_file_is_refused(tmp_path: Path) -> None:
    ident = captures.keep(PNG)
    os.link(root() / f"{ident}.png", root() / "other")
    with pytest.raises(captures.RefusedError, match="hard links"):
        captures.read(ident, 0, 1)


def test_a_wide_file_is_refused() -> None:
    ident = captures.keep(PNG)
    os.chmod(root() / f"{ident}.png", 0o644)
    with pytest.raises(captures.RefusedError, match="wider than private"):
        captures.read(ident, 0, 1)


def test_a_symlinked_root_is_refused_and_nothing_is_written_through_it(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    root().parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root().symlink_to(elsewhere)
    with pytest.raises(captures.RefusedError, match="link"):
        captures.keep(PNG)
    with pytest.raises(captures.RefusedError, match="link"):
        captures.clear()
    (elsewhere / f"{hashlib.sha256(PNG).hexdigest()}.png").write_bytes(PNG)
    with pytest.raises(captures.RefusedError, match="link"):
        captures.read(hashlib.sha256(PNG).hexdigest(), 0, 1)
    assert [p.name for p in elsewhere.iterdir()] == [
        f"{hashlib.sha256(PNG).hexdigest()}.png"
    ]


def test_a_wide_root_is_refused() -> None:
    captures.keep(PNG)
    os.chmod(root(), 0o755)
    with pytest.raises(captures.RefusedError, match="wider than private"):
        captures.keep(PNG[:-1])
    with pytest.raises(captures.RefusedError, match="wider than private"):
        captures.clear()
    assert len(list(root().iterdir())) == 1


def test_a_root_another_account_owns_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures.keep(PNG)
    real = os.fstat

    def foreign(fd: int) -> os.stat_result:
        found = real(fd)
        if not stat.S_ISDIR(found.st_mode):
            return found
        fields = list(found)
        fields[stat.ST_UID] = found.st_uid + 1
        return os.stat_result(fields)

    monkeypatch.setattr(os, "fstat", foreign)
    with pytest.raises(captures.RefusedError, match="owned by uid"):
        captures.keep(PNG[:-1])
    with pytest.raises(captures.RefusedError, match="owned by uid"):
        captures.read(hashlib.sha256(PNG).hexdigest(), 0, 1)
    monkeypatch.undo()
    assert len(list(root().iterdir())) == 1


def test_clear_removes_only_capture_names() -> None:
    ident = captures.keep(PNG)
    (root() / f"{ident}.txt").write_text("not a capture")
    (root() / ("b" * 64 + ".png.bak")).write_text("not a capture")
    assert captures.clear() == 1
    assert sorted(p.name for p in root().iterdir()) == sorted(
        [f"{ident}.txt", "b" * 64 + ".png.bak"]
    )


def test_unset_under_a_test_marker_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(db.DIRECTORY_VARIABLE)
    with pytest.raises(db.RefusedError, match="unset under a test"):
        captures.keep(PNG)
    with pytest.raises(db.RefusedError, match="unset under a test"):
        captures.clear()


@pytest.mark.parametrize("directory_name", ["database", "alternate"])
def test_the_live_capture_root_is_refused_under_pytest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    directory_name: str,
) -> None:
    live = tmp_path / "live" / "database"
    monkeypatch.setattr(db, "live_directory", lambda: live)
    monkeypatch.setenv(db.DIRECTORY_VARIABLE, str(live.parent / directory_name))
    with pytest.raises(captures.RefusedError, match="live store"):
        captures.root()
    with pytest.raises(captures.RefusedError, match="live store"):
        captures.read("a" * 64, 0, 1)

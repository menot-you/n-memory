"""A steer entry is complete or absent: a killed sender never breaks the queue."""

from __future__ import annotations

import errno
import io
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Self

import pytest

from nspawn import steer

# A sender that dies the way a SIGKILL does: the lock is released and nothing
# else runs. `where` picks the moment: right after the entry file is created
# ("created"), or after half of the payload reached the file ("half").
SENDER = r"""
import os
import socket
import signal
import sys
from pathlib import Path

from nspawn import steer

run_root, where = Path(sys.argv[1]), sys.argv[2]
real_fdopen = os.fdopen


class Dying:
    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()

    def write(self, data):
        self.handle.write(data[: len(data) // 2])
        self.handle.flush()
        os.kill(os.getpid(), signal.SIGKILL)

    def __getattr__(self, name):
        return getattr(self.handle, name)


def fdopen(descriptor, *args, **kwargs):
    if where == "created":
        os.kill(os.getpid(), signal.SIGKILL)
    return Dying(real_fdopen(descriptor, *args, **kwargs))


os.fdopen = fdopen
steer.send(run_root, "run-1", "never delivered")
"""


def _channel(tmp_path: Path) -> Path:
    (tmp_path / "run-1").mkdir()
    return steer.create(tmp_path / "run-1")


def _kill_sender(tmp_path: Path, where: str) -> None:
    done = subprocess.run(
        [sys.executable, "-I", "-c", SENDER, str(tmp_path), where],
        check=False,
        env={"PYTHONPATH": str(Path(steer.__file__).parent.parent)},
    )
    assert done.returncode == -9


@pytest.mark.parametrize("where", ["created", "half"])
def test_a_sender_killed_midway_loses_no_acknowledged_message(
    tmp_path: Path, where: str
) -> None:
    directory = _channel(tmp_path)
    steer.send(tmp_path, "run-1", "first")
    _kill_sender(tmp_path, where)
    steer.send(tmp_path, "run-1", "last")

    assert steer.take(directory) == ["first", "last"]
    assert steer.take(directory) == []
    assert steer.seal_if_empty(directory)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b'{"text": "cut',
        b"[]",
        b'{"text": 3}',
        b"{}",
        b"\xff\xfe",
        pytest.param(b"[" * 200_000, id="nested-too-deep"),
    ],
    ids=lambda body: body.decode("latin-1")[:12] or "empty",
)
def test_take_skips_an_unreadable_entry_and_names_it(
    tmp_path: Path, body: bytes
) -> None:
    directory = _channel(tmp_path)
    steer.send(tmp_path, "run-1", "first")
    broken = directory / f"{1:020d}{steer.MESSAGE_SUFFIX}"
    broken.write_bytes(body)
    steer.send(tmp_path, "run-1", "last")

    assert steer.take(directory) == ["first", "last"]
    assert not broken.exists()
    kept = directory / (broken.name + steer.REJECTED_SUFFIX)
    assert kept.read_bytes() == body
    assert steer.rejected(directory) == [kept.name]
    assert steer.seal_if_empty(directory)


def _fail_reads(monkeypatch: pytest.MonkeyPatch, names: set[str]) -> None:
    """Make the open of the named entries fail once, like a full descriptor table."""
    real = os.open
    failed: set[str] = set()

    def open_entry(path: object, *args: int, **kwargs: int) -> int:
        name = Path(os.fspath(path)).name  # type: ignore[arg-type]
        if name in names and name not in failed:
            failed.add(name)
            raise OSError(errno.EMFILE, "too many open files")
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(steer.os, "open", open_entry)


def test_a_failed_read_is_not_a_bad_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _channel(tmp_path)
    steer.send(tmp_path, "run-1", "first")
    steer.send(tmp_path, "run-1", "last")
    last = max(directory.glob("*" + steer.MESSAGE_SUFFIX))
    _fail_reads(monkeypatch, {last.name})

    with pytest.raises(OSError, match="too many open files"):
        steer.take(directory)

    assert steer.rejected(directory) == []
    # The failure lost nothing: both messages still wait for the next take.
    assert steer.take(directory) == ["first", "last"]


def test_send_does_not_write_through_a_planted_temporary_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _channel(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    stamp = 1_700_000_000_000_000_000
    monkeypatch.setattr(steer.time, "time_ns", lambda: stamp)
    real_clear = steer._clear_temporary

    def clear_then_plant(path: Path) -> None:
        # Another process of the same user wins the race after the clean-up.
        real_clear(path)
        name = f"{stamp:020d}{steer.MESSAGE_SUFFIX}{steer.TEMPORARY_SUFFIX}"
        (path / name).symlink_to(victim)

    monkeypatch.setattr(steer, "_clear_temporary", clear_then_plant)

    with pytest.raises(steer.SteerRefused, match="temporary"):
        steer.send(tmp_path, "run-1", "payload")

    assert victim.read_bytes() == b"keep"
    monkeypatch.setattr(steer, "_clear_temporary", real_clear)
    assert steer.take(directory) == []
    assert victim.read_bytes() == b"keep"


def _plant(directory: Path, kind: str, victim: Path) -> Path:
    """Put an entry that is not a regular file at a queue name."""
    entry = directory / f"{1:020d}{steer.MESSAGE_SUFFIX}"
    if kind == "link":
        entry.symlink_to(victim)
    elif kind == "directory":
        entry.mkdir()
    else:
        os.mkfifo(entry)
    return entry


@pytest.mark.parametrize("kind", ["link", "directory", "fifo"])
def test_take_sets_aside_an_entry_that_is_not_a_regular_file(
    tmp_path: Path, kind: str
) -> None:
    directory = _channel(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b'{"text": "from outside the queue"}')
    steer.send(tmp_path, "run-1", "first")
    entry = _plant(directory, kind, victim)
    steer.send(tmp_path, "run-1", "last")

    assert steer.take(directory) == ["first", "last"]
    assert not entry.exists() and not entry.is_symlink()
    assert steer.rejected(directory) == [entry.name + steer.REJECTED_SUFFIX]
    assert victim.read_bytes() == b'{"text": "from outside the queue"}'


@pytest.mark.parametrize("kind", ["link", "directory"])
def test_the_clean_up_removes_only_regular_temporary_files(
    tmp_path: Path, kind: str
) -> None:
    directory = _channel(tmp_path)
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    suffix = steer.MESSAGE_SUFFIX + steer.TEMPORARY_SUFFIX
    dead = directory / f"{2:020d}{suffix}"
    dead.write_bytes(b'{"text": "cut')
    other = directory / f"{3:020d}{suffix}"
    if kind == "link":
        other.symlink_to(victim)
    else:
        other.mkdir()

    steer.send(tmp_path, "run-1", "only")

    assert not dead.exists()
    assert other.is_symlink() or other.is_dir()
    assert victim.read_bytes() == b"keep"
    assert steer.take(directory) == ["only"]


def test_the_summary_lists_the_sender_shaped_names_and_counts_the_rest(
    tmp_path: Path,
) -> None:
    directory = _channel(tmp_path)
    suffix = steer.MESSAGE_SUFFIX + steer.REJECTED_SUFFIX
    shaped = [f"{n:020d}{suffix}" for n in range(1, 11)]
    for name in [*shaped, "forged name" + suffix, "١" * 20 + suffix]:
        (directory / name).write_bytes(b"x")

    summary = steer.rejected_summary(directory)

    assert summary.startswith("steer entries set aside: ")
    assert all(name in summary for name in shaped[:8])
    assert not any(name in summary for name in shaped[8:])
    assert "2 more" in summary and "2 other" in summary
    assert "forged" not in summary
    assert steer.rejected_summary(_channel_other(tmp_path)) == ""


def _channel_other(tmp_path: Path) -> Path:
    (tmp_path / "run-2").mkdir()
    return steer.create(tmp_path / "run-2")


@pytest.mark.parametrize("kind", ["socket", "no-access"])
def test_take_sets_aside_an_entry_that_the_system_refuses_to_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = _channel(tmp_path)
    steer.send(tmp_path, "run-1", "first")
    name = f"{1:020d}{steer.MESSAGE_SUFFIX}"
    entry = directory / name
    if kind == "socket":
        # A relative name keeps the path under the limit of a socket address.
        monkeypatch.chdir(directory)
        with socket.socket(socket.AF_UNIX) as server:
            server.bind(name)
    else:
        entry.write_bytes(b'{"text": "hidden"}')
        entry.chmod(0)
    steer.send(tmp_path, "run-1", "last")

    assert steer.take(directory) == ["first", "last"]
    assert steer.rejected(directory) == [name + steer.REJECTED_SUFFIX]
    assert steer.seal_if_empty(directory)


@pytest.mark.parametrize(
    "number", [errno.EMFILE, errno.ENFILE, errno.ENOMEM, errno.EIO]
)
def test_a_resource_error_at_open_is_not_a_property_of_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    directory = _channel(tmp_path)
    steer.send(tmp_path, "run-1", "first")
    real = os.open

    def refuse(path: object, *args: int, **kwargs: int) -> int:
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(steer.os, "open", refuse)
    with pytest.raises(OSError):
        steer.take(directory)
    monkeypatch.setattr(steer.os, "open", real)

    assert steer.rejected(directory) == []
    assert steer.take(directory) == ["first"]


def test_take_sets_aside_an_entry_larger_than_the_message_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = _channel(tmp_path)
    steer.send(tmp_path, "run-1", "first")
    big = directory / f"{1:020d}{steer.MESSAGE_SUFFIX}"
    text = "a" * steer.MAX_MESSAGE_BYTES
    big.write_bytes(b'{"text": "' + text.encode() + b'"}')
    steer.send(tmp_path, "run-1", "last")
    sizes: list[int] = []
    real_fdopen = os.fdopen

    class Spy:
        def __init__(self, handle: io.BufferedReader) -> None:
            self.handle = handle

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc: object) -> None:
            self.handle.close()

        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return self.handle.read(size)

    monkeypatch.setattr(steer.os, "fdopen", lambda *a, **k: Spy(real_fdopen(*a, **k)))

    assert steer.take(directory) == ["first", "last"]
    assert steer.rejected(directory) == [big.name + steer.REJECTED_SUFFIX]
    # No read asks for more than the limit and one byte.
    assert sizes and all(0 <= size <= steer.MAX_MESSAGE_BYTES + 1 for size in sizes)

"""Integration conformance test: the door's network seam over a real unix socket."""

from __future__ import annotations

import fcntl
import os
import socket
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

from nott_socket.door import Admitted, Refused, UnixDoor


def test_the_door_admits_the_owner_and_reports_the_peer_uid() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "door.sock"
        owner_uid = os.geteuid()

        door = UnixDoor.bind(path)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))

        adm = door.accept()
        assert isinstance(adm, Admitted)
        assert adm.peer_uid == owner_uid
        adm.stream.close()
        client.close()

        door.close()
        assert not path.exists(), f"a dropped door left its socket behind: {path}"


def test_the_door_refuses_a_foreign_uid_unserved() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "door_refuse.sock"
        owner_uid = os.geteuid()

        door = UnixDoor.bind(path)

        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(path))

        # The kernel reports a uid that is not the door owner's.
        with patch("nott_socket.door.peer_uid", return_value=owner_uid + 1):
            adm = door.accept()
        assert isinstance(adm, Refused)
        assert adm.peer_uid == owner_uid + 1

        # The stream was closed unserved by accept
        client.close()
        door.close()
        assert not path.exists()


def test_an_accept_blocked_on_a_door_being_closed_sees_it_closed() -> None:
    """`close` marks the door before it wakes the blocked accept, so the waking
    error (EINVAL on Linux, EBADF on macOS) reads as the door's own shutdown."""
    import threading

    from nott_socket.door import AcceptError

    with tempfile.TemporaryDirectory(dir="/var/tmp") as tmpdir:
        door = UnixDoor.bind(Path(tmpdir) / "door.sock")
        assert not door.closed
        seen: list[bool] = []

        def wait() -> None:
            try:
                door.accept()
            except AcceptError:
                seen.append(door.closed)

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        door.close()
        waiter.join(5.0)
        assert seen == [True]
        assert door.closed


@pytest.mark.parametrize("replacement_kind", ["socket", "file"])
def test_old_close_preserves_a_replacement_after_shutdown(
    replacement_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tempfile.TemporaryDirectory(prefix="s-") as directory:
        path = Path(directory) / "door.sock"
        old = UnixDoor.bind(path)
        replacement: UnixDoor | None = None
        shutdown = socket.socket.shutdown
        content = b"replacement bytes\x00\xff"

        def replace_after_shutdown(sock: socket.socket, how: int) -> None:
            nonlocal replacement
            shutdown(sock, how)
            if sock is not old.socket:
                return
            with (
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe,
                pytest.raises(ConnectionRefusedError),
            ):
                probe.connect(str(path))
            path.unlink()
            if replacement_kind == "socket":
                replacement = UnixDoor.bind(path)
            else:
                path.write_bytes(content)

        try:
            with monkeypatch.context() as patched:
                patched.setattr(socket.socket, "shutdown", replace_after_shutdown)
                old.close()

            assert path.exists(), "old cleanup removed the replacement"
            if replacement_kind == "file":
                assert path.read_bytes() == content
            else:
                assert replacement is not None
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(1)
                    client.connect(str(path))
                    admission = replacement.accept()
                    assert isinstance(admission, Admitted)
                    with admission.stream:
                        admission.stream.sendall(content)
                        assert client.recv(len(content)) == content
            old.close()
            assert path.exists()
        finally:
            old.close()
            if replacement is not None:
                replacement.close()


def test_close_holds_the_startup_lock_through_unlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(prefix="s-") as directory:
        path = Path(directory) / "door.sock"
        old = UnixDoor.bind(path)
        unlink = os.unlink
        reached_unlink = threading.Event()
        finish_unlink = threading.Event()

        def pause_unlink(name: str | Path, *, dir_fd: int | None = None) -> None:
            if name == path:
                reached_unlink.set()
                assert finish_unlink.wait(5), "cleanup did not resume"
            unlink(name, dir_fd=dir_fd)

        with monkeypatch.context() as patched:
            patched.setattr(os, "unlink", pause_unlink)
            with ThreadPoolExecutor(max_workers=1) as executor:
                cleanup = executor.submit(old.close)
                try:
                    assert reached_unlink.wait(5), "cleanup did not reach unlink"
                    with (
                        path.with_suffix(".lock").open("a") as lock,
                        pytest.raises(BlockingIOError),
                    ):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    finish_unlink.set()
                    cleanup.result(timeout=5)
        assert old.closed
        assert not path.exists()

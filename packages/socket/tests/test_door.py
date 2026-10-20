"""Tests for door module."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

from nott_socket.door import (
    STAGING_ATTEMPTS,
    STAGING_MODE,
    BindError,
    PathOccupied,
    Staging,
    UnixDoor,
    _staging_parent,
)


def test_a_staged_socket_moved_out_is_not_unlinked_by_teardown() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        destination = Path(tmpdir) / "door.sock"
        staging = Staging.beside(destination)
        staged_name = staging.socket
        with open(staged_name, "wb") as f:
            f.write(b"the socket this staging created")

        staging.install(destination)

        # Staged name is free; plant replacement
        with open(staged_name, "wb") as f:
            f.write(b"NOT the door's")

        staging.close()

        assert os.path.exists(staged_name)
        with open(staged_name, "rb") as f:
            assert f.read() == b"NOT the door's"
        with open(destination, "rb") as f:
            assert f.read() == b"the socket this staging created"


def test_a_destination_that_appears_after_staging_is_refused_and_its_bytes_survive() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmpdir:
        destination = Path(tmpdir) / "door.sock"
        staging = Staging.beside(destination)
        staged_name = staging.socket
        with open(staged_name, "wb") as f:
            f.write(b"the socket this staging created")

        with open(destination, "wb") as f:
            f.write(b"somebody else got here first")

        with pytest.raises(PathOccupied):
            staging.install(destination)

        with open(destination, "rb") as f:
            assert f.read() == b"somebody else got here first"

        staging.close()
        assert not os.path.exists(staged_name)


def test_staged_and_installed_socket_names_have_exact_custody() -> None:
    assert _staging_parent(Path("journal.sock")) == Path(".")
    assert _staging_parent(Path("doors/journal.sock")) == Path("doors")

    with tempfile.TemporaryDirectory() as tmpdir:
        parent = Path(tmpdir) / "doors"
        parent.mkdir()
        destination = parent / "journal.sock"

        staging = Staging.beside(destination)
        assert staging.directory.parent == parent
        st = os.stat(staging.directory)
        assert (st.st_mode & 0o7777) == STAGING_MODE

        staged_dir = staging.directory
        staged_sock = staging.socket
        with open(staged_sock, "wb") as f:
            f.write(b"fixture")

        # The name is hexadecimal and short on purpose: every byte here comes out of
        # the AF_UNIX ceiling of 108 (see `Staging.beside`).
        prefix = f".s{os.getpid():x}."
        assert staging.directory.name.startswith(prefix)
        serial = int(staging.directory.name.removeprefix(prefix), 16)

        staging.close()
        assert not staged_sock.exists()
        assert not staged_dir.exists()

        # Collisions test: occupy twice the bounded attempts starting from serial + 1
        for collision in range(serial + 1, serial + 2 * STAGING_ATTEMPTS + 1):
            (parent / f".s{os.getpid():x}.{collision:x}").mkdir(exist_ok=True)

        with pytest.raises(BindError) as exc_info:
            Staging.beside(destination)
        assert "collided" in str(exc_info.value) or isinstance(
            exc_info.value.source, FileExistsError
        )

        # Parent is not a directory
        not_dir = Path(tmpdir) / "not-a-directory"
        not_dir.write_text("file")
        with pytest.raises(BindError) as exc_notdir:
            Staging.beside(not_dir / "journal.sock")
        assert isinstance(exc_notdir.value.source, NotADirectoryError)

        # Installed door drop
        installed = Path(tmpdir) / "installed.sock"
        door = UnixDoor.bind(installed)
        assert installed.exists()
        door.close()
        assert not installed.exists()


def test_accept_raises_accept_error_on_failure(tmp_path: Path) -> None:
    import tempfile

    from nott_socket.door import AcceptError, UnixDoor

    short_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    door = UnixDoor.bind(short_dir / "test.sock")
    # Force the underlying socket to close to cause accept() to fail
    door._sock.close()
    with pytest.raises(AcceptError) as exc_info:
        door.accept()
    assert "test.sock" in str(exc_info.value)


def test_peer_credentials_error_forces_illegal_path() -> None:
    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    from nott_socket.door import PeerCredentialsError, UnixDoor

    short_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    door = UnixDoor.bind(short_dir / "test2.sock")

    class MockSocket:
        def getsockopt(
            self, level: int, optname: int, buflen: int | None = None
        ) -> bytes:
            raise OSError("mock getsockopt failure")

        def settimeout(self, timeout: float | None) -> None:
            # The real admitted peer receives the port's deadline before being read.
            pass

        def close(self) -> None:
            pass

    with patch("socket.socket.accept", return_value=(MockSocket(), None)):
        with pytest.raises(PeerCredentialsError) as exc_info:
            door.accept()
        assert "mock getsockopt failure" in str(exc_info.value)
    door.close()


def test_um_accept_transitorio_e_um_accept_fatal_sao_distintos() -> None:
    """A peer that gives up midway through the connection is transient; a closed port is fatal.

    Without this distinction the server either dies at the first ECONNABORTED, or spins
    forever on a socket that no longer exists.
    """
    import errno
    from unittest.mock import patch

    from nott_socket.door import AcceptError

    short_dir = Path(tempfile.mkdtemp(dir="/tmp"))
    door = UnixDoor.bind(short_dir / "classe.sock")
    try:
        with (
            patch(
                "socket.socket.accept",
                side_effect=OSError(
                    errno.ECONNABORTED, "software caused connection abort"
                ),
            ),
            pytest.raises(AcceptError) as transitorio,
        ):
            door.accept()
        assert transitorio.value.transient is True

        door._sock.close()
        with pytest.raises(AcceptError) as fatal:
            door.accept()
        assert fatal.value.transient is False
    finally:
        door.close()


def test_uma_porta_cabe_num_destino_que_o_kernel_ainda_aceita() -> None:
    """AF_UNIX's ceiling (108 bytes on Linux, 104 on macOS) belongs to the
    REQUESTED PATH, not to our staging.

    The `bind` is born in a private folder next to the destination. If that folder's name
    costs more bytes than the destination itself, a port the kernel would accept
    dies at `bind` — that's what took down `xdist` on twenty members, because the
    worker's `popen-gwN` pushed the path past the limit.

    Here a destination 8 bytes under the ceiling is requested: it fits the kernel and
    fits a client that connects to it. What it can't afford is the staging tax eating into it.
    """
    import socket as stdlib_socket

    raiz = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        alvo = (104 if sys.platform == "darwin" else 108) - 8
        # raiz + "/" + <enchimento> + "/" + "d.sock"
        enchimento = alvo - len(str(raiz)) - len("/") - len("/d.sock")
        assert enchimento > 0, f"root {raiz} no longer leaves room for the measurement"
        pasta = raiz / ("d" * enchimento)
        pasta.mkdir()
        destino = pasta / "d.sock"
        assert len(str(destino)) == alvo

        with UnixDoor.bind(destino):
            cliente = stdlib_socket.socket(
                stdlib_socket.AF_UNIX, stdlib_socket.SOCK_STREAM
            )
            with cliente:
                cliente.connect(str(destino))
    finally:
        import shutil

        shutil.rmtree(raiz, ignore_errors=True)

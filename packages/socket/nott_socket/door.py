"""A bound unix-socket listener that admits callers by kernel-proven peer credentials."""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import socket
import stat
import struct
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Self

logger = logging.getLogger(__name__)

SOCKET_MODE: int = 0o600
STAGING_MODE: int = 0o700
STAGING_ATTEMPTS: int = 16

# `accept` errors that concern the CONNECTION, not the port: the peer gave up
# midway, a signal interrupted the call, or the process ran out of descriptors. None
# of them means the port died, and so none of them can bring down the loop.
# Everything else (EBADF, EINVAL, ENOTSOCK) speaks to the port itself and ends the service.
TRANSIENT_ACCEPT_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EAGAIN,
        errno.ECONNABORTED,
        errno.EINTR,
        errno.EMFILE,
        errno.ENFILE,
        errno.ENOBUFS,
        errno.ENOMEM,
        errno.EPERM,
        errno.EPROTO,
        errno.ETIMEDOUT,
        errno.EWOULDBLOCK,
    }
)


class ListenError(Exception):
    """What went wrong at the door."""


class PathOccupied(ListenError):
    """Something already occupies the listener path."""

    def __init__(self, path: Path | str) -> None:
        p = Path(path)
        super().__init__(
            f"listener path {p} already exists; the door never unlinks a path "
            f"it did not create — prove nothing is listening on it, then remove it"
        )
        self.path = p


class BindError(ListenError):
    """The bind failed."""

    def __init__(self, path: Path | str, source: Exception) -> None:
        p = Path(path)
        super().__init__(f"binding the listener at {p} failed: {source}")
        self.path = p
        self.source = source


class ModeError(ListenError):
    """The socket was born with the wrong mode."""

    def __init__(self, path: Path | str, observed: int, declared: int) -> None:
        p = Path(path)
        super().__init__(
            f"listener at {p} was created with mode {observed:04o}, not {declared:04o}"
        )
        self.path = p
        self.observed = observed
        self.declared = declared


class OwnershipError(ListenError):
    """The socket was born owned by the wrong uid."""

    def __init__(self, path: Path | str, observed: int, declared: int) -> None:
        p = Path(path)
        super().__init__(
            f"listener at {p} was created owned by uid {observed}, not {declared}"
        )
        self.path = p
        self.observed = observed
        self.declared = declared


class AcceptError(ListenError):
    """The accept failed."""

    def __init__(self, path: Path | str, source: Exception) -> None:
        p = Path(path)
        super().__init__(f"accepting on the listener at {p} failed: {source}")
        self.path = p
        self.source = source

    @property
    def transient(self) -> bool:
        """True when the failure was the CONNECTION's and the port is still standing."""
        return getattr(self.source, "errno", None) in TRANSIENT_ACCEPT_ERRNOS


class PeerCredentialsError(ListenError):
    """Peer credentials could not be read."""

    def __init__(self, path: Path | str, source: Exception) -> None:
        p = Path(path)
        super().__init__(f"reading peer credentials for a peer on {p} failed: {source}")
        self.path = p
        self.source = source


class Admission:
    """What one accept decided about one peer."""


@dataclass(frozen=True)
class Admitted(Admission):
    """Peer credential reported a uid this door admits."""

    stream: socket.socket
    peer_uid: int


@dataclass(frozen=True)
class Refused(Admission):
    """Peer credential reported a foreign uid; connection was closed unserved."""

    peer_uid: int


def _hold_declared_directory_mode(directory: Path, declared: int) -> None:
    """Put back on directory whatever umask took out."""
    fd = os.open(
        directory,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fchmod(fd, declared)
    finally:
        os.close(fd)


def _staging_parent(destination: Path) -> Path:
    parent = destination.parent
    if str(parent) and str(parent) != ".":
        return parent
    return Path(".")


_next_serial = 0


class Staging:
    """The private directory a socket is born in, removed when it leaves scope."""

    def __init__(self, directory: Path, socket_path: Path) -> None:
        self.directory = directory
        self.socket = socket_path
        self.installed = False

    @classmethod
    def beside(cls, destination: Path) -> Staging:
        global _next_serial
        parent = _staging_parent(destination)
        if not parent.exists():
            raise BindError(
                parent, FileNotFoundError(f"parent directory {parent} does not exist")
            )
        if not parent.is_dir():
            raise BindError(
                parent, NotADirectoryError(f"parent {parent} is not a directory")
            )

        for _ in range(STAGING_ATTEMPTS):
            serial = _next_serial
            _next_serial += 1
            # THE NAME IS PAID FOR IN AF_UNIX BYTES. The `bind` happens right here,
            # and the kernel cuts the socket path off at 108 bytes — a ceiling of its own,
            # not one of ours. The earlier readable name
            # (`.socket.<pid>.<serial>.staging/socket`) cost 33 bytes after the
            # parent folder; a 10-byte destination then came out 23 bytes more expensive
            # to be born than to live, and ports the kernel would accept
            # died at `bind`. In hex, `.s<pid>.<serial>/s` costs ~12:
            # what's left of the ceiling belongs to the requested path.
            dir_name = f".s{os.getpid():x}.{serial:x}"
            directory = parent / dir_name
            try:
                os.mkdir(directory, mode=STAGING_MODE)
            except FileExistsError:
                continue
            except OSError as e:
                raise BindError(directory, e) from e

            try:
                _hold_declared_directory_mode(directory, STAGING_MODE)
            except OSError as e:
                try:
                    os.rmdir(directory)
                except OSError:
                    pass
                raise BindError(directory, e) from e

            return cls(directory=directory, socket_path=directory / "s")

        raise BindError(
            parent,
            FileExistsError(
                f"all {STAGING_ATTEMPTS} staging attempts collided in {parent}"
            ),
        )

    def install(self, destination: Path) -> None:
        """Move staged socket onto destination path without replacing."""
        if os.path.lexists(destination):
            raise PathOccupied(destination)
        try:
            os.link(self.socket, destination)
            os.unlink(self.socket)
            self.installed = True
        except FileExistsError as e:
            raise PathOccupied(destination) from e
        except OSError as e:
            raise BindError(destination, e) from e

    def close(self) -> None:
        if not self.installed:
            try:
                os.unlink(self.socket)
            except OSError:
                pass
        try:
            os.rmdir(self.directory)
        except OSError:
            pass

    def __del__(self) -> None:
        self.close()


def peer_uid(stream: socket.socket, socket_path: Path) -> int:
    """Read peer UID from socket via kernel peer credentials."""
    if sys.platform == "darwin":
        # macOS LOCAL_PEERCRED (SOL_LOCAL=0, LOCAL_PEERCRED=1, struct xucred: 76 bytes)
        try:
            raw = stream.getsockopt(0, 1, 76)
            _, uid = struct.unpack_from("=II", raw, 0)
            return int(uid)
        except OSError as e:
            raise PeerCredentialsError(socket_path, e) from e
    elif sys.platform.startswith("linux"):
        # Linux SO_PEERCRED (SOL_SOCKET, SO_PEERCRED, struct ucred: 12 bytes pid, uid, gid)
        try:
            so_peercred = getattr(socket, "SO_PEERCRED", 17)
            raw = stream.getsockopt(
                socket.SOL_SOCKET, so_peercred, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
        except OSError as e:
            raise PeerCredentialsError(socket_path, e) from e
    else:
        raise PeerCredentialsError(
            socket_path,
            OSError(errno.ENOTSUP, f"peer credentials not supported on {sys.platform}"),
        )


@contextmanager
def ownership(path: Path) -> Iterator[None]:
    """Serialize startup and removal at the same socket path."""
    handle = os.open(
        path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600
    )
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


class UnixDoor:
    """A bound owner-only unix-socket listener that admits only its own owner's uid."""

    def __init__(
        self,
        sock: socket.socket,
        path: Path,
        *,
        identity: os.stat_result | None = None,
    ) -> None:
        self._sock = sock
        self._path = path
        self._owner_uid = os.geteuid()
        self._closed = True
        try:
            self._identity = os.lstat(path) if identity is None else identity
        except OSError:
            sock.close()
            raise
        self._closed = False

    @classmethod
    def bind(cls, path: str | Path) -> UnixDoor:
        """Bind path, refusing an occupied one. An admitted peer carries no deadline:
        a connection is held open while a turn thinks."""
        dest = Path(path)
        owner_uid = os.geteuid()

        if os.path.lexists(dest):
            raise PathOccupied(dest)

        staging = Staging.beside(dest)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(staging.socket))
            os.chmod(staging.socket, SOCKET_MODE)
            sock.listen(128)
            # The hard link keeps the staged inode, mode and owner.
            st = os.lstat(staging.socket)
            obs_mode = st.st_mode & 0o7777
            if obs_mode != SOCKET_MODE:
                raise ModeError(dest, obs_mode, SOCKET_MODE)
            if st.st_uid != owner_uid:
                raise OwnershipError(dest, st.st_uid, owner_uid)
            staging.install(dest)
        except Exception:
            sock.close()
            staging.close()
            raise

        return cls(sock, dest, identity=st)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def socket(self) -> socket.socket:
        return self._sock

    @property
    def closed(self) -> bool:
        """True once `close` began: an accept that fails after this is the door's own shutdown."""
        return self._closed

    def accept(self) -> Admission:
        """Accept one connection and decide it against peer credentials.

        Refused connections are closed unserved.
        """
        try:
            conn, _ = self._sock.accept()
        except OSError as e:
            raise AcceptError(self._path, e) from e
        try:
            uid = peer_uid(conn, self._path)
        except Exception:
            conn.close()
            raise

        if uid == self._owner_uid:
            return Admitted(stream=conn, peer_uid=uid)
        else:
            conn.close()
            return Refused(peer_uid=uid)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                # Closing the fd alone does not wake a concurrent thread already
                # blocked in accept() on it. Shutting it down first does: accept()
                # raises EINVAL (Linux) or EBADF (macOS) immediately; `closed` is
                # already true, so the caller reads it as the end of serving, not a fault.
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    with ownership(self._path):
                        st = os.lstat(self._path)
                        if stat.S_ISSOCK(st.st_mode) and os.path.samestat(
                            st, self._identity
                        ):
                            os.unlink(self._path)
                except OSError:
                    pass
            finally:
                self._sock.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

"""The one door to every pynott SQLite store.

Every open goes through `connect`, which refuses a symlink, a foreign owner, a mode
wider than 0700/0600, and the account's live store unless a production entry point
asks for it. Test processes and their children cannot open the live store.
A connection is idle in Python autocommit with `query_only=ON`.
It writes only inside `write()` (or `begin`/`finish`),
which lifts `query_only` and issues `BEGIN IMMEDIATE`. A nested `write()` is a
SAVEPOINT. Failures come out as `BusyError`, `RefusedError` or `SchemaError`.

Writers are admitted in front of `BEGIN IMMEDIATE` by a kernel `flock` on the store's
`-write.lock` sidecar. SQLite's busy handler sleeps and polls, so the writer that just
committed takes the lock again and a waiter can starve for the whole busy timeout; a
waiter blocked in the kernel is woken the moment the lock is released. A connection
has at most one such wait: a `begin` that times out leaves it queued, and the next
`begin` resumes it in place.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import pwd
import shlex
import sqlite3
import stat
import sys
import threading
import time
import weakref
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import sqlite_vec  # type: ignore[import-untyped]

#: SQLite's only wait, the same 10 s the PostgreSQL boundary gave its lock timeout.
BUSY_TIMEOUT_MS = 10_000
#: Set per test by this distribution's pytest11 plugin; otherwise the live directory.
DIRECTORY_VARIABLE = "NOTT_DATABASE_DIR"
_LOCK_POLL_S = 0.02


class DatabaseError(Exception):
    """A store failure this library has named."""


class BusyError(DatabaseError):
    """The write lock (or a `lock()` file) stayed held past the wait."""


class RefusedError(DatabaseError):
    """The open or the statement is not allowed: path, owner, mode, live store, read-only."""


class SchemaError(DatabaseError):
    """The file is not a sound database: corrupt, not a database, or failing its check."""


#: The raw errors `translate` passes through untyped, named here so that no consumer
#: imports `sqlite3` itself (Rail P).
Error = sqlite3.Error
IntegrityError = sqlite3.IntegrityError
OperationalError = sqlite3.OperationalError
#: The row type consumers annotate with or install as `row_factory`.
Row = sqlite3.Row


class _Wait:
    """A kernel wait for the admission flock, in a daemon thread because the kernel
    wait has no deadline. It owns its fd until `claim` hands it to a gate. A caller
    that gives up leaves it queued for the connection's next `begin`; granted while
    nobody wants it, it lets the lock go at once."""

    def __init__(self, fd: int, name: str) -> None:
        self.fd: int | None = fd
        self.guard = threading.Lock()
        self.done = threading.Event()
        self.wanted = True
        self.error: OSError | None = None
        threading.Thread(
            target=self._block, args=(fd,), name=f"flock {name}", daemon=True
        ).start()

    def _block(self, fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as error:
            self.error = error
        with self.guard:
            self.done.set()
            if not self.wanted:
                self._let_go()

    def _let_go(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def resume(self) -> bool:
        """Want the grant again; False when it was already let go."""
        with self.guard:
            if self.fd is None:
                return False
            self.wanted = True
            return True

    def claim(self, wait: float, gate: _Gate) -> bool:
        """Hand the fd to `gate` if the lock is granted within `wait`; else stay queued."""
        try:
            self.done.wait(wait)
        except BaseException:
            self.abandon()
            raise
        with self.guard:
            if not self.done.is_set():
                self.wanted = False
                return False
            if self.error is not None:
                self._let_go()
                raise RefusedError(f"the write lock could not be taken: {self.error}")
            gate.fd, self.fd = self.fd, None
            return True

    def abandon(self) -> None:
        with self.guard:
            self.wanted = False
            if self.done.is_set():
                self._let_go()


class _Gate:
    """One connection's write admission: the fd holding the flock, a wait still queued
    for it, and its same-thread claim. It holds no reference to the connection, so a
    connection dropped mid-write lets all three go when it is collected."""

    def __init__(self) -> None:
        self.identity: tuple[int, int] = (0, 0)
        self.fd: int | None = None
        self.waiting: _Wait | None = None

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is not None:
            os.close(fd)

    def unclaim(self) -> None:
        with _claims_lock:
            holder = _claims.get(self.identity)
            if holder is not None and holder[1] is self:
                del _claims[self.identity]

    def drop(self) -> None:
        waiting, self.waiting = self.waiting, None
        if waiting is not None:
            waiting.abandon()
        self.release()
        self.unclaim()


class Connection(sqlite3.Connection):
    """A `sqlite3.Connection` that knows its file and its write depth."""

    depth: int = 0
    sidecar: Path

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.gate = _Gate()
        self._drop = weakref.finalize(self, self.gate.drop)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._drop()


def _release_admission(conn: Connection) -> None:
    conn.gate.release()


# The write lock each database file's holder took, by thread and gate. Taking it again
# through a second connection in the same thread would wait on itself for the whole
# busy timeout; `begin` refuses that at once instead. The holder's gate releases it,
# from whichever thread finishes or closes the write.
_claims: dict[tuple[int, int], tuple[int, _Gate]] = {}
_claims_lock = threading.Lock()


def _release_claim(conn: Connection) -> None:
    conn.gate.unclaim()


# -- errors -------------------------------------------------------------------


def translate(error: sqlite3.Error) -> Exception:
    """The typed error for `error`, or `error` itself when it names none of them."""
    code = (getattr(error, "sqlite_errorcode", None) or 0) & 0xFF
    if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
        return BusyError(str(error))
    if code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_AUTH) or str(error).startswith(
        "unsafe use of"
    ):
        return RefusedError(str(error))
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return SchemaError(str(error))
    return error


@contextlib.contextmanager
def mapped() -> Iterator[None]:
    """Raise the typed error for any `sqlite3.Error` in the block."""
    try:
        yield
    except sqlite3.Error as error:
        typed = translate(error)
        if typed is error:
            raise
        raise typed from error


# -- paths and the live store --------------------------------------------------


def live_directory() -> Path:
    """This account's store directory, from the passwd home (never `$HOME`)."""
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".nott" / "state" / "database"


#: Set by pytest in the test process and inherited by every child it starts; the
#: `pytest` in `sys.modules` fence does not cross into a child, these do.
TEST_MARKERS = ("PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER")


def directory() -> Path:
    """Where this process keeps its stores: `NOTT_DATABASE_DIR`, else the live one.

    Unset under a test's marker, it is a refusal: a test's child that lost the
    variable would otherwise open the account's live store."""
    configured = os.environ.get(DIRECTORY_VARIABLE, "")
    if not configured:
        marker = next((m for m in TEST_MARKERS if os.environ.get(m)), None)
        if marker is not None:
            raise RefusedError(
                f"{DIRECTORY_VARIABLE} is unset under a test ({marker} is set);"
                " refused rather than falling back to the live store"
            )
        return live_directory()
    path = Path(configured)
    if not path.is_absolute():
        raise RefusedError(
            f"{DIRECTORY_VARIABLE}={configured!r} is not an absolute path"
        )
    return path


def _is_live(folder: Path) -> bool:
    live = live_directory()
    try:
        a, b = os.stat(folder), os.stat(live)
    except FileNotFoundError:
        return os.path.realpath(folder) == os.path.realpath(live)
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _admit(folder: Path, allow_live_store: bool) -> None:
    if not _is_live(folder):
        return
    if "pytest" in sys.modules or any(
        os.environ.get(marker) for marker in TEST_MARKERS
    ):
        raise RefusedError(f"{folder} is the live store, refused under pytest")
    if not allow_live_store:
        raise RefusedError(
            f"{folder} is the live store; only an entry point with allow_live_store opens it"
        )


#: What a group-read snapshot's directory may grant beyond its owner: group read and
#: search, which an ACL naming the reading account also shows as (plan section 10).
GROUP_READ_DIRECTORY = 0o050


#: The verb that brings each store into existence (it makes the directory too).
CREATORS = {
    "nmemory.sqlite3": "nmemory database create",
    "nspawn.sqlite3": "nspawn database create",
    "brake.sqlite3": "nott brake off",
}


def _create_cure(path: Path) -> str:
    creator = CREATORS.get(path.name)
    if creator is None:
        return "create it with its owner's `database create` command"
    return f"create it with `{creator}`"


def _owned_private(st: os.stat_result, path: Path, allowed: int = 0) -> None:
    if st.st_uid != os.geteuid():
        raise RefusedError(f"{path} is owned by uid {st.st_uid}, not {os.geteuid()}")
    if st.st_mode & 0o077 & ~allowed:
        private = "700" if stat.S_ISDIR(st.st_mode) else "600"
        raise RefusedError(
            f"{path} has mode {oct(stat.S_IMODE(st.st_mode))}, wider than private;"
            f" run `chmod {private} {shlex.quote(str(path))}`"
        )


def _check_directory(folder: Path, allowed: int = 0, cure: str = "") -> None:
    try:
        st = os.lstat(folder)
        if (
            sys.platform == "linux"
            and folder.parent == Path("/proc/self/fd")
            and folder.name.isascii()
            and folder.name.isdecimal()
        ):
            st = os.fstat(int(folder.name))
    except FileNotFoundError:
        raise RefusedError(f"{folder} is missing{cure}") from None
    if stat.S_ISLNK(st.st_mode):
        raise RefusedError(f"{folder} is a symlink")
    if not stat.S_ISDIR(st.st_mode):
        raise RefusedError(f"{folder} is not a directory")
    _owned_private(st, folder, allowed)


def directory_bind(descriptor: int) -> Path:
    """Bind a disposable store to a directory that the caller keeps open."""
    if sys.platform != "linux":
        raise RefusedError("descriptor-bound stores require Linux")
    path = Path(f"/proc/self/fd/{descriptor}")
    _check_directory(path)
    _admit(path, allow_live_store=False)
    return path


def _check_file(path: Path) -> os.stat_result:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise RefusedError(f"{path} is missing; {_create_cure(path)}") from None
    if stat.S_ISLNK(st.st_mode):
        raise RefusedError(f"{path} is a symlink")
    if not stat.S_ISREG(st.st_mode):
        raise RefusedError(f"{path} is not a regular file")
    if st.st_nlink != 1:
        # Another name for the file (the live store's, say) is a way around the guard.
        raise RefusedError(f"{path} has {st.st_nlink} hard links, not 1")
    _owned_private(st, path)
    return st


def _new_private_file(path: Path) -> None:
    try:
        fd = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except FileExistsError:
        raise RefusedError(f"{path} already exists") from None
    os.close(fd)


def _sidecar(path: Path) -> Path:
    return Path(f"{path}-write.lock")


def _remove(path: Path) -> None:
    for name in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
        _sidecar(path),
    ):
        name.unlink(missing_ok=True)


def remove(path: str | os.PathLike[str]) -> None:
    """Delete a store and its sidecars; in the live directory, refused before any goes.

    2026-09-26: a test helper deleted the live nmemory store by hand, and the guard
    that refuses creating a store there only ran after the delete."""
    path = Path(path).absolute()
    if _is_live(path.parent):
        raise RefusedError(
            f"{path.parent} is the live store; no store is removed there"
        )
    _remove(path)


# -- opening ------------------------------------------------------------------


def connect(
    path: str | os.PathLike[str],
    *,
    readonly: bool = False,
    allow_live_store: bool = False,
    check_same_thread: bool = True,
    group_read: bool = False,
) -> Connection:
    """Open an existing store (never create one) with the fixed pragmas.

    `check_same_thread=False` only where the caller's own lock guards a connection
    that several threads share (the nSPAWN supervisor). `group_read` admits a
    directory that grants its group read and search (a group-read snapshot's).
    """
    path = Path(path).absolute()
    _admit(path.parent, allow_live_store)
    _check_directory(
        path.parent,
        GROUP_READ_DIRECTORY if group_read else 0,
        f"; {_create_cure(path)}",
    )
    before = _check_file(path)
    uri = f"{path.as_uri()}?mode={'ro' if readonly else 'rw'}"
    if sys.platform == "linux" and path.parent.parent == Path("/proc/self/fd"):
        from ._directory import uri as directory_uri

        uri = directory_uri(path, "ro" if readonly else "rw")
    with mapped():
        conn = sqlite3.connect(
            uri,
            uri=True,
            autocommit=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            factory=Connection,
            check_same_thread=check_same_thread,
        )
    try:
        after = os.lstat(path)
        if stat.S_ISLNK(after.st_mode) or (after.st_dev, after.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise RefusedError(f"{path} changed while it was opened")
        conn.gate.identity = (after.st_dev, after.st_ino)
        conn.sidecar = _sidecar(path)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)
        with mapped():
            for pragma in (
                f"busy_timeout = {BUSY_TIMEOUT_MS}",
                "foreign_keys = ON",
                "trusted_schema = OFF",
                "synchronous = FULL",
            ):
                conn.execute(f"PRAGMA {pragma}")
            if sys.platform == "darwin":
                # darwin fsync(2) stops at the drive cache; these make SQLite F_FULLFSYNC.
                conn.execute("PRAGMA fullfsync = ON")
                conn.execute("PRAGMA checkpoint_fullfsync = ON")
            if not readonly:
                query(conn, "PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def create(
    path: str | os.PathLike[str], ddl: str, *, allow_live_store: bool = False
) -> None:
    """Bring a store into existence: a new 0600 file in a 0700 directory, then `ddl`."""
    path = Path(path).absolute()
    _admit(path.parent, allow_live_store)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _check_directory(path.parent)
    _new_private_file(path)
    try:
        conn = connect(path, allow_live_store=allow_live_store)
        try:
            with write(conn):
                conn.executescript(ddl)
        finally:
            conn.close()
    except BaseException:
        _remove(path)
        raise


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    """Every row of `sql`, with the cursor closed so no read transaction lingers."""
    with contextlib.closing(conn.execute(sql, params)) as cursor:
        return cursor.fetchall()


def _scalar(conn: sqlite3.Connection, sql: str) -> Any:
    """The first column of the first row, read as a tuple whatever the caller's
    `row_factory` is, on a cursor closed so no read transaction lingers."""
    with contextlib.closing(conn.cursor()) as cursor:
        cursor.row_factory = None
        return cursor.execute(sql).fetchone()[0]


def user_version(conn: sqlite3.Connection) -> int:
    return int(_scalar(conn, "PRAGMA user_version"))


def load_vec(conn: sqlite3.Connection) -> None:
    """Load `sqlite-vec`, with extension loading on only for this call and only in C.

    The SQL `load_extension()` function stays disabled throughout, so no statement or
    trigger can load anything even while the loader is open.
    """
    conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, True)
    try:
        conn.load_extension(sqlite_vec.loadable_path())
    finally:
        conn.setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)


# -- transactions -------------------------------------------------------------


def begin(conn: Connection) -> None:
    """Start a write: `BEGIN IMMEDIATE` at depth 0, a SAVEPOINT inside one."""
    if conn.depth:
        with mapped():
            conn.execute(f"SAVEPOINT nott_{conn.depth}")
        conn.depth += 1
        return
    gate = conn.gate
    with _claims_lock:
        holder = _claims.get(gate.identity)
    if holder is not None and holder[0] == threading.get_ident():
        raise BusyError(
            "the same thread already holds this database's write lock through another "
            "connection; waiting would deadlock"
        )
    # The connection's own busy_timeout bounds the whole wait; 0 asks for a refusal.
    with mapped():
        busy_ms = int(_scalar(conn, "PRAGMA busy_timeout"))
    wait = busy_ms / 1000
    deadline = time.monotonic() + wait
    try:
        _take(conn, wait)
        # What the admission wait used is gone from SQLite's own wait.
        left_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if busy_ms:
            left_ms = max(1, left_ms)
        with mapped():
            conn.execute(f"PRAGMA busy_timeout = {left_ms}")
            try:
                conn.execute("PRAGMA query_only = OFF")
                conn.execute("BEGIN IMMEDIATE")
            finally:
                conn.execute(f"PRAGMA busy_timeout = {busy_ms}")
        conn.depth = 1
        with _claims_lock:
            _claims[gate.identity] = (threading.get_ident(), gate)
    except BaseException:
        _end(conn)
        raise


def _end(conn: Connection) -> None:
    """Leave no write behind, whatever interrupted it: roll back an open transaction,
    hand admission and the claim on, and restore `query_only`."""
    try:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    finally:
        conn.depth = 0
        _release_admission(conn)
        _release_claim(conn)
        conn.execute("PRAGMA query_only = ON")


def finish(conn: Connection, ok: bool) -> None:
    """End the innermost write: commit (or release) when `ok`, else roll it back."""
    if not conn.depth:
        raise DatabaseError("finish without begin")
    if conn.depth > 1:
        conn.depth -= 1
        name = f"nott_{conn.depth}"
        with mapped():
            if not ok:
                conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        return
    try:
        # The next admitted writer now waits in SQLite only for this COMMIT, not for
        # the WAL checkpoint SQLite runs inside the same call after it has released
        # its lock.
        _release_admission(conn)
        with mapped():
            conn.execute("COMMIT" if ok else "ROLLBACK")
    finally:
        _end(conn)


@contextlib.contextmanager
def write(conn: Connection) -> Iterator[Connection]:
    """A write transaction around the block; errors inside it come out typed."""
    begin(conn)
    ok = False
    try:
        with mapped():
            yield conn
        ok = True
    finally:
        finish(conn, ok)


# -- lock and snapshot --------------------------------------------------------


@contextlib.contextmanager
def lock(
    path: str | os.PathLike[str], *, wait: float, allow_live_store: bool = False
) -> Iterator[None]:
    """Hold an exclusive `flock` on `path`, waiting up to `wait` seconds for it."""
    path = Path(path).absolute()
    _admit(path.parent, allow_live_store)
    _check_directory(path.parent)
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as error:
        raise RefusedError(
            f"{path} is a symlink or cannot be opened: {error}"
        ) from error
    try:
        _owned_private(os.fstat(fd), path)
        deadline = time.monotonic() + wait
        # ponytail: polling, not a fair queue; a waiter gets the lock within one poll.
        # `_take` is fair, but a waiter that gives up still takes the lock once, which
        # a `wait=0` election probe would read as another holder.
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise BusyError(
                        f"{path} is held by another holder past {wait} s"
                    ) from None
                time.sleep(min(_LOCK_POLL_S, left))
        yield
    finally:
        os.close(fd)


def _take(conn: Connection, wait: float) -> None:
    """Hold the admission flock on `conn`'s sidecar, waited for in the kernel.

    A kernel waiter is woken as soon as the holder releases, where a poll would let the
    releaser take it again first. A wait that runs out stays queued on the connection,
    and its next `begin` resumes it in place, so retries never pile up waiters.
    """
    gate = conn.gate
    waiting = gate.waiting
    if waiting is None or not waiting.resume():
        gate.waiting = None
        try:
            fd = os.open(
                conn.sidecar,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except OSError as error:
            raise RefusedError(
                f"{conn.sidecar} is a symlink or cannot be opened: {error}"
            ) from error
        try:
            _owned_private(os.fstat(fd), conn.sidecar)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gate.fd = fd
            return
        except BlockingIOError:
            if wait <= 0:
                # No kernel waiter: one would take the lock later and refuse the next writer.
                os.close(fd)
                raise BusyError(f"{conn.sidecar} is held by another holder") from None
        except BaseException:
            os.close(fd)
            raise
        waiting = gate.waiting = _Wait(fd, conn.sidecar.name)
    if not waiting.claim(wait, gate):
        raise BusyError(f"{conn.sidecar} is held by another holder past {wait} s")
    gate.waiting = None


def _integrity(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in query(conn, "PRAGMA integrity_check")]


def snapshot(
    conn: sqlite3.Connection,
    dest: str | os.PathLike[str],
    *,
    group_read: bool = False,
    allow_live_store: bool = False,
) -> None:
    """Copy the store to a new file `dest` with `VACUUM INTO` and check it.

    The copy is one read transaction of the source, written once, compacted, as a
    single rollback-journal file. It takes no writer lock: `VACUUM INTO` cannot write
    the source (callers pass a `readonly=True` connection), and `query_only` is lifted
    for that one statement only, because `VACUUM INTO` refuses to run under it.

    `dest` is created 0600 with O_EXCL (`VACUUM INTO` alone writes 0644) in a checked
    directory, synced, checked through a read-only `connect` while still private, and
    widened to 0640 only when `group_read` asks for it. Only then may the directory
    grant its group read and search; group write and any access by others stay refused.
    """
    if conn.in_transaction:
        # VACUUM INTO cannot run inside a transaction.
        raise RefusedError("snapshot refused inside an open transaction")
    dest = Path(dest).absolute()
    _admit(dest.parent, allow_live_store)
    _check_directory(dest.parent, GROUP_READ_DIRECTORY if group_read else 0)
    _new_private_file(dest)
    try:
        with mapped():
            conn.execute("PRAGMA query_only = OFF")
            try:
                conn.execute("VACUUM INTO ?", (str(dest),))
            finally:
                conn.execute("PRAGMA query_only = ON")
        # SQLite does not sync a VACUUM INTO output.
        fd = os.open(dest, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        # Read-only: a read-write `connect` would turn the copy into WAL.
        target = connect(
            dest,
            readonly=True,
            allow_live_store=allow_live_store,
            group_read=group_read,
        )
        try:
            with mapped():
                problems = _integrity(target)
                if problems != ["ok"]:
                    raise SchemaError(
                        f"snapshot {dest} failed integrity_check: {problems}"
                    )
        finally:
            target.close()
        if group_read:
            os.chmod(dest, 0o640)
    except BaseException:
        _remove(dest)
        raise

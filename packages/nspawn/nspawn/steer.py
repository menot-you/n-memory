"""The steer channel: user messages that wait for a live Claude run.

`nspawn turn send` and the driver of a run share one directory inside the run
directory, `steer/`. The driver creates it when it spawns the child. A sender
adds one file for each message. The driver takes the files in order and writes
each one to the child as a user message.

A sender writes and closes a temporary file, then renames it to its `.json`
name under the lock. A `.json` file is therefore always complete, even when the
sender is killed. A file that does not decode is not a message: `take`
renames it to `<name>.json.rejected` and goes on, so the other messages reach
the child and the run does not fail. The rejected files stay in the run
directory as its record (`rejected`).

One lock file orders the two sides. Under that lock a sender adds a message
only while the channel is open, and the driver seals the channel only when no
message waits. A message is therefore never lost between the last read of the
driver and its seal: either the driver reads it, or the sender gets a refusal.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import stat
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from .errors import NERR_2003, NSpawnError

STEER_DIR: Final = "steer"
LOCK_FILE: Final = "lock"
SEALED_FILE: Final = "sealed"
#: A message file is `<time in ns, 20 digits>.json`; names sort in send order.
MESSAGE_SUFFIX: Final = ".json"
#: A sender writes `<name>.json.tmp`; `take` and `seal_if_empty` ignore it.
TEMPORARY_SUFFIX: Final = ".tmp"
#: `take` renames an entry that is not a message to `<name>.json.rejected`.
REJECTED_SUFFIX: Final = ".rejected"
#: Errors of `open` that describe the entry, not the lack of a resource.
ENTRY_ERRORS: Final = frozenset({errno.EACCES, errno.EPERM, errno.ENXIO, errno.ELOOP})
#: The only name a sender gives an entry, with the suffix `take` adds. A name
#: of any other shape did not come from a sender and is only counted.
SENDER_REJECTED_NAME: Final = re.compile(r"[0-9]{20}\.json\.rejected")
#: The most names that `rejected_summary` lists; it counts the rest.
SUMMARY_NAMES: Final = 8
#: The largest steer message in bytes. The driver writes a message to a pipe
#: that the child reads, and it must not hold the polling loop for long.
MAX_MESSAGE_BYTES: Final = 1024 * 1024


class SteerRefused(NSpawnError):
    """The run takes no steer message: it is not live, or its channel is sealed."""

    def __init__(self, detail: str) -> None:
        super().__init__(NERR_2003, detail)


def _safe_run_id(run_id: str) -> None:
    if not run_id or "/" in run_id or run_id in (".", "..") or "\x00" in run_id:
        raise SteerRefused(f"run id {run_id!r} is not a safe directory name")


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Hold the channel lock. A missing lock file means no channel."""
    try:
        descriptor = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CLOEXEC)
    except FileNotFoundError as error:
        raise SteerRefused("the run has no open steer channel") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def create(run_directory: Path) -> Path:
    """Open the channel of a run. The driver calls it before the child starts.

    A run that starts a second child in the same directory (a failover) reopens
    the channel and keeps the messages that still wait.
    """
    directory = run_directory / STEER_DIR
    directory.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(
        directory / LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600
    )
    os.close(descriptor)
    (directory / SEALED_FILE).unlink(missing_ok=True)
    return directory


def send(run_directory: Path, run_id: str, text: str) -> None:
    """Queue one message. Refuse when the channel is absent or sealed."""
    _safe_run_id(run_id)
    if not text:
        raise SteerRefused("a steer message must not be empty")
    payload = json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise SteerRefused(
            f"a steer message is at most {MAX_MESSAGE_BYTES} bytes, got {len(payload)}"
        )
    directory = run_directory / run_id / STEER_DIR
    with _locked(directory):
        if (directory / SEALED_FILE).exists():
            raise SteerRefused(f"run {run_id} no longer takes steer messages")
        _clear_temporary(directory)
        stamp = time.time_ns()
        while (directory / f"{stamp:020d}{MESSAGE_SUFFIX}").exists():
            stamp += 1
        final = directory / f"{stamp:020d}{MESSAGE_SUFFIX}"
        temporary = final.with_name(final.name + TEMPORARY_SUFFIX)
        # The name must be new: a link or a file that another process put there
        # is never opened, so a sender never writes through it.
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
        except FileExistsError as error:
            raise SteerRefused(
                f"the steer temporary name {temporary.name} is already taken"
            ) from error
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final)


def _clear_temporary(directory: Path) -> None:
    """Remove the temporary files of senders that died. Call it under the lock.

    A live sender writes its temporary file only while it holds the lock, so a
    temporary file seen by the holder of the lock belongs to a dead sender. Only
    a regular file is removed: a link or a directory at that name did not come
    from a sender and stays as it is.
    """
    for path in directory.iterdir():
        if not path.name.endswith(MESSAGE_SUFFIX + TEMPORARY_SUFFIX):
            continue
        try:
            regular = stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            continue
        if regular:
            path.unlink(missing_ok=True)


def _pending(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.iterdir() if path.name.endswith(MESSAGE_SUFFIX)
    )


def _read_message(path: Path) -> str:
    """Return the text of one entry. Raise ValueError when it is not a message.

    An entry that is not a regular file, that the system refuses to open for
    what it is (`EACCES`, `EPERM`, `ENXIO`, `ELOOP`), whose bytes do not decode or exceed
    `MAX_MESSAGE_BYTES`, or that decodes to something other than a message, raises
    ValueError. A failed read for lack of a resource (`EMFILE`, `ENFILE`,
    `ENOMEM`, `EIO`) is not a property of the entry, so it leaves this
    function as `OSError`.
    """
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"steer entry {path.name} is not a regular file")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
    except OSError as error:
        if error.errno in ENTRY_ERRORS:
            raise ValueError(
                f"steer entry {path.name} cannot be opened: {error.strerror}"
            ) from error
        raise
    try:
        # The check is on the open descriptor, so no swap of the name can pass
        # a link, a directory or a pipe as a message.
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError(f"steer entry {path.name} is not a regular file")
        if opened.st_size > MAX_MESSAGE_BYTES:
            raise ValueError(f"steer entry {path.name} is larger than the limit")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            # One byte more than the limit shows an entry that grew after fstat.
            body = handle.read(MAX_MESSAGE_BYTES + 1)
        if len(body) > MAX_MESSAGE_BYTES:
            raise ValueError(f"steer entry {path.name} is larger than the limit")
    finally:
        os.close(descriptor)
    try:
        message = json.loads(body)
    except (ValueError, RecursionError) as error:
        # `UnicodeDecodeError` and `JSONDecodeError` are ValueError.
        raise ValueError(f"steer entry {path.name} does not decode: {error}") from error
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(text, str) or not text:
        raise ValueError(f"steer entry {path.name} has no message text")
    return text


def take(directory: Path) -> list[str]:
    """Remove and return the queued messages, oldest first.

    An entry that does not decode to a message is renamed with `REJECTED_SUFFIX`
    and does not stop the other messages. `rejected` names it. A failed read
    (`OSError`) leaves `take` and removes nothing: every message still waits.
    """
    with _locked(directory):
        _clear_temporary(directory)
        read: list[tuple[Path, str]] = []
        for path in _pending(directory):
            try:
                read.append((path, _read_message(path)))
            except ValueError:
                path.replace(path.with_name(path.name + REJECTED_SUFFIX))
        for path, _ in read:
            path.unlink()
    return [text for _, text in read]


def rejected(directory: Path) -> list[str]:
    """Return the names of the entries that `take` set aside, oldest first."""
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.name.endswith(MESSAGE_SUFFIX + REJECTED_SUFFIX)
    )


def rejected_summary(directory: Path) -> str:
    """Return one line for the run record about the set-aside entries.

    Text that a name carries is data from outside, so the line holds only a
    name of the sender's own shape (ASCII digits and the two suffixes), at most
    `SUMMARY_NAMES` of them. It counts every other entry and the names it does
    not list. It returns an empty string when no entry was set aside.
    """
    names = rejected(directory)
    shaped = [name for name in names if SENDER_REJECTED_NAME.fullmatch(name)]
    other = len(names) - len(shaped)
    if not names:
        return ""
    parts: list[str] = []
    if shaped:
        listed = ", ".join(shaped[:SUMMARY_NAMES])
        if len(shaped) > SUMMARY_NAMES:
            listed += f" and {len(shaped) - SUMMARY_NAMES} more"
        parts.append(listed)
    if other:
        parts.append(f"{other} other")
    line = "steer entries set aside: " + "; ".join(parts)
    return line


def seal_if_empty(directory: Path) -> bool:
    """Seal the channel when no message waits. True means sealed.

    A False answer means a message arrived since the last `take`. The driver
    then takes it and tries again.
    """
    with _locked(directory):
        if _pending(directory):
            return False
        descriptor = os.open(
            directory / SEALED_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        os.close(descriptor)
        return True


def seal(directory: Path) -> None:
    """Seal the channel at once. The driver calls it when the child has ended."""
    with _locked(directory), contextlib.suppress(FileExistsError):
        descriptor = os.open(
            directory / SEALED_FILE,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        os.close(descriptor)

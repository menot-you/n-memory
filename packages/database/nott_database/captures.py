"""The capture root: browser page captures kept on this account's disk for the owner.

A capture is a PNG file named `<sha256>.png` in `directory().parent / "captures"`:
`~/.nott/state/captures` from the passwd home on a live host, and a directory inside
each test's private parent under pytest. nBROWSER writes it, and the node host reads
and clears it. Each open goes through the checked root's descriptor without following
a link, and refuses a file that is not regular, private, single-linked and the
account's own. A caller never names a path: an id is 64 lowercase hex characters.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import secrets
import stat
from pathlib import Path

from nott_database import (
    RefusedError,
    _admit,
    _owned_private,
    directory,
    live_directory,
)

__all__ = ["ID", "LIMIT", "Missing", "RefusedError", "clear", "keep", "read", "root"]

#: The largest capture kept, the cap nBROWSER puts on one screenshot.
LIMIT = 4 * 1024 * 1024
#: At most 1 GiB of captures. A full cache needs an explicit clear, not expiry.
COUNT = 256
#: What a capture id is: the lowercase hex sha256 of its bytes.
ID = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[0-9a-f]{64}\.png")
_TEMPORARY = ".capture-"


class Missing(RefusedError):
    """No capture has this id."""


def root() -> Path:
    """The capture root beside this process's store directory.

    Test processes and their children cannot use the live capture root."""
    folder = directory()
    _admit(folder.parent / live_directory().name, allow_live_store=True)
    return folder.parent / "captures"


def _root(create: bool) -> int | None:
    """The checked root's descriptor; None when it is missing and `create` is False."""
    path = root()
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        if create:
            raise RefusedError(f"{path} could not be made") from None
        return None
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise RefusedError(f"{path} is a link or not a directory") from None
        raise RefusedError(f"{path} cannot be opened: {error}") from error
    try:
        _owned_private(os.fstat(fd), path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _ident(ident: object) -> str:
    if not isinstance(ident, str) or not ID.fullmatch(ident):
        raise RefusedError("captureId must be 64 lowercase hex characters")
    return ident


def keep(data: bytes) -> str:
    """Write `data` as `<sha256>.png` (0600) in the root (0700); return the sha256."""
    if not 0 < len(data) <= LIMIT:
        raise RefusedError(f"a capture holds 1 to {LIMIT} bytes, not {len(data)}")
    ident = hashlib.sha256(data).hexdigest()
    fd = _root(create=True)
    assert fd is not None
    temporary = _TEMPORARY + secrets.token_hex(8)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        names = os.listdir(fd)
        if f"{ident}.png" in names:
            try:
                kept, _ = read(ident, 0, LIMIT)
            except RefusedError:
                pass  # A refused file can be replaced inside this checked root.
            else:
                if kept == data:
                    return ident
        if (
            sum(
                bool(_NAME.fullmatch(name)) or name.startswith(_TEMPORARY)
                for name in names
            )
            >= COUNT
        ):
            raise RefusedError(
                "The capture cache is full. Clear browser captures in app Settings."
            )
        out = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=fd,
        )
        try:
            with os.fdopen(out, "wb", closefd=False) as stream:
                stream.write(data)
            os.fsync(out)
        finally:
            os.close(out)
        # A rename replaces a link standing at the name, never what it points to.
        os.replace(temporary, f"{ident}.png", src_dir_fd=fd, dst_dir_fd=fd)
    except OSError as error:
        try:
            os.unlink(temporary, dir_fd=fd)
        except OSError:
            pass  # never made, or already renamed: nothing of this write is left
        raise RefusedError(f"the capture could not be written: {error}") from error
    finally:
        os.close(fd)
    return ident


def read(ident: str, offset: int, length: int) -> tuple[bytes, int]:
    """At most `length` bytes of capture `ident` from `offset`, and its whole size."""
    ident = _ident(ident)
    fd = _root(create=False)
    if fd is None:
        raise Missing(f"no capture has id {ident}")
    try:
        try:
            file = os.open(
                f"{ident}.png",
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=fd,
            )
        except FileNotFoundError:
            raise Missing(f"no capture has id {ident}") from None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise RefusedError(f"capture {ident} is a link") from None
            raise RefusedError(f"capture {ident} cannot be opened: {error}") from error
    finally:
        os.close(fd)
    try:
        found = os.fstat(file)
        if not stat.S_ISREG(found.st_mode):
            raise RefusedError(f"capture {ident} is not a regular file")
        if found.st_nlink != 1:
            raise RefusedError(
                f"capture {ident} has {found.st_nlink} hard links, not 1"
            )
        _owned_private(found, root() / f"{ident}.png")
        if not 0 < found.st_size <= LIMIT:
            raise RefusedError(f"capture {ident} holds {found.st_size} bytes")
        if offset > found.st_size:
            raise RefusedError(f"offset {offset} is past the end ({found.st_size})")
        return os.pread(file, length, offset), found.st_size
    finally:
        os.close(file)


def clear() -> int:
    """Delete every capture (and any temporary file a write left); return how many
    captures went. Only capture names are unlinked; a link goes, never its target."""
    fd = _root(create=False)
    if fd is None:
        return 0
    removed = 0
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        for name in os.listdir(fd):
            capture = bool(_NAME.fullmatch(name))
            if capture or name.startswith(_TEMPORARY):
                try:
                    os.unlink(name, dir_fd=fd)
                except OSError as error:
                    raise RefusedError(
                        f"{name} could not be deleted: {error}"
                    ) from error
                removed += capture
    finally:
        os.close(fd)
    return removed

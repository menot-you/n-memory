"""Bind evaluation file operations to open directories."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path


@contextmanager
def directory_open(
    path: Path,
    *,
    parent_fd: int | None = None,
    create: bool = False,
    fresh: bool = False,
) -> Iterator[int]:
    """Open each directory component without symlinks. Close all descriptors on exit."""
    if ".." in path.parts or (parent_fd is not None and path.is_absolute()):
        raise OSError(
            errno.EINVAL, "directory path must stay inside its root", str(path)
        )
    if parent_fd is None:
        path = path.absolute()
        # macOS exposes its temporary directory through the system /var link.
        if path.is_relative_to("/var/tmp"):
            path = Path("/var/tmp").resolve() / path.relative_to("/var/tmp")
        parts = path.parts[1:]
    else:
        parts = path.parts
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        current = (
            os.open(path.anchor, flags) if parent_fd is None else os.dup(parent_fd)
        )
        stack.callback(os.close, current)
        for index, part in enumerate(parts):
            exclusive = fresh and index == len(parts) - 1
            if create or exclusive:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    if exclusive:
                        raise
            current = os.open(part, flags, dir_fd=current)
            stack.callback(os.close, current)
        yield current


def directory_check(path: Path, descriptor: int) -> None:
    """Refuse a path that no longer names the open directory."""
    with directory_open(path) as current:
        if not os.path.samestat(os.fstat(current), os.fstat(descriptor)):
            raise OSError(
                errno.ESTALE, "directory changed during the evaluation", str(path)
            )


def file_read(directory: int, path: Path) -> bytes:
    """Read a regular file through its directory descriptor."""
    with directory_open(path.parent, parent_fd=directory) as parent:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError(errno.EINVAL, "source must be a regular file", str(path))
            return stream.read()


def file_replace(directory: int, name: str, chunks: Iterable[str]) -> None:
    """Replace one file without following links or changing another inode's contents."""
    if Path(name).name != name or name in ("", ".", ".."):
        raise OSError(errno.EINVAL, "file name must have one component", name)
    try:
        existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(existing.st_mode):
            raise OSError(errno.EINVAL, "destination must be a regular file", name)
    temporary = f".nmemory-evals-{secrets.token_hex(16)}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.writelines(chunks)
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass

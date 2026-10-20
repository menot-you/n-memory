"""Vendor-neutral byte fences for reading an owner's vendor files.

The owner-controlled parent directory, and the 0600 single-link regular file
read through O_NOFOLLOW and double-statted against concurrent mutation.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .errors import NSpawnError

#: Upper bound on the bytes a credential file may carry.
AUTH_MAX_BYTES = 64 * 1024

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class VendorHomeError(NSpawnError):
    """Why a private vendor home could not be prepared, proven, or removed."""

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)

    @staticmethod
    def io(operation: str, path: Path, source: OSError) -> VendorHomeError:
        return VendorHomeError(f"{operation} failed for {path}: {source}")

    @staticmethod
    def invalid(message: str) -> VendorHomeError:
        return VendorHomeError(f"private vendor home invariant failed: {message}")


class CredentialInFlightError(VendorHomeError):
    """A safe credential read while its writer was mid-rewrite: empty, or changed.

    The only refusal a later read can cure; ownership, mode, link and symlink
    refusals stay plain `VendorHomeError`.
    """


def read_owner_credential(path: Path) -> bytes:
    """Read an existing native account; its directory may be publicly traversable.

    Unlike a generated private home, an owner's CLI directory can be 0755.
    Only its owner may replace entries; the credential itself still has every
    0600/non-symlink/single-link/size/concurrent-change fence below. No file is copied.
    """
    return _read_owner_file(path, configuration=False)


def read_owner_config(path: Path) -> bytes:
    """Read configuration, including stable owner aliases and empty/0644 files."""
    _validate_owner_file_parent(path)
    try:
        before = path.lstat()
        if not stat.S_ISLNK(before.st_mode):
            return _read_owner_file(path, configuration=True)
        if before.st_uid != os.geteuid():
            raise VendorHomeError.invalid("configuration alias is not owner-held")
        target = path.resolve(strict=True)
        target_before = target.lstat()
        data = _read_owner_file(target, configuration=True)
        for subject, snapshot in ((path, before), (target, target_before)):
            after = subject.lstat()
            if any(
                getattr(snapshot, field) != getattr(after, field)
                for field in (
                    "st_dev",
                    "st_ino",
                    "st_uid",
                    "st_mode",
                    "st_nlink",
                    "st_size",
                    "st_mtime_ns",
                    "st_ctime_ns",
                )
            ):
                raise VendorHomeError.invalid("configuration alias changed during read")
        if path.resolve(strict=True) != target:
            raise VendorHomeError.invalid("configuration alias changed during read")
        return data
    except (OSError, RuntimeError) as error:
        raise VendorHomeError.invalid(
            "configuration alias cannot be resolved stably"
        ) from error


def _read_owner_file(path: Path, *, configuration: bool) -> bytes:
    _validate_owner_file_parent(path)
    return _read_credential_file(path, configuration=configuration)


def _validate_owner_file_parent(path: Path) -> None:
    try:
        parent = path.parent.lstat()
        if stat.S_ISLNK(parent.st_mode):
            # THE OWNER'S OWN LAYOUT PUTS A SYMLINKED DIRECTORY ON THE PATH.
            # Measured 2026-09-20: the roster points `defaults.system_prompt`
            # at `~/plugins/nott/NOTT.md`, and `plugins/nott` is an owner-held
            # link into `~/.local/share/nott/bundles/<id>`; `lstat` read "not a
            # directory" and every native codex dispatch was refused. The
            # invariant is WHO may replace entries, so an owner-held alias is
            # resolved and its target carries the same uid and mode checks --
            # an alias the owner does not hold is still refused, and so is a
            # target that is group- or other-writable.
            if parent.st_uid != os.geteuid():
                raise VendorHomeError.invalid(
                    "native credential directory alias is not owner-held"
                )
            parent = path.parent.stat()
    except OSError as error:
        raise VendorHomeError.io(
            "inspect owner credential directory", path.parent, error
        ) from error
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_mode & 0o022
    ):
        raise VendorHomeError.invalid(
            "native credential directory is not owner-controlled"
        )


def _read_credential_file(path: Path, *, configuration: bool = False) -> bytes:
    """The shared no-follow, bounded, double-stat credential read."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise VendorHomeError.io("open auth source", path, error) from error
    try:
        try:
            before = os.fstat(fd)
        except OSError as error:
            raise VendorHomeError.io("inspect auth source", path, error) from error
        unsafe_mode = (
            before.st_mode & 0o022 if configuration else before.st_mode & 0o777 != 0o600
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or unsafe_mode
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or before.st_size > AUTH_MAX_BYTES
        ):
            raise VendorHomeError.invalid(
                f"config source must be owner-held, not group/other writable, regular, single-link and at most {AUTH_MAX_BYTES} bytes: {path}"
                if configuration
                else f"auth source must be an owner-held, non-symlink regular file with "
                f"mode 0600, one link, and 1..={AUTH_MAX_BYTES} bytes: {path}"
            )
        if not configuration and before.st_size == 0:
            raise CredentialInFlightError(f"auth source is empty: {path}")
        chunks: list[bytes] = []
        total = 0
        while total <= AUTH_MAX_BYTES:
            chunk = os.read(fd, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        bytes_ = b"".join(chunks)
        try:
            after = os.fstat(fd)
        except OSError as error:
            raise VendorHomeError.io("reinspect auth source", path, error) from error
        if (
            (not configuration and not bytes_)
            or len(bytes_) > AUTH_MAX_BYTES
            or len(bytes_) != before.st_size
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mode != after.st_mode
            or before.st_uid != after.st_uid
            or before.st_nlink != after.st_nlink
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise CredentialInFlightError(
                f"private vendor home invariant failed: "
                f"auth source changed while it was being copied: {path}"
            )
        return bytes_
    finally:
        os.close(fd)

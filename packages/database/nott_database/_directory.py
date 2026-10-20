"""Keep SQLite paths relative to an open directory on Linux."""

from __future__ import annotations

import _sqlite3
import ctypes
from pathlib import Path

from . import RefusedError


def descriptor(path: Path) -> int | None:
    """Read a descriptor only from the process's exact descriptor directory."""
    if (
        path.parent == Path("/proc/self/fd")
        and path.name.isascii()
        and path.name.isdecimal()
    ):
        return int(path.name)
    return None


class _Vfs(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_int),
        ("file_size", ctypes.c_int),
        ("path_size", ctypes.c_int),
        ("next", ctypes.c_void_p),
        ("name", ctypes.c_char_p),
        ("data", ctypes.c_void_p),
        ("open", ctypes.c_void_p),
        ("delete", ctypes.c_void_p),
        ("access", ctypes.c_void_p),
        ("full_path", ctypes.c_void_p),
        ("dl_open", ctypes.c_void_p),
        ("dl_error", ctypes.c_void_p),
        ("dl_symbol", ctypes.c_void_p),
        ("dl_close", ctypes.c_void_p),
        ("random", ctypes.c_void_p),
        ("sleep", ctypes.c_void_p),
        ("time", ctypes.c_void_p),
        ("last_error", ctypes.c_void_p),
    ]


_FullPath = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.POINTER(_Vfs), ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p
)
_library = ctypes.CDLL(_sqlite3.__file__)
_library.sqlite3_vfs_find.argtypes = [ctypes.c_char_p]
_library.sqlite3_vfs_find.restype = ctypes.POINTER(_Vfs)
_library.sqlite3_vfs_register.argtypes = [ctypes.POINTER(_Vfs), ctypes.c_int]
_library.sqlite3_vfs_register.restype = ctypes.c_int
_native = _library.sqlite3_vfs_find(b"unix")
if not _native:
    raise RefusedError("SQLite's Unix filesystem is unavailable")
_vfs = _Vfs.from_buffer_copy(_native.contents)
_native_full_path = _FullPath(_vfs.full_path)


def _full_path(vfs: ctypes._Pointer[_Vfs], name: bytes, size: int, output: int) -> int:
    # The native resolver replaces descriptor paths with mutable ancestor paths.
    if descriptor(Path(name.decode("utf-8", errors="surrogateescape")).parent) is None:
        return int(_native_full_path(vfs, name, size, output))
    if len(name) >= size:
        return _sqlite3.SQLITE_CANTOPEN
    ctypes.memmove(output, name + b"\0", len(name) + 1)
    return _sqlite3.SQLITE_OK


# Version 1 uses the native clock through xCurrentTime and needs no later callbacks.
_vfs.version = 1
_vfs.next = None
_vfs.name = b"nott-directory"
_callback = _FullPath(_full_path)
_vfs.full_path = ctypes.cast(_callback, ctypes.c_void_p)
if _library.sqlite3_vfs_register(ctypes.byref(_vfs), 0) != _sqlite3.SQLITE_OK:
    raise RefusedError("SQLite cannot register the directory filesystem")


def uri(path: Path, mode: str) -> str:
    """Select the native file operations with descriptor paths kept intact."""
    return f"{path.as_uri()}?mode={mode}&vfs=nott-directory"

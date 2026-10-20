"""A process identity that a freed pid cannot impersonate, and the two probes.


A bare pid is not an identity: the kernel reuses it, and the very next fork can
hand it to an unrelated process. The triple below pins the pid to the boot it was
allocated in and to the start time it was allocated at, so "is this still the same
process?" has an answer that survives reuse.

The probes run on Linux (`/proc`) and macOS (the `kern.proc` sysctl), the two
platforms NOTT runs on. Anywhere else they RAISE a typed refusal: there is no
fallback identity and no invented default -- a probe that cannot prove identity
says so instead of guessing one.

`parse_proc_start_ticks` is a pure string parser with no platform of its own, so
it is proven on any host; the Linux probes that touch `/proc` read it through the
injectable `PROC_ROOT`.
"""

import ctypes
import errno
import functools
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import NSpawnError

#: The root the two probes resolve against. The canonical boot-id path is
#: /proc/sys/kernel/random/boot_id under this root. Production leaves this at the real
#: ``/proc``; tests INJECT a fixture tree here so the probe code paths run on
#: any host — the coverage-by-injection pattern (journal §31). A bare ``Path``
#: constant rather than a parameter: the oracle reaches these probes from
#: custody paths that must not grow a root argument.
PROC_ROOT = Path("/proc")

#: The 0-based index of the start-time field among the whitespace-separated
#: fields that follow the command in `/proc/<pid>/stat`.
START_TICKS_FIELD_INDEX = 19

#: The 0-based index of the process group among the same fields.
PGRP_FIELD_INDEX = 2

U32_MAX = 2**32 - 1

# macOS: the `kern.proc` sysctl. `struct kinfo_proc` is 648 bytes; its first member
# `kp_proc` (struct extern_proc) starts with `p_starttime` (a timeval) and holds
# `p_stat` at 36 and `p_pid` at 40. Measured on macOS 26 arm64 on 2026-09-25; the
# layout is the Darwin ABI on arm64 and x86_64 alike.
_CTL_KERN = 1
_KERN_PROC = 14
_KERN_PROC_PID = 1
_KERN_PROC_PGRP = 2
_KINFO_PROC_SIZE = 648
_P_STARTTIME = struct.Struct("=qi")
_P_FLAG = struct.Struct("=i")
_P_FLAG_OFFSET = 32
_P_WEXIT = 0x2000
_P_STAT = struct.Struct("=b")
_P_STAT_OFFSET = 36
_P_PID = struct.Struct("=i")
_P_PID_OFFSET = 40
_SZOMB = 5


class ProcessIdentityError(NSpawnError):
    """Why an identity could not be established or re-checked.

    Its own type, because this module is the one every custody path depends on,
    so it MUST NOT depend on any of them. Consumers wrap it.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        Exception.__init__(self, message)


@dataclass(frozen=True)
class ProcessIdentity:
    """A pid pinned to its boot and to the tick it started at."""

    pid: int
    boot_id: str
    start_ticks: int

    def __post_init__(self) -> None:
        if not isinstance(self.pid, int) or isinstance(self.pid, bool):
            raise TypeError("pid must be an integer")
        if self.pid < 0 or self.pid > U32_MAX:
            raise ValueError(f"pid must fit an unsigned 32-bit integer, got {self.pid}")
        if self.start_ticks < 0:
            raise ValueError("start_ticks must not be negative")


def _platform() -> str:
    """The one platform switch: `linux`, `darwin`, or anything else (refused)."""
    return "linux" if sys.platform.startswith("linux") else sys.platform


def _read_boot_id() -> str:
    try:
        boot_id = (
            (PROC_ROOT / "sys/kernel/random/boot_id")
            .read_text(encoding="utf-8")
            .strip()
        )
    except OSError as error:
        raise ProcessIdentityError(f"cannot read boot id: {error}") from error
    if not boot_id:
        raise ProcessIdentityError("kernel boot id is empty")
    return boot_id


def _read_stat(pid: int) -> str | None:
    """The process's stat line, or None when the process is simply gone."""
    try:
        return (PROC_ROOT / f"{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError, ProcessLookupError:
        return None
    except OSError as error:
        raise ProcessIdentityError(f"cannot read process stat: {error}") from error


def parse_proc_start_ticks(stat: str) -> int:
    """Read the start-time field out of a `/proc/<pid>/stat` line.

    The command sits in parentheses and may itself contain spaces AND a closing
    parenthesis, so the scan takes the LAST `)` as the command terminator and
    counts fields only after it.
    """
    command_end = stat.rfind(")")
    if command_end < 0:
        raise ProcessIdentityError("process stat has no command terminator")
    fields = stat[command_end + 1 :].split()
    if len(fields) <= START_TICKS_FIELD_INDEX:
        raise ProcessIdentityError("process stat has no start-time field")
    raw = fields[START_TICKS_FIELD_INDEX]
    try:
        return int(raw)
    except ValueError as error:
        raise ProcessIdentityError(f"invalid process start time: {error}") from error


@functools.cache
def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def _sysctl(mib: tuple[int, ...]) -> bytes:
    """One `sysctl` table read; b"" when the kernel names no such process.

    A process-group table can grow between the size query and the read (ENOMEM):
    each retry asks for room for a few more rows.
    """
    name = (ctypes.c_int * len(mib))(*mib)
    spare = 0
    for _ in range(4):
        size = ctypes.c_size_t(0)
        if _libc().sysctl(name, len(mib), None, ctypes.byref(size), None, 0) != 0:
            code = ctypes.get_errno()
            if code == errno.ESRCH:
                return b""
            raise ProcessIdentityError(
                f"cannot read kern.proc: {errno.errorcode.get(code, code)}"
            )
        if size.value == 0:
            return b""
        size.value += spare
        buffer = ctypes.create_string_buffer(size.value)
        if _libc().sysctl(name, len(mib), buffer, ctypes.byref(size), None, 0) == 0:
            return buffer.raw[: size.value]
        code = ctypes.get_errno()
        if code == errno.ESRCH:
            return b""
        if code != errno.ENOMEM:
            raise ProcessIdentityError(
                f"cannot read kern.proc: {errno.errorcode.get(code, code)}"
            )
        spare += 8 * _KINFO_PROC_SIZE
    raise ProcessIdentityError("cannot read kern.proc: the table kept growing")


def _darwin_boot_id() -> str:
    """`kern.bootsessionuuid`: a UUID drawn anew at every boot, like Linux's boot_id."""
    size = ctypes.c_size_t(64)
    buffer = ctypes.create_string_buffer(size.value)
    if (
        _libc().sysctlbyname(
            b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0
        )
        != 0
    ):
        code = ctypes.get_errno()
        raise ProcessIdentityError(
            f"cannot read boot id: {errno.errorcode.get(code, code)}"
        )
    boot_id = buffer.value.decode("ascii").strip()
    if not boot_id:
        raise ProcessIdentityError("kernel boot id is empty")
    return boot_id


def _darwin_rows(mib: tuple[int, ...]) -> list[tuple[int, int, int, int]]:
    """(pid, p_stat, start microseconds since the epoch, p_flag) per `kinfo_proc` row."""
    raw = _sysctl(mib)
    rows = []
    for offset in range(0, len(raw) - _KINFO_PROC_SIZE + 1, _KINFO_PROC_SIZE):
        seconds, micros = _P_STARTTIME.unpack_from(raw, offset)
        (state,) = _P_STAT.unpack_from(raw, offset + _P_STAT_OFFSET)
        (pid,) = _P_PID.unpack_from(raw, offset + _P_PID_OFFSET)
        (flag,) = _P_FLAG.unpack_from(raw, offset + _P_FLAG_OFFSET)
        rows.append((pid, state, seconds * 1_000_000 + micros, flag))
    return rows


def _boot_id() -> str:
    return _darwin_boot_id() if _platform() == "darwin" else _read_boot_id()


def _start_ticks(pid: int) -> int | None:
    """The start of `pid` on this host, or None when it is gone (reaped).

    Linux: clock ticks since boot, from `/proc/<pid>/stat`. macOS: `p_starttime` in
    microseconds since the epoch. The unit differs; a triple is only ever compared
    on the host that wrote it, which the boot id pins. An unreaped zombie answers
    on both, as `child.poll` requires.
    """
    if _platform() == "darwin":
        rows = [
            row
            for row in _darwin_rows((_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid))
            if row[0] == pid
        ]
        return rows[0][2] if rows else None
    stat = _read_stat(pid)
    return None if stat is None else parse_proc_start_ticks(stat)


def _supported() -> bool:
    return _platform() in ("linux", "darwin")


def capture_process_identity(pid: int) -> ProcessIdentity:
    """Establish an identity for `pid`, or REFUSE on a platform that cannot."""
    if not _supported():
        raise ProcessIdentityError(
            "non-reusable process identity is supported only on Linux and macOS"
        )
    boot_id = _boot_id()
    start_ticks = _start_ticks(pid)
    if start_ticks is None:
        raise ProcessIdentityError(
            f"cannot read process stat: no such process {pid}",
        )
    return ProcessIdentity(pid=pid, boot_id=boot_id, start_ticks=start_ticks)


def process_identity_matches(identity: ProcessIdentity) -> bool:
    """True when the live process is still the one this identity named.

    A different boot, a vanished process, or a different start tick each answer
    False. A platform that cannot check REFUSES rather than answering True.
    """
    if not _supported():
        raise ProcessIdentityError(
            "stale process identity cannot be verified on this platform"
        )
    if _boot_id() != identity.boot_id:
        return False
    return _start_ticks(identity.pid) == identity.start_ticks


def of_earlier_boot(identity: ProcessIdentity) -> bool:
    """True when `identity` names a process of an earlier boot, so no process of it
    can still live. A platform that cannot read its boot id REFUSES."""
    if not _supported():
        raise ProcessIdentityError("the boot id cannot be read on this platform")
    return _boot_id() != identity.boot_id


def parse_proc_pgrp(stat: str) -> int | None:
    """The `pgrp` field of a `/proc/<pid>/stat` line, or None when unreadable."""
    command_end = stat.rfind(")")
    if command_end < 0:
        return None
    fields = stat[command_end + 1 :].split()
    if len(fields) <= PGRP_FIELD_INDEX:
        return None
    try:
        return int(fields[PGRP_FIELD_INDEX])
    except ValueError:
        return None


def group_member_pids(pgid: int) -> tuple[int, ...]:
    """Every pid in process group `pgid`, the leader included; () when none.

    A zombie is still a member until it is reaped, as its identity still matches.
    """
    if _platform() == "darwin":
        return tuple(
            row[0]
            for row in _darwin_rows((_CTL_KERN, _KERN_PROC, _KERN_PROC_PGRP, pgid))
        )
    try:
        entries = list(PROC_ROOT.iterdir())
    except OSError:
        return ()
    members: list[int] = []
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        if parse_proc_pgrp(stat) == pgid:
            members.append(int(entry.name))
    return tuple(members)


def group_is_only_zombies(pgid: int) -> bool:
    """True when group `pgid` has members and none is still running: each is an
    unreaped zombie or, on macOS, already exiting (`P_WEXIT`).

    macOS answers `killpg` on such a group with EPERM, the same errno it gives for
    a live member the caller may not signal; this tells the two apart. A member
    between `P_WEXIT` and `SZOMB` was measured there (1 in 300 group kills).
    """
    if _platform() == "darwin":
        rows = _darwin_rows((_CTL_KERN, _KERN_PROC, _KERN_PROC_PGRP, pgid))
        return bool(rows) and all(
            state == _SZOMB or flag & _P_WEXIT for _, state, _, flag in rows
        )
    states = []
    for pid in group_member_pids(pgid):
        stat = _read_stat(pid)
        if stat is not None:
            states.append(stat[stat.rfind(")") + 1 :].split()[0])
    return bool(states) and all(state == "Z" for state in states)

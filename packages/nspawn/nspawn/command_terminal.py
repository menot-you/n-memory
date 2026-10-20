"""The shared PTY transport of a command that nSPAWN owns."""

from __future__ import annotations

import base64
import codecs
import json
import os
import pty
import select
import signal
import sys
import termios
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .custody import AggregateScope
from .errors import NERR_2002, NSpawnError


class CommandRefused(NSpawnError):
    def __init__(self, message: str) -> None:
        super().__init__(NERR_2002, message)


def canonical_cwd(value: object) -> str:
    if not isinstance(value, str) or "\0" in value or not Path(value).is_absolute():
        raise CommandRefused("cwd must be an absolute directory")
    path = Path(value).resolve()
    if not path.is_dir():
        raise CommandRefused("cwd must name an existing directory")
    return str(path)


# A tty becomes the controlling tty on open, and /proc gives session ids, on Linux.
LINUX = sys.platform == "linux"
RING = 256 * 1024
CHUNK = 16 * 1024
INPUT_LIMIT = 64 * 1024
RECORDS = 64
INPUT_WAIT_S = 1.0
HUP_WAIT_S = 2.0
EXIT_WAIT_S = 5.0
# Only these host variables enter the custody launcher. The scope removes its
# control variables before execution. The target receives the Push policy below.
KEPT = ("HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "XDG_RUNTIME_DIR")
# posix_spawn here has no chdir action: sh changes to the folder, then becomes the
# command, so the session leader's pid is the command's.
CHDIR = 'cd -- "$1" && shift && exec "$@"'


def number(params: dict[str, Any], key: str, low: int, high: int) -> int:
    value = params.get(key)
    if type(value) is not int or not low <= value <= high:
        raise CommandRefused(f"{key} must be an integer from {low} to {high}")
    return value


def writer(params: dict[str, Any]) -> str | None:
    if "writerId" not in params:
        return None
    value = params["writerId"]
    if not isinstance(value, str) or len(value) != 36:
        raise CommandRefused("writerId must be a UUID")
    try:
        return str(UUID(value))
    except ValueError:
        raise CommandRefused("writerId must be a UUID") from None


def input_sequence(params: dict[str, Any]) -> tuple[str | None, int | None]:
    writer_id = writer(params)
    if (writer_id is not None) != ("inputSeq" in params):
        raise CommandRefused("writerId and inputSeq are required together")
    sequence = (
        number(params, "inputSeq", 1, 2**53 - 1) if writer_id is not None else None
    )
    return writer_id, sequence


@dataclass
class InputProgress:
    sequence: int
    size: int
    accepted: int = 0


def size(params: dict[str, Any]) -> tuple[int, int]:
    return number(params, "cols", 2, 500), number(params, "rows", 2, 200)


class Screen:
    """The visible text of a VT100 stream: placement, cursor moves and erases."""

    # shortcut: no colors, wide characters, scroll regions or alternate screen; use
    # a full emulator if an agent's read shows a wrong screen.

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.lines = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0
        self.state = ""
        self.params = ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.private_modes: set[int] = set()

    def feed(self, data: bytes) -> None:
        for char in self.decoder.decode(data):
            self.put(char)

    def text(self) -> list[str]:
        return ["".join(line).rstrip() for line in self.lines]

    def resize(self, cols: int, rows: int) -> None:
        self.lines = [(line + [" "] * cols)[:cols] for line in self.lines[:rows]]
        self.lines.extend([[" "] * cols for _ in range(rows - len(self.lines))])
        self.cols, self.rows = cols, rows
        self.row, self.col = min(self.row, rows - 1), min(self.col, cols)

    def linefeed(self) -> None:
        if self.row == self.rows - 1:
            self.lines = [*self.lines[1:], [" "] * self.cols]
        else:
            self.row += 1

    def put(self, char: str) -> None:
        if self.state == "esc":
            if char == "c":
                self.private_modes.clear()
            self.state = {"[": "csi", "]": "osc"}.get(char, "")
            self.state = "charset" if char in "()*+#%" else self.state
            self.params = ""
        elif self.state == "charset":
            self.state = ""
        elif self.state == "osc":
            self.state = {"\x07": "", "\x1b": "esc"}.get(char, "osc")
        elif self.state == "csi-ignore":
            if "@" <= char <= "~":
                self.state = ""
        elif self.state == "csi":
            if "@" <= char <= "~":
                self.state = ""
                self.csi(char)
            elif len(self.params) < 64:
                self.params += char
            else:
                self.params, self.state = "", "csi-ignore"
        elif char == "\x1b":
            self.state = "esc"
        elif char == "\r":
            self.col = 0
        elif char in "\n\x0b\x0c":
            self.linefeed()
        elif char == "\b":
            self.col = max(0, min(self.col, self.cols - 1) - 1)
        elif char == "\t":
            self.col = min(self.cols - 1, self.col // 8 * 8 + 8)
        elif char >= " " and char != "\x7f":
            if self.col >= self.cols:
                self.col = 0
                self.linefeed()
            self.lines[self.row][self.col] = char
            self.col += 1

    def csi(self, final: str) -> None:
        if self.params.startswith("?") and final in {"h", "l"}:
            for value in self.params[1:].split(";"):
                mode = int(value) if value.isdecimal() else 0
                if mode in {1, 1049, 2004}:
                    if final == "h":
                        self.private_modes.add(mode)
                    else:
                        self.private_modes.discard(mode)
            return
        if self.params[:1] in {"?", ">", "=", "<"}:
            return
        values = [int(v) if v.isdecimal() else 0 for v in self.params.split(";")]
        n = max(values[0], 1)
        line = self.lines[self.row]
        col = min(self.col, self.cols - 1)
        if final in "Hf":
            self.row = min(n, self.rows) - 1
            self.col = min(max(values[1] if len(values) > 1 else 1, 1), self.cols) - 1
        elif final in "ABCDGd":
            row, col = {
                "A": (self.row - n, col),
                "B": (self.row + n, col),
                "C": (self.row, col + n),
                "D": (self.row, col - n),
                "G": (self.row, n - 1),
                "d": (n - 1, col),
            }[final]
            self.row = max(0, min(row, self.rows - 1))
            self.col = max(0, min(col, self.cols - 1))
        elif final in "JK":
            mode = values[0]
            if final == "J":
                below, above = range(self.row + 1, self.rows), range(self.row)
                for row in {0: below, 1: above}.get(mode, range(self.rows)):
                    self.lines[row] = [" "] * self.cols
            start, end = {0: (col, self.cols), 1: (0, col + 1)}.get(
                mode, (0, self.cols)
            )
            self.lines[self.row][start:end] = [" "] * (end - start)
        elif final == "P":
            del line[col : col + n]
            line.extend([" "] * (self.cols - len(line)))
        elif final == "@":
            n = min(n, self.cols - col)
            line[col:col] = [" "] * n
            del line[self.cols :]
        elif final == "X":
            line[col : col + n] = [" "] * len(line[col : col + n])


def members(session: int) -> list[int]:
    """The live (not zombie) processes whose session id is `session`."""
    found = []
    for entry in os.scandir("/proc"):
        if entry.name.isdigit() and sid(int(entry.name)) == session:
            found.append(int(entry.name))
    return found


def sid(pid: int) -> int | None:
    """`pid`'s session id (field 6 of its stat), or None when it is gone or a zombie."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat:
            fields = stat.read().rsplit(b")", 1)[1].split()
    except OSError:
        return None
    return None if fields[0] == b"Z" else int(fields[3])


class Terminal:
    """One PTY child of the host, its last output and the screen that output draws."""

    def __init__(
        self,
        params: dict[str, Any],
        notify: Callable[[dict[str, Any]], None],
        *,
        scope: AggregateScope,
        retain: Callable[[dict[str, Any]], None] | None = None,
        identity: dict[str, int],
    ) -> None:
        if not LINUX:
            raise CommandRefused("terminals need a Linux host")
        self.cwd = canonical_cwd(params.get("cwd"))
        self.cols, self.rows = size(params)
        environment = {key: os.environ[key] for key in KEPT if key in os.environ}
        environment |= {
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "NOTT_MEMORY_POLICY": "push",
        }
        argv = params["argv"]
        name = params.get("name", os.path.basename(argv[0]))
        if not isinstance(name, str) or len(name) > 80:
            raise CommandRefused("name must be text of at most 80 characters")
        self.ident, self.name, self.argv = f"terminal-{uuid4().hex}", name, argv
        self.replaces: str | None = None
        self.notify = notify
        self.retain = retain
        master, slave = pty.openpty()
        try:
            termios.tcsetwinsize(slave, (self.rows, self.cols))
            target = [
                sys.executable,
                "-P",
                "-m",
                "nspawn.command_exec",
                json.dumps(identity),
                *argv,
            ]
            launch = [
                "/bin/sh",
                "-c",
                CHDIR,
                "nott-terminal",
                self.cwd,
                *target,
            ]
            launch = scope.command(launch)
            environment = scope.environment(environment)
            self.pid = os.posix_spawn(
                launch[0],
                launch,
                environment,
                # Opened after setsid, the slave becomes the controlling tty; no
                # other host fd reaches the child.
                file_actions=[
                    (os.POSIX_SPAWN_OPEN, 0, os.ttyname(slave), os.O_RDWR, 0),
                    (os.POSIX_SPAWN_DUP2, 0, 1),
                    (os.POSIX_SPAWN_DUP2, 0, 2),
                    (os.POSIX_SPAWN_CLOSEFROM, 3),
                ],
                setsid=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        # `lock` guards output and state. `writing` guards input and descriptor close.
        self.lock = threading.Lock()
        self.writing = threading.Lock()
        self.input_writers: dict[str, InputProgress] = {}
        self.ring = bytearray()
        self.seq = 0
        self.exited: int | None = None
        self.records: deque[dict[str, Any]] = deque(maxlen=RECORDS)
        self.screen = Screen(self.cols, self.rows)
        self.closing = threading.Event()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.waiter = threading.Thread(target=self.wait, daemon=True)
        self.reader.start()
        self.waiter.start()

    def info(self) -> dict[str, Any]:
        return {
            "terminalId": self.ident,
            "name": self.name,
            "cwd": self.cwd,
            "argv": self.argv,
            "cols": self.cols,
            "rows": self.rows,
            "exited": self.exited,
            **({"replaces": self.replaces} if self.replaces is not None else {}),
        }

    def read(self) -> None:
        """Copy the child's output to the ring and to every app, until the PTY ends."""
        try:
            ready = select.poll()
            ready.register(self.master, select.POLLIN)
            while not self.closing.is_set():
                if not ready.poll(250):
                    continue
                try:
                    data = os.read(self.master, CHUNK)
                except BlockingIOError:
                    continue
                except OSError:
                    return  # EIO: every slave fd is closed
                if not data:
                    return
                with self.lock:
                    event = {
                        "terminalId": self.ident,
                        "seq": self.seq + len(data),
                        "data": base64.b64encode(data).decode(),
                    }
                    if self.retain is not None:
                        self.retain(event)
                    self.screen.feed(data)
                    self.ring += data
                    del self.ring[:-RING]
                    self.seq += len(data)
                self.notify(event)
        except CommandRefused:
            return
        finally:
            with self.writing:
                os.close(self.master)
                self.master = -1

    def wait(self) -> None:
        """Tell the exit; never reap (WNOWAIT), so the pid stays reserved."""
        try:
            status = os.waitid(os.P_PID, self.pid, os.WEXITED | os.WNOWAIT)
        except ChildProcessError:
            return  # the close reaped it
        assert status is not None  # no WNOHANG
        code = status.si_status
        code = code if status.si_code == os.CLD_EXITED else 128 + code
        # The shell's last output goes before its exit when nothing else holds the PTY;
        # a job that still holds it delays the exit by at most this wait.
        self.reader.join(EXIT_WAIT_S)
        with self.lock:
            self.exited = code
            seq = self.seq
        self.notify({"terminalId": self.ident, "seq": seq, "data": "", "exited": code})

    def write(
        self, data: bytes, writer_id: str | None = None, sequence: int | None = None
    ) -> None:
        deadline = time.monotonic() + INPUT_WAIT_S
        with self.writing:
            progress = None
            if writer_id is not None:
                assert sequence is not None
                progress = self.input_writers.get(writer_id)
                previous = progress.sequence if progress is not None else 0
                if sequence < previous:
                    return
                if sequence == previous and progress is not None:
                    if len(data) != progress.size:
                        raise CommandRefused(
                            "the input sequence has a different length"
                        )
                    if progress.accepted == progress.size:
                        return
                elif sequence == previous + 1:
                    if progress is None and len(self.input_writers) >= RECORDS:
                        # shortcut: Retry history holds 64 writers. Add a lease if more stay active.
                        del self.input_writers[next(iter(self.input_writers))]
                    progress = InputProgress(sequence, len(data))
                    self.input_writers[writer_id] = progress
                else:
                    raise CommandRefused("the input sequence is out of order")
            ready = select.poll()
            if self.master >= 0:
                ready.register(self.master, select.POLLOUT)
            view = memoryview(data)[progress.accepted if progress is not None else 0 :]
            while view:
                if self.closing.is_set() or self.exited is not None or self.master < 0:
                    raise CommandRefused("the terminal exited")
                try:
                    count = os.write(self.master, view)
                    view = view[count:]
                    if progress is not None:
                        progress.accepted += count
                except BlockingIOError:
                    left = deadline - time.monotonic()
                    if left <= 0 or not ready.poll(int(left * 1000) + 1):
                        raise CommandRefused(
                            "the terminal does not take input now: it took "
                            f"{len(data) - len(view)} of {len(data)} bytes"
                        ) from None

    def resize(self, params: dict[str, Any]) -> None:
        cols, rows = size(params)
        if "redraw" in params and type(params["redraw"]) is not bool:
            raise CommandRefused("redraw must be true or false")
        with self.writing:
            if params.get("redraw") is True:
                with self.lock:
                    cols, rows = self.cols, self.rows
            if self.master >= 0:
                termios.tcsetwinsize(self.master, (rows, cols))
                if params.get("redraw") is True:
                    # A real size change makes programs that ignore same-size signals redraw.
                    termios.tcsetwinsize(
                        self.master, (rows, cols - 1 if cols > 2 else 3)
                    )
                    time.sleep(0.05)
                    termios.tcsetwinsize(self.master, (rows, cols))
            with self.lock:
                self.cols, self.rows = cols, rows
                self.screen.resize(cols, rows)

    def attach(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        since = number(params, "since", 0, 2**53 - 1) if "since" in params else None
        writer_id = writer(params)
        with self.writing, self.lock:
            if since is None:
                since = self.seq - len(self.ring)
            reset = not self.seq - len(self.ring) <= since <= self.seq
            data = (
                self.ring[-(self.seq - since) :]
                if not reset and since < self.seq
                else b""
            )
            progress = (
                self.input_writers.get(writer_id) if writer_id is not None else None
            )
            return {
                "info": self.info(),
                "data": base64.b64encode(data).decode(),
                "seq": self.seq,
                "sends": list(self.records),
                "reset": reset,
                "privateModes": sorted(self.screen.private_modes),
                "inputSeq": progress.sequence if progress is not None else 0,
            }

    def look(self) -> dict[str, Any]:
        """Read the screen that every output chunk updates, including discarded bytes."""
        with self.lock:
            return {
                "terminalId": self.ident,
                "lines": self.screen.text(),
                "cursor": {"row": self.screen.row, "col": self.screen.col},
                "cols": self.cols,
                "rows": self.rows,
                "seq": self.seq,
                "exited": self.exited,
            }

    def close(self) -> None:
        """Hang up, kill every process left in the session, then reap the shell."""
        self.closing.set()
        with suppress(ProcessLookupError):
            os.killpg(self.pid, signal.SIGHUP)
        self.waiter.join(HUP_WAIT_S)
        for _ in range(10):
            found = members(self.pid)
            if not found:
                break
            for pid in found:
                try:
                    handle = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    # The pidfd pins the process: a pid reused since the scan fails
                    # this check and is never signalled.
                    if sid(pid) == self.pid:
                        with suppress(ProcessLookupError):
                            signal.pidfd_send_signal(handle, signal.SIGKILL)
                finally:
                    os.close(handle)
            time.sleep(0.05)
        with suppress(ChildProcessError):
            os.waitpid(self.pid, 0)
        self.reader.join(1.0)
        self.waiter.join(1.0)

"""PATH command identity and the private control door of one registered console."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from nott_socket.door import AcceptError, Admitted, UnixDoor

from .command_terminal import (
    INPUT_LIMIT,
    CommandRefused,
    Terminal,
    input_sequence,
    number,
    size,
)
from .custody import AggregateScope
from .limits import MAX_DURABLE_IDENTIFIER_BYTES, MAX_DURABLE_ROW_BYTES
from .owner_roster import Roster
from .pump import MAX_RUN_OUTPUT_BYTES
from .session import FRAME_BYTES, descriptor

#: The retained output file of one command never grows past this.
RETAINED_BYTES = MAX_RUN_OUTPUT_BYTES
#: How long one control connection has to send its request and read the answer.
REQUEST_S = 5.0
_WRAPPERS = {"env", "nice", "nohup", "setsid", "stdbuf", "timeout"}


def _unwrapped_executables(name: str, path: Path, arguments: list[str]) -> set[str]:
    names = {Path(name).name, path.name}
    current = Path(name).name if Path(name).name in _WRAPPERS else path.name
    tokens = list(arguments)
    idx = 0
    while current in _WRAPPERS and idx < len(tokens):
        arg = tokens[idx]
        idx += 1
        if arg == "--":
            if idx < len(tokens):
                target = Path(tokens[idx]).name
                names.add(target)
                current = target
                idx += 1
            continue
        if current == "env":
            if arg == "-" or ("=" in arg and not arg.startswith("-")):
                continue
            if arg in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
                if arg in {"-S", "--split-string"} and idx < len(tokens):
                    tokens = tokens[idx].split() + tokens[idx + 1 :]
                    idx = 0
                else:
                    idx += 1
                continue
            if arg.startswith(("-u", "-C", "-S")) and len(arg) > 2:
                if arg.startswith("-S"):
                    tokens = arg[2:].split() + tokens[idx:]
                    idx = 0
                continue
            if arg.startswith("--split-string="):
                tokens = arg.partition("=")[2].split() + tokens[idx:]
                idx = 0
                continue
        elif (
            current == "nice"
            and arg in {"-n", "--adjustment"}
            or current == "timeout"
            and arg in {"-k", "--kill-after", "-s", "--signal"}
            or current == "stdbuf"
            and arg
            in {
                "-i",
                "-o",
                "-e",
                "--input",
                "--output",
                "--error",
            }
        ):
            idx += 1
            continue
        if arg.startswith("-"):
            continue
        if current == "timeout":
            current = "env"
            continue
        target = Path(arg).name
        names.add(target)
        current = target
    return names


class NativeCommandRoute(CommandRefused):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.data = {"kind": "native_route", "provider": provider}
        super().__init__(
            f"Use the existing {provider} account route for this executable."
        )


def resolve(value: object, roster: Roster, *, cwd: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {
        "executable",
        "arguments",
        "cols",
        "rows",
        "name",
        "centerId",
        "replacesRun",
    }:
        raise CommandRefused("command must identify an executable and its arguments")
    name, arguments = value.get("executable"), value.get("arguments", [])
    if not isinstance(name, str) or not name or "\0" in name:
        raise CommandRefused("executable must contain a PATH command name")
    if not isinstance(arguments, list) or any(
        not isinstance(item, str) or "\0" in item for item in arguments
    ):
        raise CommandRefused("arguments must contain strings without NUL")
    if len(json.dumps(value).encode()) > MAX_DURABLE_ROW_BYTES:
        raise CommandRefused("command exceeds the durable record boundary")
    center = value.get("centerId")
    if (
        not isinstance(center, str)
        or not center
        or len(center.encode()) > MAX_DURABLE_IDENTIFIER_BYTES
    ):
        raise CommandRefused("centerId must identify the owning command center")
    label = value.get("name", Path(name).name)
    if not isinstance(label, str) or len(label) > 80:
        raise CommandRefused("name must contain at most 80 characters")
    replaces = value.get("replacesRun")
    if replaces is not None and (
        not isinstance(replaces, str)
        or not replaces
        or len(replaces.encode()) > MAX_DURABLE_IDENTIFIER_BYTES
    ):
        raise CommandRefused("replacesRun must identify an earlier command run")
    size(value)
    path_entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
    search = os.pathsep.join(
        str(Path(entry) if Path(entry).is_absolute() else Path(cwd) / entry)
        for entry in path_entries
    )
    requested = (
        str(Path(cwd) / name) if "/" in name and not Path(name).is_absolute() else name
    )
    selected = shutil.which(requested, path=search)
    if selected is None:
        raise CommandRefused("The executable is not available on PATH.")
    path = Path(selected).resolve()
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise CommandRefused("The executable must be a regular file.")
    checked = _unwrapped_executables(name, path, arguments)
    if checked & {"qwen", "strife"}:
        raise CommandRefused("This executable is outside the admitted provider scope.")
    for provider in roster.providers:
        if provider.bin is None:
            continue
        try:
            native_file = provider.bin.stat()
        except FileNotFoundError:
            continue
        if os.path.samestat(native_file, info):
            if arguments:
                raise CommandRefused(
                    "Native provider arguments require the existing account route."
                )
            if provider.kind not in {"claude", "codex", "agy"}:
                raise CommandRefused(
                    "This native executable has no admitted account route."
                )
            raise NativeCommandRoute(provider.kind)
    if Path(name).name in {"claude", "codex", "agy"}:
        if arguments:
            raise CommandRefused(
                "Native provider arguments require the existing account route."
            )
        raise NativeCommandRoute(Path(name).name)
    return {
        **value,
        "executable": str(path),
        "launchPath": os.path.abspath(selected),
        "arguments": arguments,
        "identity": {
            "device": info.st_dev,
            "inode": info.st_ino,
            "size": info.st_size,
            "modifiedNs": info.st_mtime_ns,
        },
    }


def verify(selected: dict[str, Any], info: os.stat_result | None = None) -> None:
    info = Path(selected["executable"]).stat() if info is None else info
    actual = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "modifiedNs": info.st_mtime_ns,
    }
    if actual != selected["identity"]:
        raise CommandRefused("The executable changed after command admission.")


def request(stream: socket.socket, deadline: float) -> bytes:
    """The first line `stream` sends by `deadline`, at most `FRAME_BYTES` long."""
    data = bytearray()
    while b"\n" not in data and len(data) <= FRAME_BYTES:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("The command request did not arrive in time.")
        stream.settimeout(left)
        chunk = stream.recv(64 * 1024)
        if not chunk:
            break
        data += chunk
    return bytes(data.partition(b"\n")[0])


class Command:
    def __init__(
        self,
        selected: dict[str, Any],
        *,
        cwd: str,
        run_id: str,
        run_dir: Path,
        scope: AggregateScope,
        exited: Callable[[int], None],
    ) -> None:
        self.selected, self.run_id, self.scope, self.exited = (
            selected,
            run_id,
            scope,
            exited,
        )
        self.root = Path(tempfile.mkdtemp(prefix="nscm-", dir="/var/tmp"))
        self.door = UnixDoor.bind(self.root / "control.sock")
        self.lock = threading.Lock()
        self.closing = threading.Lock()
        self.stopped = threading.Event()
        self.retention_failure: str | None = None
        # Read here, not at import, so a test can lower it.
        self.limit = RETAINED_BYTES
        # Bytes emptied from the retained file; output cursors count them.
        self.dropped = 0
        # Records in the retained file, and records emptied from it: the
        # transcript's line cursor counts both.
        self.records = 0
        self.dropped_records = 0
        self.retained_seq = 0
        self.output_path = run_dir / "stdout"
        descriptor_fd = os.open(
            self.output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        self.output = os.fdopen(descriptor_fd, "wb", buffering=0)
        try:
            verify(selected)
            self.terminal = Terminal(
                {
                    "cwd": cwd,
                    "cols": selected["cols"],
                    "rows": selected["rows"],
                    "name": selected.get("name", Path(selected["executable"]).name),
                    "argv": [selected["launchPath"], *selected["arguments"]],
                },
                self.received,
                scope=scope,
                retain=self.retain,
                identity=selected["identity"],
            )
            self.terminal.replaces = selected.get("replaces")
        except BaseException:
            self.output.close()
            self.door.close()
            (self.root / "control.lock").unlink(missing_ok=True)
            self.root.rmdir()
            raise
        self.thread = threading.Thread(
            target=self.serve, daemon=True, name="nspawn-command-control"
        )
        self.thread.start()

    def data(self) -> dict[str, str]:
        return {"path": str(self.door.path), "control": str(self.door.path)}

    def info(self) -> dict[str, Any]:
        retention: dict[str, Any] = (
            {"droppedBytes": self.dropped} if self.dropped else {}
        )
        if self.retention_failure is not None:
            retention["failure"] = self.retention_failure
        return {
            **self.terminal.info(),
            "runId": self.run_id,
            "centerId": self.selected["centerId"],
            **({"retention": retention} if retention else {}),
        }

    def retain(self, event: dict[str, Any]) -> None:
        """Append `event` to the retained output, never past `RETAINED_BYTES`.

        A full file is emptied and restarts with one record that names the bytes
        and the records dropped so far: retention never stops the command. A
        failed write ends retention for the run, and `info()` reports it."""
        encoded = json.dumps({"type": "terminal_output", **event}).encode() + b"\n"
        with self.lock:
            if self.retention_failure is not None:
                return
            records = [encoded]
            if self.output.tell() + len(encoded) > self.limit:
                self.dropped += self.output.tell()
                self.dropped_records += self.records
                self.records = 0
                gap = {
                    "type": "terminal_output",
                    "terminalId": event["terminalId"],
                    "seq": self.retained_seq,
                    "data": "",
                    "droppedBytes": self.dropped,
                    "droppedRecords": self.dropped_records,
                }
                records.insert(0, json.dumps(gap).encode() + b"\n")
            wrapped = len(records) > 1
            start = 0 if wrapped else self.output.tell()
            try:
                if wrapped:
                    self.output.truncate(0)
                    self.output.seek(0)
                for record in records:
                    if self.output.write(record) != len(record):
                        raise OSError("the retained output took a short write")
                    self.records += 1
                    start = self.output.tell()
                self.retained_seq = event["seq"]
            except OSError as error:
                self.retention_failure = str(error)
                # A record cut short would end every reader at its line: keep the
                # whole records, the gap record of an emptied file included.
                with suppress(OSError):
                    self.output.truncate(start)

    def received(self, event: dict[str, Any]) -> None:
        if "exited" in event:
            self.exited(event["exited"])

    def output_page(self, params: dict[str, Any]) -> dict[str, Any]:
        offset = number(params, "offset", 0, 2**53 - 1)
        events: list[dict[str, Any]] = []
        # The cursor addresses retained records, dropped ones included: a cursor
        # behind the file starts at its first record, which names the gap. The
        # attach ring has its own limit.
        with self.terminal.lock, self.lock, self.output_path.open("rb") as retained:
            length = self.dropped + os.fstat(retained.fileno()).st_size
            if offset > length:
                raise CommandRefused("The output cursor exceeds the retained output.")
            start = max(offset, self.dropped)
            retained.seek(start - self.dropped)
            while self.dropped + retained.tell() - start < 256 * 1024:
                line = retained.readline(FRAME_BYTES)
                if not line:
                    break
                events.append(json.loads(line))
            next_offset = self.dropped + retained.tell()
            info = self.info()
        return {
            "events": events,
            "offset": next_offset,
            "more": next_offset < length,
            "info": info,
        }

    def operate(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "terminal/output":
            return self.output_page(params)
        if method == "terminal/attach":
            return {**self.terminal.attach(params), "info": self.info()}
        if method == "terminal/info":
            return self.info()
        if method == "terminal/read":
            return self.terminal.look()
        if method == "terminal/resize":
            self.terminal.resize(params)
            return {}
        if method == "terminal/input":
            data = params.get("data")
            if not isinstance(data, str) or len(data) > INPUT_LIMIT * 2:
                raise CommandRefused("data must contain at most 64 KiB of base64 input")
            try:
                decoded = base64.b64decode(data, validate=True)
            except ValueError as error:
                raise CommandRefused("data must contain base64 input") from error
            if len(decoded) > INPUT_LIMIT:
                raise CommandRefused("data must contain at most 64 KiB of input")
            writer, sequence = input_sequence(params)
            self.terminal.write(decoded, writer, sequence)
            return {}
        raise CommandRefused("The command control operation is not supported.")

    def serve(self) -> None:
        while not self.stopped.is_set():
            try:
                admitted = self.door.accept()
            except AcceptError as error:
                if self.stopped.is_set():
                    return
                if not error.transient:
                    raise
                # EMFILE and its kin repeat until a descriptor frees: do not spin.
                self.stopped.wait(0.05)
                continue
            if not isinstance(admitted, Admitted):
                continue
            with admitted.stream:
                self.answer(admitted.stream)

    def answer(self, stream: socket.socket) -> None:
        """Serve one request within `REQUEST_S`; no request ends the door."""
        deadline = time.monotonic() + REQUEST_S
        try:
            frame = json.loads(request(stream, deadline))
            if not isinstance(frame, dict) or not isinstance(frame.get("params"), dict):
                raise CommandRefused("The command request must contain parameters.")
            method = frame.get("method")
            if not isinstance(method, str):
                raise CommandRefused("The command request must name an operation.")
            result: dict[str, Any] = {"result": self.operate(method, frame["params"])}
        except (
            ValueError,
            TypeError,
            RecursionError,
            CommandRefused,
            OSError,
        ) as error:
            result = {"error": str(error)}
        except Exception as error:  # noqa: BLE001 - answered, and the door lives on
            # Best effort, as `_log_tick_failure`: a log never fails the answer.
            with suppress(Exception):
                print("nspawn.command: a control operation failed", file=sys.stderr)
                traceback.print_exception(error, file=sys.stderr)
            result = {"error": f"The command control operation failed: {error!r}"}
        try:
            stream.settimeout(max(deadline - time.monotonic(), 0.1))
            stream.sendall(json.dumps(result).encode() + b"\n")
        except OSError:
            pass  # the peer left or stopped reading: its answer is moot

    def close(self) -> None:
        with self.closing:
            if self.stopped.is_set():
                return
            self.terminal.close()
            self.stopped.set()
            self.door.close()
            self.thread.join(1)
            with self.lock:
                self.output.close()
            (self.root / "control.lock").unlink(missing_ok=True)
            self.root.rmdir()


def call(port: dict[str, str], method: str, params: dict[str, Any]) -> dict[str, Any]:
    checked = descriptor(port)
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(30)
        peer.connect(checked["control"])
        peer.sendall(json.dumps({"method": method, "params": params}).encode() + b"\n")
        with peer.makefile("rb") as incoming:
            reply = json.loads(incoming.readline(FRAME_BYTES + 1))
    if isinstance(reply, dict) and isinstance(reply.get("error"), str):
        raise CommandRefused(reply["error"])
    if not isinstance(reply, dict) or "result" not in reply:
        raise CommandRefused("The command custodian refused this operation.")
    result = reply["result"]
    if not isinstance(result, dict):
        raise CommandRefused("The command custodian returned an invalid result.")
    return result

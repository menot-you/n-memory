"""Native session process custody and its private ACP observation/control ports.

Only the supervisor creates the process. A host attaches to output, and every
native request enters the existing run primitive before it reaches stdin.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import stat
import tempfile
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from nott_socket.door import AcceptError, Admitted, UnixDoor

from . import child
from .custody import AggregateScope
from .errors import NSpawnError
from .redact import SecretNeedles

OPERATIONS = frozenset(
    {
        "initialize",
        "session/new",
        "session/load",
        "session/prompt",
        "session/cancel",
        "session/set_mode",
        "session/set_model",
        "session/set_config_option",
        "session/fork",
        "session/resume",
        "_nott/session/admit",
        "_nott/turn/start",
        "_nott/turn/resume",
        "_nott/turn/cancel",
        "_nott/turn/show",
        "_nott/model/list",
        "_nott/history/show",
        "_session/steering",
    }
)
FRAME_BYTES = 4 * 1024 * 1024


def operation(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) - {"jsonrpc", "id", "method", "params", "result", "error"}
        or value.get("jsonrpc", "2.0") != "2.0"
        or not isinstance(value.get("id"), (str, int))
        and value.get("method") != "session/cancel"
        or isinstance(value.get("id"), bool)
    ):
        raise ValueError("native session operation requires a request identity")
    if "method" in value:
        if value["method"] not in OPERATIONS or not isinstance(
            value.get("params"), dict
        ):
            raise ValueError("native session operation is not supported")
    elif ("result" in value) == ("error" in value):
        raise ValueError("native session response requires one outcome")
    return {"jsonrpc": "2.0", **value}


def descriptor(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"path", "control"}:
        raise ValueError("native session has no recorded control port")
    for location in value.values():
        if not isinstance(location, str) or not Path(location).is_absolute():
            raise ValueError("native session port must be absolute")
        path = Path(location)
        root = path.parent.lstat()
        entry = path.lstat()
        if (
            path.parent.resolve() != path.parent
            or root.st_uid != os.geteuid()
            or stat.S_IMODE(root.st_mode) != 0o700
            or entry.st_uid != os.geteuid()
            or not stat.S_ISSOCK(entry.st_mode)
            or stat.S_IMODE(entry.st_mode) != 0o600
        ):
            raise ValueError("native session port is not privately owned")
    return value


def submit(port: Mapping[str, str], frame: dict[str, Any]) -> None:
    """One request to the actual custody owner; an uncertain write is not replayed."""
    checked = descriptor(dict(port))
    encoded = json.dumps(operation(frame), allow_nan=False).encode() + b"\n"
    if len(encoded) > FRAME_BYTES:
        raise ValueError("native session operation exceeds the frame boundary")
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(30)
        client.connect(checked["control"])
        client.sendall(encoded)
        with client.makefile("rb") as response:
            result = json.loads(response.readline(FRAME_BYTES + 1))
        if result != {"accepted": True}:
            raise ValueError("native session custodian refused the operation")


class NativeSession:
    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        environment: dict[str, str],
        cwd: Path,
        run_dir: Path,
        needles: SecretNeedles,
        observe: Callable[[str, dict[str, Any]], None],
        scope: AggregateScope,
        pass_fds: tuple[int, ...] = (),
    ) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="nssp-", dir="/var/tmp"))
        self.output = UnixDoor.bind(self.root / "output.sock")
        self.control = UnixDoor.bind(self.root / "control.sock")
        self.observe = observe
        self.attached = threading.Event()
        self.stopped = threading.Event()
        self.input_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.buffer = bytearray()
        self.peer: socket.socket | None = None
        self.scope = scope
        self.failure: BaseException | None = None
        try:
            self.spawned = child.spawn_child(
                argv,
                cwd,
                environment,
                run_dir,
                needles,
                stdin_pipe=True,
                stdout_transform=self.received,
                scope=self.scope,
                pass_fds=pass_fds,
            )
        except BaseException:
            self.scope.close()
            self.output.close()
            self.control.close()
            for path in self.root.iterdir():
                path.unlink()
            self.root.rmdir()
            raise
        self.threads = [
            threading.Thread(
                target=self.guarded,
                args=(self.attach,),
                name="nspawn-session-output",
                daemon=True,
            ),
            threading.Thread(
                target=self.guarded,
                args=(self.serve,),
                name="nspawn-session-control",
                daemon=True,
            ),
        ]
        for worker in self.threads:
            worker.start()

    def data(self) -> dict[str, str]:
        return {"path": str(self.output.path), "control": str(self.control.path)}

    def guarded(self, action: Callable[[], None]) -> None:
        try:
            action()
        except (OSError, ValueError, TypeError, NSpawnError) as error:
            if not self.stopped.is_set():
                self.failure = error
                self.stopped.set()

    def attach(self) -> None:
        try:
            admitted = self.output.accept()
        except AcceptError:
            if self.stopped.is_set():
                return
            raise
        if not isinstance(admitted, Admitted):
            return
        with admitted.stream:
            self.peer = admitted.stream
            self.attached.set()
            # Requests use the run primitive; this peer only observes and keeps
            # its attachment alive. EOF closes exactly this managed activation.
            while not self.stopped.is_set() and admitted.stream.recv(1):
                raise ValueError("native session observer sent an unowned request")
        self.stopped.set()

    def received(self, raw: bytes) -> bytes:
        try:
            with self.output_lock:
                self.buffer.extend(raw)
                if len(self.buffer) > FRAME_BYTES:
                    raise ValueError("native session output exceeds the frame boundary")
                while b"\n" in self.buffer:
                    line, _, remaining = self.buffer.partition(b"\n")
                    self.buffer[:] = remaining
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise TypeError("native session emitted a non-object frame")
                    self.observe("output", frame)
            if not self.attached.wait(30) or self.peer is None:
                raise ValueError("native session has no observing controller")
            self.peer.sendall(raw)
            return raw
        except (OSError, ValueError, TypeError, NSpawnError) as error:
            if not self.stopped.is_set():
                self.failure = error
                self.stopped.set()
            if self.peer is not None and self.peer.fileno() >= 0:
                with contextlib.suppress(OSError):
                    self.peer.shutdown(socket.SHUT_RDWR)
            raise

    def serve(self) -> None:
        while not self.stopped.is_set():
            try:
                admitted = self.control.accept()
            except AcceptError:
                if self.stopped.is_set():
                    return
                raise
            if not isinstance(admitted, Admitted):
                continue
            with admitted.stream, admitted.stream.makefile("rb") as stream:
                frame = operation(json.loads(stream.readline(FRAME_BYTES + 1)))
                with self.input_lock:
                    encoded = json.dumps(frame, allow_nan=False).encode() + b"\n"
                    self.observe("input", frame)
                    self.spawned.write_stdin(encoded, timeout=30)
                admitted.stream.sendall(b'{"accepted":true}\n')

    def wait(self) -> None:
        while not self.stopped.wait(0.1):
            if self.spawned.poll() is not None:
                return
        if self.failure is not None:
            raise self.failure

    def close(self) -> None:
        self.stopped.set()
        if self.peer is not None and self.peer.fileno() >= 0:
            with contextlib.suppress(OSError):
                self.peer.shutdown(socket.SHUT_RDWR)
        self.scope.close()
        self.spawned.wait(timeout=10)
        self.output.close()
        self.control.close()
        for worker in self.threads:
            worker.join(1)
        for path in self.root.iterdir():
            path.unlink()
        self.root.rmdir()

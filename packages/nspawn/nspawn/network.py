"""A private CONNECT-only egress port for one isolated node process.

The outer network namespace has no host route. This owner-side Unix port admits
only the profile's explicit host/port destinations; it never accepts a generic
forwarding request or starts a producer on its caller's behalf.
"""

from __future__ import annotations

import ctypes
import os
import selectors
import signal
import socket
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .admission import AdmissionRefusal

HEADER_LIMIT = 8192
CONNECTION_LIMIT = 16
CONNECT_TIMEOUT_S = 60.0


def destination_read(value: str) -> tuple[str, int]:
    """An explicit ASCII hostname/IPv4 and port, without proxy option syntax."""
    host, separator, raw_port = value.rpartition(":")
    if (
        not separator
        or not host
        or not host.isascii()
        or any(
            character
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-*"
            for character in host
        )
        or "*" in host.removeprefix("*.")
        or not raw_port.isdecimal()
        or not 1 <= int(raw_port) <= 65535
    ):
        raise AdmissionRefusal("network destination must name a hostname and port")
    return host.lower(), int(raw_port)


def destination_admit(value: str, destinations: tuple[str, ...]) -> tuple[str, int]:
    """Match exactly or a declared domain suffix; overbroad requests are refused."""
    host, port = destination_read(value)
    if "*" in host:
        raise AdmissionRefusal("a proxy request cannot name a wildcard")
    for destination in destinations:
        allowed, allowed_port = destination_read(destination)
        if port == allowed_port and (
            host == allowed
            or allowed.startswith("*.")
            and host.endswith(allowed[1:])
            and host != allowed[2:]
        ):
            return host, port
    raise AdmissionRefusal("network destination was not admitted")


class EgressPort(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path: Path, destinations: tuple[str, ...]) -> None:
        self.destinations = destinations
        self.slots = threading.BoundedSemaphore(CONNECTION_LIMIT)
        for destination in destinations:
            destination_read(destination)
        super().__init__(str(path), EgressRequest)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: str
    ) -> None:
        if not isinstance(request, socket.socket):
            raise AdmissionRefusal("the private egress port requires a stream socket")
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: str
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class EgressRequest(socketserver.BaseRequestHandler):
    server: EgressPort
    request: socket.socket

    def handle(self) -> None:
        self.request.settimeout(CONNECT_TIMEOUT_S)
        header = bytearray()
        try:
            while not header.endswith(b"\r\n\r\n"):
                chunk = self.request.recv(1)
                if not chunk:
                    return
                header.extend(chunk)
                if len(header) > HEADER_LIMIT:
                    raise AdmissionRefusal("proxy header exceeds its bound")
            parts = bytes(header).split(b"\r\n", 1)[0].decode("ascii").split(" ")
            if (
                len(parts) != 3
                or parts[0] != "CONNECT"
                or parts[2] not in {"HTTP/1.0", "HTTP/1.1"}
            ):
                raise AdmissionRefusal("only CONNECT is admitted")
            host, port = destination_admit(parts[1], self.server.destinations)
            with socket.create_connection((host, port), CONNECT_TIMEOUT_S) as upstream:
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.relay(upstream)
        except AdmissionRefusal:
            self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
        except OSError, UnicodeError, ValueError:
            return

    def relay(self, upstream: socket.socket) -> None:
        sockets = (self.request, upstream)
        with selectors.DefaultSelector() as ready:
            for connection in sockets:
                ready.register(connection, selectors.EVENT_READ)
            while True:
                selected = ready.select(CONNECT_TIMEOUT_S)
                if not selected:
                    return
                for key, _ in selected:
                    origin = self.request if key.fileobj is self.request else upstream
                    data = origin.recv(65536)
                    if not data:
                        return
                    target = upstream if origin is self.request else self.request
                    target.sendall(data)


@contextmanager
def egress_start(path: Path, destinations: tuple[str, ...]) -> Iterator[None]:
    """An owned process, tied to its launcher lifetime and exact private socket.

    No credential is placed in argv or sent through the readiness pipe. A failed
    exec leaves this context responsible for the only child it started.
    """
    if len(os.fsencode(path)) > 100 or "," in str(path):
        raise AdmissionRefusal("private proxy path exceeds its native boundary")
    if path.exists():
        raise AdmissionRefusal("private proxy name is already in use")
    server = EgressPort(path, destinations)
    path.chmod(0o600)
    ready_read, ready_write = os.pipe()
    parent = os.getpid()
    try:
        child = os.fork()
    except OSError:
        server.server_close()
        os.close(ready_read)
        os.close(ready_write)
        path.unlink(missing_ok=True)
        raise
    if child == 0:
        os.close(ready_read)
        try:
            # Linux PR_SET_PDEATHSIG; exec preserves the parent pid. If the
            # launcher dies, this port cannot outlive its owning process group.
            library = ctypes.CDLL(None, use_errno=True)
            if library.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
                os._exit(1)
            os.write(ready_write, b"ready")
            os.close(ready_write)
            server.serve_forever()
        finally:
            os._exit(0)
    os.close(ready_write)
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(ready_read, selectors.EVENT_READ)
            if (
                not ready.select(CONNECT_TIMEOUT_S)
                or os.read(ready_read, 5) != b"ready"
            ):
                raise AdmissionRefusal("private proxy did not become ready")
        yield
    finally:
        os.close(ready_read)
        server.server_close()
        try:
            os.kill(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)
        path.unlink(missing_ok=True)

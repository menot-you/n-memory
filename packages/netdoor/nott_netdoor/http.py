"""Loopback bind and bearer admission in front of an ASGI app (the MCP SDK's)."""

from __future__ import annotations

import hmac
import ipaddress
import socket
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Any

import uvicorn


class NetDoorError(Exception):
    """Base exception for all netdoor errors."""


class BindError(NetDoorError):
    """The bind failed."""

    def __init__(self, addr: tuple[str, int], error: Exception) -> None:
        ip, port = addr
        super().__init__(f"binding the network door at {ip}:{port} failed: {error}")
        self.addr = addr
        self.error = error


AUTH_HEADER: str = "authorization"
BEARER_PREFIX: str = "Bearer "
ASCII_TRIM_CHARS: bytes = b" \t\n\r\x0c"


class HttpNotAnAddressError(NetDoorError):
    """The text is not `<ip>:<port>`."""

    def __init__(self, spec: str) -> None:
        super().__init__(f"{spec!r} is not an <ip>:<port> address")
        self.spec = spec


class NotLoopbackError(NetDoorError):
    """The address names a non-loopback interface.

    THIS door is hard-fenced to loopback: the split-horizon posture (tailnet +
    public) puts a reverse proxy in front of it, never the daemon alone.
    """

    def __init__(self, addr: tuple[str, int]) -> None:
        ip, port = addr
        super().__init__(
            f"{ip}:{port} is not a loopback address; this door refuses to bind an interface a "
            "reverse proxy is responsible for exposing. Pass 127.0.0.1:<port> explicitly"
        )
        self.addr = addr


class UnreadableKeyError(NetDoorError):
    """The operator did not pass `--api-key-file`, or the file is empty.

    Either is a hard refusal: a door that admitted because its own credential
    store was empty would fail OPEN, and the whole point of asking is that the
    answer fences.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"the API key file {str(path)!r} could not be read or is empty; "
            "this door refuses to start without a key"
        )
        self.path = path


def parse(spec: str) -> tuple[str, int]:
    """Read a `--net-allow`-shaped address, refusing anything that is not loopback.

    A reverse proxy owns remote exposure; this listener stays on loopback.
    """
    if spec.startswith("["):
        closing = spec.find("]:")
        if closing == -1:
            raise HttpNotAnAddressError(spec)
        ip_str = spec[1:closing]
        port_str = spec[closing + 2 :]
    else:
        parts = spec.split(":")
        if len(parts) != 2:
            raise HttpNotAnAddressError(spec)
        ip_str, port_str = parts

    try:
        port = int(port_str)
    except ValueError:
        raise HttpNotAnAddressError(spec) from None

    if not (0 <= port <= 65535):
        raise HttpNotAnAddressError(spec)

    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        raise HttpNotAnAddressError(spec) from None

    if not ip.is_loopback:
        raise NotLoopbackError((str(ip), port))

    return (str(ip), port)


class ApiKey:
    """A bearer key the door refuses to start without.

    Trims trailing whitespace the way every system secret-file does, then holds
    the bytes in memory for the lifetime of the door. Constant-time compare
    against `Authorization: Bearer <key>`.
    """

    def __init__(self, key_bytes: bytes) -> None:
        self._bytes = key_bytes

    @classmethod
    def load(cls, path: str | Path) -> ApiKey:
        """Load a key from a file the operator pointed at.

        The file MUST exist, be readable, and be non-empty (after trimming
        ASCII whitespace).
        """
        p = Path(path)
        try:
            raw = p.read_bytes()
        except OSError as e:
            raise UnreadableKeyError(p) from e

        trimmed = raw.strip(ASCII_TRIM_CHARS)
        if not trimmed:
            raise UnreadableKeyError(p)

        return cls(trimmed)

    def matches(self, presented: bytes) -> bool:
        """Constant-time equality against `presented`."""
        return hmac.compare_digest(self._bytes, presented)


#: Connections and tasks served at once; past it, uvicorn answers 503.
# shortcut: uvicorn 0.54 times out a connection only after a response. A local
# peer that opens this many sockets and sends nothing makes every request get 503
# until it closes them. Add a first-request timeout if that is seen.
LIMIT_CONCURRENCY = 64
#: How long an idle keep-alive connection stays open.
KEEP_ALIVE_S = 5

#: An ASGI application: what the MCP SDK's Streamable HTTP app is.
Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
AsgiApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def admit(app: AsgiApp, key: ApiKey) -> AsgiApp:
    """`app` behind the bearer: an HTTP request or a WebSocket without exactly one
    `Authorization: Bearer <key>` header never reaches it (401, or close 1008).
    Lifespan events pass through; any other scope is dropped."""

    def authorized(scope: Scope) -> bool:
        auth = [
            value.strip()
            for name, value in scope["headers"]
            if name.lower() == AUTH_HEADER.encode()
        ]
        prefix = BEARER_PREFIX.encode()
        return (
            len(auth) == 1
            and auth[0].startswith(prefix)
            and key.matches(auth[0][len(prefix) :])
        )

    async def door(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await app(scope, receive, send)
        elif scope["type"] == "http" and not authorized(scope):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
        elif scope["type"] == "websocket" and not authorized(scope):
            await send({"type": "websocket.close", "code": 1008})
        elif scope["type"] in ("http", "websocket"):
            await app(scope, receive, send)

    return door


def bind(local: tuple[str, int]) -> socket.socket:
    """A listening socket on `local`, which must be loopback: a reverse proxy
    owns remote exposure. Port 0 binds a free port; read it off the socket."""
    ip_str, port = local
    ip = ipaddress.ip_address(ip_str)
    if not ip.is_loopback:
        raise NotLoopbackError((str(ip), port))
    listener = socket.socket(
        socket.AF_INET6 if ip.version == 6 else socket.AF_INET, socket.SOCK_STREAM
    )
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((ip_str, port))
        listener.listen()
    except OSError as error:
        listener.close()
        raise BindError(local, error) from error
    return listener


def serve_app(app: AsgiApp, listener: socket.socket, key: ApiKey) -> None:
    """Serve `app` behind the bearer on `listener` until interrupted.

    uvicorn owns HTTP parsing; it logs no request lines (requests may carry
    credentials)."""
    config = uvicorn.Config(
        admit(app, key),
        log_config=None,
        access_log=False,
        lifespan="on",
        limit_concurrency=LIMIT_CONCURRENCY,
        timeout_keep_alive=KEEP_ALIVE_S,
    )
    with listener:
        uvicorn.Server(config).run(sockets=[listener])

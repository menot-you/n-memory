"""Bounded stdio MCP transport shared by nSPAWN and workflow memory clients."""

from __future__ import annotations

import errno
import json
import os
import re
import selectors
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import IO, Any, BinaryIO, Self, cast

#: The largest single reply the client buffers before refusing.
REPLY_CEILING = 1 << 22
#: How long one request waits for its reply.
DEFAULT_DEADLINE_S = 30.0
#: What a call on a client whose close has run, or is running, is refused with.
CLOSED = "MCP client is closed"


class McpClientError(Exception):
    """The configured MCP could not be read, reached, or did not answer."""


class McpProtocolError(McpClientError):
    """The peer's reply or configured contract is invalid; retry cannot repair it."""


class McpConfigurationError(McpClientError):
    """The local MCP configuration cannot be used."""


class McpOversized(McpProtocolError):
    """A reply line exceeded the client's buffer ceiling."""


class McpDeadline(McpClientError):
    """The request did not receive its answer within the client's deadline."""


class McpRefused(McpClientError):
    """The peer returned a structured error, rather than losing transport.

    `data` preserves error.data or a refused result's structuredContent;
    `kind` exposes its string marker when present. An admission-pending response
    may identify a launch that still needs reconciliation; an error alone
    does not establish that no side effect occurred.
    """

    def __init__(self, tool: str, message: str, data: dict[str, Any]) -> None:
        super().__init__(f"{tool} refused: {message}")
        self.message = message
        self.data = data
        kind = data.get("kind")
        self.kind: str | None = kind if isinstance(kind, str) else None


@dataclass(frozen=True, slots=True)
class StdioServer:
    """One stdio MCP server as ``plugin/.mcp.json`` declares it."""

    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise McpProtocolError(f"{label} must be a JSON object")
    return value


def _decoded(raw: str | bytes | bytearray, label: str) -> dict[str, Any]:
    """`raw` as the JSON object it must be, or one `McpClientError`.

    Every JSON this client reads comes through here. A document nested deeper than
    the parser can recurse raises `RecursionError`, which is no `ValueError`; left
    alone it escaped every caller's `McpClientError` catch, and one such reply ended
    the ACP service reading it."""
    try:
        return _object(json.loads(raw), label)
    except (ValueError, RecursionError) as error:
        raise McpProtocolError(f"{label} is not JSON") from error


def _prefix_id(raw: bytearray) -> int | None:
    """A provisional envelope id in a bounded head, without decoding the whole line.

    Walk complete top-level members in the first 4096 bytes, stopping at an
    unfinished result/error. Nested ids, duplicate ids and a head ending before
    the payload are ambiguous and retain the size refusal. The fixed bound and
    index walk keep refusal prompt. A late candidate still needs a streaming
    check for duplicate ids beyond this head before it can be discarded.
    """
    try:
        # A pipe chunk may end inside a UTF-8 character in the unfinished payload.
        prefix = raw[:4096].decode("utf-8", errors="replace")
        decoder = json.JSONDecoder()
        position = 0
        while position < len(prefix) and prefix[position] in " \t\r\n":
            position += 1
        if prefix[position : position + 1] != "{":
            return None
        position += 1
        answered: int | None = None
        while True:
            while position < len(prefix) and prefix[position] in " \t\r\n":
                position += 1
            key, position = decoder.raw_decode(prefix, position)
            if not isinstance(key, str):
                return None
            while position < len(prefix) and prefix[position] in " \t\r\n":
                position += 1
            if prefix[position : position + 1] != ":":
                return None
            position += 1
            while position < len(prefix) and prefix[position] in " \t\r\n":
                position += 1
            try:
                value, position = decoder.raw_decode(prefix, position)
            except json.JSONDecodeError:
                return (
                    answered
                    if key in ("result", "error") and position < len(prefix)
                    else None
                )
            while position < len(prefix) and prefix[position] in " \t\r\n":
                position += 1
            if prefix[position : position + 1] not in (",", "}"):
                return None
            if key == "id":
                if (
                    answered is not None
                    or not isinstance(value, int)
                    or isinstance(value, bool)
                ):
                    return None
                answered = value
            if prefix[position : position + 1] != ",":
                return answered
            position += 1
    except ValueError, RecursionError:
        return None


_STRING_MARK = re.compile(rb'["\\]')


class _EnvelopeIds:
    """Count envelope ids while draining; retain at most one 14-byte JSON key.

    Quotes and escapes carry across chunks. Nested keys and text are skipped;
    even two fully escaped letters in "id" fit the key bound. Payload strings
    are scanned by the regex without copying or walking every byte in Python.
    """

    def __init__(self) -> None:
        self.depth = 0
        self.string = False
        self.escape = False
        self.member = False
        self.key: bytearray | None = None
        self.ids = 0

    def feed(self, raw: bytes | bytearray) -> None:
        position = 0
        while position < len(raw):
            if self.string:
                if self.escape:
                    self._key_part(raw, position, position + 1)
                    position += 1
                    self.escape = False
                    continue
                mark = _STRING_MARK.search(raw, position)
                end = len(raw) if mark is None else mark.end()
                self._key_part(raw, position, end)
                position = end
                if mark is None:
                    return
                if raw[end - 1] == 92:  # Backslash escapes the next byte.
                    self.escape = True
                    continue
                self.string = False
                if self.key is not None:
                    try:
                        if json.loads(self.key) == "id":
                            self.ids += 1
                    except ValueError:
                        pass  # Invalid keys cannot establish another envelope id.
                    self.key = None
                continue
            token = raw[position]
            position += 1
            if token == 34:
                self.string = True
                if self.depth == 1 and self.member:
                    self.key = bytearray(b'"')
            elif token in (123, 91):  # Object or array opens.
                self.depth += 1
                self.member = self.depth == 1 and token == 123
            elif token in (125, 93):
                self.depth -= 1
            elif self.depth == 1:
                if token == 44:
                    self.member = True
                elif token == 58:
                    self.member = False

    def _key_part(self, raw: bytes | bytearray, start: int, end: int) -> None:
        if self.key is not None:
            if len(self.key) + end - start > 14:
                self.key = None
            else:
                self.key.extend(raw[start:end])


def _read_pipe[T](step: Callable[..., T], *args: Any) -> T:
    """One step on the reply pipe, refused as a closed client where a close on another
    thread closed it under this call: a closed file object raises `ValueError` (and
    its stale descriptor `OSError`), which no caller catches as a transport error."""
    try:
        return step(*args)
    except (ValueError, OSError) as error:
        raise McpClientError(CLOSED) from error


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise McpProtocolError(f"{label} must be a non-empty string")
    return value


def configured_server(root: Path, name: str = "nspawn") -> StdioServer:
    """Read a stdio MCP from a caller-validated runtime configuration root.

    The caller must establish trust in ``root`` before executing its command.
    This parser validates shape and does not authorize a selected project.
    """
    path = root / "plugin/.mcp.json"
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise McpConfigurationError(f"MCP configuration unreadable: {error}") from error
    config = _decoded(raw, "MCP configuration")
    server = _object(
        _object(config.get("mcpServers"), "MCP servers").get(name), f"{name} MCP"
    )
    command = _text(server.get("command"), f"{name} command")
    args = server.get("args", [])
    if (
        server.get("type", "stdio") != "stdio"
        or not Path(command).is_absolute()
        or not isinstance(args, list)
        or any(not isinstance(item, str) for item in args)
    ):
        raise McpConfigurationError(
            "the configured MCP must be an absolute stdio command"
        )
    environment: dict[str, str] = {}
    for key, value in _object(server.get("env", {}), "MCP environment").items():
        expanded = os.path.expandvars(_text(value, "MCP environment value"))
        if "$" in expanded:
            raise McpConfigurationError("MCP environment has unresolved variables")
        environment[str(key)] = expanded
    return StdioServer(command, tuple(args), environment)


class McpClient:
    """A live stdio session with one MCP process; use as a context manager."""

    def __init__(
        self,
        server: StdioServer,
        cwd: Path,
        deadline_s: float = DEFAULT_DEADLINE_S,
        *,
        server_name: str = "nspawn",
    ) -> None:
        self._server = server
        self._server_name = server_name
        self._cwd = cwd
        self._deadline_s = deadline_s
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = bytearray()
        self._next_id = 1
        #: Whether the line being read went past `REPLY_CEILING`: its bytes are
        #: dropped until its newline, whichever call is reading then.
        self._discarding = False
        self._late_envelope: _EnvelopeIds | None = None

    @classmethod
    def open(cls, root: Path, cwd: Path | None = None) -> McpClient:
        """A client over the configured MCP at ``root``."""
        return cls(configured_server(root), root if cwd is None else cwd)

    def __enter__(self) -> Self:
        environment = dict(os.environ, **self._server.env)
        try:
            self._process = subprocess.Popen(
                [self._server.command, *self._server.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                cwd=self._cwd,
                bufsize=0,
            )
        except (FileNotFoundError, PermissionError) as error:
            raise McpConfigurationError(f"MCP could not be spawned: {error}") from error
        except OSError as error:
            if error.errno in {
                errno.ENOEXEC,
                errno.ENOTDIR,
                errno.EISDIR,
                errno.ELOOP,
                errno.ENAMETOOLONG,
            }:
                raise McpConfigurationError(
                    f"MCP could not be spawned: {error}"
                ) from error
            raise McpClientError(f"MCP could not be spawned: {error}") from error
        # Every write is bounded by select, never by a blocking pipe. Only this
        # process holds the pipe's write end, so no other reader sees the flag.
        os.set_blocking(cast(BinaryIO, self._process.stdin).fileno(), False)
        try:
            initialized = self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "nott-harness", "version": "1"},
                },
            )
            if (
                _object(initialized.get("serverInfo"), "server identity").get("name")
                != self._server_name
            ):
                raise McpProtocolError(f"the configured MCP is not {self._server_name}")
            self._write(
                b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
                time.monotonic() + self._deadline_s,
            )
        except McpClientError:
            self.close()
            raise
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._process = None
        try:
            # __enter__ owns both PIPE handles for the process's entire lifetime.
            cast(BinaryIO, process.stdin).close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        finally:
            cast(BinaryIO, process.stdout).close()

    def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """One ``tools/call``; the single JSON text content, decoded.

        A tool refusal — a JSON-RPC error or ``isError`` — is a
        :class:`McpClientError` carrying the server's own words.
        """
        reply = self._request("tools/call", {"name": tool, "arguments": arguments})
        content = reply.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise McpProtocolError(f"{tool} returned incomplete content")
        text = _text(_object(content[0], "tool content").get("text"), "tool JSON")
        if reply.get("isError") is True:
            structured = reply.get("structuredContent")
            raise McpRefused(
                tool, text, structured if isinstance(structured, dict) else {}
            )
        return _decoded(text, f"{tool} answer")

    def _write(self, frame: bytes, deadline: float | None) -> None:
        """`frame` to the peer's stdin, whole by `deadline`. A peer that does not take
        it in time fails the call and is killed: a frame cut short would corrupt every
        later one. Without a deadline, a frame the pipe has no room for now is not
        written: one shorter than `PIPE_BUF` goes whole or not at all (POSIX)."""
        process = self._process
        stdin = None if process is None else process.stdin
        if process is None or stdin is None or stdin.closed:
            raise McpClientError(CLOSED)
        view = memoryview(frame)
        try:
            descriptor = stdin.fileno()
            with selectors.DefaultSelector() as selector:
                selector.register(descriptor, selectors.EVENT_WRITE)
                while view:
                    # Before every write, not only when one would block: the rest
                    # of a frame never goes out after its deadline.
                    remaining = (
                        None if deadline is None else deadline - time.monotonic()
                    )
                    if remaining is not None and remaining <= 0:
                        break
                    try:
                        view = view[os.write(descriptor, view) :]
                    except BlockingIOError:
                        if remaining is None:
                            return
                        if not selector.select(remaining):
                            break
            if view:
                process.kill()
                self.close()
                raise McpDeadline("MCP did not read its request in time")
        except ValueError as error:
            # Closed between the check and the write, by a close on another thread.
            raise McpClientError(CLOSED) from error
        except OSError as error:
            raise McpClientError(f"MCP stdin closed: {error}") from error

    def _cancel(self, number: int) -> None:
        """Tell the peer request `number` is cancelled, as MCP has a sender do on a
        timeout. Its answer, if one still comes, is read past as a late one.

        Ids only grow and requests are one at a time, so an abandoned id cannot
        collide with a later request's; the session stays usable without a respawn,
        which would have to wait out a peer that is only slow.

        The call's deadline has passed, so this write gets no time of its own: a peer
        not reading a full stdin pipe is not told, and the call fails on time anyway.
        """
        self._write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": number, "reason": "deadline exceeded"},
                }
            ).encode()
            + b"\n",
            None,
        )

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        stdout = None if process is None else process.stdout
        if stdout is None or stdout.closed:
            raise McpClientError(CLOSED)
        number = self._next_id
        self._next_id += 1
        deadline = time.monotonic() + self._deadline_s
        self._write(
            json.dumps(
                {"jsonrpc": "2.0", "id": number, "method": method, "params": params},
                ensure_ascii=False,
            ).encode()
            + b"\n",
            deadline,
        )
        frame = self._own_frame(stdout, number, method, deadline)
        if "error" in frame:
            error_body = frame["error"]
            message = (
                error_body.get("message") if isinstance(error_body, dict) else None
            )
            data = error_body.get("data") if isinstance(error_body, dict) else None
            raise McpRefused(
                method,
                message
                if isinstance(message, str) and message
                else json.dumps(error_body),
                data if isinstance(data, dict) else {},
            )
        return _object(frame.get("result"), "nspawn response")

    def _own_frame(
        self, stdout: IO[bytes], number: int, method: str, deadline: float
    ) -> dict[str, Any]:
        """The frame answering request `number`, read past everything that is not an
        answer to any request (a notification or a request of the peer's own), past
        blank lines, and past the late answers of requests that stopped reading before
        theirs came: a deadline, a stray line, a frame naming no request of ours.

        Requests go one at a time and ids only grow, so a frame naming an id below
        `number` can only be such a late answer, however many came before it.

        The deadline bounds the wait for the peer, so it is checked before each frame
        is taken and before each read: frames already buffered, a large one decoded
        among them, can outlast it as surely as a silent peer. A frame taken in time is
        decoded, and returned if it is the answer, however long that decode takes.
        `deadline` is the call's: the request's write spent part of it."""
        with selectors.DefaultSelector() as selector:
            _read_pipe(selector.register, stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._cancel(number)
                    raise McpDeadline(f"{method} deadline exceeded")
                if b"\n" in self._buffer:
                    raw, _, rest = self._buffer.partition(b"\n")
                    self._buffer[:] = rest
                    if not raw.strip():
                        continue
                    frame = _decoded(raw, "MCP frame")
                    if "method" in frame or "id" not in frame:
                        continue
                    answered = frame["id"]
                    # `True == 1` in Python, so an id is a request's only as an int
                    # that is not a bool.
                    if not isinstance(answered, int) or isinstance(answered, bool):
                        raise McpProtocolError("MCP answered another request")
                    if answered < number:
                        continue
                    if answered != number:
                        raise McpProtocolError("MCP answered another request")
                    return frame
                if not _read_pipe(selector.select, remaining):
                    self._cancel(number)
                    raise McpDeadline(f"{method} deadline exceeded")
                # ponytail: a close() on another thread between fileno() and the read
                # could leave this reading a reused descriptor. Serialized does not
                # hold its lock across close, so the window is shutdown only; closing
                # under that lock is the upgrade if it is ever seen.
                chunk = _read_pipe(lambda: os.read(stdout.fileno(), 65536))
                if not chunk:
                    raise McpClientError("MCP reply truncated or oversized")
                if self._discarding:
                    chunk = self._drain(chunk)
                # The buffer holds no newline here, so it and the chunk up to its
                # first newline are one line.
                cut = chunk.find(b"\n")
                if len(self._buffer) + (len(chunk) if cut < 0 else cut) > REPLY_CEILING:
                    answered = _prefix_id(self._buffer)
                    late = answered is not None and answered < number
                    if late:
                        self._late_envelope = _EnvelopeIds()
                        self._late_envelope.feed(self._buffer)
                    self._buffer.clear()
                    self._discarding = True
                    self._buffer.extend(self._drain(chunk))
                    # A head id is provisional until the remaining envelope has
                    # been scanned. A duplicate anywhere retains the size refusal.
                    if late:
                        continue
                    raise McpOversized("MCP reply truncated or oversized")
                self._buffer.extend(chunk)

    def _drain(self, chunk: bytes) -> bytes:
        """Drop one oversized line, checking a late candidate through its newline."""
        cut = chunk.find(b"\n")
        envelope = self._late_envelope
        if envelope is not None:
            envelope.feed(chunk if cut < 0 else chunk[:cut])
        if cut < 0:
            rest = b""
        else:
            rest = chunk[cut + 1 :]
            self._discarding = False
            self._late_envelope = None
        if envelope is not None and envelope.ids > 1:
            self._late_envelope = None
            self._buffer.extend(rest)
            raise McpOversized("MCP reply truncated or oversized")
        return rest


__all__ = [
    "DEFAULT_DEADLINE_S",
    "McpClient",
    "McpClientError",
    "McpConfigurationError",
    "McpDeadline",
    "McpOversized",
    "McpProtocolError",
    "StdioServer",
    "configured_server",
]

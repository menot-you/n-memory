"""A stdio MCP peer for tests that drive a real `nmemory mcp serve` process.

The SDK door wants a complete `initialize` (protocol version, capabilities,
client) followed by `notifications/initialized`, and it cancels whatever is
still running when stdin closes: a peer keeps stdin open until every request
it sent is answered.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

INIT: dict[str, Any] = {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "nmemory-tests", "version": "1"},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def complete(frame: dict[str, Any]) -> dict[str, Any]:
    """`frame` as the SDK takes it: an `initialize` gets the fields it lacks."""
    frame = {"jsonrpc": "2.0", **frame}
    if frame.get("method") == "initialize":
        frame["params"] = {**INIT, **(frame.get("params") or {})}
    return frame


def wire(server: Any, frames: list[dict[str, Any]]) -> list[str]:
    """The exact lines the SDK door writes for `frames`, served in process.

    The handshake is done first and its answer left out; each frame that is a
    request gets its answer line, serialized as the stdio writer does."""
    import anyio
    import mcp_types as types
    from mcp.shared.message import SessionMessage

    from nmemory.server import build

    sdk = build(server)

    async def run() -> list[str]:
        send, inbox = anyio.create_memory_object_stream[Any](16)
        outbox, receive = anyio.create_memory_object_stream[Any](16)
        lines: list[str] = []
        async with anyio.create_task_group() as tg:
            tg.start_soon(sdk.run, inbox, outbox, sdk.create_initialization_options())
            for frame in [{"id": 0, "method": "initialize"}, INITIALIZED, *frames]:
                message = types.jsonrpc_message_adapter.validate_python(complete(frame))
                await send.send(SessionMessage(message))
                if "id" in frame:
                    answer = await receive.receive()
                    lines.append(
                        answer.message.model_dump_json(
                            by_alias=True, exclude_unset=True
                        )
                    )
            await send.aclose()
        return lines[1:]

    return anyio.run(run)


def exchange(
    command: list[str],
    frames: list[dict[str, Any]],
    *,
    deadline_s: float = 60.0,
    **popen: Any,
) -> tuple[list[dict[str, Any]], str, int]:
    """Send `frames` one by one, each request answered before the next, then
    close stdin. Returns the answers, stderr and the exit code."""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **popen,
    )
    assert proc.stdin is not None and proc.stdout is not None
    answers: list[dict[str, Any]] = []
    end = time.monotonic() + deadline_s
    try:
        for frame in frames:
            frame = complete(frame)
            proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
            if frame.get("method") == "initialize":
                proc.stdin.flush()
                answers.append(json.loads(proc.stdout.readline()))
                proc.stdin.write(json.dumps(INITIALIZED) + "\n")
            proc.stdin.flush()
            if "id" in frame and frame.get("method") != "initialize":
                if time.monotonic() > end:
                    break
                line = proc.stdout.readline()
                if not line:
                    break
                answers.append(json.loads(line))
    finally:
        proc.stdin.close()
        _, errors = proc.communicate(timeout=deadline_s)
    return answers, errors, proc.returncode

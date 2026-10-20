"""The SDK door's own refusals: whole-params ceiling and opaque internal errors."""

from __future__ import annotations

import threading
from typing import Any

import anyio
import mcp_types as types
import pytest
from mcp.shared.message import SessionMessage

from nspawn import main
from nspawn.mcp import MAX_MCP_PARAMS_BYTES

INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "t", "version": "1"},
}


class Crashing:
    calls = 0

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        Crashing.calls += 1
        raise RuntimeError("secret-vendor-text /home/x")


def _call(params: dict[str, Any]) -> dict[str, Any]:
    server = main.build(Crashing())  # type: ignore[arg-type]
    frames: list[dict[str, Any]] = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
    ]

    async def run() -> dict[str, Any]:
        send, inbox = anyio.create_memory_object_stream[Any](8)
        outbox, receive = anyio.create_memory_object_stream[Any](8)
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                server.run, inbox, outbox, server.create_initialization_options()
            )
            answer: dict[str, Any] = {}
            for frame in frames:
                message = types.jsonrpc_message_adapter.validate_python(frame)
                await send.send(SessionMessage(message))
                if "id" in frame:
                    got = await receive.receive()
                    answer = got.message.model_dump(by_alias=True, exclude_unset=True)
            await send.aclose()
        return answer

    return anyio.run(run)


def test_an_unexpected_failure_is_an_internal_error_without_its_text() -> None:
    answer = _call({"name": "nspawn_provider_list", "arguments": {}})
    assert answer["error"]["code"] == -32603
    assert "secret" not in answer["error"]["message"]
    assert "RuntimeError" in answer["error"]["message"]


@pytest.mark.parametrize(
    "pad", ["x" * MAX_MCP_PARAMS_BYTES, "\U0001f600" * (MAX_MCP_PARAMS_BYTES // 2)]
)
def test_the_ceiling_counts_meta_in_utf8_bytes(pad: str) -> None:
    Crashing.calls = 0
    answer = _call(
        {
            "name": "nspawn_provider_list",
            "arguments": {},
            "_meta": {"pad": pad},
        }
    )
    assert answer["error"]["code"] == -32602
    assert "ceiling" in answer["error"]["message"]
    assert Crashing.calls == 0


class Polling:
    """A long-poll that waits until released, and a read that answers at once."""

    def __init__(self) -> None:
        self.release = threading.Event()

    def call_tool(self, name: str, arguments: Any) -> dict[str, Any]:
        if name in ("nspawn_run_show", "nspawn_result_show"):
            assert self.release.wait(10), "the long-poll was never released"
        return {"content": [{"type": "text", "text": name}]}


@pytest.mark.parametrize("wait", ["nspawn_run_show", "nspawn_result_show"])
def test_a_long_poll_does_not_hold_the_door(wait: str) -> None:
    """2026-10-07: one teammate's 600 s run_show held every call of the session."""
    session = Polling()
    server = main.build(session)  # type: ignore[arg-type]
    calls = [
        (wait, {"run_id": "r", "timeout_ms": 600000}),
        ("nspawn_provider_list", {}),
    ]

    async def run() -> list[Any]:
        send, inbox = anyio.create_memory_object_stream[Any](8)
        outbox, receive = anyio.create_memory_object_stream[Any](8)
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                server.run, inbox, outbox, server.create_initialization_options()
            )
            for frame in [
                {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
            ] + [
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
                for index, (name, arguments) in enumerate(calls, 1)
            ]:
                message = types.jsonrpc_message_adapter.validate_python(frame)
                await send.send(SessionMessage(message))
            order = [(await receive.receive()).message.id]
            try:
                with anyio.fail_after(5):
                    order.append((await receive.receive()).message.id)
            finally:
                session.release.set()
            order.append((await receive.receive()).message.id)
            await send.aclose()
        return order

    assert anyio.run(run) == [0, 2, 1]

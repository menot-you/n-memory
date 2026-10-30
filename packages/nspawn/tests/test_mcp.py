"""The MCP tool boundary, driven call by call from OUTSIDE.

The SDK door (`nspawn.main`) owns the protocol envelope; `test_cli.py` drives it
over a real pipe. Every test here asserts a call's answer is either the tool's
own shape or a TYPED refusal that names what is missing.
"""

import threading

from mcp_fakes import FakeSupervisor, handle

from nspawn import errors
from nspawn.errors import NSpawnError
from nspawn.mcp import INVALID_PARAMS, McpServer


def bounded(call, seconds: float = 2.0):
    """Run `call` on a thread and REFUSE to wait past `seconds`.

    A ceiling that waits is not a ceiling, and a proof that hangs is not a proof:
    an implementation that queues must fail these tests, not wedge them.
    """
    outcome: list[object] = []

    def attempt() -> None:
        try:
            outcome.append(call())
        except BaseException as error:  # noqa: BLE001 - the outcome is the assertion
            outcome.append(error)

    thread = threading.Thread(target=attempt, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    assert not thread.is_alive(), "the call QUEUED instead of answering"
    return outcome[0]


def ask(server: McpServer, method: str, params: object = None, request_id: object = 1):
    frame: dict[str, object] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        frame["params"] = params
    return handle(server, frame)


def test_an_unknown_tool_is_invalid_params_not_a_body() -> None:
    answer = ask(McpServer(), "tools/call", {"name": "definitely_not_a_tool"})
    assert answer is not None
    assert answer["error"] == {
        "code": INVALID_PARAMS,
        "message": "nSPAWN has no tool named 'definitely_not_a_tool'",
    }
    assert "result" not in answer


def test_malformed_calls_are_refused_with_invalid_params() -> None:
    server = McpServer()
    for params in (
        {},
        {"name": 7},
        {"name": "nspawn_run_show", "arguments": ["not", "an", "object"]},
    ):
        answer = ask(server, "tools/call", params)
        assert answer is not None
        assert answer["error"]["code"] == INVALID_PARAMS


def test_a_typed_refusal_from_the_tool_is_invalid_params() -> None:
    refusing = FakeSupervisor(
        status_result=NSpawnError(errors.NERR_1113, "refuse for test")
    )
    server = McpServer(supervisor=refusing)
    answer = ask(
        server,
        "tools/call",
        {"name": "nspawn_run_show", "arguments": {"run_id": "r1"}},
    )
    assert answer is not None
    assert answer["error"]["code"] == INVALID_PARAMS

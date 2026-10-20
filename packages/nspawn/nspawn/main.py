"""nSPAWN stdio MCP boot, on the official MCP Python SDK (`mcp`).

The stdio door is the one the spawning session holds: one session, one
`McpServer`. The SDK owns the JSON-RPC framing, the handshake and the protocol
version; this module hands it the tool catalogue and the one call boundary,
`McpServer.call_tool`. Each dispatch is handed to a driver (`driver.py`), which
the door launches as `nspawn dispatch run`. The operator verbs are verbs of the
executable (`cli.COMMANDS`), never arguments of the server.
"""

import sys
from contextlib import nullcontext
from typing import Any

import anyio
import mcp_types as types
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError

from .mcp import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    MAX_MCP_PARAMS_BYTES,
    NSPAWN_TOOL_CONTRACTS,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    McpError,
    McpServer,
)

TOOLS = [
    types.Tool(name=c.name, description=c.description, input_schema=c.input_schema)
    for c in NSPAWN_TOOL_CONTRACTS
]


def build(session: McpServer) -> Server[Any]:
    """The SDK server answering for `session`."""
    # ponytail: one call at a time, as the hand-written loop answered them;
    # concurrent calls once McpServer is shown thread-safe.
    lock = anyio.Lock()
    # Except a long-poll, which only reads the lock-guarded supervisor. The
    # teammates of one Claude Code process share this door, and one 600 s wait
    # held every call they made (2026-10-07). Its own limiter keeps the default
    # one, which reads stdin, from being exhausted by waits.
    polls = anyio.CapacityLimiter(16)

    async def list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

    async def call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        # The ceiling measures the whole params, `_meta` included.
        size = len(
            params.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        )
        if size > MAX_MCP_PARAMS_BYTES:
            raise MCPError(
                INVALID_PARAMS,
                f"tools/call params of {size} bytes exceeded the "
                f"{MAX_MCP_PARAMS_BYTES}-byte ceiling",
            )
        waits = params.name in ("nspawn_run_show", "nspawn_result_show") and bool(
            (params.arguments or {}).get("timeout_ms")
        )
        async with nullcontext() if waits else lock:
            try:
                result = await anyio.to_thread.run_sync(
                    session.call_tool,
                    params.name,
                    params.arguments,
                    limiter=polls if waits else None,
                )
            except McpError as error:
                raise MCPError(error.code, error.rpc_message, error.data) from error
            except Exception as error:
                # Never the raw text: it can carry vendor output or a path.
                raise MCPError(
                    INTERNAL_ERROR, f"internal error: {type(error).__name__}"
                ) from error
        return types.CallToolResult.model_validate(result)

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def serve(session: McpServer | None = None) -> None:
    """Serve one session on stdio until its peer stops talking.

    The `finally` block stops the reaper daemon and drops the instance row the
    supervisor registered on first dispatch, so a peer disconnect or a tool crash
    does not leak a heartbeat thread or a phantom instance.
    """
    session = McpServer() if session is None else session
    server = build(session)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    try:
        anyio.run(run)
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    """Serve the MCP surface on stdio until the peer stops talking."""
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in ([], ["--host-control"]):
        sys.stderr.write(
            "nspawn MCP accepts only --host-control for trusted host callers\n"
        )
        return 2
    if arguments:
        serve(McpServer(host_control=True))
    else:
        serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

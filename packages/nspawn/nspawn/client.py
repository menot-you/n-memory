"""Configured nSPAWN client; framing lives in the shared transport."""

from nott_socket.mcp import (
    DEFAULT_DEADLINE_S,
    McpClient,
    StdioServer,
    configured_server,
)
from nott_socket.mcp import (
    McpClientError as NspawnClientError,
)
from nott_socket.mcp import (
    McpRefused as NspawnRefused,
)

__all__ = [
    "DEFAULT_DEADLINE_S",
    "NspawnClient",
    "NspawnClientError",
    "NspawnRefused",
    "StdioServer",
    "configured_server",
]


class NspawnClient(McpClient):
    """A client whose MCP peer must identify as nspawn."""

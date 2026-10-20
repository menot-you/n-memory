"""Use the shared nSPAWN fixtures without a memory runtime dependency."""

from live.live_support import live_environment, mcp_client, registry_file

__all__ = ["live_environment", "mcp_client", "registry_file"]

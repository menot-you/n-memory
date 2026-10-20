"""The nSPAWN facade retains its configured peer and public compatibility."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from nott_socket import mcp

from nspawn import client


def test_public_transport_aliases_remain_compatible() -> None:
    assert client.NspawnClientError is mcp.McpClientError
    assert client.NspawnRefused is mcp.McpRefused
    assert client.StdioServer is mcp.StdioServer
    assert client.configured_server is mcp.configured_server
    assert client.DEFAULT_DEADLINE_S == mcp.DEFAULT_DEADLINE_S


@pytest.mark.parametrize("peer", ["nspawn", "nmemory"])
def test_configured_facade_accepts_only_the_nspawn_peer(
    tmp_path: Path, peer: str
) -> None:
    script = tmp_path / "peer.py"
    script.write_text("""import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request: continue
    result = {"serverInfo": {"name": sys.argv[1]}} if request["method"] == "initialize" else {"content": [{"type": "text", "text": '{"run_id": "registered-run"}'}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    (tmp_path / "plugin").mkdir()
    (tmp_path / "plugin/.mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nspawn": {"command": sys.executable, "args": [str(script), peer]}
                }
            }
        )
    )
    if peer == "nmemory":
        with (
            pytest.raises(client.NspawnClientError, match="not nspawn"),
            client.NspawnClient.open(tmp_path),
        ):
            pass
    else:
        with client.NspawnClient.open(tmp_path) as transport:
            assert isinstance(transport, client.NspawnClient)
            assert transport.call("nspawn_run_show", {"run_id": "registered-run"}) == {
                "run_id": "registered-run"
            }

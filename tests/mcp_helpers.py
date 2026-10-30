"""Fresh in-process MCP servers and successful response decoding."""

from __future__ import annotations

import json
from typing import Any

from storefile import fresh_store_path

from nmemory.server import MemoryServer
from nmemory.store import Store


def fresh_server() -> MemoryServer:
    url = fresh_store_path("mcp")
    return MemoryServer(store=Store.open(url))


def ok(res: dict[str, Any]) -> dict[str, Any]:
    assert "result" in res and res["result"]["isError"] is False, res
    payload: dict[str, Any] = json.loads(res["result"]["content"][0]["text"])
    return payload

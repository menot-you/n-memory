"""The JSON-RPC refusal a tool returns for an argument it cannot act on."""

from __future__ import annotations

from typing import Any


def invalid_params(message: str) -> dict[str, Any]:
    """A -32602 (invalid params) refusal carrying `message`.

    `MemoryServer.call_tool` answers it on the JSON-RPC error channel and rolls
    back whatever the call wrote before refusing.
    """
    return {"jsonrpc": "2.0", "error": {"code": -32602, "message": message}}


def unknown_capsule(tool: str, given: str) -> dict[str, Any]:
    """Refuse an id that does not resolve to a stored capsule."""
    return {
        "jsonrpc": "2.0",
        "error": {
            "code": -32002,
            "message": (
                f"unknown capsule {given!r}: {tool} takes the id of a stored capsule "
                "(cap-<n>, short id or handle); find it with nmemory_capsule_retrieve "
                "or nmemory_capsule_list"
            ),
            "data": {"kind": "unknown_capsule", "id": given},
        },
    }

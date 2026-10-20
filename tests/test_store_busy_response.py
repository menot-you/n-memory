"""Contention adds a marker without changing either existing error channel."""

from typing import Any

import pytest
from nott_database import BusyError, DatabaseError

from nmemory.server import TOOLS, MemoryServer
from nmemory.store import Store


@pytest.mark.parametrize("error_type", [BusyError, DatabaseError])
def test_tool_store_error_keeps_result_and_text(
    monkeypatch: pytest.MonkeyPatch, error_type: type[DatabaseError]
) -> None:
    def fail(*args: Any) -> Any:
        raise error_type("database table is locked")

    monkeypatch.setitem(TOOLS, "nmemory_context_show", fail)
    store = Store.open_disposable_test_store()
    called = MemoryServer(store=store).call_tool(
        "nmemory_context_show", {"query": "busy"}
    )
    expected: dict[str, Any] = {
        "content": [{"type": "text", "text": "database table is locked"}],
        "isError": True,
    }
    if error_type is BusyError:
        expected["structuredContent"] = {"kind": "store-busy"}
    assert called == {"result": expected}


@pytest.mark.parametrize("error_type", [BusyError, DatabaseError])
def test_lazy_store_error_keeps_protocol_error_and_text(
    error_type: type[DatabaseError],
) -> None:
    def fail() -> Store:
        raise error_type("database is locked")

    called = MemoryServer(open_store=fail).call_tool(
        "nmemory_context_show", {"query": "busy"}
    )
    expected: dict[str, Any] = {
        "code": -32002,
        "message": "store unavailable: database is locked",
    }
    if error_type is BusyError:
        expected["data"] = {"kind": "store-busy"}
    assert called == {"jsonrpc": "2.0", "error": expected}

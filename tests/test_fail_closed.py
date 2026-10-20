"""Paths that swallowed failures and stayed green, and the live-store door.

1. `MemoryServer.get_tool_definitions` returned `[]` when `tools_schema.json`
   was not beside the module. An installed server served ZERO tools and appeared
   healthy (the comment above `[tool.uv.build-backend]` in pyproject relates
   that install). It is now a typed refusal on the first call.

2. A store file that is not this build's store — garbage, or another schema
   version — is refused at open, before any worker, tool or write.

3. The account's live store opens only from an entry point that says so
   (`allow_live_store`), and never under pytest.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import nott_database
import pytest
from nott_database import RefusedError, SchemaError
from storefile import fresh_store_path

from nmemory import server as server_module
from nmemory.database import STORE_FILE, Database
from nmemory.journal import BOUNDARY_ACTION, BOUNDARY_SUBJECT
from nmemory.server import MemoryServer
from nmemory.store import Store


def _servidor() -> MemoryServer:
    return MemoryServer(store=Store.open(fresh_store_path()))


def test_tools_list_recusa_catalogo_ausente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `tools_schema.json` beside the module, the server refuses instead of serving zero tools."""
    monkeypatch.setattr(server_module, "__file__", str(tmp_path / "server.py"))

    with pytest.raises(FileNotFoundError) as excinfo:
        _servidor().get_tool_definitions()

    assert "tools_schema.json" in str(excinfo.value)


def test_tools_list_recusa_catalogo_vazio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present but empty catalog is the same ailment: zero tools served as healthy."""
    (tmp_path / "tools_schema.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(server_module, "__file__", str(tmp_path / "server.py"))

    with pytest.raises(ValueError):
        _servidor().get_tool_definitions()


def test_tools_list_serve_o_catalogo_real() -> None:
    """The catalog beside the module loads all 27 tools."""
    tools = _servidor().get_tool_definitions()

    assert len(tools) == 27, f"expected 27 tools in the catalog, got {len(tools)}"
    assert "nmemory_work_list" in {tool["name"] for tool in tools}
    assert "nmemory_board_show" in {tool["name"] for tool in tools}


def test_the_reserved_action_alone_never_hides_a_row_from_the_public_count() -> None:
    """The reserved action is not the boundary; the four fields are.

    `count_audit_events` and `list_audit` hid the boundary by filtering only by
    `action <> 'journal:rust_parity'`. A row that uses the reserved action with
    ANOTHER actor is not a boundary — `verify_chain` REJECTS it — but it vanished from
    both public surfaces. The count dropped silently while the chain broke: two readers
    disagreeing on what constitutes a boundary.
    """
    url = fresh_store_path()
    store = Store.open(url)
    server = MemoryServer(store=store)
    res = server.call_tool(
        "nmemory_capsule_ingest",
        {
            "items": [
                {
                    "content": "A capsule so that the boundary gets written.",
                    "source": "session:test-fail-closed",
                    "anchor": "tests/test_fail_closed.py:1",
                    "confidence": 0.9,
                    "project_id": "nmemory",
                }
            ]
        },
    )
    assert res["result"]["isError"] is False, res
    baseline = store.audit.count_audit_events()

    with Database(url).writing() as conn:
        next_seq = conn.execute(
            "SELECT coalesce(max(seq), 0) + 1 FROM audit_events"
        ).fetchone()
        assert next_seq is not None
        conn.execute(
            "INSERT INTO audit_events "
            "(seq, at, actor, action, subject, reason, chained_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                next_seq[0],
                "2026-08-28T22:00:00.000001Z",
                "atacante",
                BOUNDARY_ACTION,
                BOUNDARY_SUBJECT,
                None,
                "0" * 64,
            ),
        )

    assert store.audit.count_audit_events() == baseline + 1
    assert any(event.actor == "atacante" for event in store.audit.list_audit()), (
        "the row with the reserved action and a foreign actor vanished from the public list"
    )


def test_a_file_that_is_not_a_store_is_refused_at_open() -> None:
    path = fresh_store_path()
    with open(path, "r+b") as f:
        f.write(b"not a database" * 16)
    with pytest.raises(SchemaError):
        Store.open(path)


def test_a_store_of_another_schema_version_is_refused_at_open() -> None:
    path = fresh_store_path()
    with Database(path).writing() as conn:
        conn.execute("PRAGMA query_only = OFF")
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(SchemaError, match="v99"):
        Store.open(path)


def test_the_live_store_is_refused_under_pytest() -> None:
    """Even with the entry point's permission: a test never opens the live store."""
    live = nott_database.live_directory() / STORE_FILE
    with pytest.raises(RefusedError, match="live store"):
        Store.open(live, allow_live_store=True)
    with pytest.raises(RefusedError, match="live store"):
        Store.open(live)


def test_stdio_and_every_subcommand_declare_the_live_store_permission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The WIRING, not the parameter: every CLI opener passes `allow_live_store`.

    Measured on 2026-08-29: the installed binary died at boot refusing the live
    store, and the installer probe could never list tools. The store is the
    account's own file, so a production entry point opens it by declaring so; a
    library caller (a tool script, a test) gets the closed default. A `main()`
    that forgets the permission turns this test red.
    """
    seen: list[bool] = []

    def _fake_open(
        path: object = None, *, read_only: bool = False, allow_live_store: bool = False
    ) -> object:
        seen.append(allow_live_store)
        raise SystemExit(0)

    from nmemory import cli as cli_mod

    monkeypatch.setattr(cli_mod.Store, "open", staticmethod(_fake_open))
    monkeypatch.setattr(
        "nmemory.server.run_stdio_server",
        lambda server: server.call_tool("nmemory_context_show", {"query": "x"}),
    )
    # The REAL invocation of the live link is the release launcher, which runs
    # `nmemory mcp serve --project nott`. Permission must hold for that form.
    monkeypatch.setattr(sys, "argv", ["nmemory", "mcp", "serve", "--project", "nott"])
    with pytest.raises(SystemExit):
        cli_mod.main()
    assert seen == [True], "the stdio server did not declare the live-store permission"

    seen.clear()
    monkeypatch.setattr(sys, "argv", ["nmemory", "digest", "show"])
    with pytest.raises(SystemExit):
        cli_mod.main()
    assert seen == [True], "a CLI subcommand did not declare the live-store permission"

    # The signature, not `__kwdefaults__`: under mutmut `__init__` is a trampoline whose
    # own `__kwdefaults__` is None; `inspect.signature` follows its `__wrapped__` original.
    assert (
        inspect.signature(Database.__init__).parameters["allow_live_store"].default
        is False
    )

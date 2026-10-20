"""The one `nspawn` executable: `nspawn <noun> <verb>`, one literal COMMANDS dict.

`mcp serve` serves stdio; a bare `nspawn` is a usage error, and the spellings
before the table (`server serve`, the retired probe verbs) are unknown commands.
This file measures the dispatch that reaches the server and every verb.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest
from nott_socket.mcp import McpClient, McpRefused, StdioServer

from nspawn import cli
from nspawn import main as server
from nspawn.mcp import NSPAWN_TOOL_CONTRACTS


def _record_serve(monkeypatch: pytest.MonkeyPatch) -> list[None]:
    served: list[None] = []
    monkeypatch.setattr(server, "serve", lambda: served.append(None))
    return served


def test_bare_is_a_usage_error_not_a_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served = _record_serve(monkeypatch)
    assert cli.main([]) == 2
    assert served == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: nspawn" in captured.err


def test_mcp_serve_serves_the_mcp_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    served = _record_serve(monkeypatch)
    assert cli.main(["mcp", "serve"]) == 0
    assert len(served) == 1


def test_mcp_serve_refuses_arguments_it_does_not_take(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    served = _record_serve(monkeypatch)
    assert cli.main(["mcp", "serve", "--stdio"]) == 2
    assert served == []
    assert "accepts only --host-control" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["serve"],
        ["proxy"],
        ["server", "serve"],
        ["server", "start"],
        ["refresh-proxy-probe", "https://127.0.0.1:1/"],
        ["credential-persist-probe", "/tmp/x"],
        ["proxy", "probe", "https://127.0.0.1:1/"],
        ["credential", "persist", "/tmp/x"],
        ["--serve"],
        ["export"],
        ["telemetry"],
    ],
)
def test_an_unknown_command_is_refused_with_the_usage(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("unknown command: nspawn")
    assert "usage: nspawn" in captured.err


def test_the_table_holds_exactly_the_declared_commands() -> None:
    assert set(cli.COMMANDS) == {
        ("mcp", "serve"),
        ("dispatch", "run"),
        ("notice", "drain"),
        ("notice", "take"),
        ("notice", "renew"),
        ("notice", "ack"),
        ("notice", "release"),
        ("database", "create"),
        ("database", "snapshot"),
        ("database", "rollback"),
        ("admission", "stop"),
        ("admission", "resume"),
        ("admission", "bind"),
        ("learning", "export"),
        ("run", "list"),
        ("run", "close"),
        ("run", "show"),
        ("result", "show"),
        ("transcript", "show"),
        ("quota", "show"),
        ("quota", "record"),
        ("provider", "list"),
        ("route", "show"),
        ("route", "start"),
        ("route", "list"),
        ("route", "switch"),
        ("route", "wall"),
        ("thread", "list"),
        ("thread", "show"),
        ("skill", "install"),
        ("plugin", "install"),
    }


def _handlers_in_module() -> set[str]:
    return {
        name
        for name, value in vars(cli).items()
        if name.startswith("cmd_") and inspect.isfunction(value)
    }


def test_every_command_has_its_own_handler() -> None:
    for (noun, verb), command in cli.COMMANDS.items():
        assert command.handler.__name__ == f"cmd_{noun}_{verb}", (noun, verb)
        assert command.handler is getattr(cli, command.handler.__name__)


def test_every_handler_has_a_command() -> None:
    bound = {command.handler.__name__ for command in cli.COMMANDS.values()}
    assert _handlers_in_module() == bound


def test_a_handler_without_a_command_is_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falsifier of the test above: an orphan handler breaks the equality."""

    def cmd_proxy_orphan(args: list[str]) -> int:
        return 0

    monkeypatch.setattr(cli, "cmd_proxy_orphan", cmd_proxy_orphan, raising=False)
    bound = {command.handler.__name__ for command in cli.COMMANDS.values()}
    assert _handlers_in_module() != bound


def test_a_command_without_a_handler_is_refused() -> None:
    with pytest.raises(TypeError):
        cli.Command("<x>", "no handler")  # type: ignore[call-arg]


def test_help_is_rendered_from_the_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for (noun, verb), command in cli.COMMANDS.items():
        assert f"nspawn {noun} {verb} {command.synopsis}".rstrip() in out
        assert command.summary in out


def test_mcp_serve_as_a_real_process_serves_the_catalogue_and_its_calls(
    tmp_path: Path,
) -> None:
    """The SDK door, driven over a real stdio pipe: the handshake, the catalogue
    byte for byte, one tool call, and a typed refusal that keeps the door up."""
    peer = StdioServer(sys.executable, ("-m", "nspawn.cli", "mcp", "serve"))
    with McpClient(peer, tmp_path) as client:
        listed = client._request("tools/list", {})
        assert json.dumps(listed["tools"], sort_keys=True) == json.dumps(
            [
                {
                    "name": c.name,
                    "description": c.description,
                    "inputSchema": c.input_schema,
                }
                for c in NSPAWN_TOOL_CONTRACTS
            ],
            sort_keys=True,
        )
        with pytest.raises(McpRefused) as refused:
            client.call("nspawn_no_such_tool", {})
        assert refused.value.message == "nSPAWN has no tool named 'nspawn_no_such_tool'"
        assert "providers" in client.call("nspawn_provider_list", {})

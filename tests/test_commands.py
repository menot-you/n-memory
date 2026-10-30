"""The command table: `nmemory <noun> <verb>`, one literal COMMANDS dict.

`tools/organization.py` reads COMMANDS by AST; these tests hold what the guard cannot:
every command has its own handler, every handler has a command, the help is derived
from the table, and the pre-table spellings (`migrate`, `recall`, `digest`, `get`,
`github-sync`, `embeddings status`, ...) are unknown commands rather than aliases. A bare
`nmemory` is a usage error, not the server: the server is `mcp serve`.
"""

from __future__ import annotations

import inspect

import pytest

from nmemory import cli


def handlers_in_module() -> set[str]:
    return {
        name
        for name, value in vars(cli).items()
        if name.startswith("cmd_") and inspect.isfunction(value)
    }


def test_the_table_is_the_declared_grammar() -> None:
    assert set(cli.COMMANDS) == {
        ("project", "bootstrap"),
        ("release", "export"),
        ("curator", "set"),
        ("mcp", "serve"),
        ("database", "create"),
        ("database", "snapshot"),
        ("capsule", "retrieve"),
        ("capsule", "list"),
        ("capsule", "show"),
        ("capsule", "export"),
        ("capsule", "relate"),
        ("capsule", "audit"),
        ("proposal", "export"),
        ("digest", "show"),
        ("context", "search"),
        ("index", "build"),
        ("project", "scan"),
        ("portal", "export"),
        ("anchor", "list"),
        ("anchor", "check"),
        ("quality", "measure"),
        ("review", "run"),
        ("embedding", "show"),
        ("rule", "write"),
    }


def test_every_command_has_its_own_handler() -> None:
    for (noun, verb), command in cli.COMMANDS.items():
        assert command.handler.__name__ == f"cmd_{noun}_{verb}", (noun, verb)
        assert command.handler is getattr(cli, command.handler.__name__)


def test_every_handler_has_a_command() -> None:
    bound = {command.handler.__name__ for command in cli.COMMANDS.values()}
    assert handlers_in_module() == bound


def test_a_handler_without_a_command_is_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Falsifier of the test above: an orphan handler breaks the equality."""

    def cmd_capsule_orphan(args: list[str]) -> None:
        return None

    monkeypatch.setattr(cli, "cmd_capsule_orphan", cmd_capsule_orphan, raising=False)
    bound = {command.handler.__name__ for command in cli.COMMANDS.values()}
    assert handlers_in_module() != bound


def test_a_command_without_a_handler_is_refused() -> None:
    with pytest.raises(TypeError):
        cli.Command("<x>", "no handler")  # type: ignore[call-arg]


def test_help_is_rendered_from_the_table(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    for (noun, verb), command in cli.COMMANDS.items():
        assert f"nmemory {noun} {verb} {command.synopsis}".rstrip() in out
        assert command.summary in out


@pytest.mark.parametrize(
    "argv",
    [
        ["migrate"],
        ["snapshot-repair"],
        ["recall", "x"],
        ["digest"],
        ["backfill-reads"],
        ["rebind-projects"],
        ["get", "--id", "x"],
        ["export"],
        ["git-scan", "--repo", "."],
        ["context", "x"],
        ["index"],
        ["vault", "--out", "x"],
        ["github-sync", "--verify"],
        ["github", "sync", "--verify"],
        ["relate"],
        ["anchors"],
        ["anchor-check", "--all"],
        ["staleness"],
        ["curate", "--daily"],
        ["hygiene"],
        ["embeddings", "status"],
        ["embeddings", "batch-submit"],
        ["--project", "pynott", "hygiene"],
        ["--read-only"],
    ],
)
def test_the_old_spellings_are_unknown(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(argv) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("unknown command: nmemory"), captured.err


def test_bare_nmemory_is_a_usage_error_not_the_server(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main([]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage: nmemory" in captured.err


def test_version_is_the_one_top_level_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("nmemory ")

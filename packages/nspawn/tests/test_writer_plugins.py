"""Writer plugins keep hooks without exposing another supervisor server."""

import json
from pathlib import Path

import pytest
from supervisor_support import registry, roster_home
from test_writer_cage import writer_launch

from nspawn.owner_roster import Roster

__all__ = ["registry", "roster_home", "writer_launch"]


@pytest.mark.parametrize("inline", [False, True])
def test_each_extra_writer_plugin_keeps_hooks_without_nspawn(
    writer_launch, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inline: bool
) -> None:
    extra = tmp_path / "extra-plugin"
    (extra / "hooks").mkdir(parents=True)
    (extra / "hooks/hooks.json").write_text('{"hooks": {}}')
    (extra / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"nspawn": {"command": "never-start"}}})
    )
    resolve = Roster.resolve

    def with_extra(roster, name):
        resolved = resolve(roster, name)
        resolved.args = [
            *resolved.args,
            *(
                ["--plugin-dir=" + str(extra)]
                if inline
                else ["--plugin-dir", str(extra)]
            ),
        ]
        return resolved

    monkeypatch.setattr(Roster, "resolve", with_extra)
    seen = writer_launch()
    assert len(seen.starts) == 1
    argv = seen.starts[0].argv
    plugins = []
    for index, argument in enumerate(argv):
        if argument == "--plugin-dir":
            plugins.append(Path(argv[index + 1]))
        elif argument.startswith("--plugin-dir="):
            plugins.append(Path(argument.partition("=")[2]))
    assert len(plugins) == 2
    assert extra not in plugins
    assert any(
        (plugin / "hooks/hooks.json").read_bytes() == b'{"hooks": {}}'
        for plugin in plugins
    )
    for plugin in plugins:
        assert (
            "nspawn" not in json.loads((plugin / ".mcp.json").read_text())["mcpServers"]
        )
    assert "nspawn" in json.loads((extra / ".mcp.json").read_text())["mcpServers"]

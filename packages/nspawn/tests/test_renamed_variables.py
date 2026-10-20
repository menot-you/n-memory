"""A set old variable name is refused by name."""

from __future__ import annotations

import pytest

from nspawn.renamed import RENAMED, RenamedVariableError, env


@pytest.mark.parametrize("new", sorted(RENAMED))
def test_every_renamed_variable_is_refused_by_its_old_name(
    monkeypatch: pytest.MonkeyPatch, new: str
) -> None:
    old = RENAMED[new]
    monkeypatch.setenv(new, "pynott")
    monkeypatch.setenv(old, "pynott")
    with pytest.raises(RenamedVariableError) as refused:
        env(new)
    assert (refused.value.old, refused.value.new) == (old, new)


def test_a_command_refuses_a_set_old_system1_name_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nott_system1.renamed import RENAMED as SYSTEM1

    from nspawn import cli

    old = SYSTEM1["NOTT_SYSTEM1_ENDPOINT"]
    monkeypatch.setenv(old, "http://127.0.0.1:9/never")
    assert cli.main(["mcp", "serve"]) == 2
    assert f"{old} was renamed to NOTT_SYSTEM1_ENDPOINT" in capsys.readouterr().err

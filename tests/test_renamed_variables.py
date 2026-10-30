"""A set old variable name is refused by name."""

from __future__ import annotations

import os
import secrets

import pytest

from nmemory.renamed import RENAMED, RenamedVariableError, env


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


def test_a_command_refuses_a_set_old_name_before_it_runs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nmemory import cli

    old = RENAMED["NOTT_NMEMORY_ANCHOR_ROOT"]
    monkeypatch.setenv(old, "/nonexistent")
    assert cli.main(["digest", "show"]) == 2
    assert f"{old} was renamed to NOTT_NMEMORY_ANCHOR_ROOT" in capsys.readouterr().err


def test_a_command_refuses_a_set_old_system1_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nott_system1.renamed import RENAMED as SYSTEM1

    from nmemory import cli

    old = SYSTEM1["NOTT_SYSTEM1_API_KEY"]
    monkeypatch.setenv(old, "old-spelling-sentinel")
    assert cli.main(["digest", "show"]) == 2
    assert f"{old} was renamed to NOTT_SYSTEM1_API_KEY" in capsys.readouterr().err


def test_the_check_is_never_handed_an_environment_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing frame prints its arguments: no frame may hold a value, only names."""
    new, old = min(RENAMED.items())
    # Made at run time: the long traceback prints this function's source, literals too.
    held = {old: secrets.token_hex(8), "NOTT_CANARY_SECRET": secrets.token_hex(8)}
    monkeypatch.setattr(os, "environ", held)
    with pytest.raises(RenamedVariableError) as refused:
        env(new)
    text = str(refused.getrepr(style="long", funcargs=True, truncate_locals=False))
    assert old in text
    assert not any(value in text for value in held.values())

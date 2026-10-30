"""A set old variable name is refused by name."""

from __future__ import annotations

import os
import secrets

import pytest

from nott_system1.renamed import RENAMED, RETIRED, RenamedVariableError, env


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


@pytest.mark.parametrize("old", sorted(RETIRED))
def test_every_retired_spelling_is_refused_by_the_variable_read_in_its_place(
    monkeypatch: pytest.MonkeyPatch, old: str
) -> None:
    new = RETIRED[old]
    monkeypatch.setenv(new, "pynott")
    monkeypatch.setenv(old, "pynott")
    with pytest.raises(RenamedVariableError) as refused:
        env(new)
    assert (refused.value.old, refused.value.new) == (old, new)


def test_the_transport_refuses_the_old_key_rather_than_read_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nott_system1

    old = RENAMED["NOTT_SYSTEM1_API_KEY"]
    monkeypatch.delenv("NOTT_SYSTEM1_API_KEY", raising=False)
    monkeypatch.setenv(old, "old-spelling-sentinel")
    with pytest.raises(RenamedVariableError):
        nott_system1.ask(
            "fixture state",
            {"q": {"type": "noul", "instructions": "Is this a fixture?"}},
            endpoint="http://127.0.0.1:9/never",
            model="fixture",
            timeout=1.0,
        )


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

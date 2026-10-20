"""A set old variable name is refused by name."""

from __future__ import annotations

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

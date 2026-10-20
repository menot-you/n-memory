"""A `[classes]` policy with `fast = true` runs its class on the route's `model_fast`.

The owner's method: the strong model decides and judges,
the fast one runs experiments, probes and sweeps that have an oracle. Those are
dispatched as `mechanical_fanout`, so the roster says that class takes the fast
lane; the caller still pins whatever it wants, and an observation never moves a
fast class onto a slower model.
"""

from __future__ import annotations

import re
from pathlib import Path

from workspace_root import workspace_path

from nspawn import classify, picker
from nspawn.owner_roster import Roster

_ROSTER = (
    "[classes]\n"
    'mechanical_fanout = { effort = "low", ceiling = "medium", fast = true }\n'
    'unclassified = { effort = "low", ceiling = "medium" }\n'
    'review = { effort = "low", ceiling = "medium" }\n'
    "\n"
    '[bins]\nclaude = "/never-execute/vendor"\n'
    '[[provider]]\nname = "route"\nkind = "claude"\n'
    'secrets = "/never-read/secrets"\n'
    'model = "strong"\nmodel_fast = "quick"\nmodels = ["strong", "quick"]\n'
    'efforts = ["low", "medium"]\ndefault = true\n'
)


def _roster(tmp_path: Path, text: str = _ROSTER) -> Roster:
    path = tmp_path / "spawn.toml"
    path.write_text(text)
    return Roster.read(path)


def test_a_fast_class_runs_on_the_routes_fast_model(tmp_path: Path) -> None:
    picked = picker.pick_execution(
        _roster(tmp_path), 0, execution_class="mechanical_fanout"
    )
    assert picked.model == "quick"
    # The fast lane is the class's, not a preference a re-pick would repeat.
    assert picked.preferred_model is None


def test_a_class_without_the_fast_lane_keeps_the_routes_model(tmp_path: Path) -> None:
    picked = picker.pick_execution(_roster(tmp_path), 0, execution_class="review")
    assert picked.model == "strong"


def test_a_pinned_model_still_wins_over_the_fast_lane(tmp_path: Path) -> None:
    picked = picker.pick_execution(
        _roster(tmp_path),
        0,
        execution_class="mechanical_fanout",
        requested_model="strong",
    )
    assert picked.model == "strong"


def test_a_route_without_model_fast_keeps_its_model(tmp_path: Path) -> None:
    roster = _roster(tmp_path, _ROSTER.replace('model_fast = "quick"\n', ""))
    picked = picker.pick_execution(roster, 0, execution_class="mechanical_fanout")
    assert picked.model == "strong"


def test_an_observation_never_moves_a_fast_class_onto_the_slow_model(
    tmp_path: Path,
) -> None:
    roster = _roster(tmp_path)
    # Equal on every other axis: only the lane differs, and leaving it is dearer.
    assert not classify._is_cheaper(roster, "unclassified", "mechanical_fanout")
    # Entering the fast lane at no other cost is a descent.
    assert classify._is_cheaper(roster, "mechanical_fanout", "unclassified")


def test_fast_must_be_a_boolean(tmp_path: Path) -> None:
    import pytest

    from nspawn.owner_roster import RosterError

    with pytest.raises(RosterError, match="fast"):
        _roster(tmp_path, _ROSTER.replace("fast = true", 'fast = "yes"'))


def test_the_versioned_roster_sends_experiments_to_haiku(tmp_path: Path) -> None:
    """The template, never the live roster, carries the owner's method.

    Its `[classes]` example ships commented (a declared table changes every
    class's effort), so the example is uncommented here, with `secrets` moved
    to an empty directory so no account discovery reads the host's store.
    """
    template = (workspace_path("templates/nott/spawn.toml")).read_text()
    assert Roster.read(workspace_path("templates/nott/spawn.toml")).classes == {}
    text = re.sub(
        r"(?m)^# (\[classes\]|(?:unclassified|mechanical_fanout|deterministic) +=)",
        r"\1",
        template,
    )
    assert '"~/.nott/secrets/claude"' in text
    text = text.replace('"~/.nott/secrets/claude"', f'"{tmp_path}"')
    roster = _roster(tmp_path, text)
    claude = roster.select("claude")
    assert claude is not None
    assert claude.model_fast == "haiku"
    assert roster.classes["mechanical_fanout"].fast
    picked = picker.pick_execution(roster, 0, execution_class="mechanical_fanout")
    assert picked.model == "haiku"


def test_account_selection_keeps_the_fast_lane(tmp_path: Path) -> None:
    """An account that overrides the model must not swallow the fast default."""
    roster = _roster(
        tmp_path,
        _ROSTER
        + '\n[provider.accounts.main]\ncapacity = 4\nauth_env = "NEVER_SET_MAIN"\n'
        + '\n[provider.accounts.slow]\nmodel = "strong"\nauth_env = "NEVER_SET_SLOW"\n',
    )
    picked = picker.pick_execution(
        roster,
        0,
        execution_class="mechanical_fanout",
        account_load={"main": 1, "slow": 0},
    )
    assert (picked.executed_account, picked.model) == ("main", "quick")


def test_account_selection_keeps_the_fast_lane_without_a_models_list(
    tmp_path: Path,
) -> None:
    """`models` is optional; the route's own `model` is declared without it."""
    roster = _roster(
        tmp_path,
        _ROSTER.replace('models = ["strong", "quick"]\n', "")
        + '\n[provider.accounts.main]\ncapacity = 4\nauth_env = "NEVER_SET_MAIN"\n'
        + '\n[provider.accounts.slow]\nmodel = "strong"\nauth_env = "NEVER_SET_SLOW"\n',
    )
    picked = picker.pick_execution(
        roster,
        0,
        execution_class="mechanical_fanout",
        account_load={"main": 1, "slow": 0},
    )
    assert (picked.executed_account, picked.model) == ("main", "quick")


def test_a_family_fast_lane_does_not_choose_a_pinned_opus_account(
    tmp_path: Path, claude_models: dict[str, str]
) -> None:
    text = _ROSTER.replace('"strong"', '"opus"').replace('"quick"', '"haiku"')
    text += '\n[provider.accounts.main]\nauth_env = "NEVER_SET_MAIN"\n'
    text += '\n[provider.accounts.slow]\nauth_env = "NEVER_SET_SLOW"\nmodel = "claude-opus-5-5"\n'
    picked = picker.pick_execution(
        _roster(tmp_path, text),
        0,
        execution_class="mechanical_fanout",
        account_load={"main": 1, "slow": 0},
    )
    assert (picked.executed_account, picked.model) == ("main", "haiku")


def test_no_account_running_the_fast_model_keeps_the_accounts_model(
    tmp_path: Path,
) -> None:
    roster = _roster(
        tmp_path,
        _ROSTER
        + '\n[provider.accounts.slow]\nmodel = "strong"\nauth_env = "NEVER_SET_SLOW"\n',
    )
    picked = picker.pick_execution(roster, 0, execution_class="mechanical_fanout")
    assert (picked.executed_account, picked.model) == ("slow", "strong")


def test_an_account_spelling_the_same_model_its_own_way_keeps_its_load(
    tmp_path: Path,
) -> None:
    """vLLM says `m-27b`, ollama says `m:27b`: the same model, so no re-pick."""
    roster = _roster(
        tmp_path,
        "[classes]\n"
        'mechanical_fanout = { effort = "low", ceiling = "medium", fast = true }\n'
        'unclassified = { effort = "low", ceiling = "medium" }\n'
        '\n[bins]\nclaude = "/never-execute/vendor"\n'
        '[[provider]]\nname = "route"\nkind = "claude"\n'
        'secrets = "/never-read/secrets"\n'
        'model = "m-27b"\nmodel_fast = "m-27b"\nefforts = ["low", "medium"]\n'
        "default = true\n"
        '\n[provider.accounts.server]\nmodel = "m-27b"\ncapacity = 4\n'
        'auth_env = "NEVER_SET_SERVER"\n'
        '\n[provider.accounts.m5]\nmodel = "m:27b"\ncapacity = 2\n'
        'auth_env = "NEVER_SET_M5"\n',
    )
    picked = picker.pick_execution(
        roster,
        0,
        execution_class="mechanical_fanout",
        account_load={"server": 4, "m5": 0},
    )
    assert (picked.executed_account, picked.model) == ("m5", "m:27b")

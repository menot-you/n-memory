"""The roster role decides the route of an unpinned review or native writer turn.

Run 01e02736 was an `nspawn_review_start` with the agent
plan-reviewer (`model: opus`) and no provider: it ran on `claude`, the route of
the agent's model, and the reviewer role came third. An unpinned native writer
does not read `[roles]` at all.

With no `provider`, a review takes the route `[roles]` binds to the reviewer, and
a fresh native writer turn the route it binds to the writer. The agent's model
and effort apply only where that route serves them. Else the route runs the
role's `[role_profile]`, with a warning that names the agent's model. The
profile is the route's own choice, not a pin: an agent's model that the route
serves wins over it.

Each start goes through the real door, the real supervisor and the real picker.
The pick is recorded and then refused (`test_agent_suggestion._start`), so no run
directory, row or child exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from class_dispatch import system1_never_reaches_the_network
from supervisor_support import _sv, registry, roster_home
from test_agent_suggestion import _BINS, _CLAUDE, _UNCLASSIFIED, _start
from test_driver import _call, _payload
from test_native_write import _CLAUDE_TOKEN, _claude_write, claude_writer

from nspawn import picker
from nspawn.owner_roster import Provider, Roster
from nspawn.ranks import REVIEW, VERIFICATION

# Export imported fixtures so pytest discovers them in this module.
__all__ = [
    "claude_writer",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]

#: The claude-kind `gpt` route, as on the live roster: the Claude client through
#: a local proxy. Its own model is not the one that the role profiles name.
_GPT = (
    '[[provider]]\nname = "gpt"\nkind = "claude"\n'
    'secrets = "/never-read/secrets"\nstrength = 1\n'
    'model = "gpt-5.6-terra"\nmodels = ["gpt-5.6-terra", "gpt-6.1-sol"]\n'
    'efforts = ["low", "medium", "high", "xhigh", "max"]\n'
    "[provider.env]\n"
    'ANTHROPIC_BASE_URL = "http://127.0.0.1:1"\n'
)

_ROLES = '[roles]\nwriter = "gpt"\nreviewer = "gpt"\n'


def _profile(role: str, model: str, effort: str) -> str:
    return f'[role_profile.{role}]\nmodel = "{model}"\neffort = "{effort}"\n'


def _writer(tmp_path: Path) -> tuple[str, dict[str, object]]:
    """A fresh native writer turn of the verification class, which needs no RED
    record, in a standalone clone under a declared `[native_write]` root."""
    clone = tmp_path / "writer"
    (clone / ".git").mkdir(parents=True)
    roots = f'[native_write]\nroots = ["{tmp_path}"]\n'
    return roots, {
        "cwd": str(clone),
        "read_only": False,
        "native_write": True,
        "class": VERIFICATION,
    }


def _first(picks: list[dict[str, Any]], answer: dict[str, Any]) -> Any:
    """The first route that the start tried, after a check that it tried one."""
    assert picks, answer
    return picks[0]


@pytest.fixture(autouse=True)
def _no_catalogue_binary(claude_models: dict[str, str]) -> None:
    """No pick in this module starts a Claude binary to read its catalogue."""


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("nspawn_review_start", {}), ("nspawn_run_start", {"class": REVIEW})],
    ids=["review-verb", "review-class"],
)
def test_an_unpinned_review_runs_on_the_reviewer_role_route_not_the_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, object],
) -> None:
    """Run 01e02736: the agent's `opus` sent an unpinned review to claude."""
    routes = (
        _BINS + _CLAUDE + _GPT + _ROLES + _profile("reviewer", "gpt-6.1-sol", "high")
    )
    picks, answer = _start(
        tmp_path, monkeypatch, routes, {"agent": "planner", **arguments}, tool=tool
    )
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    picked = first["picked"]
    # gpt does not serve opus: the role's model runs. The agent's effort `max`
    # is served, so it wins over the role's `high`.
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "max",
    )
    assert 'not the suggested model "opus"' in (picked.warning or "")


def test_an_unpinned_native_writer_runs_on_the_writer_role_route_not_the_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, writer = _writer(tmp_path)
    routes = (
        _BINS
        + _CLAUDE
        + _GPT
        + _ROLES
        + _profile("writer", "gpt-6.1-sol", "xhigh")
        + roots
    )
    picks, answer = _start(
        tmp_path, monkeypatch, routes, {**writer, "agent": "planner"}
    )
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    picked = first["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "max",
    )
    assert 'not the suggested model "opus"' in (picked.warning or "")


def test_an_agentless_native_writer_runs_the_writer_role_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no agent, the writer role's route, model and effort run. The route's
    own model, `gpt-5.6-terra`, does not."""
    roots, writer = _writer(tmp_path)
    routes = (
        _BINS
        + _CLAUDE
        + _GPT
        + _ROLES
        + _profile("writer", "gpt-6.1-sol", "xhigh")
        + roots
    )
    picks, answer = _start(tmp_path, monkeypatch, routes, writer)
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    picked = first["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "xhigh",
    )


def test_a_role_effort_above_the_class_ceiling_runs_at_the_ceiling_on_the_role_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The role's effort is a default, not a pin: above the class ceiling it runs
    at the ceiling, and it never moves the turn off the role's route."""
    roots, writer = _writer(tmp_path)
    routes = (
        "[classes]\n"
        'verification = { effort = "low", ceiling = "medium" }\n'
        + _UNCLASSIFIED
        + _BINS
        + _CLAUDE
        + _GPT
        + _ROLES
        + _profile("writer", "gpt-6.1-sol", "xhigh")
        + roots
    )
    picks, answer = _start(tmp_path, monkeypatch, routes, writer)
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    picked = first["picked"]
    assert (picked.executed_provider.name, picked.effort) == ("gpt", "medium")


def test_an_unpinned_review_tries_the_owner_routes_before_the_routes_of_the_agent() -> (
    None
):
    """The owner's `[preference]` list, then the reviewer role, then the routes
    that serve the agent's model, then the class rank."""
    roster = Roster(
        providers=[
            Provider(name="claude", strength=9, model="opus", models=["opus"]),
            Provider(
                name="gpt", strength=1, model="gpt-5.6-terra", models=["gpt-6.1-sol"]
            ),
            Provider(name="qwen", strength=5, model="qwen-27b"),
        ],
        roles={"reviewer": "gpt"},
        role_profiles={"reviewer": {"model": "gpt-6.1-sol"}},
        preferences={"review": [("qwen", None)]},
    )
    order = picker.preferences(
        roster, REVIEW, preferred_provider="claude", preferred_model="opus"
    )
    assert [str(preference) for preference in order] == [
        "qwen",
        "gpt/gpt-6.1-sol",
        "claude",
        "gpt",
    ]


# Guards. Each one passes today and must keep passing.


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [("nspawn_review_start", {}), ("nspawn_run_start", {"class": REVIEW})],
    ids=["review-verb", "review-class"],
)
def test_a_review_agents_model_that_the_role_route_serves_wins_over_the_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict[str, object],
) -> None:
    routes = (
        _BINS + _CLAUDE + _GPT + _ROLES + _profile("reviewer", "gpt-5.6-terra", "high")
    )
    picks, answer = _start(
        tmp_path, monkeypatch, routes, {"agent": "adversarial", **arguments}, tool=tool
    )
    picked = _first(picks, answer)["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "max",
    )
    assert "suggested model" not in (picked.warning or "")


def test_a_writer_agents_model_that_the_role_route_serves_wins_over_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, writer = _writer(tmp_path)
    routes = (
        _BINS
        + _CLAUDE
        + _GPT
        + _ROLES
        + _profile("writer", "gpt-5.6-terra", "high")
        + roots
    )
    picks, answer = _start(
        tmp_path, monkeypatch, routes, {**writer, "agent": "adversarial"}
    )
    picked = _first(picks, answer)["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "max",
    )
    assert "suggested model" not in (picked.warning or "")


def test_an_agentless_review_runs_the_reviewer_role_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = (
        _BINS + _CLAUDE + _GPT + _ROLES + _profile("reviewer", "gpt-6.1-sol", "high")
    )
    picks, answer = _start(
        tmp_path, monkeypatch, routes, {}, tool="nspawn_review_start"
    )
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    picked = first["picked"]
    assert (picked.model, picked.effort) == ("gpt-6.1-sol", "high")


def test_a_pinned_review_keeps_its_route_and_the_agents_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    routes = (
        _BINS + _CLAUDE + _GPT + _ROLES + _profile("reviewer", "gpt-6.1-sol", "high")
    )
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        routes,
        {"agent": "planner", "provider": "claude"},
        tool="nspawn_review_start",
    )
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "claude"
    assert first["picked"].model == "opus"


def test_a_pinned_native_writer_keeps_its_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots, writer = _writer(tmp_path)
    routes = (
        _BINS
        + _CLAUDE
        + _GPT
        + _ROLES
        + _profile("writer", "gpt-6.1-sol", "xhigh")
        + roots
    )
    picks, answer = _start(
        tmp_path,
        monkeypatch,
        routes,
        {**writer, "agent": "planner", "provider": "claude"},
    )
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "claude"
    assert first["picked"].model == "opus"


def test_a_credential_repick_keeps_the_roles_model_and_effort(tmp_path: Path) -> None:
    """A second account keeps the role defaults, not the route defaults."""
    import time

    from registry_reset import store

    from nspawn.owner_roster import NativeAccount
    from nspawn.registry import Registry
    from nspawn.supervisor import Supervisor

    provider = Provider(
        name="gpt",
        model="gpt-5.6-terra",
        models=["gpt-5.6-terra", "gpt-6.1-sol"],
        effort="low",
        efforts=["low", "high", "xhigh"],
        accounts={
            "acct-a": NativeAccount(home=None),
            "acct-b": NativeAccount(home=None),
        },
    )
    roster = Roster(
        providers=[provider],
        roles={"writer": "gpt"},
        role_profiles={"writer": {"model": "gpt-6.1-sol", "effort": "xhigh"}},
    )
    profile = roster.role_profiles["writer"]
    registry = Registry(store(), "role-repick").connect()
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        picked = picker.pick_execution(
            roster,
            int(time.time()),
            requested_provider="gpt",
            execution_class=VERIFICATION,
            role_model=profile["model"],
            role_effort=profile["effort"],
            account_load={"acct-b": 1},
        )
        assert (picked.executed_account, picked.model, picked.effort) == (
            "acct-a",
            "gpt-6.1-sol",
            "xhigh",
        )
        assert (picked.requested_model, picked.requested_effort) == (None, None)
        sv._wall_rejected_credential(picked, limit_type="credential_rejected")
        nxt = sv._repick_after_credential_rejection(
            roster, picked, {"acct-a"}, execution_class=VERIFICATION
        )
    finally:
        sv.shutdown()
        registry.close()
    assert isinstance(nxt, picker.PickerResult)
    assert (nxt.executed_account, nxt.model, nxt.effort) == (
        "acct-b",
        "gpt-6.1-sol",
        "xhigh",
    )
    assert (nxt.requested_model, nxt.requested_effort) == (None, None)
    assert (nxt.role_model, nxt.role_effort) == ("gpt-6.1-sol", "xhigh")


# Cure round 1. Each test below reproduces one finding of the round-1 reader.


def _gpt_efforts(*efforts: str) -> str:
    """The `gpt` route with only `efforts` on its effort ladder."""
    ladder = ", ".join(f'"{effort}"' for effort in efforts)
    return _GPT.replace(
        'efforts = ["low", "medium", "high", "xhigh", "max"]\n',
        f"efforts = [{ladder}]\n",
    )


def test_a_role_effort_admits_a_reviewer_route_that_serves_nothing_at_the_initial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding F1: the route serves no rung at or below the review initial `low`.
    The reviewer profile's `high` is the call's effort, as it was when the
    profile was a pin, so the class admits the route and `high` runs on it."""
    routes = (
        "[classes]\n"
        'review = { effort = "low", ceiling = "high" }\n'
        + _UNCLASSIFIED
        + _BINS
        + _CLAUDE
        + _gpt_efforts("high")
        + _ROLES
        + _profile("reviewer", "gpt-6.1-sol", "high")
    )
    picks, answer = _start(
        tmp_path, monkeypatch, routes, {}, tool="nspawn_review_start"
    )
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    assert "picked" in first, ("the picker refused the reviewer route", answer)
    picked = first["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "high",
    )


def test_a_role_effort_admits_a_writer_route_that_serves_nothing_at_the_initial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding F1, writer form: the verification initial is `low` and its
    ceiling `medium`. The route serves `medium` and `xhigh`. The writer
    profile's `xhigh` runs at the ceiling on the writer's route."""
    roots, writer = _writer(tmp_path)
    routes = (
        "[classes]\n"
        'verification = { effort = "low", ceiling = "medium" }\n'
        + _UNCLASSIFIED
        + _BINS
        + _CLAUDE
        + _gpt_efforts("medium", "xhigh")
        + _ROLES
        + _profile("writer", "gpt-6.1-sol", "xhigh")
        + roots
    )
    picks, answer = _start(tmp_path, monkeypatch, routes, writer)
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    assert "picked" in first, ("the picker refused the writer route", answer)
    picked = first["picked"]
    assert (picked.executed_provider.name, picked.model, picked.effort) == (
        "gpt",
        "gpt-6.1-sol",
        "medium",
    )


def _two_model_route(*accounts: tuple[str, str | None]) -> Roster:
    """A route that serves `opus` and `sonnet`, with `accounts` in declared order.

    Each account is an alias and its own model, or None for no override.
    """
    from nspawn.owner_roster import NativeAccount

    provider = Provider(
        name="claude",
        model="opus",
        models=["opus", "sonnet"],
        accounts={
            alias: NativeAccount(home=None, model=model) for alias, model in accounts
        },
    )
    return Roster(
        providers=[provider],
        roles={"reviewer": "claude"},
        role_profiles={"reviewer": {"model": "opus"}},
    )


def test_the_role_model_runs_on_an_account_of_the_route_that_runs_it() -> None:
    """Finding F2: load takes the first account, whose own model is the route's
    other declared model. The role's model is not a pin, but another account of
    the route runs it, so that account takes the turn."""
    import time

    roster = _two_model_route(("b", "sonnet"), ("a", None))
    picked = picker.pick_execution(
        roster,
        int(time.time()),
        requested_provider="claude",
        execution_class=REVIEW,
        role_model="opus",
    )
    assert (picked.executed_account, picked.model) == ("a", "opus")


def test_a_credential_repick_takes_an_account_that_runs_the_roles_model(
    tmp_path: Path,
) -> None:
    """Finding F2, D6 form: account `a` ran the role's model and its credential
    was refused. Load would take `b`, whose own model is `sonnet`. Account `c`
    runs the role's model, so `c` takes the turn."""
    import time

    from registry_reset import store

    from nspawn.registry import Registry
    from nspawn.supervisor import Supervisor

    roster = _two_model_route(("a", None), ("b", "sonnet"), ("c", None))
    registry = Registry(store(), "role-repick-model").connect()
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        picked = picker.pick_execution(
            roster,
            int(time.time()),
            requested_provider="claude",
            execution_class=REVIEW,
            role_model="opus",
        )
        assert (picked.executed_account, picked.model) == ("a", "opus")
        sv._wall_rejected_credential(picked, limit_type="credential_rejected")
        nxt = sv._repick_after_credential_rejection(
            roster, picked, {"a"}, execution_class=REVIEW
        )
    finally:
        sv.shutdown()
        registry.close()
    assert isinstance(nxt, picker.PickerResult)
    assert (nxt.executed_account, nxt.model) == ("c", "opus")


def test_an_unpinned_role_writer_walk_ends_with_the_other_accounts_of_its_route(
    registry: Any,
    tmp_path: Path,
    claude_writer: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finding F3: a writer never moves to another account inside its run, so its
    walk ends with the other accounts of the route that it took. The walk of an
    unpinned writer did so before the writer role chose its route."""
    from nspawn.mcp import McpServer
    from nspawn.supervisor import Supervisor, _AccountStep

    secrets = tmp_path / "secrets"
    (secrets / "writer-c.token").write_text(_CLAUDE_TOKEN[:-4] + "wxyz\n")
    (secrets / "writer-c.token").chmod(0o600)
    with claude_writer.roster.open("a", encoding="utf-8") as roster:
        roster.write('[roles]\nwriter = "writer"\n')
    monkeypatch.setattr(
        Supervisor, "_drive_claude_caged", lambda self, *args, **kwargs: None
    )
    arguments = _claude_write(claude_writer)
    del arguments["provider"]
    sv = _sv(registry, tmp_path)
    try:
        reply = _call(McpServer(supervisor=sv), "nspawn_run_start", arguments)
        assert "error" not in reply, reply
        run_id = _payload(reply)["run_id"]
        took = registry.get_run(run_id).executed_account
        retained = sv._failovers.get(run_id)
        assert retained is not None, "the writer run has no walk"
        tail = {
            (step.route, step.account)
            for step in retained[0].remaining
            if isinstance(step, _AccountStep)
        }
    finally:
        sv.shutdown()
    accounts = {"writer-a", "writer-b", "writer-c"}
    assert took in accounts
    assert tail == {("writer", alias) for alias in accounts - {took}}


# Cure round 2. The test below reproduces the finding of the round-2 reader.


def test_a_writer_role_effort_never_runs_above_the_class_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding F4: the writer's route serves no rung at or below the verification
    ceiling `medium`. The roster checks a writer profile against the judgment
    class, so its `max` is valid. The role's effort caps at the ceiling, and the
    clamp to the served ladder then gives the least rung that the route serves,
    `xhigh`, above the ceiling. No pick runs above the class ceiling, so the
    writer's route cannot take the turn.

    A reviewer profile cannot reach this input: the roster checks it against the
    review ceiling and against the efforts that its route serves.
    """
    roots, writer = _writer(tmp_path)
    routes = (
        "[classes]\n"
        'verification = { effort = "low", ceiling = "medium" }\n'
        + _UNCLASSIFIED
        + _BINS
        + _CLAUDE
        + _gpt_efforts("xhigh", "max")
        + _ROLES
        + _profile("writer", "gpt-6.1-sol", "max")
        + roots
    )
    picks, answer = _start(tmp_path, monkeypatch, routes, writer)
    first = _first(picks, answer)
    assert first["asked"]["requested_provider"] == "gpt"
    ran = [
        (pick["picked"].executed_provider.name, pick["picked"].effort)
        for pick in picks
        if "picked" in pick
    ]
    limit = picker.EFFORT_LADDER.index("medium")
    above = [
        (route, effort)
        for route, effort in ran
        if effort is not None and picker.EFFORT_LADDER.index(effort) > limit
    ]
    assert not above, ("a pick runs above the class ceiling", ran)

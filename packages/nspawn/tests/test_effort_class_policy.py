"""The roster's `[classes]` table decides how much reasoning a kind of work buys.

Measured on a real session (codex 01a0baad, 2026-09-19): nine `nspawn_run_start` for
mechanical implementation went out with `provider="codex", model="gpt-6-astra",
effort="ultra"` by the agent's own choice, and the picker accepted because `ultra` is
on the provider's declared ladder. The owner did not accept the bill.

The policy belongs to the owner and lives in the roster, never in the caller: each
class of work declares the initial effort and the ceiling. A request above the ceiling is refused,
naming the ceiling and the class; a request with no effort gets the class's initial one.
A roster with no `[classes]` keeps the old behavior.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
from types import SimpleNamespace
from typing import Any

import pytest
from mcp_fakes import handle

from nspawn import classify, mcp, picker, supervisor, system1
from nspawn.owner_roster import Roster

_ROSTER = (
    "[classes]\n"
    'mechanical_fanout = { effort = "low", ceiling = "medium" }\n'
    'unclassified = { effort = "low", ceiling = "low" }\n'
    'planning = { effort = "high" }\n'
    "\n"
    '[bins]\ncodex = "/never-execute/vendor"\n'
    '[[provider]]\nname = "route"\nkind = "codex"\n'
    'secrets = "/never-read/secrets"\n'
    'model = "m"\nmodels = ["m"]\nstrength = 9\ndefault = true\n'
    'efforts = ["low", "medium", "high", "max"]\neffort = "max"\n'
)


def _roster_at(tmp_path: Path, text: str = _ROSTER) -> Roster:
    path = tmp_path / "classes.toml"
    path.write_text(text)
    return Roster.read(path)


def _dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roster: Roster,
    arguments: dict[str, object],
    *,
    provider: str | None = "route",
    observed: str = "mechanical_fanout",
    ask: Callable[..., dict[str, system1.Answer]] | None = None,
) -> dict:
    """The ROUTE that measured the defect, with dispatch itself forbidden.

    `provider` is a parameter and not an injection: automatic routing has no
    leg at all while every dispatch arrives pinned.
    """
    monkeypatch.setattr(system1, "ask", ask or _answering(observed))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster.path))
    instance = object.__new__(supervisor.Supervisor)
    instance._lock = RLock()
    instance._run_root = tmp_path
    instance._registry = SimpleNamespace(
        get_provider_walls=lambda now: {},
        get_account_walls=lambda now: {},
        # No run is alive in these doubles, so every account is idle.
        get_account_load=dict,
        get_account_usage=tuple,
        get_account_last_run=dict,
    )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("DISPATCH_ESCAPED")

    monkeypatch.setattr(supervisor, "create_run_directory", forbidden)
    answer = handle(
        mcp.McpServer(supervisor=instance),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "nspawn_run_start",
                "arguments": {
                    "prompt": "never execute",
                    "cwd": str(tmp_path),
                    # Read-only: the route is the same in every mode, and a
                    # writable dispatch that is not a native write is refused
                    # before the route is read.
                    "read_only": True,
                    **({} if provider is None else {"provider": provider}),
                    **arguments,
                },
            },
        },
    )
    assert "error" in answer, answer
    return answer["error"]


def test_an_effort_above_the_class_ceiling_is_refused_on_the_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path),
        {"effort": "max", "class": "mechanical_fanout"},
    )
    assert error["code"] == -32602
    message = error["message"]
    # The refusal carries class, ceiling, and request: caller does not need
    # to open spawn.toml to know what to declare.
    assert "mechanical_fanout" in message
    assert "medium" in message and "max" in message


def test_an_effort_within_the_ceiling_passes_this_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid progress: `medium` passes the picker and dies at the NEXT boundary."""
    error = _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path),
        {"effort": "medium", "class": "mechanical_fanout"},
    )
    assert error["message"] == "native credentials must belong to the selected account"


def test_no_effort_takes_the_class_initial_not_the_provider_default(
    tmp_path: Path,
) -> None:
    picked = picker.pick_execution(
        _roster_at(tmp_path),
        0,
        requested_provider="route",
        execution_class="mechanical_fanout",
    )
    assert picked.effort == "low"


def test_no_class_is_governed_by_the_unclassified_policy(tmp_path: Path) -> None:
    """Work that names no class buys the cheapest reasoning, not the provider's max."""
    roster = _roster_at(tmp_path)
    assert picker.pick_execution(roster, 0, requested_provider="route").effort == "low"
    with pytest.raises(picker.PickerError, match="unclassified"):
        picker.pick_execution(
            roster, 0, requested_provider="route", requested_effort="high"
        )


def test_a_ceiling_defaults_to_the_class_initial_and_high_classes_climb(
    tmp_path: Path,
) -> None:
    roster = _roster_at(tmp_path)
    picked = picker.pick_execution(
        roster,
        0,
        requested_provider="route",
        execution_class="planning",
        requested_effort="high",
    )
    assert picked.effort == "high"
    with pytest.raises(picker.PickerError, match="planning"):
        picker.pick_execution(
            roster,
            0,
            requested_provider="route",
            execution_class="planning",
            requested_effort="max",
        )


def test_a_class_initial_the_provider_does_not_serve_clamps_down(
    tmp_path: Path,
) -> None:
    """agy serves [low, medium, high]; a class starting at `max` lands on `high`."""
    text = (
        _ROSTER.replace(
            'planning = { effort = "high" }', 'planning = { effort = "max" }'
        )
        .replace(
            'efforts = ["low", "medium", "high", "max"]',
            'efforts = ["low", "medium", "high"]',
        )
        .replace('effort = "max"\n', 'effort = "high"\n')
    )
    picked = picker.pick_execution(
        _roster_at(tmp_path, text),
        0,
        requested_provider="route",
        execution_class="planning",
    )
    assert picked.effort == "high"


def test_a_roster_without_classes_keeps_the_old_ladder(tmp_path: Path) -> None:
    text = "\n".join(_ROSTER.splitlines()[4:]) + "\n"
    picked = picker.pick_execution(
        _roster_at(tmp_path, text),
        0,
        requested_provider="route",
        execution_class="mechanical_fanout",
        requested_effort="max",
    )
    assert picked.effort == "max"


def test_a_class_policy_outside_the_ladder_is_refused_at_read(tmp_path: Path) -> None:
    from nspawn.owner_roster import RosterError

    path = tmp_path / "bad.toml"
    path.write_text('[classes]\nplanning = { effort = "turbo" }\n')
    with pytest.raises(RosterError, match="turbo"):
        Roster.read(path)


# ---------------------------------------------------------------------------
# Strength ceiling: the class decides which providers are ELIGIBLE at all.
# ---------------------------------------------------------------------------

#: Adversarial on purpose: NEITHER provider declares `thrift`, so the
#: `mechanical_fanout` ranking (by thrift, then by name) picks `claude` unless
#: the strength cap makes it ineligible. A roster where `glm` already won by
#: thrift would pass without the filter and measure nothing.
_STRENGTH_ROSTER = (
    "[classes]\n"
    'deterministic = { effort = "low", ceiling = "low", max_strength = 7 }\n'
    'mechanical_fanout = { effort = "low", ceiling = "medium", max_strength = 7 }\n'
    'unclassified = { effort = "low", ceiling = "medium", max_strength = 7 }\n'
    'review = { effort = "medium", ceiling = "high" }\n'
    'design = { effort = "medium", ceiling = "high" }\n'
    'judgment = { effort = "high", ceiling = "ultra" }\n'
    "\n"
    '[bins]\ncodex = "/never-execute/vendor"\n'
    '[[provider]]\nname = "claude"\nkind = "codex"\n'
    'secrets = "/never-read/secrets"\n'
    'model = "m"\nmodels = ["m"]\nstrength = 9\ndefault = true\n'
    'efforts = ["low", "medium", "high", "ultra"]\neffort = "high"\n'
    '[[provider]]\nname = "glm"\nkind = "codex"\n'
    'secrets = "/never-read/secrets"\n'
    'model = "m"\nmodels = ["m"]\nstrength = 7\n'
    'efforts = ["low", "medium", "high", "ultra"]\neffort = "high"\n'
)


def _answering(
    value: object = "mechanical_fanout",
    confidence: float | None = 1.0,
    *,
    error: Exception | None = None,
    answers: bool = True,
) -> Callable[..., dict[str, system1.Answer]]:
    """A double of `system1.ask` — the module wrapper the contract pins."""

    def ask(
        state: object,
        questions: dict[str, object],
        model: object = None,
        timeout: float | None = None,
        scope: str | None = None,
    ) -> dict[str, system1.Answer]:
        if error is not None:
            raise error
        if not answers:
            return {}
        ident = next(iter(questions))
        return {
            ident: system1.Answer(
                id=ident,
                type="choice",
                value=value,
                probabilities={str(value): 1.0},
                confidence=confidence,
                legend=None,
            )
        }

    return ask


@pytest.fixture(autouse=True)
def _system1(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test in this module reaches the live classifier.

    With `NOTT_SYSTEM1_API_KEY` in this host's environment, every dispatch would
    otherwise bill a real call and read a vendor answer as a product verdict.
    """
    monkeypatch.setattr(system1, "ask", _answering())


def _recorder(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Records what reached the picker, then refuses so no dispatch escapes."""
    seen: dict[str, Any] = {}
    real = picker.pick_execution

    def spy(*args: Any, **kwargs: Any) -> object:
        seen["kwargs"] = kwargs
        # A refusal the picker itself raises (a pin above the cap) belongs to
        # the caller, not to this double.
        seen["picked"] = real(*args, **kwargs)
        raise picker.PickerError("RECORDED")

    monkeypatch.setattr(picker, "pick_execution", spy)
    return seen


def test_a_provider_above_the_class_strength_cap_is_not_routed_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "mechanical_fanout"},
        provider=None,
    )
    assert seen["picked"].executed_provider.name == "glm"


def test_strength_equal_to_the_cap_is_eligible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`glm` sits exactly ON the cap: the comparison is `<=`, never `<`."""
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "mechanical_fanout"},
        provider=None,
    )
    assert seen["picked"].executed_provider.strength == 7


def test_a_pinned_provider_above_the_cap_is_refused_never_rerouted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "mechanical_fanout"},
        provider="claude",
    )
    assert error["code"] == -32602
    message = error["message"]
    assert "mechanical_fanout" in message
    assert "7" in message and "9" in message and "claude" in message


def test_a_class_without_a_cap_still_reaches_the_strong_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive control, not a RED: the cheap cap must not govern every class."""
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "judgment"},
        provider=None,
        observed="judgment",
    )
    assert seen["picked"].executed_provider.name == "claude"


def test_the_observed_class_is_what_reaches_the_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declared `judgment`, observed `mechanical_fanout`: the observation binds."""
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "judgment"},
        provider=None,
        observed="mechanical_fanout",
    )
    # Checking the provider alone does not kill "pass the DECLARED class on":
    # the class is what the mutant changes.
    assert seen["kwargs"]["execution_class"] == "mechanical_fanout"
    assert seen["picked"].executed_provider.name == "glm"


def test_a_costlier_observation_never_raises_the_declared_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "mechanical_fanout"},
        provider=None,
        observed="judgment",
    )
    assert seen["kwargs"]["execution_class"] == "mechanical_fanout"


def test_a_tie_in_cost_keeps_the_declared_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`review` and `design` cost the same; the System1 was not asked to pick an axis."""
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "review"},
        provider=None,
        observed="design",
    )
    assert seen["kwargs"]["execution_class"] == "review"


def test_a_pin_above_the_cap_of_the_OBSERVED_class_carries_both_classes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin refuses, and the refusal says why the ceiling moved."""
    error = _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _STRENGTH_ROSTER),
        {"class": "judgment"},
        provider="claude",
        observed="mechanical_fanout",
    )
    message = error["message"]
    assert "mechanical_fanout" in message and "judgment" in message
    assert "7" in message


_SYSTEM1_FAILURES = [
    pytest.param({"error": system1.System1Unavailable("down")}, id="unavailable"),
    pytest.param({"answers": False}, id="no-answer"),
    pytest.param({"value": 0.5}, id="value-off-vocabulary"),
    pytest.param({"value": "nenhuma"}, id="none-of-them"),
    pytest.param({"confidence": 0.3}, id="unsure"),
]


@pytest.mark.parametrize("shape", _SYSTEM1_FAILURES)
def test_every_failure_of_the_system1_falls_closed_to_unclassified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: dict[str, Any]
) -> None:
    roster = _roster_at(tmp_path, _STRENGTH_ROSTER)
    monkeypatch.setattr(system1, "ask", _answering(**shape))
    effective, warning, _ = classify.resolve_execution_class("judgment", "p", roster)
    assert effective == "unclassified"
    assert warning is not None and "judgment" in warning
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        roster,
        {"class": "judgment"},
        provider=None,
        ask=_answering(**shape),
    )
    assert seen["kwargs"]["execution_class"] == "unclassified"
    assert seen["picked"].executed_provider.name == "glm"


@pytest.mark.parametrize(
    ("pins", "provider"),
    [
        pytest.param({"effort": "high"}, None, id="effort"),
        pytest.param({}, "route", id="provider"),
    ],
)
def test_a_pinned_dispatch_keeps_its_declared_class_when_system1_is_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pins: dict[str, str],
    provider: str | None,
) -> None:
    """Measured 2026-10-16: `class=planning, effort=high` was refused against
    the `unclassified` ceiling whenever System1 was down. A failed observation
    is no evidence against the caller's pin, so the declared class stands."""
    seen = _recorder(monkeypatch)
    _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path),
        {"class": "planning", **pins},
        provider=provider,
        ask=_answering(error=system1.System1Unavailable("down")),
    )
    assert seen["kwargs"]["execution_class"] == "planning"


def test_falling_closed_never_raises_a_cheaper_declared_class(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deterministic` tops out BELOW `unclassified`: the fall is a `min`."""
    monkeypatch.setattr(
        system1, "ask", _answering(error=system1.System1Unavailable("down"))
    )
    effective, warning, _ = classify.resolve_execution_class(
        "deterministic", "p", _roster_at(tmp_path, _STRENGTH_ROSTER)
    )
    assert effective == "deterministic"
    assert warning is not None


def test_the_class_warning_reaches_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _dispatch_to_row(
        tmp_path, monkeypatch, "judgment", observed="mechanical_fanout"
    )
    assert row.warning is not None
    assert "judgment" in row.warning and "mechanical_fanout" in row.warning


class _Admitted(Exception):
    """Sentinel: the row exists and nothing beyond it should run."""


def _dispatch_to_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution_class: str,
    *,
    observed: str = "mechanical_fanout",
) -> Any:
    """The variant that REACHES the row, only to read its warning.

    Without it, deleting the composition in `RunRow` survives every other test
    here: they all stop at the picker.
    """
    roster = _roster_at(
        tmp_path,
        _STRENGTH_ROSTER.replace('kind = "codex"', 'kind = "claude"').replace(
            "[bins]\ncodex =", "[bins]\nclaude ="
        ),
    )
    monkeypatch.setattr(system1, "ask", _answering(observed))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster.path))
    rows: list[Any] = []

    def admit_run(row: Any, *args: object, **kwargs: object) -> None:
        rows.append(row)
        raise _Admitted

    def create_run_directory(root: Path, run_id: str, *, dir_fd: int) -> Any:
        directory = tmp_path / run_id
        directory.mkdir()
        return supervisor.RunDirectory(
            root=directory,
            stdout=directory / "stdout",
            stderr=directory / "stderr",
        )

    monkeypatch.setattr(supervisor, "create_run_directory", create_run_directory)
    instance = object.__new__(supervisor.Supervisor)
    instance._lock = RLock()
    instance._run_root = tmp_path
    instance._registry = SimpleNamespace(
        get_provider_walls=lambda now: {},
        get_account_walls=lambda now: {},
        get_account_load=dict,
        get_account_usage=tuple,
        get_account_last_run=dict,
        instance_id="instance",
        admit_run=admit_run,
    )
    # READ-ONLY, and the reason is the contract and not convenience: a writable
    # run is a native write, which only a `codex` route serves, and these
    # routes are deliberately not codex. The warning path this measures is the
    # same in both modes — the class resolves, the picker decides, the row is
    # born — so read-only reaches it without pretending to be a backend it is
    # not.
    with pytest.raises(_Admitted):
        instance.execute(
            "never execute",
            str(tmp_path),
            execution_class=execution_class,
            read_only=True,
        )
    return rows[0]


def test_a_wall_on_every_route_still_carries_the_class_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`AllProvidersWalled` is not a `PickerError`: without it the warning is lost.

    Declared `judgment`, observed `mechanical_fanout`, every route inside the
    cap cooling down — the caller must still learn it was running with the
    ceiling of the observed class.
    """
    roster = _roster_at(tmp_path, _STRENGTH_ROSTER)
    monkeypatch.setattr(system1, "ask", _answering("mechanical_fanout"))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster.path))
    monkeypatch.setattr(supervisor, "create_run_directory", _forbidden)
    walls = {"claude": 2**31, "glm": 2**31}
    instance = object.__new__(supervisor.Supervisor)
    instance._lock = RLock()
    instance._run_root = tmp_path
    instance._registry = SimpleNamespace(
        get_provider_walls=lambda now: walls,
        get_account_walls=lambda now: {},
        get_account_load=dict,
        get_account_usage=tuple,
        get_account_last_run=dict,
    )
    with pytest.raises(picker.AllProvidersWalled) as caught:
        instance.execute(
            "never execute", str(tmp_path), execution_class="judgment", read_only=True
        )
    message = str(caught.value)
    assert "cooling down" in message
    assert "judgment" in message and "mechanical_fanout" in message
    assert caught.value.nearest_reset == 2**31


def _forbidden(*args: object, **kwargs: object) -> None:
    pytest.fail("DISPATCH_ESCAPED")


#: `review` buys `medium` and declares no strength cap; the only route serves
#: `high` and up. The class refuses it through the effort ladder alone, which
#: is the case measured on SERVER on 2026-09-26: a review pinned to `fugu`
#: reached an `assert cap is not None` in the picker and the driver died with
#: an AssertionError instead of answering.
_LADDER_ROSTER = (
    "[classes]\n"
    'review = { effort = "medium", ceiling = "high" }\n'
    'unclassified = { effort = "medium", ceiling = "high" }\n'
    "\n"
    '[bins]\ncodex = "/never-execute/vendor"\n'
    '[[provider]]\nname = "high_only"\nkind = "codex"\n'
    'secrets = "/never-read/secrets"\n'
    'model = "m"\nmodels = ["m"]\nstrength = 9\ndefault = true\n'
    'efforts = ["high", "max"]\neffort = "high"\n'
)


@pytest.mark.parametrize("provider", ["high_only", None])
def test_a_route_below_no_rung_of_the_class_is_a_typed_refusal(
    tmp_path: Path, provider: str | None
) -> None:
    with pytest.raises(picker.PickerError) as refused:
        picker.pick_execution(
            _roster_at(tmp_path, _LADDER_ROSTER),
            0,
            requested_provider=provider,
            execution_class="review",
        )
    message = str(refused.value)
    assert "review" in message and "medium" in message, message


@pytest.mark.parametrize("provider", ["high_only", None])
def test_explicit_effort_within_review_ceiling_admits_a_high_only_route(
    tmp_path: Path, provider: str | None
) -> None:
    """Native review refused high because admission checked its medium default."""
    picked = picker.pick_execution(
        _roster_at(tmp_path, _LADDER_ROSTER),
        0,
        requested_provider=provider,
        requested_effort="high",
        execution_class="review",
    )
    assert picked.executed_provider.name == "high_only"
    assert picked.effort == "high"


@pytest.mark.parametrize("provider", ["high_only", None])
def test_explicit_effort_still_cannot_exceed_the_review_ceiling(
    tmp_path: Path, provider: str | None
) -> None:
    with pytest.raises(picker.PickerError, match='tops out at "high"'):
        picker.pick_execution(
            _roster_at(tmp_path, _LADDER_ROSTER),
            0,
            requested_provider=provider,
            requested_effort="max",
            execution_class="review",
        )


@pytest.mark.parametrize("provider", ["high_only", None])
def test_explicit_effort_preserves_the_review_strength_cap(
    tmp_path: Path, provider: str | None
) -> None:
    capped = _LADDER_ROSTER.replace(
        'review = { effort = "medium", ceiling = "high" }',
        'review = { effort = "medium", ceiling = "high", max_strength = 8 }',
    )
    with pytest.raises(picker.PickerError, match="strength at 8"):
        picker.pick_execution(
            _roster_at(tmp_path, capped),
            0,
            requested_provider=provider,
            requested_effort="high",
            execution_class="review",
        )


# ---------------------------------------------------------------------------
# A class-less run (a pinned, tool-free, read-only inference) has no class cap,
# so it runs only its route's fast lane.
# ---------------------------------------------------------------------------

_FAST_LANE_ROSTER = (
    "[classes]\n"
    'deterministic = { effort = "low", ceiling = "low", max_strength = 7, fast = true }\n'
    'unclassified = { effort = "low", ceiling = "medium", max_strength = 7 }\n'
    "\n"
    '[bins]\ncodex = "/never-execute/vendor"\n'
    '[[provider]]\nname = "route"\nkind = "codex"\n'
    'model = "big"\nmodel_fast = "small"\nmodels = ["big", "small"]\n'
    "strength = 9\ndefault = true\n"
    'efforts = ["high", "low", "max"]\neffort = "max"\n'
    '[provider.accounts.pinned]\nhome = "/never-read/pinned"\nmodel = "big"\n'
    # The route the class cap leaves to `deterministic`.
    '[[provider]]\nname = "cheap"\nkind = "codex"\nmodel = "m"\nmodels = ["m"]\n'
    'strength = 7\nefforts = ["low"]\neffort = "low"\n'
    '[provider.accounts.cheap-main]\nhome = "/never-read/cheap"\n'
)


@pytest.mark.parametrize(
    ("provider", "model", "effort", "admitted"),
    [
        # The gist's own call: the fast model at the lowest effort served.
        ("route", "small", "low", True),
        # An account that declares its own model runs only that model.
        ("pinned", "big", "low", True),
        ("route", "big", "low", False),
        ("route", "small", "max", False),
        ("pinned", "small", "low", False),
    ],
)
def test_a_class_less_run_takes_only_its_route_fast_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model: str,
    effort: str,
    admitted: bool,
) -> None:
    seen = _recorder(monkeypatch)
    error = _dispatch(
        tmp_path,
        monkeypatch,
        _roster_at(tmp_path, _FAST_LANE_ROSTER),
        {
            "model": model,
            "effort": effort,
            "inference_profile": {"tools": "none", "output_schema": {"type": "object"}},
        },
        provider=provider,
    )
    if admitted:
        assert seen["kwargs"]["requested_model"] == model, error
        assert seen["kwargs"]["execution_class"] is None
    else:
        assert "kwargs" not in seen, seen
        assert error["code"] == -32602
        assert error["data"]["nerr"] == 1129, error
        assert "fast lane" in error["message"], error

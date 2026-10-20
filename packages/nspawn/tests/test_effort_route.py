"""An effort the codex transport rewrites is a refused turn, never a slower one.

Measured on a real run (2026-09-17): `nspawn_review_start(provider="fugu",
model="fugu-ultra", effort="ultra")` died with `tokens: 0` and the error only in the state's
`warning` field — `reasoning.effort must be one of "high", "xhigh", "max",
got "medium"`. The request named `ultra`; the API saw `medium`.

The translation belongs to the external binary. Measured against `codex-cli 0.154.0` with a
local upstream capturing the request body, one name per run: `low`,
`medium`, `high`, `xhigh`, `max` and even a name the codex does NOT know travel
verbatim; only `ultra` leaves `turn/start` and reaches the wire as
`reasoning.effort: "medium"`, with the same `model` and nothing else different.

The host's roster declares `ultra` for `codex` and `fugu` and that is owner data.
The layer that can speak is this one: the route does not serve the effort, so the turn is
refused at dispatch time, naming what it does serve.
"""

from __future__ import annotations

from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from class_dispatch import (
    DECLARED_CLASS,
    system1_never_reaches_the_network,  # noqa: F401
)
from mcp_fakes import handle

from nspawn import mcp, picker, supervisor
from nspawn.owner_roster import Provider, Roster
from nspawn.rows import AccountUsageRow


def _roster_at(tmp_path: Path, kind: str = "codex") -> Roster:
    path = tmp_path / "effort.toml"
    path.write_text(
        f'[bins]\n{kind} = "/never-execute/vendor"\n'
        f'[[provider]]\nname = "route"\nkind = "{kind}"\n'
        'secrets = "/never-read/secrets"\n'
        'model = "m"\nmodels = ["m"]\nstrength = 9\ndefault = true\n'
        'efforts = ["high", "xhigh", "max", "ultra"]\neffort = "xhigh"\n'
    )
    return Roster.read(path)


def _dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    roster: Roster,
    effort: str,
    registry: SimpleNamespace | None = None,
) -> dict:
    """The ROUTE, not the helper: the same MCP verb that measured the defect.

    `create_run_directory` fails the test if it is ever reached, so a refusal
    that moved out of the picker into some later branch is still visible here.
    """
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster.path))
    instance = object.__new__(supervisor.Supervisor)
    instance._lock = RLock()
    instance._run_root = tmp_path
    instance._registry = registry or SimpleNamespace(
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
                    "provider": "route",
                    "effort": effort,
                    # This test's roster does not declare `[classes]`, so the
                    # class sets no ceiling: measured effort arrives intact at the
                    # route guard.
                    "class": DECLARED_CLASS,
                    # Read-only: the route is the same in every mode, and a
                    # writable dispatch that is not a native write is refused
                    # before the route is read.
                    "read_only": True,
                },
            },
        },
    )
    assert "error" in answer, answer
    return answer["error"]


def test_a_rewritten_effort_is_refused_on_the_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = _dispatch(tmp_path, monkeypatch, _roster_at(tmp_path), "ultra")
    assert error["code"] == -32602
    message = error["message"]
    assert "ultra" in message and "medium" in message
    # A recusa carrega o vocabulario vivo, como `unknown_provider`/`unknown_model`.
    for served in ("high", "xhigh", "max"):
        assert served in message, f"a recusa nao nomeia {served}"


def test_a_served_effort_is_not_refused_by_this_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid progress: `max` passes the picker and dies at the NEXT boundary.

    Without this, a refusal that denied every effort would pass the test above.
    """
    error = _dispatch(tmp_path, monkeypatch, _roster_at(tmp_path), "max")
    assert error["message"] == (
        "native credentials must belong to the selected account"
    )


def test_a_claude_route_still_carries_ultra() -> None:
    """The rewrite is the CODEX app-server's, so the guard is scoped to it."""
    provider = Provider(
        name="p", kind="claude", model="m", models=["m"], efforts=["max", "ultra"]
    )
    picked = picker.pick_execution(
        Roster(providers=[provider]),
        0,
        requested_provider="p",
        requested_effort="ultra",
    )
    assert picked.effort == "ultra"


def test_the_roster_default_effort_is_refused_too(tmp_path: Path) -> None:
    """A rewritten effort lies whoever chose it — caller or roster line."""
    path = tmp_path / "default-ultra.toml"
    path.write_text(
        '[bins]\ncodex = "/never-execute/vendor"\n'
        '[[provider]]\nname = "route"\nkind = "codex"\n'
        'secrets = "/never-read/secrets"\n'
        'model = "m"\nmodels = ["m"]\n'
        'efforts = ["max", "ultra"]\neffort = "ultra"\n'
    )
    with pytest.raises(picker.PickerError, match="ultra"):
        picker.pick_execution(Roster.read(path), 0, requested_provider="route")


def test_the_provider_listing_advertises_only_what_it_serves(tmp_path: Path) -> None:
    """`nspawn_provider_list` is where a caller LEARNS the ladder.

    Advertising `ultra` and then refusing it sends the caller to read the roster
    for an answer the report already had.
    """
    roster = _roster_at(tmp_path)
    listing = supervisor._provider_listing(roster.providers[0], roster)
    assert listing.efforts == ("high", "xhigh", "max")


def test_a_dispatch_spreads_by_the_registry_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor hands the picker the windows and last runs it read."""
    windows = (AccountUsageRow("route-b", "seven_day", 0.5, 2000, 1000),)
    seen: dict[str, object] = {}

    def spy(*args: object, **kwargs: object) -> None:
        seen.update(kwargs)
        raise picker.PickerError("stopped after the pick")

    monkeypatch.setattr(supervisor.picker, "pick_execution", spy)
    registry = SimpleNamespace(
        get_provider_walls=lambda now: {},
        get_account_walls=lambda now: {},
        get_account_load=dict,
        get_account_usage=lambda: windows,
        get_account_last_run=lambda: {"route-a": 1000},
    )
    error = _dispatch(tmp_path, monkeypatch, _roster_at(tmp_path), "max", registry)
    assert error["message"] == "stopped after the pick"
    assert seen["account_use"] == picker.AccountUse(windows, {"route-a": 1000})

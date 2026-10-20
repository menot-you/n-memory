"""Native kinds are selectable, but legacy credentials never reach a child."""

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
from nspawn.owner_roster import Roster


def roster_at(tmp_path: Path, kind: str) -> Roster:
    path = tmp_path / "admission.toml"
    path.write_text(
        f'[bins]\n{kind} = "/never-execute/vendor"\n'
        f'[[provider]]\nname = "route"\nkind = "{kind}"\n'
        'secrets = "/never-read/secrets"\n'
        'model = "m"\nmodels = ["m"]\nstrength = 9\ndefault = true\n'
    )
    return Roster.read(path)


@pytest.mark.parametrize("kind", ["codex", "agy"])
@pytest.mark.parametrize("selection", ["pin", "default", "rank", "fallback"])
def test_direct_kind_selected(tmp_path: Path, kind: str, selection: str) -> None:
    roster = roster_at(tmp_path, kind)
    kwargs = {}
    if selection == "pin":
        kwargs["requested_provider"] = "route"
    if selection == "rank":
        kwargs["execution_class"] = "judgment"
    if selection == "fallback":
        from nspawn.owner_roster import Provider

        roster.providers.insert(0, Provider(name="walled", default=True))
        kwargs["provider_walls"] = {"walled": 100}
    picked = picker.pick_execution(roster, 0, **kwargs)
    assert picked.executed_provider.name == "route"
    assert picked.executed_provider.kind == kind
    assert picked.model == "m"


def test_claude_kind_still_passes(tmp_path: Path) -> None:
    picked = picker.pick_execution(roster_at(tmp_path, "claude"), 0)
    assert (picked.executed_provider.name, picked.model) == ("route", "m")


@pytest.mark.parametrize("kind", ["codex", "agy"])
def test_direct_kind_reaches_mcp_as_invalid_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    roster = roster_at(tmp_path, kind)
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
    # The probe is read-only (below): a writable dispatch that is not a native
    # write is refused before credentials are read, so only a read-only
    # dispatch reaches the credential law on both kinds.

    def forbidden(*args, **kwargs):
        pytest.fail("ADMISSION_ESCAPED")

    # The credential law sits behind the AGY admission refusal, which this test
    # does not measure (`test_agy_admission.py` does).
    monkeypatch.setattr(supervisor, "AGY_LAUNCH_REFUSAL", None)
    monkeypatch.setattr(supervisor, "create_run_directory", forbidden)
    monkeypatch.setattr(supervisor.claude_child, "resolve_claude_binary", forbidden)
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
                    # A route probe, and provider is pinned: the class
                    # satisfies the mandatory argument without altering the choice.
                    "class": DECLARED_CLASS,
                    # A reader: a writer needs `native_write`, and `agy` has
                    # no native write mode, so it would be refused before
                    # the credential boundary this test measures.
                    "read_only": True,
                },
            },
        },
    )
    assert answer["error"]["code"] == -32602
    assert (
        answer["error"]["message"]
        == "native credentials must belong to the selected account"
    )

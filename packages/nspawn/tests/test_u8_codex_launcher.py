"""U8 seam: select the vendor kind before any Claude-specific precondition."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from class_dispatch import (
    DECLARED_CLASS,
    system1_never_reaches_the_network,  # noqa: F401
)
from mcp_fakes import FakeSupervisor, handle

from nspawn import child, picker, supervisor
from nspawn.mcp import INTERNAL_ERROR, INVALID_PARAMS, McpServer
from nspawn.owner_roster import Roster
from nspawn.supervisor import RunAccepted, Supervisor, create_run_directory


def dispatch_kind(tmp_path, monkeypatch, kind, *, plugin=False, read_only=True):
    root = tmp_path / "plugin"
    path = tmp_path / "roster.toml"
    path.write_text(
        (f"[defaults]\nplugins=[{json.dumps(str(root))}]\n" if plugin else "")
        + f'[bins]\n{kind}="/never-execute/vendor"\n'
        + f'[[provider]]\nname="route"\nkind="{kind}"\n'
        'model="m"\nmodels=["m"]\n'
        'secrets="/never-read/secrets"\n'
    )
    roster = Roster.read(path)
    # The replaced environ drops the conftest fence; name this test's roster as the
    # single config so the operator's ~/.nott/spawn.toml never decides this dispatch.
    monkeypatch.setattr(
        child.os,
        "environ",
        {
            "HOME": str(tmp_path),
            "PATH": "/bin",
            "NOTT_NSPAWN_CONFIG": str(path),
        },
    )
    monkeypatch.setattr(FakeSupervisor, "execute", Supervisor.execute)
    fake = FakeSupervisor()
    fake._lock = threading.Condition()
    fake._registry = SimpleNamespace(inference_profile=lambda run_id: None)
    seen = []

    def forbidden(*args, **kwargs):
        pytest.fail("refused dispatch reached child construction or a live process")

    monkeypatch.setattr(child.subprocess, "Popen", forbidden)
    for name in ("build_child_env", "compose_child_argv", "spawn_child"):
        monkeypatch.setattr(child, name, forbidden)

    def resolve(**kwargs):
        seen.append("resolve")
        assert kwargs["provider_binary"] == Path("/never-execute/vendor")
        return kwargs["provider_binary"]

    def inspect_installation(binary, probe):
        seen.append("installation")
        return SimpleNamespace(binary=binary)

    def inspect_mcp(plugin_root):
        seen.append("mcp")
        assert plugin_root == root
        raise child.ChildArgvError("claude inspection sentinel")

    monkeypatch.setattr(supervisor.claude_child, "resolve_claude_binary", resolve)
    monkeypatch.setattr(
        supervisor.claude_child, "inspect_claude_installation", inspect_installation
    )
    monkeypatch.setattr(
        supervisor.claude_child, "inspect_child_mcp_config", inspect_mcp
    )

    def admit(prompt, cwd, **kwargs):
        picked = picker.pick_execution(roster, 0, requested_provider="route")
        fake._spawn_and_wait(
            "u8-run",
            cwd,
            create_run_directory(
                tmp_path,
                "u8-run",
                dir_fd=os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY),
            ),
            prompt,
            roster,
            picked,
            "u8-thread",
            False,
            kwargs["read_only"],
        )
        return RunAccepted("u8-run")

    monkeypatch.setattr(fake, "_admit", admit)
    answer = handle(
        McpServer(supervisor=fake),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "nspawn_run_start",
                "arguments": {
                    "prompt": "Reply pong",
                    "cwd": str(tmp_path),
                    "provider": "route",
                    "read_only": read_only,
                    # The U8 seam measures ORDER of kind preconditions; class
                    # does not enter it, only satisfies mandatory argument.
                    "class": DECLARED_CLASS,
                },
            },
        },
    )
    assert answer is not None
    return answer, seen


@pytest.mark.parametrize("plugin", [False, True])
@pytest.mark.parametrize("read_only", [False, True])
def test_codex_gets_its_named_refusal_before_claude_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin: bool, read_only: bool
) -> None:
    answer, seen = dispatch_kind(
        tmp_path, monkeypatch, "codex", plugin=plugin, read_only=read_only
    )
    message = answer["error"]["message"]
    if read_only:
        assert answer["error"]["code"] == INVALID_PARAMS, answer
        assert (
            "Claude worker cannot execute provider 'route' with kind 'codex'" in message
        )
    else:
        # A writable dispatch that is not a native write never reaches a kind
        # precondition: it is refused first, naming the one writable mode.
        assert answer["error"]["data"]["type"] == "InvalidRunRequest", answer
        assert "native_write" in message
    assert "plugin_root" not in message
    assert seen == []


@pytest.mark.parametrize("plugin", [False, True])
def test_agy_stays_refused_before_claude_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, plugin: bool
) -> None:
    answer, seen = dispatch_kind(tmp_path, monkeypatch, "agy", plugin=plugin)
    assert answer["error"]["code"] == INVALID_PARAMS, answer
    assert (
        "Claude worker cannot execute provider 'route' with kind 'agy'"
        in answer["error"]["message"]
    )
    assert seen == []


def test_claude_still_requires_its_plugin_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, seen = dispatch_kind(tmp_path, monkeypatch, "claude")
    assert answer["error"]["code"] == INTERNAL_ERROR
    assert "declare [defaults] plugins" in answer["error"]["message"]
    assert seen == []


def test_claude_retains_its_installation_and_mcp_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, seen = dispatch_kind(tmp_path, monkeypatch, "claude", plugin=True)
    assert answer["error"]["code"] == INVALID_PARAMS
    assert "claude inspection sentinel" in answer["error"]["message"]
    assert seen == ["resolve", "installation", "mcp"]

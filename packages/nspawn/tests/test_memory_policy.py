"""Keep managed memory capture separate from stored-memory insertion."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from nspawn import child, claude_child, native, picker, supervisor
from nspawn.owner_roster import Defaults, NativeAccount, Provider, Roster
from nspawn.rows import Backend, RunPhase

POLICY = "NOTT_MEMORY_POLICY"


@pytest.fixture
def plugin(tmp_path: Path) -> Path:
    root = tmp_path / "plugin"
    root.mkdir()
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "nmemory": {
                        "command": "/fixture/nmemory",
                        "args": ["mcp", "serve"],
                    },
                    "nmemory-http": {
                        "type": "http",
                        "url": "http://fixture.invalid/mcp",
                    },
                    "nspawn": {"command": "/fixture/nspawn"},
                }
            }
        )
    )
    return root


@pytest.fixture
def codex(tmp_path: Path) -> Provider:
    binary = tmp_path / "codex-fixture"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    home = tmp_path / "account"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text(json.dumps({"tokens": {"access_token": "fixture-access-token"}}))
    auth.chmod(0o600)
    return Provider(
        name="fixture",
        kind="codex",
        bin=binary,
        model="fixture-model",
        accounts={"fixture": NativeAccount(home)},
    )


@pytest.mark.parametrize("policy", [None, "push"])
@pytest.mark.parametrize("read_only", [False, True])
def test_codex_private_projection(
    codex: Provider, plugin: Path, tmp_path: Path, policy: str | None, read_only: bool
) -> None:
    ambient = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "FOREIGN_SETTING": "fixture",
    }
    if policy is not None:
        ambient[POLICY] = policy
    launch = native.prepare_native(
        codex,
        "fixture",
        roster=Roster(defaults=Defaults(plugin_root=plugin)),
        cwd=tmp_path,
        read_only=read_only,
        ambient=ambient,
    )
    assert launch.env.get(POLICY) == policy
    assert "FOREIGN_SETTING" not in launch.env
    assert "nmemory-http" not in launch.mcp_servers
    if policy == "push":
        assert POLICY in launch.mcp_servers["nspawn"]["env_vars"]
        if read_only:
            assert "nmemory" not in launch.mcp_servers
            projected = native.readonly_mcp_servers(
                launch.mcp_servers,
                [
                    {
                        "name": "nmemory",
                        "transport": {"command": "/fixture/nmemory"},
                    },
                    {
                        "name": "nmemory-http",
                        "transport": {"url": "http://fixture.invalid/mcp"},
                    },
                ],
                launch.needles,
            )
            assert projected["nmemory"]["enabled"] is False
            assert projected["nmemory-http"]["enabled"] is False
        else:
            assert POLICY in launch.mcp_servers["nmemory"]["env_vars"]
    else:
        assert "nmemory" in launch.mcp_servers


@pytest.mark.parametrize("override", ["", "pull", "push"])
@pytest.mark.parametrize("policy", [None, "push"])
def test_native_roster_cannot_select_the_memory_policy(
    codex: Provider, tmp_path: Path, policy: str | None, override: str
) -> None:
    codex.env[POLICY] = override
    ambient = {"HOME": str(tmp_path)}
    if policy is not None:
        ambient[POLICY] = policy
    launch = native.prepare_native(
        codex,
        "fixture",
        roster=Roster(),
        cwd=tmp_path,
        read_only=False,
        ambient=ambient,
    )
    assert launch.env.get(POLICY) == policy


@pytest.mark.parametrize("layer", ["provider", "defaults", "account"])
@pytest.mark.parametrize("policy", [None, "push"])
def test_claude_roster_cannot_select_the_memory_policy(
    tmp_path: Path, layer: str, policy: str | None
) -> None:
    token = tmp_path / "fixture.token"
    token.write_text("fixture-access-token")
    provider = Provider(name="fixture", token_file=token)
    defaults: dict[str, str] = {}
    if layer == "account":
        provider.accounts["fixture"] = NativeAccount(None, env={POLICY: ""})
    elif layer == "defaults":
        defaults[POLICY] = "pull"
    else:
        provider.env[POLICY] = "pull"
    ambient = {"HOME": str(tmp_path)}
    if policy is not None:
        ambient[POLICY] = policy
    env, _ = child.build_child_env(
        provider, "fixture", defaults_env=defaults, ambient=ambient
    )
    assert env.get(POLICY) == policy


@pytest.mark.parametrize("policy", ["", "pull", "Push"])
def test_native_refuses_unknown_memory_policy(
    codex: Provider, tmp_path: Path, policy: str
) -> None:
    with pytest.raises(native.NativePreparationError, match=POLICY):
        native.prepare_native(
            codex,
            "fixture",
            roster=Roster(),
            cwd=tmp_path,
            read_only=False,
            ambient={POLICY: policy},
        )


@pytest.mark.parametrize("policy", ["", "pull", "Push"])
def test_claude_refuses_unknown_policy_before_credential_access(
    tmp_path: Path, policy: str
) -> None:
    provider = Provider(name="fixture", token_file=tmp_path / "absent-token")
    with pytest.raises(child.ChildEnvironmentError, match=POLICY):
        child.build_child_env(provider, None, ambient={POLICY: policy})


@pytest.mark.parametrize("policy", [None, "push"])
def test_orchestrator_launch_preserves_the_parent_policy(
    tmp_path: Path, policy: str | None
) -> None:
    prepared = {"CODEX_HOME": str(tmp_path), "FOREIGN_SETTING": "fixture"}
    if policy is not None:
        prepared[POLICY] = policy
    env = native.orchestrator_launch_environment(
        prepared, disposable_root=str(tmp_path)
    )
    assert env.get(POLICY) == policy
    assert "FOREIGN_SETTING" not in env


@pytest.mark.parametrize("policy", [None, "push"])
@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("transport", ["stdio", "http"])
@pytest.mark.parametrize("caged", [False, True])
def test_claude_private_projection(
    plugin: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy: str | None,
    read_only: bool,
    transport: str,
    caged: bool,
) -> None:
    if transport == "http":
        document = json.loads((plugin / ".mcp.json").read_text())
        document["mcpServers"]["nmemory"] = {
            "type": "http",
            "url": "http://fixture.invalid/mcp",
        }
        (plugin / ".mcp.json").write_text(json.dumps(document))
    binary = tmp_path / "claude-fixture"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('2.1.299 (fixture)')\n"
        "    raise SystemExit(0)\n"
        "sys.stdin.read()\n"
        "roots = [Path(sys.argv[i + 1]) for i, arg in enumerate(sys.argv) if arg == '--plugin-dir']\n"
        "views = [json.loads((root / '.mcp.json').read_text()) for root in roots]\n"
        "Path('projection.json').write_text(json.dumps({'views': views, 'managed': os.environ.get('NOTT_MEMORY_POLICY') == 'push'}))\n"
        "print(json.dumps({'type': 'result', 'is_error': False, 'terminal_reason': 'completed', 'result': 'PONG'}))\n"
    )
    binary.chmod(0o700)
    token = tmp_path / "fixture.token"
    token.write_text("fixture-access-token")
    provider = Provider(name="fixture", bin=binary, token_file=token)
    roster = Roster(
        defaults=Defaults(plugin_root=plugin, plugins=[str(plugin)]),
        providers=[provider],
    )
    ambient = {"HOME": str(tmp_path), "PATH": os.defpath, "TMPDIR": str(tmp_path)}
    if policy is not None:
        ambient[POLICY] = policy
    monkeypatch.setattr(os, "environ", ambient)
    owner = SimpleNamespace(
        _lock=threading.RLock(),
        _registry=SimpleNamespace(
            inference_profile=lambda _: None,
            note_record_observed=lambda *_, **__: None,
        ),
        _children={},
        _launching=lambda _: contextlib.nullcontext(),
        _update_phase=lambda *_, **__: None,
        _spent_provider_wall=lambda *_, **__: None,
    )
    if caged:
        owner._registry.session_profile = lambda _: SimpleNamespace(
            config_dir=str(tmp_path)
        )
        monkeypatch.setattr(
            supervisor.claude_restricted,
            "compose_restricted_launch",
            lambda **_: supervisor.claude_restricted.RestrictedLaunch(
                (str(binary),),
                {"HOME": str(tmp_path), "PATH": os.defpath},
                tmp_path / "fixture-settings",
                "fixture-digest",
                tmp_path,
                None,
            ),
        )
        monkeypatch.setattr(
            supervisor.claude_restricted, "verify_pinned_settings", lambda *_: None
        )
    run = tmp_path / "run"
    run.mkdir()

    def launch() -> RunPhase:
        return supervisor.Supervisor._spawn_and_wait(
            owner,
            "fixture-run",
            str(tmp_path),
            supervisor.RunDirectory(run, run / "stdout", run / "stderr"),
            "Read the fixture.",
            roster,
            picker.PickerResult(None, provider, None, "fixture-model", None),
            "fixture-session",
            False,
            read_only,
            deadline_ms=5000,
            writer=SimpleNamespace(command=lambda argv: argv, environment=dict)
            if caged
            else None,
            scratch=tmp_path if caged else None,
        ).phase

    if policy == "push" and not read_only and transport == "http" and not caged:
        with pytest.raises(claude_child.ChildMcpConfigError, match="stdio"):
            launch()
        assert not (tmp_path / "projection.json").exists()
        return
    result = launch()
    assert result is RunPhase.COMPLETED
    observed = json.loads((tmp_path / "projection.json").read_text())
    assert observed["managed"] is (policy == "push")
    if caged:
        assert observed["views"] == []
        return
    assert len(observed["views"]) == 1
    servers = observed["views"][0]["mcpServers"]
    if policy == "push":
        assert "nmemory-http" not in servers
        if read_only:
            assert servers == {}
        else:
            assert servers["nmemory"]["env"][POLICY] == "${NOTT_MEMORY_POLICY}"
            assert servers["nspawn"]["env"][POLICY] == "${NOTT_MEMORY_POLICY}"
    elif read_only:
        assert set(servers) == {"nmemory"}
        assert "--read-only" in servers["nmemory"]["args"]
    else:
        assert "nmemory-http" in servers
    assert POLICY not in (plugin / ".mcp.json").read_text()


@pytest.mark.parametrize("policy", [None, "push"])
def test_native_writer_tool_environment_preserves_the_policy(
    codex: Provider, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str | None
) -> None:
    ambient = {"HOME": str(tmp_path), "FOREIGN_SETTING": "fixture"}
    if policy is not None:
        ambient[POLICY] = policy
    launch = native.prepare_native(
        codex,
        "fixture",
        roster=Roster(),
        cwd=tmp_path,
        read_only=False,
        ambient=ambient,
    )
    observed: dict[str, str] = {}

    class ProjectionObserved(BaseException):
        pass

    def run(*_: object, **kwargs: object) -> None:
        observed.update(kwargs["tool_environment"])
        raise ProjectionObserved

    session = SimpleNamespace(child=SimpleNamespace(identity=None), run=run)
    monkeypatch.setattr(
        supervisor.app_server.AppServerTurn,
        "start",
        lambda *_, **__: contextlib.nullcontext(session),
    )
    owner = SimpleNamespace(
        _lock=threading.RLock(),
        _children={},
        _writers={},
        _write_baselines={},
        _launching=lambda _: contextlib.nullcontext(),
        _open_writer=lambda _: object(),
        _close_writer=lambda *_: None,
        _native_readonly_mcp=lambda *_, **__: ({}, {}),
        _update_phase=lambda *_, **__: None,
    )
    row = SimpleNamespace(
        run_id="fixture-run",
        sandbox="workspace-write",
        backend=Backend.CODEX,
        worktree=str(tmp_path),
        thread_id=None,
    )
    run_root = tmp_path / "run"
    run_root.mkdir()
    with pytest.raises(ProjectionObserved):
        supervisor.Supervisor._drive_native_run(
            owner,
            row,
            supervisor.RunDirectory(run_root, run_root / "stdout", run_root / "stderr"),
            "Read the fixture.",
            picker.PickerResult(None, codex, "fixture", "fixture-model", None),
            launch,
            0,
            {},
            native_write=True,
        )
    assert observed.get(POLICY) == policy
    assert "FOREIGN_SETTING" not in observed

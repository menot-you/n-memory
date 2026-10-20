"""The owning curator setter preserves foreign configuration and native identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
from nspawn import config as spawn_config
from nspawn.mcp import McpServer
from nspawn.owner_roster import Roster
from nspawn.registry import Registry, create_store, store_path

from nmemory import config
from nmemory.cli import main
from nmemory.curator import CuratorClient, CuratorError
from nmemory.provider import has_provider, provider_env, provider_name


@pytest.fixture
def curator_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Only declared test accounts are used; no vendor executable is launched."""
    native_home = tmp_path / "codex-main"
    native_home.mkdir(mode=0o700)
    roster = tmp_path / "spawn.toml"
    roster.write_text(
        "[bins]\ncodex = '/bin/false'\n"
        "[[provider]]\nname = 'codex'\nkind = 'codex'\n"
        "model = 'gpt-6-astra'\neffort = 'low'\nstrength = 9\n"
        "[provider.accounts.codex-main]\n"
        f"home = {json.dumps(str(native_home))}\n"
    )
    roster.chmod(0o600)
    path = tmp_path / "memory.toml"
    path.write_text(
        "# The owning mirror keeps this comment and formatting.\n"
        "[github]\nrepo = 'menot-you/pynott'  # unchanged\n"
        "label = 'memory'\n\n"
        "[system1]\nendpoint = 'http://127.0.0.1:1'\n"
        "model = 'local-classifier'\ntimeout_seconds = 3\n"
    )
    path.chmod(0o600)
    monkeypatch.setenv("NOTT_NMEMORY_CONFIG", str(path))
    monkeypatch.setenv("NOTT_NSPAWN_CONFIG", str(roster))
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR_PROVIDER", "zai")
    return path


def test_curator_set_pins_native_account_and_reset_keeps_foreign_bytes(
    curator_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = curator_config.read_bytes()
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 0
    )
    response = json.loads(capsys.readouterr().out)
    assert response == {"provider": "codex", "account": "codex-main"}
    current = curator_config.read_bytes()
    assert current.startswith(original)
    assert tomllib.loads(current.decode())["curator"] == {
        "provider": "codex",
        "account": "codex-main",
    }
    assert provider_name() == "codex", "the file route wins over the old gateway env"
    assert curator_config.stat().st_uid == os.getuid()
    assert curator_config.stat().st_mode & 0o777 == 0o600
    assert main(["curator", "set", "--reset"]) == 0
    assert json.loads(capsys.readouterr().out) == {"provider": None, "account": None}
    assert curator_config.read_bytes() == original


@pytest.mark.parametrize("compatibility", ["missing-alias", "other-home"])
def test_curator_preflight_uses_the_same_owning_roster_as_execution(
    curator_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    compatibility: str,
) -> None:
    primary = curator_config.parent / "spawn.toml"
    home = curator_config.parent / "codex-main"
    credential = home / "auth.json"
    credential.write_text("fixture-presence-only")
    credential.chmod(0o600)
    legacy = curator_config.parent / "legacy-spawn.toml"
    declared = primary.read_text()
    if compatibility == "missing-alias":
        declared = declared.replace("accounts.codex-main", "accounts.other")
    else:
        foreign_home = curator_config.parent / "other-native-home"
        foreign_home.mkdir(mode=0o700)
        declared = declared.replace(str(home), str(foreign_home))
    legacy.write_text(declared)
    legacy.chmod(0o600)
    monkeypatch.delenv("NOTT_NSPAWN_CONFIG")
    monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(legacy))
    monkeypatch.setattr(spawn_config, "DEFAULT_STORE_CONFIG_PATH", primary)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")

    assert Roster.default_path() == primary
    executed = Roster.read(primary).select("codex-main")
    assert executed.resolve_native_account("codex-main").home == home
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 0
    )
    assert json.loads(capsys.readouterr().out) == {
        "provider": "codex",
        "account": "codex-main",
    }
    assert CuratorClient().is_configured(), "preflight must inspect the owning home"
    assert credential.read_text() == "fixture-presence-only"
    assert not store_path().exists(), "selection and availability start no run"


@pytest.mark.parametrize("account", [None, "absent", "codex"])
def test_curator_set_refuses_unbound_codex_before_writing(
    curator_config: Path, capsys: pytest.CaptureFixture[str], account: str | None
) -> None:
    original = curator_config.read_bytes()
    argv = ["curator", "set", "--provider", "codex"]
    if account is not None:
        argv.extend(["--account", account])
    assert main(argv) == 2
    assert "account" in capsys.readouterr().err
    assert curator_config.read_bytes() == original


@pytest.mark.parametrize("mode", [0o622, 0o660, 0o666])
def test_curator_set_cannot_replace_a_writable_foreign_configuration(
    curator_config: Path, capsys: pytest.CaptureFixture[str], mode: int
) -> None:
    original = curator_config.read_bytes()
    curator_config.chmod(mode)
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 2
    )
    assert "owner-controlled" in capsys.readouterr().err
    assert curator_config.read_bytes() == original


def test_curator_set_refuses_symlink_without_touching_its_target(
    curator_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = curator_config.with_name("foreign.toml")
    curator_config.rename(target)
    original = target.read_bytes()
    curator_config.symlink_to(target)
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 2
    )
    assert "safely" in capsys.readouterr().err
    assert curator_config.is_symlink()
    assert target.read_bytes() == original


def test_curator_set_refuses_invalid_existing_table_without_leaking_value(
    curator_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    curator_config.write_text("[system1]\napi_key = 'private-test-value'\n")
    original = curator_config.read_bytes()
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 2
    )
    error = capsys.readouterr().err
    assert "api_key" in error
    assert "private-test-value" not in error
    assert curator_config.read_bytes() == original


def test_curator_set_detects_a_real_concurrent_file_replacement(
    curator_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original = curator_config.read_bytes()
    foreign = original.replace(b"label = 'memory'", b"label = 'other-writer'")
    stage = config.tempfile.mkstemp

    def concurrent_stage(*args: object, **kwargs: object) -> tuple[int, str]:
        descriptor, name = stage(*args, **kwargs)
        curator_config.write_bytes(foreign)
        return descriptor, name

    monkeypatch.setattr(config.tempfile, "mkstemp", concurrent_stage)
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 2
    )
    assert "changed before replacement" in capsys.readouterr().err
    assert curator_config.read_bytes() == foreign


def test_native_curator_checks_only_original_account_presence_and_closes_gateway_overlay(
    curator_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 0
    )
    assert not has_provider(), "a missing native auth source cannot fall back to Z.ai"
    home = curator_config.parent / "codex-main"
    credential = home / "auth.json"
    credential.write_text("fixture-presence-only")
    credential.chmod(0o600)
    assert has_provider()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "foreign-fixture-key")
    monkeypatch.setenv("ANTHROPIC_MODEL", "wrong-model")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    env = provider_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_MODEL" not in env
    assert "CLAUDE_CODE_EFFORT_LEVEL" not in env
    assert not any(name in env for name in ("CODEX_HOME", "OPENAI_API_KEY"))
    credential.chmod(0o644)
    assert not has_provider()


def test_curator_set_is_an_actual_cli_without_a_store_or_native_launch(
    curator_config: Path,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "nmemory.cli",
            "curator",
            "set",
            "--provider",
            "codex",
            "--account",
            "codex-main",
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"provider": "codex", "account": "codex-main"}
    assert result.stderr == ""
    assert (
        tomllib.loads(curator_config.read_text())["curator"]["account"] == "codex-main"
    )


def test_curator_native_request_keeps_account_schema_and_owning_prelaunch_refusal(
    curator_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        main(["curator", "set", "--provider", "codex", "--account", "codex-main"]) == 0
    )
    credential = curator_config.parent / "codex-main" / "auth.json"
    credential.write_text("fixture-presence-only")
    credential.chmod(0o600)
    monkeypatch.setenv("NOTT_NMEMORY_CURATOR", "1")
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(curator_config.parent / "runs"))
    monkeypatch.setenv("ANTHROPIC_MODEL", "foreign-gateway-model")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "max")
    create_store(store_path())
    calls: list[tuple[str, dict[str, Any]]] = []
    call = McpServer.call_tool

    def observe(
        owner: McpServer, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        calls.append((name, arguments))
        return call(owner, name, arguments)

    monkeypatch.setattr(McpServer, "call_tool", observe)
    client = CuratorClient(timeout_s=10)
    assert client.is_configured()
    with pytest.raises(
        CuratorError, match="native OAuth credential structure is invalid"
    ):
        client.judge("Judge whether the supplied argument holds.")
    [(name, request)] = [(n, a) for n, a in calls if n == "nspawn_run_start"]
    assert name == "nspawn_run_start"
    assert request["provider"] == "codex-main"
    assert request["class"] == "judgment"
    assert request["read_only"] is True
    assert request["inference_profile"]["tools"] == "none"
    assert "model" not in request and "effort" not in request
    assert [n for n, _ in calls] == ["nspawn_run_start", "nspawn_run_close"]
    with Registry(store_path(), "curator-native-refusal-reader").connect() as registry:
        assert registry.list_runs() == ()

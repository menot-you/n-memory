"""Keep live judges private without ambient executable or manager fallback."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from live import test_driver_survival as survival
from live import test_operation_admission as admission


def test_operation_target_copies_only_the_synced_checkout_entry_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    source = checkout / ".venv" / "bin" / "nspawn"
    source.parent.mkdir(parents=True)
    source.write_text("#!/bin/false\n")
    source.chmod(0o700)
    monkeypatch.setattr(admission, "MEMBER_ROOT", checkout / "mcps" / "nspawn")
    monkeypatch.delenv("NOTT_NSPAWN_OPERATION_TEST_BINARY", raising=False)
    private = tmp_path / "private"
    private.mkdir()
    target = admission._operation_target(private)
    assert target == private / "candidate" / "nspawn"
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mode == source.stat().st_mode
    assert not target.samefile(source)


def test_operation_target_requires_root_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(admission, "MEMBER_ROOT", tmp_path / "mcps" / "nspawn")
    monkeypatch.delenv("NOTT_NSPAWN_OPERATION_TEST_BINARY", raising=False)
    with pytest.raises(
        pytest.fail.Exception, match="run uv sync at the repository root"
    ):
        admission._operation_target(tmp_path)


@pytest.mark.parametrize("problem", ["empty", "relative", "missing", "not_executable"])
def test_operation_target_rejects_invalid_override_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    target = tmp_path / "nspawn"
    if problem == "not_executable":
        target.write_text("#!/bin/false\n")
        target.chmod(0o600)
    selected = {"empty": "", "relative": "nspawn"}.get(problem, str(target))
    monkeypatch.setenv("NOTT_NSPAWN_OPERATION_TEST_BINARY", selected)
    with pytest.raises(pytest.fail.Exception, match="invalid; no source fallback"):
        admission._operation_target(tmp_path)
    assert not (tmp_path / "candidate").exists()


def test_operation_target_keeps_an_explicit_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nspawn"
    target.write_text("#!/bin/false\n")
    target.chmod(0o700)
    monkeypatch.setenv("NOTT_NSPAWN_OPERATION_TEST_BINARY", str(target))
    assert admission._operation_target(tmp_path) == target
    assert not (tmp_path / "candidate").exists()


def test_scope_launch_and_cleanup_use_only_the_public_user_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = survival._manager_environment()
    assert environment == {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.geteuid()}/bus"
    }
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/unavailable-private-manager")
    observed: dict[str, Any] = {}

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(survival.subprocess, "run", run)
    monkeypatch.setattr(survival, "SYSTEMCTL", "/bin/true")
    answer = survival._systemctl("stop", "nspawn-test-private.scope")
    assert answer is not None and answer.returncode == 0
    assert "XDG_RUNTIME_DIR" not in observed["env"]
    assert (
        observed["env"]["DBUS_SESSION_BUS_ADDRESS"]
        == environment["DBUS_SESSION_BUS_ADDRESS"]
    )

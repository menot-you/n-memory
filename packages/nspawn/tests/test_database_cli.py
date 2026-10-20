"""`database snapshot`: the copy the backup timers take of the registry every day."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from nspawn.cli import main


@pytest.mark.parametrize(("flags", "mode"), [([], 0o600), (["--group-read"], 0o640)])
def test_a_snapshot_is_private_unless_group_read_is_asked(
    tmp_path: Path, flags: list[str], mode: int
) -> None:
    assert main(["database", "create"]) == 0
    dest = tmp_path / "snapshot.sqlite"
    assert main(["database", "snapshot", str(dest), *flags]) == 0
    assert dest.stat().st_mode & 0o777 == mode


def test_a_snapshot_refuses_an_unknown_flag(tmp_path: Path) -> None:
    # The store exists, so the refusal can only come from the flag.
    assert main(["database", "create"]) == 0
    dest = tmp_path / "snapshot.sqlite"
    assert main(["database", "snapshot", str(dest), "--group"]) == 2
    assert not dest.exists()


@pytest.mark.parametrize("flag", ["--help", "--unknown"])
def test_a_snapshot_refuses_a_flag_as_its_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    assert main(["database", "create"]) == 0
    monkeypatch.chdir(tmp_path)
    code = main(["database", "snapshot", flag])
    assert not (tmp_path / flag).exists(), "an option must never create a snapshot"
    assert code == 2


@pytest.mark.parametrize("flag", ["--help", "--unknown"])
def test_credential_persist_refuses_a_flag_as_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{}")))
    code = main(["credential", "persist", flag])
    assert list(tmp_path.iterdir()) == [], "an option must never write credentials"
    assert code == 2


@pytest.mark.parametrize("command", [["database", "create"], ["learning", "export"]])
def test_database_directory_refusal_is_a_cli_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.setenv("NOTT_DATABASE_DIR", "relative")
    assert main(command) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err
    assert "not an absolute path" in captured.err


def test_missing_registry_is_a_cli_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["learning", "export"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err
    assert "registry" in captured.err

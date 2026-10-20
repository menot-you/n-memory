"""`nmemory database snapshot`: the copy the backup timer takes of the store every hour.

nMEMORY's snapshot has no `--group-read`: its backup directory belongs to `nott`
alone, so the copy is always private and any flag is refused.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nmemory import cli


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["nmemory", *argv])
    try:
        cli.main()
    except SystemExit as exit_:
        return int(exit_.code or 0)
    return 0


def test_a_snapshot_is_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(monkeypatch, "database", "create") == 0
    dest = tmp_path / "snapshot.sqlite3"
    assert _run(monkeypatch, "database", "snapshot", str(dest)) == 0
    assert dest.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("flag", ["--group-read", "--group"])
def test_a_snapshot_refuses_an_unknown_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    assert _run(monkeypatch, "database", "create") == 0
    dest = tmp_path / "snapshot.sqlite3"
    assert _run(monkeypatch, "database", "snapshot", str(dest), flag) == 2
    assert not dest.exists()

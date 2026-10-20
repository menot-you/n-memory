"""A failed child start must remove its own private control directory."""

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

from nspawn import session
from nspawn.custody import AggregateScope
from nspawn.redact import SecretNeedles


def test_child_start_failure_keeps_its_error_and_removes_door_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scope = AggregateScope(memory_bytes=64 * 1024**2, tasks=64)
    roots: list[Path] = []
    original = session.tempfile.mkdtemp

    def directory(*args: Any, **kwargs: Any) -> str:
        path = original(*args, **kwargs)
        roots.append(Path(path))
        return path

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("test child start failure")

    monkeypatch.setattr(session.tempfile, "mkdtemp", directory)
    monkeypatch.setattr(session.child, "spawn_child", fail)
    try:
        with pytest.raises(OSError, match="test child start failure"):
            session.NativeSession(
                argv=(sys.executable, "-c", "pass"),
                environment={"PATH": "/usr/bin:/bin"},
                cwd=tmp_path,
                run_dir=tmp_path,
                needles=SecretNeedles(),
                observe=lambda *args: None,
                scope=scope,
            )
        assert len(roots) == 1
        assert not roots[0].exists()
    finally:
        scope.close()
        for path in roots:
            if path.exists():
                shutil.rmtree(path)

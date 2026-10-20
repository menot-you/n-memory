"""Missing listeners permit a fenced CLI. Foreign listeners never permit it."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from workspace_root import workspace_path


@pytest.fixture
def hook() -> Any:
    spec = importlib.util.spec_from_file_location(
        "memory_recall", workspace_path("plugin/hooks/nmemory-recall.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_absent_listener_uses_cli_without_a_key(
    hook: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []
    monkeypatch.setenv("NOTT_NMEMORY_PROJECT", "sample")
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ID", raising=False)
    monkeypatch.delenv("NOTT_NSPAWN_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(hook, "listener_state", lambda port: None)
    monkeypatch.setattr(
        hook.sys, "stdin", io.StringIO(json.dumps({"prompt": "sample recall"}))
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the key must not be read")

    def cli(terms: list[str], project: str) -> list[str]:
        seen.append(project)
        return ["cap-1 [user-stated]: sample rule"]

    monkeypatch.setattr(hook, "bearer", forbidden)
    monkeypatch.setattr(hook, "cli_recall", cli)
    assert "sample rule" in hook.main()
    assert seen == ["sample"]


def test_foreign_listener_blocks_both_paths(
    hook: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOTT_NSPAWN_RUN_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr(hook, "listener_state", lambda port: False)
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO('{"prompt":"sample recall"}'))

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("foreign listeners must stop recall")

    monkeypatch.setattr(hook, "cli_recall", forbidden)
    monkeypatch.setattr(hook, "bearer", forbidden)
    assert hook.main() == ""


def test_listener_tables_distinguish_absence_from_foreign(
    hook: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = tmp_path / "tcp"
    original = open

    def mapped(path: str, *args: Any, **kwargs: Any) -> Any:
        return original(
            table if path in ("/proc/net/tcp", "/proc/net/tcp6") else path,
            *args,
            **kwargs,
        )

    monkeypatch.setattr("builtins.open", mapped)
    table.write_text("header\n")
    assert hook.listener_state(16699) is None
    table.write_text(
        f"header\n 0: 0100007F:413B 00000000:0000 0A 0 0 0 {os.getuid() + 1} 0 0\n"
    )
    assert hook.listener_state(16699) is False


def test_key_with_a_public_mode_is_refused(
    hook: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NOTT_NMEMORY_API_KEY", raising=False)
    key = tmp_path / ".nott/secrets/memory.key"
    key.parent.mkdir(parents=True)
    key.write_text("fixture-only")
    key.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        hook.bearer(environment=False)

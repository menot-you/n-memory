"""The live matrix and its wrapper never touch the operator's install or store."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import nott_database
import pytest
from workspace_root import WORKSPACE_ROOT

LIVE = Path(__file__).resolve().parent
REPO_ROOT = WORKSPACE_ROOT


def _probe() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "matrix_probe", LIVE / "matrix_probe.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The probe's dataclasses resolve their module through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tree(root: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry under root with its kind, size and modification time."""
    entries = {}
    for path in [root, *root.rglob("*")]:
        st = path.lstat()
        kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "file"
        entries[str(path.relative_to(root))] = (kind, st.st_size, st.st_mtime_ns)
    return entries


def _no_child(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the probe started a child process")


@pytest.mark.parametrize("value", [None, "live"])
def test_probe_refuses_unisolated_store_before_any_child(
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = _probe()
    if value is None:
        monkeypatch.delenv("NOTT_DATABASE_DIR", raising=False)
    else:
        monkeypatch.setenv("NOTT_DATABASE_DIR", str(nott_database.live_directory()))
    for name in ("Popen", "run", "check_output"):
        monkeypatch.setattr(probe.subprocess, name, _no_child)
    code = probe.main(["--binary", sys.executable, "--repo-root", str(REPO_ROOT)])
    assert code == 2
    assert "NOTT_DATABASE_DIR" in capsys.readouterr().err


def _git_repo(root: Path) -> Path:
    root.mkdir()
    (root / "README.md").write_text("# fixture\n")
    git = ["git", "-C", str(root), "-c", "user.email=t@t.invalid", "-c", "user.name=T"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    return root


def test_read_rows_start_read_only_and_the_write_row_writes_in_a_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe()
    sent: list[dict[str, Any]] = []
    clones: list[bool] = []

    class Client:
        def close(self) -> None:
            pass

    def call_payload(_client: Any, name: str, arguments: dict[str, Any]) -> Any:
        assert name == "nspawn_run_start"
        sent.append(arguments)
        clones.append((Path(arguments["cwd"]) / ".git").is_dir())
        raise probe.ObservationRefused("stop before dispatch")

    write_root = tmp_path / "writes"
    write_root.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_MATRIX_WRITE_ROOT", str(write_root))
    monkeypatch.setattr(probe, "_session", lambda *_args: Client())
    monkeypatch.setattr(probe, "_call_payload", call_payload)
    binary = Path(sys.executable)
    source = _git_repo(tmp_path / "source")
    probe.ld2(binary, tmp_path, {}, REPO_ROOT)
    probe.ld3(binary, tmp_path, {}, source)
    probe.ld4(binary, tmp_path, {}, REPO_ROOT)
    assert [arguments.get("read_only") for arguments in sent] == [True, False, True]
    assert [arguments.get("native_write") for arguments in sent] == [None, True, None]
    assert Path(sent[1]["cwd"]).is_relative_to(write_root)
    assert clones[1]


def test_the_write_row_without_a_write_root_is_blocked_before_any_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe()
    monkeypatch.delenv("NOTT_NSPAWN_MATRIX_WRITE_ROOT", raising=False)
    monkeypatch.setattr(probe, "_session", _no_child)
    row = probe.ld3(Path(sys.executable), tmp_path, {}, REPO_ROOT)
    assert (row.status, row.detail) == (probe.BLOCKED, probe.LD3_NEEDS_WRITE_ROOT)
    assert "NOTT_NSPAWN_MATRIX_WRITE_ROOT" in row.detail


def test_the_write_row_with_a_missing_write_root_is_blocked_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe()
    missing = tmp_path / "absent"
    monkeypatch.setenv("NOTT_NSPAWN_MATRIX_WRITE_ROOT", str(missing))
    monkeypatch.setattr(probe, "_session", _no_child)
    row = probe.ld3(Path(sys.executable), tmp_path, {}, REPO_ROOT)
    assert row.status == probe.BLOCKED
    assert str(missing) in row.detail


def test_the_write_row_reports_a_failed_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe()
    write_root = tmp_path / "writes"
    write_root.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_MATRIX_WRITE_ROOT", str(write_root))

    def lock_and_answer(*args: Any) -> Any:
        # The clone's parent can no longer be emptied, so cleanup fails.
        (Path(args[3]).parent).chmod(0o500)
        return probe.Row("LD-3", args[4], probe.GREEN, "said OK")

    monkeypatch.setattr(probe, "_ld3_in", lock_and_answer)
    try:
        row = probe.ld3(Path(sys.executable), tmp_path, {}, _git_repo(tmp_path / "s"))
    finally:
        for held in write_root.iterdir():
            held.chmod(0o700)
    assert row.status == probe.GREEN
    assert "cleanup" in row.detail


def test_the_write_row_clone_has_a_timeout_and_a_timeout_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = _probe()
    write_root = tmp_path / "writes"
    write_root.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_MATRIX_WRITE_ROOT", str(write_root))
    timeouts: list[float | None] = []

    def run(command: list[str], **kwargs: Any) -> Any:
        timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout") or 0)

    monkeypatch.setattr(probe.subprocess, "run", run)
    monkeypatch.setattr(probe, "_session", _no_child)
    row = probe.ld3(Path(sys.executable), tmp_path, {}, REPO_ROOT)
    assert timeouts and timeouts[0] is not None
    assert row.status == probe.BLOCKED
    assert "timed out" in row.detail


@pytest.mark.parametrize(
    "answer",
    [
        {"error": {"code": -32602, "message": "native_write is refused: no root"}},
        {
            "result": {
                "isError": True,
                "content": [
                    {"type": "text", "text": "native_write is refused: no root"}
                ],
            }
        },
    ],
)
def test_a_refused_call_keeps_its_refusal_text(answer: dict[str, Any]) -> None:
    probe = _probe()

    class Client:
        def call_tool(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return answer

    with pytest.raises(probe.ObservationRefused, match="native_write is refused"):
        probe._call_payload(Client(), "nspawn_run_start", {})


def test_a_non_error_answer_that_is_not_json_never_reaches_the_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a refusal carries its text out; other tool output stays inside."""
    probe = _probe()
    marker = "MARKER-tool-output-7f3a"

    class Client:
        def call_tool(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"result": {"content": [{"type": "text", "text": marker}]}}

        def close(self) -> None:
            pass

    monkeypatch.setattr(probe, "_session", lambda *_args: Client())
    row = probe.ld2(Path(sys.executable), tmp_path, {}, REPO_ROOT)
    assert row.status == probe.BLOCKED
    assert marker not in row.detail
    assert "tool answer is not a JSON object" in row.detail


def test_the_probe_starts_the_binary_as_its_mcp_server(tmp_path: Path) -> None:
    """The one executable serves MCP only under `mcp serve`; bare, it exits 2."""
    probe = _probe()
    fake = tmp_path / "nspawn"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "if sys.argv[1:] != ['mcp', 'serve']:\n"
        "    sys.exit(2)\n"
        "frame = json.loads(sys.stdin.readline())\n"
        "result = {'protocolVersion': '2024-11-05', 'capabilities': {},\n"
        "          'serverInfo': {'name': 'fake', 'version': '1'}}\n"
        "print(json.dumps({'jsonrpc': '2.0', 'id': frame['id'], 'result': result}),\n"
        "      flush=True)\n"
        "sys.stdin.readline()\n"
    )
    fake.chmod(0o700)
    client = probe._session(fake, tmp_path, {})
    client.close()


def test_wrapper_writes_nothing_under_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".nott" / "bin").mkdir(parents=True)
    before = _tree(home)
    retired = {"NOTT_DATABASE_DIR", "NOTT_NSPAWN_CONFIG", "NOTT_NSPAWN_ROSTER"}
    environment = {k: v for k, v in os.environ.items() if k not in retired}
    environment.update(
        HOME=str(home), UV_CACHE_DIR=str(tmp_path / "uv-cache"), UV_OFFLINE="1"
    )
    # An explicit binary: the wrapper syncs no venv, so the suite never
    # rewrites the shared one.
    completed = subprocess.run(
        ["bash", str(LIVE / "run_matrix.sh"), sys.executable],
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert _tree(home) == before
    assert completed.returncode == 2, completed.stderr
    assert "NOTT_DATABASE_DIR" in completed.stderr
    assert "uv sync" not in completed.stderr

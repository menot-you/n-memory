"""The detector receives only recorded coordinates, not launch environments."""

from pathlib import Path

import pytest
from registry_reset import store

from nspawn import cli
from nspawn.mcp import NSPAWN_TOOL_BY_NAME, _status_show
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow, SessionProfile
from nspawn.supervisor import Supervisor


@pytest.mark.parametrize("native_write", [False, True])
def test_monitor_status_coordinates(tmp_path: Path, native_write: bool) -> None:
    with Registry(store(), "monitor-status") as registry:
        registry.open(now=1000)
        registry.insert_run(
            RunRow(
                run_id="monitor-run",
                worktree=str(tmp_path),
                lane="builder",
                phase=RunPhase.RUNNING,
                sandbox="workspace-write",
                model="model-a",
                owner_instance="monitor-status",
                created_at=1000,
                updated_at=1000,
                backend=Backend.CLAUDE,
            )
        )
        registry.append_run_event(
            "monitor-run",
            at=1000,
            kind="lifecycle",
            payload={
                "writer_scope": {
                    "unit": "nottcontroller-abc.slice",
                    "cgroup": "/sys/fs/cgroup/example",
                }
            },
        )
        registry.append_run_event(
            "monitor-run",
            at=1000,
            kind="lifecycle",
            payload={
                "journal_context": {
                    "home": str(tmp_path / "claude"),
                    "node": "not-exported",
                }
            },
        )
        registry.append_run_event(
            "monitor-run",
            at=1000,
            kind="lifecycle",
            payload={
                "session_profile": SessionProfile(
                    original_run_id="monitor-run",
                    native_write=native_write,
                    read_only=not native_write,
                    read_paths=(),
                ).data()
            },
        )
        supervisor = Supervisor(registry, tmp_path / "runs")
        try:
            answer = _status_show(supervisor, "monitor-run")
            assert answer["native_write"] is native_write
            assert answer["observation"] == {
                "cwd": str(tmp_path),
                "writer_scope": {
                    "unit": "nottcontroller-abc.slice",
                    "cgroup": "/sys/fs/cgroup/example",
                },
                "transcript_home": str(tmp_path / "claude"),
            }
        finally:
            supervisor.shutdown()


def test_monitor_observation_description_names_nullable_fields() -> None:
    description = NSPAWN_TOOL_BY_NAME["nspawn_run_show"].description
    assert "individual fields are null when unrecorded" in description
    assert "or null for unrecorded coordinates" not in description


def test_monitor_unknown_run_cli_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    with Registry(store(), "monitor-status") as registry:
        registry.open(now=1000)
    assert cli.main(["run", "show", "missing"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert (
        captured.err
        == "nspawn run show refused: tools/call refused: no run named 'missing'\n"
    )

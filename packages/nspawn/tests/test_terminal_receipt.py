"""First-terminal and Stop/close ordering through two real store connections."""

import json
from dataclasses import replace
from pathlib import Path

import nott_database
import pytest
from registry_reset import store

from nspawn import cli, mcp
from nspawn.registry import ClosedRun, Registry, RegistryError, RunKey
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor

NODE = "node:11111111-1111-4111-8111-111111111111"
FINGERPRINT = "a" * 64


def _command(capsys: pytest.CaptureFixture[str], arguments: list[str]) -> dict:
    assert cli.main(arguments) == 0
    return json.loads(capsys.readouterr().out)


def _bind(capsys: pytest.CaptureFixture[str], project: str, key: str = "phase") -> None:
    _command(
        capsys,
        [
            "admission",
            "bind",
            "--group",
            "workflow",
            "--node",
            NODE,
            "--project",
            project,
            "--decision",
            "decision",
            "--generation",
            "0",
            "--key",
            key,
            "--fingerprint",
            FINGERPRINT,
        ],
    )


def _admit(
    registry: Registry, project: str, run_id: str = "run", key: str = "phase"
) -> RunRow:
    row = RunRow(
        run_id=run_id,
        worktree=f"{project}/{run_id}",
        phase=RunPhase.PENDING,
        sandbox="read-only",
        model="fixture-model",
        owner_instance=registry.instance_id,
        created_at=1,
        updated_at=1,
        backend=Backend.CLAUDE,
        group_name="workflow",
    )
    registry.admit_run(
        row,
        True,
        2,
        1000,
        run_key=RunKey(f"launch-{run_id}", project, key, FINGERPRINT, "b" * 64),
    )
    running = replace(row, phase=RunPhase.RUNNING, updated_at=3)
    registry.update_run(running)
    return running


def _show(supervisor: Supervisor, run_id: str = "run") -> dict:
    answer = mcp.McpServer(supervisor=supervisor).call_tool(
        "nspawn_run_show", {"run_id": run_id}
    )
    payload = json.loads(answer["content"][0]["text"])
    assert "terminal_receipt" in payload, (
        "run_show omits the observed first-terminal receipt"
    )
    assert "admission" in payload, "run_show omits the observed admission binding"
    return payload


@pytest.mark.parametrize(
    "phase,reason", [(RunPhase.COMPLETED, None), (RunPhase.FAILED, "fixture failure")]
)
def test_terminal_then_close_preserves_first_receipt_and_pruning_keeps_it(
    tmp_path: Path,
    phase: RunPhase,
    reason: str | None,
) -> None:
    with Registry(store(), "worker") as worker, Registry(store(), "closer") as closer:
        worker.open(1)
        closer.open(1)
        row = _admit(worker, str(tmp_path))
        supervisor = Supervisor(closer, tmp_path / "runs")
        try:
            worker.update_run(
                replace(row, phase=phase, blocked_reason=reason, updated_at=5)
            )
            receipt = {"phase": phase.value, "reason": reason, "at": 5}
            assert _show(supervisor)["terminal_receipt"] == receipt
            outcome = closer.close_owned_outcome("run", None, 6)
            assert outcome.run_closed
            # Failure already releases its ordinary claim in update_run.
            assert outcome.claim_released is (phase is RunPhase.COMPLETED)
            assert nott_database.query(closer.connection, "SELECT * FROM claims") == []
            assert _show(supervisor)["terminal_receipt"] == receipt
            assert (
                closer.prune_closed_runs(now=100_000_000, retain_for_ms=0, max_closed=0)
                == 1
            )
            rows = nott_database.query(
                closer.connection,
                "SELECT phase, reason, at FROM terminal_receipts WHERE run_id = 'run'",
            )
            assert [tuple(record) for record in rows] == [(phase.value, reason, 5)]
        finally:
            supervisor.shutdown()


def test_close_then_completion_keeps_closed_receipt_and_reports_late_write(
    tmp_path: Path,
) -> None:
    with Registry(store(), "worker") as worker, Registry(store(), "closer") as closer:
        worker.open(1)
        closer.open(1)
        row = _admit(worker, str(tmp_path))
        (tmp_path / "runs" / "run").mkdir(parents=True)
        supervisor = Supervisor(worker, tmp_path / "runs")
        try:
            assert closer.close_owned_outcome("run", None, 5).claim_released
            assert _show(supervisor)["terminal_receipt"] == {
                "phase": "closed",
                "reason": "closed while running",
                "at": 5,
            }
            with pytest.raises(ClosedRun):
                worker.update_run(replace(row, phase=RunPhase.COMPLETED, updated_at=6))
            assert not supervisor._update_phase(
                "run", RunPhase.COMPLETED, final_text="late"
            )
            assert (
                "completed" in (tmp_path / "runs" / "run" / "late_writes").read_text()
            )
            assert _show(supervisor)["terminal_receipt"]["phase"] == "closed"
        finally:
            supervisor.shutdown()


def test_orphan_is_provisional_and_old_terminal_rows_read_unknown(
    tmp_path: Path,
) -> None:
    with Registry(store(), "worker") as registry:
        registry.open(1)
        row = _admit(registry, str(tmp_path))
        supervisor = Supervisor(registry, tmp_path / "runs")
        try:
            registry.update_run(replace(row, phase=RunPhase.ORPHANED, updated_at=4))
            assert _show(supervisor)["terminal_receipt"]["phase"] == "orphaned"
            registry.update_run(replace(row, phase=RunPhase.COMPLETED, updated_at=5))
            assert _show(supervisor)["terminal_receipt"] == {
                "phase": "completed",
                "reason": None,
                "at": 5,
            }
            # Insert is the import/pre-v6 boundary: no transition was observed.
            registry.insert_run(
                replace(row, run_id="old", phase=RunPhase.COMPLETED, group_name=None)
            )
            assert _show(supervisor, "old")["terminal_receipt"] is None
            assert _show(supervisor, "old")["admission"] is None
        finally:
            supervisor.shutdown()


@pytest.mark.parametrize("annotate_first", [True, False])
def test_pre_v6_terminal_stays_unknown_after_annotation_and_close(
    tmp_path: Path,
    annotate_first: bool,
) -> None:
    with Registry(store(), "observer") as registry:
        registry.open(1)
        old = RunRow(
            run_id="old",
            worktree=str(tmp_path),
            phase=RunPhase.COMPLETED,
            sandbox="read-only",
            model="fixture",
            owner_instance="observer",
            created_at=1,
            updated_at=2,
            backend=Backend.CLAUDE,
        )
        registry.insert_run(old)
        supervisor = Supervisor(registry, tmp_path / "runs")
        try:
            assert _show(supervisor, "old")["terminal_receipt"] is None
            if annotate_first:
                registry.update_run(
                    replace(old, updated_at=3, warning="later observation"),
                    annotate=True,
                )
                assert _show(supervisor, "old")["terminal_receipt"] is None
            assert registry.close_owned_outcome("old", None, 4).run_closed
            assert _show(supervisor, "old")["terminal_receipt"] is None
        finally:
            supervisor.shutdown()


@pytest.mark.parametrize("completion_first", [True, False])
def test_stop_ack_then_sweep_observes_whichever_terminal_commit_won(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    completion_first: bool,
) -> None:
    path = store()
    project = str(tmp_path)
    _bind(capsys, project)
    _bind(capsys, project, "successor")
    with Registry(path, "worker") as worker, Registry(path, "sweeper") as sweeper:
        worker.open(1)
        sweeper.open(1)
        row = _admit(worker, project)
        stopped = _command(
            capsys, ["admission", "stop", "--node", NODE, "--generation", "1"]
        )
        assert stopped["groups"] == ["workflow"]
        supervisor = Supervisor(sweeper, tmp_path / "runs")
        monkeypatch.setattr(mcp, "_default_supervisor", lambda: supervisor)
        if completion_first:
            worker.update_run(replace(row, phase=RunPhase.COMPLETED, updated_at=5))
        closed = _command(capsys, ["run", "close", "--group", "workflow"])
        assert closed["closed"] == ([] if completion_first else ["run"])
        with Registry(path, "observer") as observer:
            observer.open(1)
            reader = Supervisor(observer, tmp_path / "reader")
            try:
                assert _show(reader)["terminal_receipt"]["phase"] == (
                    "completed" if completion_first else "closed"
                )
                if not completion_first:
                    with pytest.raises(ClosedRun):
                        worker.update_run(
                            replace(row, phase=RunPhase.COMPLETED, updated_at=6)
                        )
                with pytest.raises(RegistryError, match="admission held"):
                    _admit(worker, project, "successor", "successor")
                assert _show(reader)["admission"] == {
                    "group": "workflow",
                    "node": NODE,
                    "releasedGeneration": 0,
                    "nodeGeneration": 1,
                }
            finally:
                reader.shutdown()


def test_released_sweep_closes_nothing_and_unbound_group_refuses(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = store()
    project = str(tmp_path)
    _bind(capsys, project)
    with Registry(path, "worker") as worker:
        worker.open(1)
        _admit(worker, project)
    for group, expected_exit in (("workflow", 0), ("unbound", 2)):
        with Registry(path, "sweeper") as sweeper:
            sweeper.open(1)
            supervisor = Supervisor(sweeper, tmp_path / f"runs-{group}")
            monkeypatch.setattr(
                mcp, "_default_supervisor", lambda owned=supervisor: owned
            )
            assert cli.main(["run", "close", "--group", group]) == expected_exit
            captured = capsys.readouterr()
            if expected_exit == 0:
                assert json.loads(captured.out) == {
                    "group": group,
                    "status": "released",
                    "closed": [],
                }
            else:
                assert "unbound" in captured.err
    with Registry(path, "observer") as observer:
        assert observer.get_run("run").phase is RunPhase.RUNNING


def test_database_rollback_refuses_live_reserved_run_and_retains_rows_on_reupgrade(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = store()
    project = str(tmp_path)
    _bind(capsys, project)
    with Registry(path, "worker") as worker:
        worker.open(1)
        row = _admit(worker, project)
        assert cli.main(["database", "rollback"]) == 2
        assert "non-terminal" in capsys.readouterr().err
        assert nott_database.user_version(worker.connection) == 6
        worker.update_run(replace(row, phase=RunPhase.COMPLETED, updated_at=5))
        before = {
            table: [
                tuple(r)
                for r in nott_database.query(
                    worker.connection, f"SELECT * FROM {table}"
                )
            ]
            for table in ("admission_bindings", "admission_keys", "terminal_receipts")
        }
        assert _command(capsys, ["database", "rollback"]) == {"version": 5}
        assert nott_database.user_version(worker.connection) == 5
        assert worker.upgrade() == 6
        after = {
            table: [
                tuple(r)
                for r in nott_database.query(
                    worker.connection, f"SELECT * FROM {table}"
                )
            ]
            for table in before
        }
        assert before == after

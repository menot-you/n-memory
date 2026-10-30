"""Operator reads answer as the owning native stdio tools do (`result show` reads the
store itself), against disposable state."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import nott_database
import pytest
from registry_reset import store

from nspawn import cli
from nspawn.client import NspawnClientError
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow, SessionProfile
from nspawn.supervisor import FleetRun


@pytest.fixture
def fleet_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    root = tmp_path / "runs"
    directory = root / "cli-run"
    directory.mkdir(parents=True)
    directory.chmod(0o700)
    (directory / "stdout").write_text('{"type":"assistant","text":"fixture"}\n')
    (directory / "stderr").write_text("fixture stderr\n")
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        registry.insert_run(
            RunRow(
                run_id="cli-run",
                worktree=str(tmp_path),
                phase=RunPhase.COMPLETED,
                sandbox="read-only",
                model="fixture-model",
                owner_instance="fixture-owner",
                created_at=1,
                updated_at=2,
                backend=Backend.CLAUDE,
                final_text="fixture result",
                executed_provider="fixture",
            )
        )
    return "cli-run"


@pytest.mark.parametrize(
    "command",
    [
        ["run", "list"],
        ["run", "show", "cli-run"],
        ["result", "show", "cli-run"],
        ["transcript", "show", "cli-run"],
        ["quota", "show"],
        ["provider", "list"],
    ],
)
def test_fleet_reads_print_what_their_tools_show(
    fleet_run: str, command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(command) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    if command[:2] == ["run", "list"]:
        assert [row["run_id"] for row in payload["runs"]] == [fleet_run]
    elif command[:2] == ["run", "show"]:
        assert payload["run_id"] == fleet_run
        assert payload["phase"] == "completed"
    elif command[:2] == ["result", "show"]:
        assert payload["run_id"] == fleet_run
        assert payload["final_text"] == "fixture result"
        assert payload["stderr_tail"] == "fixture stderr\n"
    elif command[:2] == ["transcript", "show"]:
        assert payload["run_id"] == fleet_run
        assert len(payload["lines"]) == 1
        assert json.loads(payload["lines"][0])["text"] == "fixture"
    else:
        assert payload["providers"][0]["provider"] == "fixture"


@pytest.mark.parametrize("command", [["provider", "list"], ["quota", "show"]])
def test_provider_filter_reaches_the_owning_read_tool(
    fleet_run: str, command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([*command, "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["providers"][0]["provider"] == "fixture"
    assert cli.main([*command, "missing"]) == 2
    assert "missing" in capsys.readouterr().err


def test_provider_listing_does_not_need_a_registry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["provider", "list", "fixture"]) == 0
    assert json.loads(capsys.readouterr().out)["providers"][0]["provider"] == "fixture"


@pytest.mark.parametrize(
    "command",
    [
        ["run", "list", "--help"],
        ["run", "show", "--help"],
        ["result", "show"],
        ["transcript", "show", "cli-run", "--unknown"],
        ["quota", "show", "--refresh"],
        ["provider", "list", "fixture", "extra"],
    ],
)
def test_invalid_fleet_arguments_refuse_before_starting_a_peer(
    command: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("argument refusal must precede starting the MCP peer")

    monkeypatch.setattr(cli, "NspawnClient", forbidden, raising=False)
    assert cli.main(command) == 2


def test_unknown_run_is_a_cli_refusal(
    fleet_run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["run", "show", "missing"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err
    assert "missing" in captured.err


def test_unreachable_mcp_is_an_operational_cli_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreachable(*args: object, **kwargs: object) -> None:
        raise NspawnClientError("peer unavailable")

    monkeypatch.setattr(cli, "NspawnClient", unreachable)
    assert cli.main(["run", "list"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "failed" in captured.err
    assert "peer unavailable" in captured.err


def test_result_show_prints_an_answer_past_one_mcp_response_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stdio tool spills an answer whose JSON passes one response into a file of
    the run directory; the CLI reads the store itself, so its caller reads no file."""
    root = tmp_path / "runs"
    (root / "big-run").mkdir(parents=True)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    # Within the row's 16 KiB, and past 64 KiB once JSON escapes each control byte.
    answer = "\x01" * 16_000 + "é\r\n"
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        registry.insert_run(
            RunRow(
                run_id="big-run",
                worktree=str(tmp_path),
                phase=RunPhase.COMPLETED,
                sandbox="read-only",
                model="fixture-model",
                owner_instance="fixture-owner",
                created_at=1,
                updated_at=2,
                backend=Backend.CLAUDE,
                final_text=answer,
                executed_provider="fixture",
                owner="node:00000000-0000-4000-8000-000000000001",
            )
        )
    assert cli.main(["result", "show", "big-run"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["run_id"] == "big-run" and payload["final_text"] == answer
    # Readable, as every other read prints.
    assert out == json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    assert sorted(path.name for path in (root / "big-run").iterdir()) == []
    # A node's own nSPAWN shows no other node's run.
    monkeypatch.setenv(
        "NOTT_NSPAWN_NODE_SESSION", "00000000-0000-4000-8000-000000000002"
    )
    assert cli.main(["result", "show", "big-run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "big-run" in captured.err
    # A malformed grant opens no store, as the stdio tool's does not.
    monkeypatch.delenv("NOTT_NSPAWN_NODE_SESSION")
    monkeypatch.setenv("NOTT_NSPAWN_GRANT", "write")
    monkeypatch.setattr(cli, "Registry", lambda *_a, **_k: pytest.fail("store opened"))
    assert cli.main(["result", "show", "big-run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and "NOTT_NSPAWN_GRANT" in captured.err


def test_run_list_live_reads_the_complete_phase_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        RunRow(
            run_id=f"phase-{phase.value}",
            worktree=str(tmp_path),
            phase=phase,
            sandbox="read-only",
            model="fixture-model",
            owner_instance="fixture-owner",
            backend=Backend.CLAUDE,
            created_at=index + 1,
            updated_at=index + 2,
            lane="fixture-lane",
        )
        for index, phase in enumerate(RunPhase)
    ]
    rows.extend(
        RunRow(
            run_id=f"extra-live-{index}",
            worktree=str(tmp_path),
            phase=RunPhase.RUNNING,
            sandbox="read-only",
            model="fixture-model",
            owner_instance="fixture-owner",
            backend=Backend.CLAUDE,
            created_at=100 + index,
            updated_at=101 + index,
        )
        for index in range(140)
    )
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        for row in rows:
            registry.insert_run(row)
            if row.run_id in {"extra-live-0", "extra-live-1"}:
                registry.append_run_event(
                    row.run_id,
                    at=1000,
                    kind="lifecycle",
                    payload={
                        "session_profile": SessionProfile(
                            original_run_id=row.run_id,
                            native_write=row.run_id.endswith("0"),
                            read_only=not row.run_id.endswith("0"),
                            read_paths=(),
                        ).data(),
                        "writer_scope": {"unit": "review.slice", "cgroup": "/unused"},
                    },
                )

    def forbidden(*args: object, **kwargs: object) -> int:
        pytest.fail("--live must read the store without an MCP peer")

    monkeypatch.setattr(cli, "_read_tool", forbidden)
    code = cli.main(["run", "list", "--live"])
    assert code == 0, "--live must read the complete registry live phase set"
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    expected = sorted(
        (row for row in rows if not row.phase.is_terminal()),
        key=lambda row: (row.created_at, row.run_id),
        reverse=True,
    )
    assert len(expected) > 128
    assert [row["run_id"] for row in payload["runs"]] == [
        row.run_id for row in expected
    ]
    assert all(
        set(row) == {field.name for field in fields(FleetRun)} | {"native_write"}
        for row in payload["runs"]
    )
    assert {row["phase"] for row in payload["runs"]} == {
        phase.value for phase in RunPhase if not phase.is_terminal()
    }

    admission = {row["run_id"]: row["native_write"] for row in payload["runs"]}
    assert admission["extra-live-0"] is True
    assert admission["extra-live-1"] is False
    assert all(type(value) is bool for value in admission.values())


@pytest.mark.parametrize(
    "command",
    [
        ["run", "show", "cli-run"],
        ["run", "list", "--live"],
        ["run", "list"],
        ["result", "show", "cli-run"],
        ["transcript", "show", "cli-run"],
        ["quota", "show"],
    ],
)
def test_current_schema_reads_answer_while_a_writer_holds_both_locks(
    fleet_run: str, command: list[str]
) -> None:
    """Keep the writer admitted until the separate CLI process has answered."""
    with Registry(store(), "fixture-owner") as registry:
        registry.insert_run(
            RunRow(
                run_id="live-run",
                worktree="/live",
                phase=RunPhase.RUNNING,
                sandbox="read-only",
                model="fixture-model",
                owner_instance="fixture-owner",
                created_at=3,
                updated_at=3,
                backend=Backend.CLAUDE,
            )
        )
    with nott_database.connect(store()) as writer, nott_database.write(writer):
        assert writer.in_transaction
        result = subprocess.run(
            [sys.executable, "-m", "nspawn.cli", *command],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        if command[:2] == ["run", "show"]:
            assert payload["run_id"] == fleet_run
        elif command[:2] == ["run", "list"]:
            expected = ["live-run"] if "--live" in command else ["live-run", fleet_run]
            assert [row["run_id"] for row in payload["runs"]] == expected


def test_mcp_read_lifecycle_writes_no_rows(
    fleet_run: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nspawn import registry as registry_module
    from nspawn.mcp import McpServer

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A read must not request the registry write lock.")

    monkeypatch.setattr(registry_module, "write", forbidden)
    server = McpServer()
    try:
        assert server.nspawn_run_show(fleet_run)["run_id"] == fleet_run
        reader = server._reader
        assert reader is not None
        assert not reader._tick_thread.is_alive()
        assert reader._reconcile_thread is None
        assert not reader.announces_terminal
        assert server.supervisor is None
    finally:
        server.close()
    assert server._reader is None


def test_mcp_write_after_read_keeps_the_writer_lifecycle(fleet_run: str) -> None:
    from nspawn.mcp import McpServer

    server = McpServer()
    try:
        server.nspawn_run_show(fleet_run)
        writer = server._resolve_supervisor()
        assert writer is not server._reader
        assert writer._tick_thread.is_alive()
        assert writer._reconcile_thread is not None
        assert writer.announces_terminal
        assert (
            writer._registry._one(
                "SELECT 1 FROM instances WHERE instance_id = ?",
                (writer._registry.instance_id,),
            )
            is not None
        )
        assert (
            writer._registry._one(
                "SELECT 1 FROM sidecar_instances WHERE instance_id = ?",
                (writer._registry.instance_id,),
            )
            is not None
        )
    finally:
        server.close()

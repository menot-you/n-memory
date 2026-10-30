"""Every run reader reports the same stream observation without changing the store."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import mock_open

import pytest
from registry_reset import store

from nspawn import cli, mcp, terminal
from nspawn.registry import NoticeCaller, Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor, observe_run_progress

STREAM = (
    b'{"type":"assistant","message":{"usage":{"input_tokens":100,"output_tokens":50}}}\n'
    b'{"type":"assistant","message":{"usage":{"input_tokens":150,"output_tokens":60}}}\n'
)

TAIL_FRAGMENT_FRAME = (
    b'{"type":"assistant","message":{"usage":{"input_tokens":999,"output_tokens":0}}}\n'
)


@pytest.mark.parametrize(
    "phase,stored,stream,expected",
    [
        pytest.param(RunPhase.RUNNING, (0, 0), STREAM, (210, 2), id="live"),
        pytest.param(
            RunPhase.RUNNING,
            (0, 0),
            b"x"
            + TAIL_FRAGMENT_FRAME
            + b"x" * (524288 - len(TAIL_FRAGMENT_FRAME) - 1)
            + b"\n",
            (0, 0),
            id="invented-tail-frame",
        ),
        pytest.param(RunPhase.RUNNING, (250, 4), STREAM, (250, 4), id="stored-floor"),
        pytest.param(RunPhase.RUNNING, (70, 3), None, (70, 3), id="missing"),
        pytest.param(RunPhase.RUNNING, (70, 3), "directory", (70, 3), id="unreadable"),
        pytest.param(
            RunPhase.RUNNING, (70, 3), b'{"type":"assist', (70, 3), id="partial"
        ),
        *[
            pytest.param(phase, (70, 3), STREAM, (70, 3), id=phase.value)
            for phase in RunPhase
            if phase.is_terminal()
        ],
    ],
)
@pytest.mark.parametrize(
    "reader",
    ["live", "since", "show", "mcp_show", "group", "owner"],
)
def test_run_readers_share_live_progress(
    reader: str,
    phase: RunPhase,
    stored: tuple[int, int],
    stream: bytes | str | None,
    expected: tuple[int, int],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "runs"
    run_id = "live-progress"
    directory = root / run_id
    directory.mkdir(parents=True)
    if isinstance(stream, bytes):
        (directory / "stdout").write_bytes(stream)
    elif stream == "directory":
        (directory / "stdout").mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    owner = "node:00000000-0000-4000-8000-000000000001"
    caller = "/fixture/claude/4242.sock"
    commands = {
        "live": ["run", "list", "--live"],
        "since": ["run", "list", "--since", "1970-01-01"],
        "show": ["run", "show", run_id],
        "group": ["run", "list", "--group", "progress"],
        "owner": ["run", "list", "--owner", owner],
    }
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        row = RunRow(
            run_id=run_id,
            worktree=str(tmp_path),
            phase=RunPhase.PENDING,
            sandbox="read-only",
            model="fixture-model",
            owner_instance="fixture-owner",
            created_at=1,
            updated_at=1,
            backend=Backend.CLAUDE,
            executed_provider="fixture",
            owner=owner,
            group_name="progress",
            tokens=stored[0],
            turn_count=stored[1],
        )
        registry.admit_run(
            row,
            False,
            1,
            60_000,
            notice=NoticeCaller(
                addr=caller, pid=4242, start_ticks=1, project=str(tmp_path)
            ),
        )
        row = replace(row, phase=phase, updated_at=2)
        registry.update_run(row)
        supervisor = Supervisor(registry, root)
        try:
            server = mcp.McpServer(supervisor=supervisor)
            answer = server.call_tool("nspawn_run_list", {})
            reference = json.loads(answer["content"][0]["text"])["runs"][0]
            assert (reference["tokens"], reference["turn_count"]) == expected
            if reader == "mcp_show":
                answer = server.call_tool("nspawn_run_show", {"run_id": run_id})
                shown = json.loads(answer["content"][0]["text"])
            else:
                assert cli.main(commands[reader]) == 0
                captured = capsys.readouterr()
                assert captured.err == ""
                shown = json.loads(captured.out)
                if reader == "live" and phase.is_terminal():
                    assert shown["runs"] == []
                    assert registry.get_run(run_id) == row
                    return
                if "runs" in shown:
                    shown = shown["runs"][0]
            assert (shown["tokens"], shown["turn_count"]) == expected
            assert registry.get_run(run_id) == row
        finally:
            supervisor.shutdown()


def test_read_failure_keeps_stored_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = RunRow(
        run_id="read-failure",
        worktree=str(tmp_path),
        phase=RunPhase.RUNNING,
        sandbox="read-only",
        model="fixture-model",
        owner_instance="fixture-owner",
        created_at=1,
        updated_at=1,
        backend=Backend.CLAUDE,
        tokens=70,
        turn_count=3,
    )
    stdout = tmp_path / row.run_id / "stdout"
    stdout.parent.mkdir()
    stdout.write_bytes(STREAM)
    assert stdout.is_file()
    unreadable_stdout = mock_open(read_data=STREAM)
    unreadable_stdout.return_value.tell.return_value = len(STREAM)
    unreadable_stdout.return_value.read.side_effect = PermissionError(
        "fixture stdout is unreadable"
    )
    monkeypatch.setattr(Path, "open", unreadable_stdout)

    assert observe_run_progress(row, tmp_path) == row
    unreadable_stdout.assert_called_once_with("rb")
    unreadable_stdout.return_value.read.assert_called_once_with()


@pytest.mark.parametrize(
    "suffix",
    [
        b'{"type":"assistant","message":{"usage":{"input_tokens":999}}}',
        b'{"type":"assistant","message":{"usage":{"input_tokens":-1}}}\n',
        b'{"type":"assistant","message":{"usage":{"output_tokens":true}}}\n',
        b'{"type":"assistant","message":{"usage":{"input_tokens":"999"}}}\n',
        b'{"type":"assistant","message":{"usage":{"input_tokens":18446744073709551616}}}\n',
        b'{"type":"assistant","message":{"usage":null}}\n',
        b'{"type":"assistant","message":[]}\n',
        b"{malformed}\n",
        b'{"invalid":"\xff"}\n',
        pytest.param(b'{"nested":' + b"[" * 200000 + b"\n", id="deep-partial"),
        b"[]\n",
    ],
)
def test_live_progress_ignores_incomplete_or_invalid_usage(
    tmp_path: Path,
    suffix: bytes,
) -> None:
    stdout = tmp_path / "stdout"
    stdout.write_bytes(STREAM + suffix)
    assert terminal.observe_live_progress(stdout) == terminal.LiveProgress(210, 2)


@pytest.mark.parametrize(
    "prefix,turns",
    [pytest.param(b"x\n", 2, id="line-start"), pytest.param(b"xx", 1, id="mid-line")],
)
def test_live_progress_tail_respects_line_boundary(
    prefix: bytes,
    turns: int,
    tmp_path: Path,
) -> None:
    stdout = tmp_path / "stdout"
    stdout.write_bytes(
        prefix + STREAM + b"x" * (terminal.LIVE_PROGRESS_TAIL_BYTES - len(STREAM))
    )
    assert terminal.observe_live_progress(stdout) == terminal.LiveProgress(210, turns)


@pytest.mark.parametrize("phase", list(RunPhase))
def test_each_reader_observes_the_stream_once(
    phase: RunPhase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runs"
    row = RunRow(
        tokens=70,
        turn_count=3,
        run_id="once",
        worktree=str(tmp_path),
        phase=phase,
        sandbox="read-only",
        model="fixture-model",
        owner_instance="fixture-owner",
        created_at=1,
        updated_at=1,
        backend=Backend.CLAUDE,
    )
    observed: list[Path] = []

    def observe(stdout: Path) -> terminal.LiveProgress:
        observed.append(stdout)
        return terminal.LiveProgress(210, 2)

    monkeypatch.setattr(terminal, "observe_live_progress", observe)
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        registry.insert_run(row)
        supervisor = Supervisor(registry, root)
        try:
            expected = (70, 3) if phase.is_terminal() else (210, 3)
            paths = [] if phase.is_terminal() else [root / row.run_id / "stdout"]
            fleet = supervisor.fleet().runs[0]
            assert (fleet.tokens, fleet.turn_count) == expected
            assert observed == paths
            observed.clear()
            status = supervisor.status(row.run_id)
            assert (status.tokens, status.turn_count) == expected
            assert observed == paths
        finally:
            supervisor.shutdown()


# The wire shape of run-list-caller.v1. The harness owns the reader of it and
# round-trips these rows in its own tests.
CALLER_RUN_KEYS = {
    "runId",
    "phase",
    "createdAt",
    "group",
    "lastOutputAt",
    "continuedAs",
}


@pytest.mark.parametrize("reader", ["live", "since", "group", "caller", "owner"])
def test_listing_skips_deeply_nested_stdout_line(
    reader: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "runs"
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    owner = "node:00000000-0000-4000-8000-000000000001"
    caller = "/fixture/claude/4242.sock"
    commands = {
        "live": ["run", "list", "--live"],
        "since": ["run", "list", "--since", "1970-01-01"],
        "group": ["run", "list", "--group", "progress"],
        "caller": ["run", "list", "--caller", caller, "--json"],
        "owner": ["run", "list", "--owner", owner],
    }
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        rows = []
        for run_id, stored in (("bad-run", (250, 4)), ("other-run", (0, 0))):
            directory = root / run_id
            directory.mkdir(parents=True)
            stream = STREAM
            if run_id == "bad-run":
                stream += b'{"nested":' + b"[" * 200000 + b"\n"
            (directory / "stdout").write_bytes(stream)
            row = RunRow(
                run_id=run_id,
                worktree=str(tmp_path),
                phase=RunPhase.PENDING,
                sandbox="read-only",
                model="fixture-model",
                owner_instance="fixture-owner",
                created_at=1,
                updated_at=1,
                backend=Backend.CLAUDE,
                owner=owner,
                group_name="progress",
                tokens=stored[0],
                turn_count=stored[1],
            )
            registry.admit_run(
                row,
                False,
                1,
                60_000,
                notice=NoticeCaller(
                    addr=caller, pid=4242, start_ticks=1, project=str(tmp_path)
                ),
            )
            row = replace(row, phase=RunPhase.RUNNING, updated_at=2)
            registry.update_run(row)
            rows.append(row)
        assert cli.main(commands[reader]) == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        runs = json.loads(captured.out)["runs"]
        if reader == "caller":
            assert {run["runId"] for run in runs} == {row.run_id for row in rows}
            for run in runs:
                assert set(run) == CALLER_RUN_KEYS
                assert "tokens" not in run
                assert "turn_count" not in run
        else:
            id_field = "run_id" if reader in {"live", "since"} else "runId"
            assert {
                run[id_field]: (run["tokens"], run["turn_count"]) for run in runs
            } == {"bad-run": (250, 4), "other-run": (210, 2)}
        for row in rows:
            assert registry.get_run(row.run_id) == row

"""Operator reads answer as the owning native stdio tools do (`result show` reads the
store itself), against disposable state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from registry_reset import store

from nspawn import cli
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow


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
        raise cli.NspawnClientError("peer unavailable")

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

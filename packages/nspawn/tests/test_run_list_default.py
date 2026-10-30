import json
from dataclasses import replace
from pathlib import Path

import pytest
from registry_reset import store

from nspawn import mcp
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow


def _row(tmp_path: Path, run_id: str, phase: RunPhase) -> RunRow:
    return RunRow(
        run_id=run_id,
        worktree=str(tmp_path),
        phase=phase,
        sandbox="read-only",
        model="fixture-model",
        owner_instance="fixture-owner",
        created_at=1,
        updated_at=2,
        backend=Backend.CLAUDE,
    )


def _list(server: mcp.McpServer, arguments: dict[str, object]) -> dict[str, object]:
    answer = server.call_tool("nspawn_run_list", arguments)
    assert answer["isError"] is False
    page = json.loads(answer["content"][0]["text"])
    assert isinstance(page, dict)
    return page


def test_run_list_leaves_out_terminal_runs_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    rows = [
        _row(tmp_path, "live-run", RunPhase.PENDING),
        _row(tmp_path, "done-run", RunPhase.COMPLETED),
        _row(tmp_path, "closed-run", RunPhase.CLOSED),
    ]
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        for row in rows:
            registry.insert_run(replace(row))

    server = mcp.McpServer()
    try:
        default = _list(server, {})
        runs = default["runs"]
        assert isinstance(runs, list)
        assert all(not RunPhase(run["phase"]).is_terminal() for run in runs), (
            "The default listing must not carry a terminal run without a notice."
        )
        assert default["terminal_omitted"] + len(runs) == len(rows)
        assert default["terminal_omitted"] >= 2

        full = _list(server, {"include_terminal": True})
        assert {run["run_id"] for run in full["runs"]} == {row.run_id for row in rows}
    finally:
        server.close()

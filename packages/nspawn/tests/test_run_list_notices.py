"""Fleet filtering preserves ready notices and every nonterminal phase."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from registry_reset import store

from nspawn.mcp import McpServer
from nspawn.registry import NoticeCaller, Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor


def test_run_list_keeps_undelivered_notices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        for index, phase in enumerate(RunPhase):
            row = RunRow(
                run_id=phase.value,
                backend=Backend.CLAUDE,
                worktree=str(tmp_path),
                phase=phase,
                sandbox="read-only",
                model="fixture-model",
                owner_instance="fixture-owner",
                created_at=index + 1,
                updated_at=index + 1,
            )
            registry.insert_run(row)
            if not phase.is_terminal():
                continue
            for delivered in (False, True):
                notice_row = replace(
                    row,
                    run_id=f"{phase.value}-notice-{delivered}",
                    phase=RunPhase.PENDING,
                )
                caller = NoticeCaller(
                    addr="fixture-caller",
                    pid=4242,
                    start_ticks=1,
                    project=str(tmp_path),
                )
                registry.admit_run(notice_row, False, 1, 60_000, notice=caller)
                registry.update_run(replace(notice_row, phase=phase))
                if delivered:
                    claim = registry.claim_notice(notice_row.run_id, caller.addr)
                    assert claim is not None
                    assert registry.mark_notice_delivered(
                        notice_row.run_id, caller.addr, claim[0]
                    )
        supervisor = Supervisor(registry, root)
        server = McpServer(supervisor=supervisor)
        try:
            pages = []
            for arguments in (
                {},
                {"include_terminal": False},
                {"include_terminal": True},
            ):
                answer = server.call_tool("nspawn_run_list", arguments)
                assert answer["isError"] is False
                pages.append(json.loads(answer["content"][0]["text"]))
            default, explicit, full = pages
            notices = {notice["run_id"] for notice in full["notices"]}
            expected_notices = {
                f"{phase.value}-notice-False"
                for phase in RunPhase
                if phase.is_terminal()
            }
            assert notices == expected_notices
            expected = [
                run
                for run in full["runs"]
                if not RunPhase(run["phase"]).is_terminal() or run["run_id"] in notices
            ]
            assert default["runs"] == explicit["runs"] == expected
            assert default["notices"] == explicit["notices"] == full["notices"]
            omitted = 2 * len(expected_notices)
            assert (
                default["terminal_omitted"] == explicit["terminal_omitted"] == omitted
            )
            assert len(full["runs"]) == len(RunPhase) + omitted
            assert full["terminal_omitted"] == 0
            assert len(registry.undelivered_notices()) == len(notices)
        finally:
            server.close()


def test_run_list_empty_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
    server = McpServer()
    try:
        answer = server.call_tool("nspawn_run_list", {})
        page = json.loads(answer["content"][0]["text"])
        assert page["runs"] == page["notices"] == []
        assert page["terminal_omitted"] == 0
    finally:
        server.close()

"""Fresh read sessions retain result spills and detect lost drivers."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import nott_database
import pytest
from registry_reset import sql, store

from nspawn import mcp
from nspawn import registry as registry_module
from nspawn.process_identity import (
    ProcessIdentityError,
    capture_process_identity,
    process_identity_matches,
)
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor


def _row(tmp_path: Path) -> RunRow:
    return RunRow(
        run_id="reader-run",
        worktree=str(tmp_path),
        phase=RunPhase.COMPLETED,
        sandbox="read-only",
        model="fixture-model",
        owner_instance="fixture-owner",
        created_at=1,
        updated_at=2,
        backend=Backend.CLAUDE,
    )


@pytest.mark.parametrize(
    "tool",
    [
        "nspawn_run_list",
        "nspawn_run_show",
        "nspawn_result_show",
        "nspawn_transcript_show",
        "nspawn_usage_show",
        "nspawn_quota_show",
    ],
)
def test_fresh_reader_spills_large_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    root = tmp_path / "runs"
    directory = root / "reader-run"
    directory.mkdir(parents=True)
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(root))
    large_text = "\x01" * 16_000 + "é\r\n"
    row = replace(_row(tmp_path), final_text=large_text, blocked_reason=large_text)
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        registry.insert_run(row)
        if tool == "nspawn_run_list":
            for index in range(24):
                registry.insert_run(
                    replace(row, run_id=f"fleet-{index}", group_name="\x01" * 512)
                )
        if tool == "nspawn_usage_show":
            for index in range(8):
                registry.append_run_event(
                    row.run_id,
                    at=int(time.time() * 1000),
                    kind="lifecycle",
                    payload={"usage_identity": f"node-{index}-" + "\x01" * 1_500},
                )
    if tool == "nspawn_transcript_show":
        (directory / "stdout").write_text(
            json.dumps({"type": "assistant", "text": "é" * 40_000}) + "\n"
        )
    if tool == "nspawn_quota_show":
        roster = tmp_path / "roster.toml"
        roster.write_text(
            '[[provider]]\nname = "fixture"\nkind = "claude"\n'
            'model = "fixture-model"\nmodels = '
            + json.dumps(
                ["fixture-model", *(f"model-{i}-" + "é" * 120 for i in range(100))],
                ensure_ascii=False,
            )
            + "\n"
        )
        monkeypatch.setenv("NOTT_NSPAWN_ROSTER", str(roster))

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("A read with nothing to record must not request the write lock.")

    monkeypatch.setattr(registry_module, "write", forbidden)
    arguments = (
        {"timeZone": "UTC"}
        if tool == "nspawn_usage_show"
        else {"refresh": 0}
        if tool == "nspawn_quota_show"
        else {"include_terminal": True}
        if tool == "nspawn_run_list"
        else {"run_id": row.run_id}
    )
    server = mcp.McpServer()
    try:
        answer = server.call_tool(tool, arguments)
        assert answer["isError"] is False
        spilled = json.loads(answer["content"][0]["text"])
        assert set(spilled) == {"preview", "path", "bytes"}
        path = Path(spilled["path"])
        assert path.is_absolute() and path.is_relative_to(root)
        if "run_id" in arguments:
            assert path.parent == directory
        payload = path.read_bytes()
        assert spilled["bytes"] == len(payload) > 65_536
        assert spilled["preview"].encode() == payload[:65_536]
        assert path.stat().st_mode & 0o777 == 0o600
        assert isinstance(json.loads(payload), dict)
        assert server.supervisor is None
        reader = server._reader
        assert reader is not None
        assert not reader._tick_thread.is_alive()
        assert reader._reconcile_thread is None
    finally:
        server.close()


def test_fresh_reader_records_last_driver_loss_within_reaper_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    with subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    ) as child:
        identity = capture_process_identity(child.pid)
        child.communicate(timeout=5)
    assert not process_identity_matches(identity)
    now = int(time.time() * 1000)
    with Registry(store(), "fixture-owner") as registry:
        registry.open(now - Supervisor.STALE_AFTER_MS - 1)
        registry.insert_run(replace(_row(tmp_path), phase=RunPhase.RUNNING))
        sql(
            registry,
            "UPDATE instances SET process_pid = ?, process_boot_id = ?, "
            "process_start_ticks = ? WHERE instance_id = ?",
            (
                identity.pid,
                identity.boot_id,
                identity.start_ticks,
                registry.instance_id,
            ),
        )
        sql(
            registry,
            "INSERT INTO notices "
            "(run_id, caller_addr, caller_pid, caller_start_ticks, caller_project, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "reader-run",
                str(tmp_path / "caller.sock"),
                identity.pid,
                identity.start_ticks,
                str(tmp_path),
                now,
            ),
        )
    server = mcp.McpServer()
    started = time.monotonic()
    try:
        answer = server.call_tool(
            "nspawn_run_show", {"run_id": "reader-run", "timeout_ms": 60_000}
        )
        elapsed = time.monotonic() - started
        status = json.loads(answer["content"][0]["text"])
        assert status["phase"] == "orphaned", status
        assert (
            status["failure_reason"]
            == "owner process gone; run requires doctor/recovery"
        )
        assert elapsed < Supervisor.TICK_INTERVAL_S + 2
        assert server.supervisor is None
        assert server._reader is not None
        registry = server._reader._registry
        notice = registry._one(
            "SELECT ready_at FROM notices WHERE run_id = ?", ("reader-run",)
        )
        assert notice is not None and notice["ready_at"] is not None
        assert registry._one("SELECT 1 FROM instances") is None
    finally:
        server.close()


@pytest.mark.parametrize(
    "owner_state",
    ["live_stale", "dead_fresh", "partial_stale", "probe_failed", "terminal_dead"],
)
def test_reader_without_proven_loss_avoids_the_writer_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owner_state: str
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    now = int(time.time() * 1000)
    live = capture_process_identity(os.getpid())
    identity = (
        live
        if owner_state == "live_stale"
        else replace(live, start_ticks=live.start_ticks + 1)
    )
    with Registry(store(), "fixture-owner") as registry:
        registry.open(now if owner_state == "dead_fresh" else 1)
        registry.insert_run(
            replace(
                _row(tmp_path),
                phase=RunPhase.COMPLETED
                if owner_state == "terminal_dead"
                else RunPhase.RUNNING,
            )
        )
        sql(
            registry,
            "UPDATE instances SET process_pid = ?, process_boot_id = ?, process_start_ticks = ?",
            (
                identity.pid,
                identity.boot_id,
                None if owner_state == "partial_stale" else identity.start_ticks,
            ),
        )

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("An unproven owner loss must not request the write lock.")

    def failed_probe(*args: object, **kwargs: object) -> bool:
        raise ProcessIdentityError("probe refused")

    if owner_state == "probe_failed":
        monkeypatch.setattr(registry_module, "process_identity_matches", failed_probe)
    monkeypatch.setattr(registry_module, "write", forbidden)
    with nott_database.connect(store()) as writer, nott_database.write(writer):
        server = mcp.McpServer()
        try:
            answer = server.call_tool("nspawn_run_show", {"run_id": "reader-run"})
            status = json.loads(answer["content"][0]["text"])
            assert status["phase"] == (
                "completed" if owner_state == "terminal_dead" else "running"
            )
            assert status["failure_reason"] is None
            assert server.supervisor is None
        finally:
            server.close()


def test_reader_rechecks_loss_during_long_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    now = int(time.time() * 1000)
    identity = capture_process_identity(os.getpid())
    with Registry(store(), "fixture-owner") as registry:
        registry.open(now)
        registry.insert_run(replace(_row(tmp_path), phase=RunPhase.RUNNING))
        sql(
            registry,
            "UPDATE instances SET process_start_ticks = ?",
            (identity.start_ticks + 1,),
        )
    server = mcp.McpServer()
    try:
        reader = server._resolve_reader()
        assert reader._registry.get_run("reader-run").phase is RunPhase.RUNNING
        waits: list[float] = []

        def expire_heartbeat(timeout: float) -> None:
            waits.append(timeout)
            monkeypatch.setattr(
                time, "time", lambda: (now + Supervisor.STALE_AFTER_MS + 1) / 1000
            )

        monkeypatch.setattr(reader._lock, "wait", expire_heartbeat)
        answer = server.call_tool(
            "nspawn_run_show", {"run_id": "reader-run", "timeout_ms": 60_000}
        )
        status = json.loads(answer["content"][0]["text"])
        assert status["phase"] == "orphaned"
        assert "owner process gone" in status["failure_reason"]
        assert waits == [1.0]
    finally:
        server.close()


@pytest.mark.parametrize("loss", ["missing", "legacy"])
def test_reader_records_existing_orphan_causes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loss: str
) -> None:
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(tmp_path / "runs"))
    with Registry(store(), "fixture-owner") as registry:
        registry.open(1)
        registry.insert_run(replace(_row(tmp_path), phase=RunPhase.RUNNING))
        if loss == "missing":
            registry.remove_instance()
        else:
            sql(
                registry,
                "UPDATE instances SET process_pid = NULL, process_boot_id = NULL, "
                "process_start_ticks = NULL",
            )
    server = mcp.McpServer()
    try:
        answer = server.call_tool("nspawn_run_show", {"run_id": "reader-run"})
        status = json.loads(answer["content"][0]["text"])
        assert status["phase"] == "orphaned"
        cause = (
            "owner instance row removed"
            if loss == "missing"
            else "owner legacy heartbeat expired"
        )
        assert status["failure_reason"] == cause + "; run requires doctor/recovery"
        assert server.supervisor is None
    finally:
        server.close()


def test_orphan_writer_rechecks_owner_after_reader_detects_loss(tmp_path: Path) -> None:
    now = int(time.time() * 1000)
    live = capture_process_identity(os.getpid())
    with (
        Registry(store(), "fixture-owner") as registry,
        Registry(store(), "reader") as reader,
    ):
        registry.open(1)
        registry.insert_run(replace(_row(tmp_path), phase=RunPhase.RUNNING))
        sql(
            registry,
            "UPDATE instances SET process_start_ticks = ?",
            (live.start_ticks + 1,),
        )
        assert reader.has_orphans(now, Supervisor.STALE_AFTER_MS)
        sql(
            registry,
            "UPDATE instances SET process_start_ticks = ?",
            (live.start_ticks,),
        )
        assert reader.mark_orphans(now, Supervisor.STALE_AFTER_MS) == 0
        assert registry.get_run("reader-run").phase is RunPhase.RUNNING

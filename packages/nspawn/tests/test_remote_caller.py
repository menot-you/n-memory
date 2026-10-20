"""A caller on another machine (a Mac over ssh) has no socket here to push to.

Its stdio on SERVER is the only local trace of the session, so admission writes
the row `remote:<session-id>` keyed to the stdio's own process identity:
`push_ready` never sends to it, and the drain delivers it once that stdio is gone
or the notice has waited 120 s.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from registry_reset import sql
from supervisor_support import registry

import nspawn.registry as store_module
from nspawn import notify
from nspawn.cli import main
from nspawn.driver import REMOTE_SESSION_ENV, DriverUnavailable, caller_identity
from nspawn.process_identity import capture_process_identity
from nspawn.registry import NoticeCaller, Registry
from nspawn.rows import Backend, RunPhase, RunRow

__all__ = ["registry"]

PROJECT = "/fixture/remote/.git"
SESSION = "6f1c2f8e-3b1a-4d7e-9a53-2f0e4b7c9d10"


def test_a_remote_session_is_a_caller_keyed_to_this_stdio() -> None:
    caller = caller_identity({REMOTE_SESSION_ENV: SESSION})
    assert caller == {
        "addr": f"remote:{SESSION}",
        "pid": os.getpid(),
        "start_ticks": capture_process_identity(os.getpid()).start_ticks,
    }


def test_a_remote_session_id_that_is_not_a_uuid_is_refused() -> None:
    with pytest.raises(DriverUnavailable):
        caller_identity({REMOTE_SESSION_ENV: "../../x.sock"})


def test_no_socket_and_no_remote_session_is_no_caller() -> None:
    assert caller_identity({}) is None


def _run(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nspawn-remote:{name}"))


def _completed(store: Registry, run_id: str, caller: NoticeCaller) -> None:
    now = store_module.now_ms()
    store.heartbeat_instance(now)
    row = RunRow(
        run_id=run_id,
        worktree=f"/fixture/{run_id}",
        phase=RunPhase.PENDING,
        sandbox="workspace-write",
        model="m",
        owner_instance=store.instance_id,
        created_at=1000,
        updated_at=1000,
        backend=Backend.CLAUDE,
    )
    store.admit_run(row, False, 1000, 60_000, notice=caller)
    for phase in (RunPhase.RUNNING, RunPhase.COMPLETED):
        current = store.get_run(run_id)
        assert current is not None
        store.update_run(
            replace(current, phase=phase, updated_at=current.updated_at + 1)
        )


def _remote(pid: int, ticks: int) -> NoticeCaller:
    return NoticeCaller(
        addr=f"remote:{SESSION}", pid=pid, start_ticks=ticks, project=PROJECT
    )


def _body(store: Registry, run_id: str) -> str:
    return notify.notice_body(
        run_id, "completed", row=store.get_run(run_id), run_dir=Path("/nonexistent")
    )


def _drained(capsys: pytest.CaptureFixture[str]) -> list[str]:
    capsys.readouterr()
    assert main(["notice", "drain", "--project", PROJECT]) == 0
    return [line for line in capsys.readouterr().out.splitlines() if line.strip()]


def test_push_ready_never_sends_to_a_remote_row_while_its_stdio_lives(
    registry: Registry,
) -> None:
    me = os.getpid()
    run_id = _run("live")
    _completed(registry, run_id, _remote(me, capture_process_identity(me).start_ticks))
    assert notify.push_ready(registry) == 0
    row = sql(registry, "SELECT delivered_at FROM notices WHERE run_id = ?", (run_id,))
    assert row.fetchone()["delivered_at"] is None


def test_the_drain_waits_while_the_stdio_lives_then_takes_it_after_120_s(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    me = os.getpid()
    run_id = _run("stale")
    _completed(registry, run_id, _remote(me, capture_process_identity(me).start_ticks))
    assert _drained(capsys) == []
    sql(
        registry,
        "UPDATE notices SET ready_at = ? WHERE run_id = ?",
        (store_module.now_ms() - 121_000, run_id),
    )
    assert _drained(capsys) == [_body(registry, run_id)]
    assert _drained(capsys) == []


def test_the_drain_takes_it_at_once_when_the_stdio_is_gone(
    registry: Registry, capsys: pytest.CaptureFixture[str]
) -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    ticks = capture_process_identity(gone.pid).start_ticks
    gone.wait()
    run_id = _run("dead")
    _completed(registry, run_id, _remote(gone.pid, ticks))
    assert _drained(capsys) == [_body(registry, run_id)]

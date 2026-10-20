"""A run whose driver ends it with no terminal write is FAILED, never a false orphan.

Run e6a57ab2 (2026-10-09): its driver exited while the row was `running`.
`shutdown` deleted the driver's instance row, and the next peer tick marked the
run `orphaned` with "owner instance heartbeat expired". Nothing wrote FAILED,
and no notice named the exit. A worker that raised after a provisional orphan
mark also left the row orphaned, because `_guarded_worker` took ORPHANED as an
end.
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from supervisor_support import _drain_worker_threads, _stop_every_reaper, registry
from test_driver import Drivers, Reaper, drv, reaper
from test_notice_outbox import _admit, _dead_caller, _move, _notice

from nspawn.registry import STORE_ERRORS, Registry, RegistryError
from nspawn.rows import RunPhase
from nspawn.supervisor import DRIVE_CALL, RunAccepted, Supervisor, UnknownRun

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "drv", "reaper", "registry"]

RUN = str(uuid.uuid5(uuid.NAMESPACE_URL, "nspawn-test:driver-exit"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _assert_failed(registry: Registry, run_id: str, reason: str) -> None:
    row = registry.get_run(run_id)
    assert row is not None
    assert (row.phase, row.blocked_reason) == (RunPhase.FAILED, reason)
    receipt = registry.terminal_receipt_show(run_id)
    assert receipt is not None
    assert (receipt["phase"], receipt["reason"]) == ("failed", reason)
    notice = _notice(registry, run_id)
    assert notice is not None
    assert notice["phase"] == "failed" and notice["ready_at"] is not None


def test_a_worker_that_raises_after_a_provisional_orphan_ends_failed(
    registry: Registry, tmp_path: Path, reaper: Reaper
) -> None:
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    owner = drivers()
    _admit(owner._registry, RUN, _dead_caller())
    _move(owner._registry, RUN, RunPhase.SPAWNING, RunPhase.RUNNING)
    owner._registry.remove_instance()
    registry.mark_orphans(now=_now_ms(), stale_after_ms=30_000)
    orphaned = registry.get_run(RUN)
    assert orphaned is not None and orphaned.phase is RunPhase.ORPHANED

    def broken() -> None:
        raise RuntimeError("the vendor stream broke")

    try:
        owner._guarded_worker(RUN, broken)
    finally:
        owner.shutdown()
    _assert_failed(registry, RUN, "WorkerDied: RuntimeError")


def test_a_driver_that_exits_before_any_terminal_write_ends_its_run_failed(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    reaper: Reaper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    monkeypatch.setattr(drv, "_dispatch", _admit_unended(_dead_caller()))
    _serve(drv, tmp_path, drivers)
    # The peer tick that follows a driver's exit.
    registry.mark_orphans(now=_now_ms(), stale_after_ms=30_000)
    _assert_failed(registry, RUN, "DriverExit: the worker ended with no terminal write")


def _serve(drv: ModuleType, tmp_path: Path, drivers: Drivers) -> None:
    frame = {
        "tool": "nspawn_run_start",
        "arguments": {"prompt": "p", "cwd": str(tmp_path)},
        "launch_uuid": str(uuid.uuid4()),
        "caller": None,
        "project": None,
        "drivers_dir": None,
    }
    drv.serve(frame, lambda ack: None, supervisor_factory=drivers, owns_supervisor=True)


def _admit_unended(caller: Any) -> Any:
    def admit(
        supervisor: Supervisor, tool: str, arguments: dict[str, Any]
    ) -> RunAccepted:
        # The admission a real dispatch makes, then a worker that ends with no
        # terminal write.
        call = DRIVE_CALL.get()
        assert call is not None
        _admit(supervisor._registry, RUN, caller)
        _move(supervisor._registry, RUN, RunPhase.SPAWNING, RunPhase.RUNNING)
        call.run_id = RUN
        call.worker = threading.Thread(target=lambda: None, daemon=True)
        call.worker.start()
        return RunAccepted(run_id=RUN, phase=RunPhase.RUNNING)

    return admit


def test_a_refused_driver_exit_write_is_retried_before_the_driver_exits(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    reaper: Reaper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FAILED write waits in `_unpublished` when the store refuses it, and
    nothing after `fail_unended` read that queue again."""
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    admit = _admit_unended(_dead_caller())

    def admit_then_refuse_once(
        supervisor: Supervisor, tool: str, arguments: dict[str, Any]
    ) -> RunAccepted:
        accepted = admit(supervisor, tool, arguments)
        store = supervisor._registry
        real_update = store.update_run
        refusals = [STORE_ERRORS[0]("database is locked")]

        def refused_once(row: Any, **kwargs: Any) -> Any:
            if row.phase is RunPhase.FAILED and refusals:
                raise refusals.pop()
            return real_update(row, **kwargs)

        monkeypatch.setattr(store, "update_run", refused_once)
        return accepted

    monkeypatch.setattr(drv, "_dispatch", admit_then_refuse_once)
    _serve(drv, tmp_path, drivers)
    registry.mark_orphans(now=_now_ms(), stale_after_ms=30_000)
    _assert_failed(registry, RUN, "DriverExit: the worker ended with no terminal write")


def test_a_driver_exit_write_that_raises_still_shuts_the_supervisor_down(
    drv: ModuleType,
    tmp_path: Path,
    reaper: Reaper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    monkeypatch.setattr(drv, "_dispatch", _admit_unended(_dead_caller()))
    shut: list[bool] = []
    real_shutdown = Supervisor.shutdown

    def unknown(self: Supervisor, run_id: str) -> None:
        raise UnknownRun(run_id)

    def shutdown(self: Supervisor) -> None:
        shut.append(True)
        real_shutdown(self)

    monkeypatch.setattr(Supervisor, "fail_unended", unknown)
    monkeypatch.setattr(Supervisor, "shutdown", shutdown)
    with pytest.raises(UnknownRun):
        _serve(drv, tmp_path, drivers)
    assert shut == [True]


def test_a_registry_refusal_of_the_driver_exit_write_shuts_down_and_propagates(
    drv: ModuleType,
    tmp_path: Path,
    reaper: Reaper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a store error is retried at exit. A refusal is not a lock: one log
    line and exit 0 hid it."""
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    admit = _admit_unended(_dead_caller())
    shut: list[bool] = []
    real_shutdown = Supervisor.shutdown

    def admit_then_refuse(
        supervisor: Supervisor, tool: str, arguments: dict[str, Any]
    ) -> RunAccepted:
        accepted = admit(supervisor, tool, arguments)
        store = supervisor._registry
        real_update = store.update_run

        def refused(row: Any, **kwargs: Any) -> Any:
            if row.phase is RunPhase.FAILED:
                raise RegistryError("the FAILED write was refused")
            return real_update(row, **kwargs)

        monkeypatch.setattr(store, "update_run", refused)
        return accepted

    def shutdown(self: Supervisor) -> None:
        shut.append(True)
        real_shutdown(self)

    monkeypatch.setattr(drv, "_dispatch", admit_then_refuse)
    monkeypatch.setattr(Supervisor, "shutdown", shutdown)
    with pytest.raises(RegistryError, match="the FAILED write was refused"):
        _serve(drv, tmp_path, drivers)
    assert shut == [True]


@pytest.mark.parametrize("drained", [True, False])
def test_a_successor_an_exit_retry_admits_is_joined_before_shutdown(
    drv: ModuleType,
    tmp_path: Path,
    reaper: Reaper,
    monkeypatch: pytest.MonkeyPatch,
    drained: bool,
) -> None:
    """A retried terminal write can hand a review on (a failover). The driver
    waits for that successor, whatever the retry returns, before `shutdown`
    stops its child."""
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    admit = _admit_unended(_dead_caller())
    calls: list[Any] = []

    def admit_and_keep_call(
        supervisor: Supervisor, tool: str, arguments: dict[str, Any]
    ) -> RunAccepted:
        calls.append(DRIVE_CALL.get())
        return admit(supervisor, tool, arguments)

    def publish(self: Supervisor) -> bool:
        # The retry admits a successor, as `_fail_over` does from a terminal write.
        successor = threading.Thread(target=time.sleep, args=(0.2,), daemon=True)
        successor.start()
        calls[0].worker = successor
        return drained

    alive_at_shutdown: list[bool] = []
    real_shutdown = Supervisor.shutdown

    def shutdown(self: Supervisor) -> None:
        alive_at_shutdown.append(calls[0].worker.is_alive())
        real_shutdown(self)

    monkeypatch.setattr(drv, "_dispatch", admit_and_keep_call)
    monkeypatch.setattr(Supervisor, "publish_pending", publish)
    monkeypatch.setattr(Supervisor, "shutdown", shutdown)
    _serve(drv, tmp_path, drivers)
    assert alive_at_shutdown == [False]

"""Check busy heartbeat ticks without a live store or child process."""

from collections.abc import Iterator
from pathlib import Path

import nott_database
import pytest
from registry_reset import sql, store

from nspawn import supervisor
from nspawn.registry import Registry
from nspawn.rows import Backend, RunPhase, RunRow
from nspawn.supervisor import Supervisor

BUSY_STORE = "nspawn.sqlite3-write.lock is held by another holder past 10.0 s"


@pytest.fixture(params=[None, "stdio", "driver"], ids=["bare", "stdio", "driver"])
def owner(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Supervisor]:
    registry = Registry(str(store()), "busy-owner").connect()
    registry.open(now=1000)
    registry.insert_run(
        RunRow(
            run_id="busy-run",
            worktree=str(tmp_path),
            phase=RunPhase.RUNNING,
            sandbox="read-only",
            model="fixture-model",
            owner_instance=registry.instance_id,
            created_at=1000,
            updated_at=1000,
            backend=Backend.CLAUDE,
        )
    )
    instance = Supervisor(registry, tmp_path / "runs", sidecar_kind=request.param)
    instance._stop_tick.set()
    instance._tick_thread.join(timeout=2)
    assert not instance._tick_thread.is_alive()
    try:
        yield instance
    finally:
        instance.shutdown()
        registry.close()


def _ticks(owner: Supervisor, monkeypatch: pytest.MonkeyPatch, *times: int) -> None:
    """Drive the production loop on a fixed clock without a background thread."""
    pending = iter(times)
    now = 1000

    def wait(interval: float) -> bool:
        nonlocal now
        assert interval == owner.TICK_INTERVAL_S
        try:
            now = next(pending)
        except StopIteration:
            return True
        return False

    with monkeypatch.context() as patch:
        patch.setattr(owner._stop_tick, "wait", wait)
        patch.setattr(supervisor.time, "time", lambda: now / 1000)
        owner._tick_loop()


def _heartbeat(registry: Registry) -> int:
    return int(
        sql(
            registry,
            "SELECT heartbeat_at FROM instances WHERE instance_id = ?",
            (registry.instance_id,),
        )[0]["heartbeat_at"]
    )


def test_one_busy_heartbeat_keeps_run_live_and_next_tick_refreshes(
    owner: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = owner._registry
    heartbeat = registry.heartbeat_instance
    calls: list[int] = []

    def busy_once(now: int) -> bool:
        calls.append(now)
        if len(calls) == 1:
            raise nott_database.BusyError(BUSY_STORE)
        row = registry.get_run("busy-run")
        assert row is not None and row.phase is RunPhase.RUNNING
        assert _heartbeat(registry) == 1000
        return heartbeat(now)

    monkeypatch.setattr(registry, "heartbeat_instance", busy_once)
    _ticks(owner, monkeypatch, 11_000, 21_000)

    assert calls == [11_000, 21_000]
    assert _heartbeat(registry) == 21_000
    row = registry.get_run("busy-run")
    assert row is not None and row.phase is RunPhase.RUNNING
    assert row.blocked_reason is None
    lines = capsys.readouterr().err.splitlines()
    named = [line for line in lines if line.startswith("nspawn.reaper:")]
    assert len(named) == 1
    assert "BusyError" in named[0] and BUSY_STORE in named[0]


def test_stale_busy_live_owner_keeps_custody(
    owner: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = owner._registry

    def busy(now: int) -> bool:
        raise nott_database.BusyError(BUSY_STORE)

    monkeypatch.setattr(registry, "heartbeat_instance", busy)
    ticks = tuple(1000 + step * 10_000 for step in range(1, 5))
    _ticks(owner, monkeypatch, *ticks)
    assert ticks[-1] - _heartbeat(registry) > owner.STALE_AFTER_MS

    peer = Registry(str(store()), "peer").connect()
    try:
        peer.open(now=ticks[-1])
        assert peer.mark_orphans(ticks[-1], owner.STALE_AFTER_MS) == 0
        row = peer.get_run("busy-run")
        assert row is not None and row.phase is RunPhase.RUNNING
        assert row.blocked_reason is None
    finally:
        peer.close()
    lines = capsys.readouterr().err.splitlines()
    named = [line for line in lines if line.startswith("nspawn.reaper:")]
    assert len(named) == len(ticks)
    assert all("BusyError" in line and BUSY_STORE in line for line in named)


def test_stale_busy_legacy_owner_uses_existing_orphan_rule(
    owner: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = owner._registry
    sql(
        registry,
        "UPDATE instances SET process_pid = NULL, process_boot_id = NULL, "
        "process_start_ticks = NULL WHERE instance_id = ?",
        (registry.instance_id,),
    )

    def busy(now: int) -> bool:
        raise nott_database.BusyError(BUSY_STORE)

    monkeypatch.setattr(registry, "heartbeat_instance", busy)
    now = 1000 + owner.STALE_AFTER_MS + 1
    _ticks(owner, monkeypatch, now)

    peer = Registry(str(store()), "peer").connect()
    try:
        peer.open(now=now)
        assert peer.mark_orphans(now - 2, owner.STALE_AFTER_MS) == 0
        assert peer.mark_orphans(now, owner.STALE_AFTER_MS) == 1
        row = peer.get_run("busy-run")
        assert row is not None and row.phase is RunPhase.ORPHANED
        assert row.blocked_reason == (
            "owner legacy heartbeat expired; run requires doctor/recovery"
        )
    finally:
        peer.close()

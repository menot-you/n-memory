"""A failed cleanup keeps custody until a later pass proves the slice stopped."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import conftest
import pytest
from supervisor_support import (
    _admit_pending,
    _drain_worker_threads,
    _stop_every_reaper,
    registry,
)
from test_slice_reconcile import _end, _Manager, manager, needs_manager

from nspawn import custody, route, session, supervisor
from nspawn.custody import AggregateScope, ControllerRefusal
from nspawn.registry import POISONED_LEASE_UNTIL, Registry, RegistryError
from nspawn.rows import RunPhase
from nspawn.supervisor import Supervisor

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "manager", "registry"]


@pytest.mark.parametrize("operation", ["drain", "close"])
def test_observation_error_is_not_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    scope = AggregateScope.recorded(f"nottcontroller-{uuid.uuid4().hex}.slice")
    scope.path = tmp_path / "group"
    scope.path.mkdir()
    original_stat, original_open = os.stat, Path.open

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == scope.path:
            raise PermissionError("observation denied")
        return original_stat(path, *args, **kwargs)

    def opened(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == scope.path / "cgroup.events":
            raise PermissionError("observation denied")
        return original_open(path, *args, **kwargs)

    def refused(self: AggregateScope, argv: list[str]) -> str:
        raise ControllerRefusal("manager unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", stat)
        patch.setattr(Path, "open", opened)
        patch.setattr(AggregateScope, "_call", refused)
        with pytest.raises(ControllerRefusal):
            getattr(scope, operation)()
        assert not scope._closed


@pytest.mark.parametrize("stop_refuses", [False, True])
def test_a_queued_start_is_not_a_stopped_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_refuses: bool
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    scope = AggregateScope.recorded(f"nottcontroller-{uuid.uuid4().hex}.slice")
    scope.path = tmp_path / "not-created-yet"

    def answer(self: AggregateScope, argv: list[str]) -> str:
        if argv[2] == "stop" and stop_refuses:
            raise ControllerRefusal("stop timed out")
        return "LoadState=loaded\nActiveState=activating\nJob=123\n"

    monkeypatch.setattr(AggregateScope, "_call", answer)
    with pytest.raises(ControllerRefusal):
        scope.close()
    assert not scope._closed


def test_reconcile_keeps_an_orphan_that_can_revive(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    unit = sv._open_writer(run_id).unit
    _end(registry, run_id, RunPhase.ORPHANED)
    registry.poison_claim(str(tmp_path), run_id)
    read_phases = registry.run_phases

    def revive(run_ids: Any) -> Any:
        snapshot = read_phases(run_ids)
        _end(registry, run_id, RunPhase.RUNNING)
        return snapshot

    monkeypatch.setattr(registry, "run_phases", revive)
    (outcome,) = sv.reconcile_slices()

    assert manager.stopped == []
    assert (outcome.unit, outcome.action) == (unit, "kept")
    assert registry.get_run(run_id).phase is RunPhase.RUNNING
    assert registry.read_claim(str(tmp_path)).lease_until == POISONED_LEASE_UNTIL


def test_reconcile_retries_release_after_the_slice_is_gone(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    sv._open_writer(run_id)
    _end(registry, run_id, RunPhase.COMPLETED)
    registry.poison_claim(str(tmp_path), run_id)
    release = registry.release_claim

    def locked(*args: Any, **kwargs: Any) -> bool:
        raise RegistryError("transient write lock")

    with monkeypatch.context() as patch:
        patch.setattr(registry, "release_claim", locked)
        assert sv.reconcile_slices()[0].action == "refused"
    assert manager.populated == {}
    assert registry.read_claim(str(tmp_path)) is not None
    monkeypatch.setattr(registry, "release_claim", release)

    outcomes = sv.reconcile_slices()

    assert registry.read_claim(str(tmp_path)) is None
    assert len(outcomes) == 1 and outcomes[0].claim_released


@pytest.mark.parametrize("kind", ["command", "native"])
@pytest.mark.parametrize("stage", ["open", "construct"])
@pytest.mark.parametrize("stop_refuses", [False, True])
def test_start_refusal_ends_the_run_and_keeps_unstopped_custody(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    stage: str,
    stop_refuses: bool,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    original_open = sv._open_writer
    opened: list[str] = []

    born = _Manager.__init__

    def scope_start(self: _Manager, **kwargs: Any) -> None:
        born(self, **kwargs)
        if stage == "open":
            raise ControllerRefusal("start refused")

    def open_writer(run_id: str) -> _Manager:
        registry.claim_worktree(str(tmp_path), run_id, 1, 100)
        opened.append(custody.writer_unit(run_id))
        return original_open(run_id)

    def construct(*args: Any, **kwargs: Any) -> None:
        raise ControllerRefusal("start refused")

    def close(self: _Manager) -> None:
        if stop_refuses:
            raise ControllerRefusal("stop refused")
        _Manager.stopped.append(self.unit)
        _Manager.populated.pop(self.unit, None)

    monkeypatch.setattr(sv, "_open_writer", open_writer)
    monkeypatch.setattr(_Manager, "__init__", scope_start)
    monkeypatch.setattr(_Manager, "close", close)
    monkeypatch.setattr(supervisor, "Command", construct)
    monkeypatch.setattr(session, "NativeSession", construct)
    monkeypatch.setattr(supervisor, "read_roster", lambda cwd: None)
    monkeypatch.setattr(
        supervisor,
        "command_resolve",
        lambda *args, **kwargs: {
            "executable": "/usr/bin/true",
            "centerId": "cleanup-test",
        },
    )
    monkeypatch.setattr(
        route,
        "route_launch",
        lambda *args, **kwargs: (["/usr/bin/true"], {"HOME": str(tmp_path)}),
    )

    with pytest.raises(ControllerRefusal, match="start refused"):
        if kind == "command":
            sv._command_start(str(tmp_path), {})
        else:
            sv._activate(
                str(tmp_path),
                {
                    "provider": "codex",
                    "account_route": "codex:fixture",
                    "model": "fixture",
                },
                None,
            )

    (row,) = registry.list_runs()
    assert row.phase is RunPhase.FAILED
    claim = registry.read_claim(str(tmp_path))
    if stop_refuses:
        assert opened[0] in manager.populated
        assert claim is not None and claim.lease_until == POISONED_LEASE_UNTIL
        stop_refuses = False
        assert sv.reconcile_slices()[0].claim_released
        assert registry.read_claim(str(tmp_path)) is None
    else:
        assert opened[0] not in manager.populated
        assert claim is None


@pytest.mark.parametrize("kind", ["command", "native"])
def test_a_cleanup_read_error_does_not_skip_custody_close(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    managed: list[Any] = []
    start_error = RegistryError("transport write failed")
    read_error = RegistryError("cleanup read failed")
    append = registry.append_run_event

    class Managed:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.scope = kwargs["scope"]
            self.spawned = object()
            self.closed = False
            manager.populated[self.scope.unit] = True
            managed.append(self)

        def data(self) -> dict[str, Any]:
            return {}

        def close(self) -> None:
            self.closed = True

    def unreadable(*args: Any, **kwargs: Any) -> None:
        raise read_error

    def append_event(*args: Any, **kwargs: Any) -> Any:
        if "session_transport" in kwargs.get("payload", {}):
            monkeypatch.setattr(registry, "get_run", unreadable)
            raise start_error
        return append(*args, **kwargs)

    monkeypatch.setattr(registry, "append_run_event", append_event)
    monkeypatch.setattr(supervisor, "Command", Managed)
    monkeypatch.setattr(session, "NativeSession", Managed)
    monkeypatch.setattr(supervisor, "read_roster", lambda cwd: None)
    monkeypatch.setattr(
        supervisor,
        "command_resolve",
        lambda *args, **kwargs: {
            "executable": "/usr/bin/true",
            "centerId": "cleanup-test",
        },
    )
    monkeypatch.setattr(
        route,
        "route_launch",
        lambda *args, **kwargs: (["/usr/bin/true"], {"HOME": str(tmp_path)}),
    )

    with pytest.raises(RegistryError) as caught:
        if kind == "command":
            sv._command_start(str(tmp_path), {})
        else:
            sv._activate(
                str(tmp_path),
                {
                    "provider": "codex",
                    "account_route": "codex:fixture",
                    "model": "fixture",
                },
                None,
            )

    (opened,) = managed
    assert opened.scope.unit in manager.stopped
    assert opened.scope.unit not in manager.populated
    assert opened.closed
    assert caught.value is start_error
    assert str(read_error) in start_error.__notes__


def test_the_slice_fixture_checks_a_failed_unbound_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = f"nottcontroller-{uuid.uuid4().hex}.slice"
    group = tmp_path / unit
    group.mkdir()
    stopped: list[str] = []

    def failed(self: AggregateScope, **kwargs: Any) -> None:
        self.unit, self.path = unit, None
        raise ControllerRefusal("start failed before binding")

    def recorded(cls: type[AggregateScope], name: str) -> AggregateScope:
        assert name == unit
        scope = object.__new__(cls)
        scope.unit, scope.path = name, group
        return scope

    def close(self: AggregateScope) -> None:
        stopped.append(self.unit)
        group.rmdir()

    monkeypatch.setattr(AggregateScope, "__init__", failed)
    monkeypatch.setattr(AggregateScope, "recorded", classmethod(recorded))
    monkeypatch.setattr(AggregateScope, "close", close)
    fixture = conftest._no_slice_outlives_its_test.__wrapped__()
    next(fixture)
    with pytest.raises(ControllerRefusal):
        AggregateScope(memory_bytes=1024, tasks=1)
    with pytest.raises(pytest.fail.Exception, match="left writer slices"):
        next(fixture)
    assert stopped == [unit]


def test_a_recorded_writer_close_does_not_skip_absence_proof(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    sv._open_writer(run_id)
    sv._writers.clear()

    def refused(self: _Manager) -> None:
        raise ControllerRefusal("unit still starting")

    monkeypatch.setattr(_Manager, "close", refused)
    with pytest.raises(supervisor.RunCloseRejected):
        sv._drain_writer(run_id, None)


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_a_unit_state_does_not_hide_a_live_cgroup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    scope = AggregateScope.recorded(f"nottcontroller-{uuid.uuid4().hex}.slice")
    scope.path = tmp_path / "still-present"
    scope.path.mkdir()
    (scope.path / "cgroup.events").write_text("populated 0\n")

    def answer(self: AggregateScope, argv: list[str]) -> str:
        return f"LoadState=loaded\nActiveState={state}\nJob=\n"

    monkeypatch.setattr(AggregateScope, "_call", answer)
    try:
        with pytest.raises(ControllerRefusal):
            scope.close()
        assert not scope._closed
    finally:
        (scope.path / "cgroup.events").unlink()
        scope.path.rmdir()


@needs_manager
def test_a_real_stopped_slice_retries_its_claim_release(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    writer = sv._open_writer(run_id)
    _end(registry, run_id, RunPhase.COMPLETED)
    registry.poison_claim(str(tmp_path), run_id)

    def locked(*args: Any, **kwargs: Any) -> bool:
        raise RegistryError("transient write lock")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(registry, "release_claim", locked)
            (outcome,) = sv.reconcile_slices()
            assert outcome.action == "refused"
        assert writer.path is not None
        with pytest.raises(FileNotFoundError):
            writer.path.stat()
        (outcome,) = sv.reconcile_slices()
        assert outcome.claim_released
        assert registry.read_claim(str(tmp_path)) is None
    finally:
        writer.close()


def test_reconcile_keeps_a_populated_slice_with_a_poisoned_claim(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    unit = sv._open_writer(run_id).unit
    _end(registry, run_id, RunPhase.COMPLETED)
    registry.poison_claim(str(tmp_path), run_id)
    manager.populated[unit] = True

    assert sv.reconcile_slices() == ()

    assert manager.stopped == []
    assert registry.read_claim(str(tmp_path)).lease_until == POISONED_LEASE_UNTIL

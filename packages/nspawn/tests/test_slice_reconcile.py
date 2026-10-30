"""Writer slices that outlive their run.

The reconcile step stops an empty slice whose run ended, a close tolerates a
slice that is already gone, and a custody refusal names the systemd call that
failed and why. The fake user manager stands in for systemd through the seam
the other supervisor tests fake (`supervisor.AggregateScope`), so no test store
reaches the live user manager unless the test needs it by its marker.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest
from supervisor_support import (
    _admit_pending,
    _drain_worker_threads,
    _stop_every_reaper,
    registry,
)

from nspawn import custody, supervisor
from nspawn.custody import AggregateScope, ControllerRefusal
from nspawn.registry import POISONED_LEASE_UNTIL, Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import Supervisor

__all__ = ["_drain_worker_threads", "_stop_every_reaper", "registry"]

LIMITS = {"memory_bytes": 64 * 1024**2, "tasks": 16}
needs_manager = pytest.mark.needs("systemd_user", "cgroup")


class _Manager:
    """The user manager as the reconcile step sees it: each slice by unit, with
    or without a process, and each stop it was asked for."""

    populated: ClassVar[dict[str, bool]] = {}
    #: When systemd made each slice's group; an hour and more ago by default.
    made: ClassVar[dict[str, float]] = {}
    stopped: ClassVar[list[str]] = []

    def __init__(self, *, memory_bytes: int, tasks: int, unit: str) -> None:
        self.unit = unit
        self.path = Path("/sys/fs/cgroup/fake") / unit
        _Manager.populated[unit] = False

    @classmethod
    def idle_units(cls) -> dict[str, float]:
        return {
            unit: cls.made.get(unit, 0.0)
            for unit, busy in cls.populated.items()
            if not busy
        }

    @classmethod
    def recorded(cls, unit: str) -> _Manager:
        scope = object.__new__(cls)
        scope.unit, scope.path = unit, Path("/sys/fs/cgroup/fake") / unit
        return scope

    def is_stopped(self) -> bool:
        return self.unit not in _Manager.populated

    def close(self) -> None:
        _Manager.stopped.append(self.unit)
        _Manager.populated.pop(self.unit, None)


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_Manager]]:
    _Manager.populated, _Manager.made, _Manager.stopped = {}, {}, []
    monkeypatch.setattr(supervisor, "AggregateScope", _Manager)
    yield _Manager


def _end(registry: Registry, run_id: str, phase: RunPhase) -> None:
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=phase, updated_at=2))


# ---------------------------------------------------------------------------
# The reconcile step.
# ---------------------------------------------------------------------------


def test_a_writer_whose_driver_died_leaves_no_slice_after_reconcile(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """The driver died with its slice open and its worktree fenced.

    A final phase lets the reconcile step release the tree.
    """
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    writer = sv._open_writer(run_id)
    sv._writers.clear()  # what a dead driver leaves: no process holds the scope
    _end(registry, run_id, RunPhase.COMPLETED)
    registry.poison_claim(str(tmp_path), run_id)

    outcomes = sv.reconcile_slices()

    assert manager.stopped == [writer.unit]
    assert [(o.unit, o.run_id, o.action, o.claim_released) for o in outcomes] == [
        (writer.unit, run_id, "stopped", True)
    ]
    assert registry.read_claim(str(tmp_path)) is None


def test_a_slice_whose_start_failed_is_still_its_runs_and_is_stopped(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """A start that systemd ran after its caller stopped waiting leaves a slice
    nobody recorded. Its name still says which run it was for."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    _end(registry, run_id, RunPhase.FAILED)
    unit = custody.writer_unit(run_id)
    manager.populated[unit] = False

    outcomes = sv.reconcile_slices()

    assert manager.stopped == [unit]
    assert [(o.run_id, o.phase, o.action) for o in outcomes] == [
        (run_id, "failed", "stopped")
    ]


def test_a_slice_of_a_live_run_is_never_stopped(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """An empty slice of a run that has not ended, and a slice that holds a
    process, stay. The sweep that also judges unknown slices stops neither."""
    sv = Supervisor(registry, tmp_path / "runs")
    live, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path / "a"))
    ended, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path / "b"))
    live_unit = sv._open_writer(live).unit
    busy_unit = sv._open_writer(ended).unit
    manager.populated[busy_unit] = True
    _end(registry, ended, RunPhase.COMPLETED)

    outcomes = sv.reconcile_slices(unknown=True)

    assert manager.stopped == []
    assert [(o.unit, o.phase, o.action) for o in outcomes] == [
        (live_unit, "spawning", "kept")
    ]


def test_only_the_sweep_stops_a_slice_no_run_of_this_store_names(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """A test store and the live store share one user manager: the automatic
    step judges only the slices of its own store's runs. The sweep, which an
    operator runs against the live store, also stops the empty unknown ones,
    and its dry run stops nothing."""
    sv = Supervisor(registry, tmp_path / "runs")
    foreign = f"nottcontroller-{uuid.uuid4().hex}.slice"
    manager.populated[foreign] = False

    assert sv.reconcile_slices() == ()
    dry = sv.reconcile_slices(unknown=True, dry_run=True)
    assert [(o.unit, o.run_id, o.action) for o in dry] == [
        (foreign, None, "would_stop")
    ]
    assert manager.stopped == []

    swept = sv.reconcile_slices(unknown=True)

    assert [(o.unit, o.action) for o in swept] == [(foreign, "stopped")]
    assert manager.stopped == [foreign]


def test_a_refused_stop_is_reported_and_keeps_the_claim(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    unit = sv._open_writer(run_id).unit
    _end(registry, run_id, RunPhase.COMPLETED)
    registry.poison_claim(str(tmp_path), run_id)

    def refuse(self: _Manager) -> None:
        raise ControllerRefusal("aggregate systemd operation unavailable: stop x")

    monkeypatch.setattr(_Manager, "close", refuse)

    (outcome,) = sv.reconcile_slices()

    assert (outcome.unit, outcome.action, outcome.claim_released) == (
        unit,
        "refused",
        False,
    )
    assert outcome.detail is not None and "stop x" in outcome.detail
    claim = registry.read_claim(str(tmp_path))
    assert claim is not None and claim.lease_until == POISONED_LEASE_UNTIL


def test_a_serving_supervisor_reconciles_its_own_ended_runs_on_its_own(
    registry: Registry,
    tmp_path: Path,
    manager: type[_Manager],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nobody calls the step: the supervisor's own loop finds the slice."""
    monkeypatch.setattr(Supervisor, "RECONCILE_INTERVAL_S", 0.02)
    sv = Supervisor(registry, tmp_path / "runs", reconciles=True)
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    with sv._lock:
        unit = sv._open_writer(run_id).unit
        _end(registry, run_id, RunPhase.FAILED)
    deadline = time.monotonic() + 5
    while unit not in manager.stopped and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.stopped == [unit]


def test_the_sweep_keeps_a_young_slice_no_run_of_this_store_names(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """Another store's run (a test's) can hold an empty slice between its start
    and its launch: the sweep keeps a slice it cannot name until it is old."""
    sv = Supervisor(registry, tmp_path / "runs")
    young = f"nottcontroller-{uuid.uuid4().hex}.slice"
    manager.populated[young] = False
    manager.made[young] = time.time() - 60

    (outcome,) = sv.reconcile_slices(unknown=True)

    assert (outcome.unit, outcome.action) == (young, "kept")
    assert outcome.detail is not None and "younger than 3600 s" in outcome.detail
    assert manager.stopped == []


def test_the_sweep_finds_a_slice_by_its_record(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """A slice made before slices carried their run's name is found by the
    record of its run; the automatic pass leaves it to the sweep."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    legacy = f"nottcontroller-{uuid.uuid4().hex}.slice"
    manager.populated[legacy] = False
    registry.append_run_event(
        run_id,
        at=2,
        kind="lifecycle",
        payload={"writer_scope": {"unit": legacy, "cgroup": f"/sys/{legacy}"}},
    )
    _end(registry, run_id, RunPhase.FAILED)

    assert sv.reconcile_slices() == ()
    (outcome,) = sv.reconcile_slices(unknown=True)

    assert (outcome.unit, outcome.run_id, outcome.action) == (
        legacy,
        run_id,
        "stopped",
    )


def test_a_bare_supervisor_starts_no_reconcile(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    """Only the door that serves reconciles: a test's supervisor never reaches
    the user manager on its own."""
    sv = Supervisor(registry, tmp_path / "runs")

    assert sv._reconcile_thread is None


def test_the_run_phases_of_many_runs_are_read_at_once(
    registry: Registry, tmp_path: Path
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    absent = [str(uuid.uuid4()) for _ in range(700)]

    phases = registry.run_phases([*absent, run_id, run_id])

    assert phases == {run_id: (RunPhase.SPAWNING, str(tmp_path))}


class _Slice:
    """A writer slice whose group is a directory the test writes."""

    def __init__(self, group: Path, populated: bool) -> None:
        group.mkdir()
        (group / "cgroup.events").write_text(f"populated {int(populated)}\nfrozen 0\n")
        self.unit, self.path, self.closed = group.name, group, 0

    def close(self) -> None:
        self.closed += 1


@pytest.mark.parametrize("populated", [False, True])
def test_a_command_that_exited_stops_its_empty_slice(
    populated: bool, registry: Registry, tmp_path: Path
) -> None:
    """A command session's run ends with its shell, and its close may never
    come: its empty slice stops then. A job the shell left behind keeps the
    slice; the reconcile step stops it once the job ends."""
    sv = Supervisor(registry, tmp_path / "runs")
    writer = _Slice(tmp_path / "nottcontroller-x.slice", populated)
    sv._writers["run"] = writer  # type: ignore[assignment]

    sv._stop_idle_writer("run")

    assert writer.closed == (0 if populated else 1)


def test_a_refused_stop_of_an_exited_command_is_reported(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    writer = _Slice(tmp_path / "nottcontroller-y.slice", False)

    def refuse() -> None:
        raise ControllerRefusal("aggregate systemd operation unavailable: stop y")

    monkeypatch.setattr(writer, "close", refuse)
    sv._writers["run"] = writer  # type: ignore[assignment]

    sv._stop_idle_writer("run")

    assert (
        "writer slice nottcontroller-y.slice of run run was not stopped: "
        "aggregate systemd operation unavailable: stop y"
    ) in capsys.readouterr().err


def test_a_supervisor_that_shuts_down_stops_the_slices_it_holds(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Its sessions and commands die with the process: their slices stop with
    it, and one that refuses is named and does not hold back the others."""
    sv = Supervisor(registry, tmp_path / "runs")
    held = _Slice(tmp_path / "nottcontroller-held.slice", False)
    refusing = _Slice(tmp_path / "nottcontroller-refusing.slice", False)

    def refuse() -> None:
        raise ControllerRefusal("aggregate systemd operation unavailable: stop")

    monkeypatch.setattr(refusing, "close", refuse)
    sv._writers.update({"refusing": refusing, "held": held})  # type: ignore[dict-item]

    sv.shutdown()

    assert held.closed == 1
    assert (
        "writer slice nottcontroller-refusing.slice of run refusing was not "
        "stopped: aggregate systemd operation unavailable: stop"
    ) in capsys.readouterr().err


@needs_manager
def test_a_real_slice_of_a_dead_driver_is_gone_after_reconcile(
    registry: Registry, tmp_path: Path
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    writer = sv._open_writer(run_id)
    try:
        assert writer.path is not None and writer.path.exists()
        sv._writers.clear()
        _end(registry, run_id, RunPhase.COMPLETED)

        outcomes = sv.reconcile_slices()

        assert [(o.unit, o.action) for o in outcomes if o.run_id == run_id] == [
            (writer.unit, "stopped")
        ]
        assert not writer.path.exists()
    finally:
        writer.close()


# ---------------------------------------------------------------------------
# A close after the slice is gone.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("gone", ["before", "kill", "events"])
def test_a_close_after_the_slice_is_gone_succeeds(
    gone: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process stopped the slice before the close began (`before`, what
    the reconcile step does), between the close's look and its kill (`kill`),
    or between its kill and its read of the group (`events`). systemd removes
    the group of a stopped slice and answers each later call on its unit with
    "not loaded": the slice is stopped, which is what the close asks for."""
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    unit = f"nottcontroller-{uuid.uuid4().hex}.slice"
    scope = AggregateScope.recorded(unit)
    scope.path = tmp_path / unit
    if gone != "before":
        scope.path.mkdir()
    calls: list[str] = []

    def answer(self: AggregateScope, argv: list[str]) -> str:
        calls.append(argv[2])
        assert self.path is not None
        if argv[2] == "show":
            return "LoadState=not-found\nActiveState=inactive\nJob=\n"
        if argv[2] == "kill" and gone == "events":
            self.path.rmdir()
            return ""
        if argv[2] == "kill" and gone == "kill":
            self.path.rmdir()
        if not self.path.exists():
            raise ControllerRefusal(
                f"aggregate systemd operation refused: systemctl {argv[2]} {unit}: "
                f"exit 5: Failed to {argv[2]} unit {unit}: Unit {unit} not loaded."
            )
        return ""

    monkeypatch.setattr(AggregateScope, "_call", answer)

    scope.close()
    scope.close()

    assert (
        calls
        == {
            "before": ["stop", "show"],
            "kill": ["kill", "show", "stop", "show"],
            "events": ["kill", "stop", "show"],
        }[gone]
    )


def test_a_refused_stop_of_a_slice_still_there_is_still_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    unit = f"nottcontroller-{uuid.uuid4().hex}.slice"
    scope = AggregateScope.recorded(unit)
    scope.path = tmp_path / unit
    scope.path.mkdir()

    def answer(self: AggregateScope, argv: list[str]) -> str:
        if argv[2] == "stop":
            raise ControllerRefusal("aggregate systemd operation unavailable: stop")
        return ""

    monkeypatch.setattr(AggregateScope, "_call", answer)

    with pytest.raises(ControllerRefusal, match="unavailable: stop"):
        scope.close()
    with pytest.raises(ControllerRefusal, match="unavailable: stop"):
        scope.close()  # not closed: a second close asks again


def test_a_refused_kill_of_a_slice_still_there_is_still_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    unit = f"nottcontroller-{uuid.uuid4().hex}.slice"
    scope = AggregateScope.recorded(unit)
    scope.path = tmp_path / unit
    scope.path.mkdir()

    def answer(self: AggregateScope, argv: list[str]) -> str:
        if argv[2] == "kill":
            raise ControllerRefusal("aggregate systemd operation unavailable: kill")
        return ""

    monkeypatch.setattr(AggregateScope, "_call", answer)

    with pytest.raises(ControllerRefusal, match="unavailable: kill"):
        scope.close()


@needs_manager
def test_a_close_of_a_real_slice_stopped_elsewhere_succeeds(
    registry: Registry, tmp_path: Path
) -> None:
    """Another process stopped the slice first. The run's own close still
    ends the run and gives its tree back."""
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))
    writer = sv._open_writer(run_id)
    subprocess.run(
        ["systemctl", "--user", "stop", writer.unit],
        env=custody.user_manager_environment(),
        check=True,
        timeout=30,
    )

    readback = sv.close(run_id)

    assert (readback.phase, readback.claim_released) == (RunPhase.CLOSED, True)


# ---------------------------------------------------------------------------
# A refusal names its cause.
# ---------------------------------------------------------------------------


def test_a_systemd_call_without_an_answer_names_the_call_and_the_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def silent(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(custody.subprocess, "run", silent)
    unit = f"nottcontroller-{uuid.uuid4().hex}.slice"

    with pytest.raises(ControllerRefusal) as refused:
        custody._systemd_call({}, ["/usr/bin/systemctl", "--user", "stop", unit])

    assert str(refused.value) == (
        f"aggregate systemd operation unavailable: systemctl stop {unit}: "
        "no answer within 5 s"
    )


def test_a_refused_systemd_call_names_the_call_and_its_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refused(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "Access denied\n")

    monkeypatch.setattr(custody.subprocess, "run", refused)

    with pytest.raises(ControllerRefusal) as refusal:
        custody._systemd_call({}, list(custody.WEIGH_CUSTODY_PARENT))

    assert str(refusal.value) == (
        "aggregate systemd operation refused: busctl SetUnitProperties "
        "nottcontroller.slice: exit 1: Access denied"
    )


def test_a_failed_start_still_stops_the_slice_it_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """systemd can run a start after its caller stopped waiting. The scope
    asks for the stop of the same unit, and the refusal names both calls."""
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    run_id = str(uuid.uuid4())
    calls: list[list[str]] = []

    def manager(self: AggregateScope, argv: list[str]) -> str:
        calls.append(argv)
        if "StartTransientUnit" in argv:
            raise ControllerRefusal("start: no answer within 5 s")
        if argv[2] == "stop":
            raise ControllerRefusal("stop: no answer within 5 s")
        return ""

    monkeypatch.setattr(AggregateScope, "_call", manager)

    with pytest.raises(ControllerRefusal) as refused:
        AggregateScope(**LIMITS, unit=custody.writer_unit(run_id))

    assert str(refused.value) == (
        "start: no answer within 5 s; stop: no answer within 5 s"
    )
    assert calls[2][2:] == ["stop", f"nottcontroller-{uuid.UUID(run_id).hex}.slice"]


def test_the_writer_slice_is_named_for_its_run(
    registry: Registry, tmp_path: Path, manager: type[_Manager]
) -> None:
    sv = Supervisor(registry, tmp_path / "runs")
    run_id, _ = _admit_pending(sv, registry, tmp_path, worktree=str(tmp_path))

    writer = sv._open_writer(run_id)

    assert writer.unit == custody.writer_unit(run_id)
    assert custody.writer_run(writer.unit) == run_id
    assert registry.writer_scope(run_id) == (writer.unit, str(writer.path))


# ---------------------------------------------------------------------------
# The operator's sweep: `nspawn slice reconcile`.
# ---------------------------------------------------------------------------


class _Sweeping:
    """The supervisor `nspawn slice reconcile` builds, as far as the verb reads it."""

    def __init__(self, outcomes: tuple[supervisor.SliceReconcile, ...]) -> None:
        self.outcomes, self.asked, self.down = outcomes, [], False
        self._registry = self

    def reconcile_slices(
        self, *, unknown: bool, dry_run: bool
    ) -> tuple[supervisor.SliceReconcile, ...]:
        self.asked.append((unknown, dry_run))
        return self.outcomes

    def shutdown(self) -> None:
        self.down = True

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    ("arguments", "outcomes", "code"),
    [
        (
            ["--dry-run"],
            (supervisor.SliceReconcile("a", "r", "failed", "would_stop"),),
            0,
        ),
        (
            [],
            (supervisor.SliceReconcile("a", "r", "orphaned", "stopped", None, True),),
            0,
        ),
        (
            [],
            (supervisor.SliceReconcile("a", None, None, "refused", "stop a: exit 1"),),
            1,
        ),
    ],
)
def test_the_sweep_command_reports_each_slice(
    arguments: list[str],
    outcomes: tuple[supervisor.SliceReconcile, ...],
    code: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from nspawn import cli, mcp

    sweeping = _Sweeping(outcomes)
    monkeypatch.setattr(mcp, "_default_supervisor", lambda: sweeping)

    assert cli.main(["slice", "reconcile", *arguments]) == code

    report = json.loads(capsys.readouterr().out)
    (outcome,) = outcomes
    assert sweeping.asked == [(True, bool(arguments))] and sweeping.down
    assert report["dry_run"] is bool(arguments)
    assert report["counts"] == {outcome.action: 1}
    (entry,) = report[outcome.action]
    assert entry["unit"] == outcome.unit and entry.get("detail") == outcome.detail
    assert entry.get("claim_released", False) is outcome.claim_released


def test_the_sweep_command_takes_only_its_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from nspawn import cli

    assert cli.main(["slice", "reconcile", "--all"]) == 2
    assert "usage: slice reconcile [--dry-run]" in capsys.readouterr().err


def test_a_native_session_whose_slice_refuses_still_closes_its_doors(
    tmp_path: Path,
) -> None:
    """The refusal is raised after the session's own doors close: the reconcile
    step stops the slice later, and nothing else of the session is left."""
    import threading

    from nspawn import session

    closed: list[str] = []

    class Door:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            closed.append(self.name)

    class Refusing:
        unit = "nottcontroller-refusing.slice"

        def close(self) -> None:
            raise ControllerRefusal("aggregate systemd operation unavailable: stop")

    class Spawned:
        def wait(self, timeout: float) -> int:
            closed.append("waited")
            return 0

    managed = object.__new__(session.NativeSession)
    managed.stopped, managed.peer = threading.Event(), None
    managed.scope = Refusing()  # type: ignore[assignment]
    managed.spawned = Spawned()  # type: ignore[assignment]
    managed.output, managed.control = Door("output"), Door("control")  # type: ignore[assignment]
    managed.threads, managed.root = [], tmp_path / "nssp"
    managed.root.mkdir()
    (managed.root / "output.sock").write_text("")

    with pytest.raises(ControllerRefusal, match="unavailable: stop"):
        managed.close()

    assert closed == ["output", "control"]  # never waits on a child it could not stop
    assert managed.stopped.is_set() and not managed.root.exists()

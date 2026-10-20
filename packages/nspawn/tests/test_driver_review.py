"""U1 review findings (FIX-FIRST, 2026-09-23), each pinned before its cure.

1. a stdio whose cwd was deleted refuses every dispatch (make_frame's getcwd)
2. the close tolerance of a child dying between the identity check and the
   signal is caught deterministically, not in 5 of 8 runs
5. the close watcher dies on any failure but psycopg's, and every close after
   it waits out its 45 s as ClosePending
6. a batch entry whose drivers dir or log cannot open raises out of the whole
   batch, losing the admitted entries' answers and leaking their ack fds
7. nothing prunes runs/drivers files, and the launcher's thread list only grows
8. the project key must be git's own answer: the realpath of the common dir,
   GIT_DIR/GIT_COMMON_DIR included, and the drain keys through the same function
9. the frame carries the canonical launch_uuid its files are named by
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from class_dispatch import DECLARED_CLASS, system1_never_reaches_the_network
from mcp_fakes import FakeSupervisor
from registry_reset import sql
from supervisor_support import (
    _drain_worker_threads,
    _stop_every_reaper,
    registry,
    roster_home,
)
from test_driver import (
    Drivers,
    Reaper,
    _call,
    _foreign_run,
    _now_ms,
    _payload,
    _stdio,
    _stub_launcher,
    drv,
    reaper,
)

from nspawn import child, supervisor
from nspawn.cli import main
from nspawn.mcp import McpServer
from nspawn.process_identity import capture_process_identity
from nspawn.registry import Registry
from nspawn.rows import RunPhase
from nspawn.supervisor import DriveCall, RunAccepted, Supervisor

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "drv",
    "reaper",
    "registry",
    "roster_home",
    "system1_never_reaches_the_network",
]


def _git(cwd: Path, *arguments: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            **(os.environ if env is None else env),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout.strip()


def _repository(root: Path) -> tuple[Path, Path]:
    repository = root / "repository"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "commit", "-q", "--allow-empty", "-m", "root")
    lane = root / "lane"
    _git(repository, "worktree", "add", "-q", str(lane), "-b", "lane/x")
    return repository, lane


def _gits_key(cwd: Path) -> str:
    return os.path.realpath(
        _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    )


# -- 1 -------------------------------------------------------------------------


def test_a_dispatch_from_a_deleted_cwd_is_keyed_by_the_calls_own_cwd(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gone, work = tmp_path / "gone", tmp_path / "work"
    gone.mkdir()
    work.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    frame = drv.make_frame(
        "nspawn_run_start", {"prompt": "p", "cwd": str(work)}, drivers_dir=None
    )
    assert frame["project"] == drv.project_key(str(work))
    server = McpServer(supervisor=FakeSupervisor(execute_result=RunAccepted("run-x")))
    answer = _call(
        server,
        "nspawn_run_start",
        {"prompt": "p", "cwd": str(work), "class": DECLARED_CLASS},
    )
    assert _payload(answer)["run_id"] == "run-x"


# -- 2 -------------------------------------------------------------------------


def test_a_child_dying_between_the_identity_check_and_the_signal_is_closed(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forced, not raced: the check sees the child alive, the signal finds it
    gone (ChildIdentityError), and a dead identity is the close done."""
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    identity = capture_process_identity(gone.pid)
    gone.wait()
    run_id = _foreign_run(registry, tmp_path, claim=False)
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.RUNNING, process_identity=identity))

    class Vanished:
        def __init__(self) -> None:
            self.identity = identity

        def stop_then_kill(self, expected: object) -> None:
            raise child.ChildIdentityError("the group vanished before the signal")

    real = supervisor.process_identity_matches
    checks: list[object] = []

    def flipping(candidate: Any) -> bool:
        checks.append(candidate)
        return True if len(checks) == 1 else real(candidate)

    monkeypatch.setattr(supervisor, "process_identity_matches", flipping)
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        sv._children[run_id] = Vanished()  # type: ignore[assignment]
        readback = sv.close_local(run_id)
    finally:
        sv.shutdown()
    assert readback.phase is RunPhase.CLOSED
    assert len(checks) >= 2, "the tolerance's re-check was never reached"


# -- 5 -------------------------------------------------------------------------


def test_the_close_watcher_survives_any_failure_and_records_a_refusal(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    reaper: Reaper,
) -> None:
    drivers = Drivers(tmp_path / "runs")
    reaper.drivers.append(drivers)
    built = drivers()
    run_id = _foreign_run(built._registry, tmp_path, claim=False)

    def broken(run_id: str) -> object:
        raise OSError(5, "the run root is unreadable")

    built.close_local = broken  # type: ignore[method-assign,assignment]
    watcher = drv._CloseWatcher(
        built, DriveCall(launch_uuid=str(uuid.uuid4()), run_id=run_id)
    )
    stdio = Supervisor(registry, tmp_path / "stdio-runs")
    watcher.start()
    try:
        for _ in range(2):  # the second proves the loop outlived the first
            with pytest.raises(supervisor.RunCloseRejected):
                stdio.close(run_id)
            outcome = registry.close_outcome(run_id)
            assert outcome is not None, "no outcome recorded"
            assert outcome["refusal"]["type"] == "OSError", outcome
        alive = watcher._thread.is_alive()
    finally:
        watcher.finish()
        stdio.shutdown()
    assert alive, "the watcher thread died on a non-database failure"


# -- 6 -------------------------------------------------------------------------


def test_a_batch_entry_whose_log_cannot_open_is_refused_alone(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_launcher(drv, tmp_path, monkeypatch, "ack")
    launcher = drv.SystemdLauncher()
    arguments = {
        "prompt": "p",
        "cwd": str(tmp_path),
        "execution_class": DECLARED_CLASS,
        "read_only": True,
    }
    good = drv.make_frame(
        "nspawn_run_start", arguments, drivers_dir=tmp_path / "runs" / "drivers"
    )
    blocked = tmp_path / "blocked"
    blocked.write_text("")
    bad = drv.make_frame("nspawn_run_start", arguments, drivers_dir=blocked / "drivers")
    stdio = _stdio(registry, tmp_path)
    fds = len(os.listdir("/dev/fd"))
    answers = launcher.dispatch([good, bad], stdio)
    for thread in launcher.threads:
        thread.join(timeout=10)
    assert answers[0]["run_id"] == "stub-run"
    assert answers[1]["refused"]["type"] == "LaunchFailed"
    assert len(os.listdir("/dev/fd")) <= fds, "a launch leaked a descriptor"


# -- 7 -------------------------------------------------------------------------


def test_finished_reaper_threads_are_dropped(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_launcher(drv, tmp_path, monkeypatch, "ack")
    launcher = drv.SystemdLauncher()
    stdio = _stdio(registry, tmp_path)
    arguments = {"prompt": "p", "cwd": str(tmp_path), "read_only": True}
    for _ in range(3):
        launcher.dispatch(
            [
                drv.make_frame(
                    "nspawn_run_start", arguments, drivers_dir=tmp_path / "runs/drivers"
                )
            ],
            stdio,
        )
        for thread in launcher.threads:
            thread.join(timeout=10)
    assert len(launcher.threads) <= 1


def test_the_tick_prunes_old_driver_files_but_never_a_live_drivers(
    registry: Registry, tmp_path: Path
) -> None:
    directory = tmp_path / "runs" / "drivers"
    directory.mkdir(parents=True)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    dead = capture_process_identity(gone.pid)
    gone.wait()
    live = capture_process_identity(os.getpid())
    old = time.time() - Supervisor.RETAIN_CLOSED_MS / 1000 - 3600

    def launch(identity: Any, *, aged: bool) -> str:
        launch_uuid = str(uuid.uuid4())
        record = {
            "pid": identity.pid,
            "boot_id": identity.boot_id,
            "start_ticks": identity.start_ticks,
        }
        for suffix, body in (
            (".pid", json.dumps(record)),
            (".log", "log"),
            (".ack", json.dumps({"run_id": "r"})),
        ):
            path = directory / f"{launch_uuid}{suffix}"
            path.write_text(body)
            if aged:
                os.utime(path, (old, old))
        return launch_uuid

    stale = launch(dead, aged=True)
    running = launch(live, aged=True)
    fresh = launch(dead, aged=False)
    sv = Supervisor(registry, tmp_path / "runs")
    try:
        registry.heartbeat_instance(_now_ms())
        sv._tick()
    finally:
        sv.shutdown()
    left = {path.name.split(".")[0] for path in directory.iterdir()}
    assert stale not in left
    assert {running, fresh} <= left


# -- 8 -------------------------------------------------------------------------


def test_the_project_key_is_gits_own_common_dir_realpath(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, lane = _repository(tmp_path)
    (repository / "sub").mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    _git(linked, "init", "-q", "-b", "main")
    store = tmp_path / "store"
    store.mkdir()
    (linked / ".git").rename(store / "dot-git")
    (linked / ".git").symlink_to(store / "dot-git")
    for cwd in (repository, repository / "sub", lane, linked):
        with monkeypatch.context() as scoped:
            scoped.delenv("GIT_DIR", raising=False)
            scoped.delenv("GIT_COMMON_DIR", raising=False)
            assert drv.project_key(str(cwd)) == _gits_key(cwd), cwd
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    gitdir = _git(lane, "rev-parse", "--path-format=absolute", "--git-dir")
    monkeypatch.setenv("GIT_DIR", gitdir)
    assert drv.project_key(str(elsewhere)) == _gits_key(elsewhere)
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.setenv("GIT_DIR", str(repository / ".git"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(store / "dot-git"))
    assert drv.project_key(str(elsewhere)) == _gits_key(elsewhere)


def test_the_drain_keys_a_directory_through_the_same_function(
    drv: ModuleType,
    registry: Registry,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository, lane = _repository(tmp_path)
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    ticks = capture_process_identity(gone.pid).start_ticks
    gone.wait()
    registry.heartbeat_instance(_now_ms())
    run_id = _foreign_run(registry, lane, claim=False)
    sql(
        registry,
        "INSERT INTO notices(run_id, caller_addr, caller_pid, "
        "caller_start_ticks, caller_project, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            run_id,
            "/nonexistent.sock",
            gone.pid,
            ticks,
            drv.project_key(str(lane)),
            _now_ms(),
        ),
    )
    row = registry.get_run(run_id)
    assert row is not None
    registry.update_run(replace(row, phase=RunPhase.FAILED, updated_at=_now_ms()))
    capsys.readouterr()
    assert main(["notice", "drain", "--project", str(repository / "sub")]) == 0
    assert f"{run_id} is failed" in capsys.readouterr().out


# -- 9 -------------------------------------------------------------------------


def test_the_frame_carries_the_canonical_launch_uuid_its_files_are_named_by(
    drv: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_root = tmp_path / "runs"
    monkeypatch.setenv("NOTT_NSPAWN_RUN_ROOT", str(run_root))
    seen: dict[str, Any] = {}

    def serve(frame: dict[str, Any], writer: Any, **kwargs: Any) -> dict[str, Any]:
        seen["frame"] = frame
        return {}

    monkeypatch.setattr(drv, "serve", serve)
    canonical = str(uuid.uuid4())
    frame = drv.make_frame(
        "nspawn_run_start", {"prompt": "p", "cwd": str(tmp_path)}, drivers_dir=None
    )
    frame["launch_uuid"] = "{" + canonical.upper() + "}"
    reader, writer = os.pipe()
    os.write(writer, json.dumps(frame).encode())
    os.close(writer)
    saved = [os.dup(n) for n in (0, 1, 2)]
    try:
        os.dup2(reader, 0)
        os.close(reader)
        assert drv.drive_main([]) == 0
    finally:
        for number, copy in enumerate(saved):
            os.dup2(copy, number)
            os.close(copy)
    assert seen["frame"]["launch_uuid"] == canonical
    assert (run_root / "drivers" / f"{canonical}.log").exists()

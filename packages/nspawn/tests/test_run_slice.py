from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from supervisor_support import (
    _admit_pending,
    _classify_by_exit_code,
    _drain_worker_threads,
    _stop_every_reaper,
    _wire_l8_and_l3,
    fake_picked,
    fake_provider,
    registry,
    roster_home,
)

from nspawn import cli, custody, mcp, supervisor
from nspawn.owner_roster import Roster
from nspawn.process_identity import ProcessIdentity
from nspawn.rows import RunPhase

__all__ = [
    "_drain_worker_threads",
    "_stop_every_reaper",
    "fake_picked",
    "fake_provider",
    "registry",
    "roster_home",
]


def _expected_unit(run_id: str) -> str:
    return f"nottcontroller-{uuid.UUID(run_id).hex}.slice"


@pytest.fixture
def run_slice(registry, tmp_path, roster_home, fake_picked, monkeypatch):
    """Use a fake manager and child. Start no process or systemd unit."""
    root = tmp_path / "cgroup"
    starts = {}
    stopped = []
    scopes = []
    launches = []
    alive = {}

    monkeypatch.setattr(custody, "CGROUP_ROOT", root)
    monkeypatch.setattr(custody, "user_manager_environment", dict)

    def coordinates(unit):
        relative = (
            f"/user.slice/user-{os.geteuid()}.slice/"
            f"user@{os.geteuid()}.service/nottcontroller.slice/{unit}"
        )
        return relative, root / relative.lstrip("/")

    def manager_call(scope, argv):
        if scope not in scopes:
            scopes.append(scope)
        if "SetUnitProperties" in argv:
            return ""
        if "StartTransientUnit" in argv:
            index = argv.index("ssa(sv)a(sa(sv))")
            unit = argv[index + 1]
            count = int(argv[index + 3])
            offset = index + 4
            properties = {
                argv[offset + 3 * i]: argv[offset + 3 * i + 2] for i in range(count)
            }
            starts[unit] = properties
            _, group = coordinates(unit)
            group.mkdir(parents=True)
            for name, value in (
                ("memory.max", properties["MemoryMax"]),
                ("memory.swap.max", properties["MemorySwapMax"]),
                ("pids.max", properties["TasksMax"]),
                ("cgroup.events", "populated 0\nfrozen 0"),
            ):
                (group / name).write_text(f"{value}\n", encoding="utf-8")
            return 'o "/org/freedesktop/systemd1/job/1"'

        relative, group = coordinates(scope.unit)
        if argv[2] == "show":
            if "--property=ControlGroup" in argv:
                return relative if group.exists() else ""
            if not group.exists():
                return "LoadState=not-found\nActiveState=inactive\nJob=\n"
            return (
                "LoadState=loaded\nActiveState=active\nSubState=active\n"
                "Job=\nStopWhenUnneeded=no\n"
            )
        if argv[2] == "kill":
            assert "--kill-whom=all" in argv
            assert "--signal=SIGKILL" in argv
            for launch in launches:
                if launch.unit == scope.unit:
                    alive[launch.run_id].clear()
            if group.exists():
                (group / "cgroup.events").write_text(
                    "populated 0\nfrozen 0\n", encoding="utf-8"
                )
            return ""
        if argv[2] == "stop":
            stopped.append(scope.unit)
            if group.exists():
                shutil.rmtree(group)
            return ""
        pytest.fail(f"unexpected fake manager call: {argv}")

    monkeypatch.setattr(custody.AggregateScope, "_call", manager_call)

    class ExitedChild:
        def __init__(self, ordinal):
            self.identity = ProcessIdentity(
                pid=424_242 + ordinal,
                boot_id="run-slice-fixture",
                start_ticks=ordinal + 1,
            )

        def write_stdin(self, payload, timeout):
            pass

        def close_stdin(self):
            pass

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def spawn(argv, cwd, env, run_dir, needles, **kwargs):
        run_id = Path(run_dir).name
        scope = kwargs.get("scope")
        unit = None if scope is None else scope.unit
        command = list(argv) if scope is None else scope.command(list(argv))
        launches.append(
            SimpleNamespace(
                run_id=run_id,
                unit=unit,
                command=command,
                recorded=registry.writer_scope(run_id),
            )
        )
        # The leader exits. A detached descendant stays until an aggregate kill.
        alive[run_id] = {"detached-descendant"}
        if scope is not None:
            (scope.path / "cgroup.events").write_text(
                "populated 1\nfrozen 0\n", encoding="utf-8"
            )
        return ExitedChild(len(launches))

    _wire_l8_and_l3(monkeypatch, spawn)
    monkeypatch.setattr(
        supervisor.terminal, "classify_terminal_run", _classify_by_exit_code
    )
    monkeypatch.setattr(supervisor, "process_identity_matches", lambda identity: False)

    sv = supervisor.Supervisor(registry, tmp_path / "runs")
    roster = Roster.read(Roster.default_path())

    def start():
        cwd = tmp_path / f"work-{len(launches)}"
        cwd.mkdir()
        run_id, thread_id = _admit_pending(
            sv, registry, tmp_path, worktree=str(cwd), claim=False
        )
        run_dir = supervisor.RunDirectory(
            root=sv._run_root_path / run_id,
            stdout=sv._run_root_path / run_id / "stdout",
            stderr=sv._run_root_path / run_id / "stderr",
        )
        # Use the uncaged worker selected by nspawn_run_start.
        sv._drive_run(
            run_id,
            str(cwd),
            run_dir,
            "Return PONG.",
            roster,
            fake_picked,
            thread_id,
            False,
            False,
        )
        assert len(launches) > 0
        assert launches[-1].run_id == run_id
        assert sv.status(run_id).phase is RunPhase.COMPLETED
        return launches[-1]

    try:
        yield SimpleNamespace(
            start=start,
            supervisor=sv,
            starts=starts,
            stopped=stopped,
            alive=alive,
        )
    finally:
        sv.shutdown()
        for scope in scopes:
            scope.close()


def test_uncaged_runs_start_in_distinct_limited_slices(run_slice):
    first = run_slice.start()
    second = run_slice.start()
    expected = [_expected_unit(first.run_id), _expected_unit(second.run_id)]

    assert [first.unit, second.unit] == expected
    assert first.unit != second.unit
    for launch in (first, second):
        properties = run_slice.starts[launch.unit]
        assert {
            name: properties[name]
            for name in ("MemoryMax", "MemorySwapMax", "TasksMax")
        } == {
            "MemoryMax": str(8 * 1024**3),
            "MemorySwapMax": "0",
            "TasksMax": "4096",
        }
        assert f"--slice={launch.unit}" in launch.command
        assert launch.recorded is not None
        assert launch.recorded[0] == launch.unit


def test_run_show_reports_the_uncaged_runs_slice(run_slice, monkeypatch, capsys):
    launch = run_slice.start()
    server = mcp.McpServer(supervisor=run_slice.supervisor)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def call(self, tool, arguments):
            assert tool == "nspawn_run_show"
            return server.nspawn_run_show(**arguments)

    monkeypatch.setattr(cli, "NspawnClient", Client)

    assert cli.main(["run", "show", launch.run_id]) == 0

    shown = json.loads(capsys.readouterr().out)
    scope = shown["observation"]["writer_scope"]
    assert scope is not None, shown["observation"]
    assert scope["unit"] == _expected_unit(launch.run_id)


def test_close_stops_the_uncaged_runs_slice_and_detached_descendant(run_slice):
    launch = run_slice.start()
    unit = _expected_unit(launch.run_id)
    server = mcp.McpServer(supervisor=run_slice.supervisor)
    assert run_slice.alive[launch.run_id] == {"detached-descendant"}

    readback = server.nspawn_run_close(run_id=launch.run_id)

    assert readback.phase is RunPhase.CLOSED
    assert readback.tree.survivors == ()
    assert (
        unit in run_slice.stopped,
        sorted(run_slice.alive[launch.run_id]),
    ) == (True, [])

"""Native aggregate cgroup custody, including descendants and shared limits.

Carried from libs/gate's resource tests when nSPAWN took ownership of its
custody primitives. The refusals that need no user manager run anywhere; every
other test measures a real transient slice under the user manager.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nspawn import custody
from nspawn.custody import AggregateScope, ControllerRefusal

LIMITS = {"memory_bytes": 64 * 1024**2, "tasks": 16}
STATE = "--property=LoadState,ActiveState,SubState,Job,StopWhenUnneeded"
needs_manager = pytest.mark.needs("systemd_user", "cgroup")


# ---------------------------------------------------------------------------
# Refusals made before the user manager is asked anything.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("memory_bytes", "tasks"),
    [(0, 16), (8 * 1024**3 + 1, 16), (64 * 1024**2, 0), (64 * 1024**2, 4097)],
)
def test_limits_outside_the_admitted_range_are_refused(
    memory_bytes: int, tasks: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", _unreachable)
    with pytest.raises(ControllerRefusal, match="resource limits"):
        AggregateScope(memory_bytes=memory_bytes, tasks=tasks)


def test_limits_of_another_type_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", _unreachable)
    with pytest.raises(ControllerRefusal, match="resource limits"):
        AggregateScope(memory_bytes=True, tasks=16)


@pytest.mark.parametrize(
    "unit",
    [
        "app.slice",
        "nottcontroller.slice",
        "nottcontroller-.slice",
        "nottcontroller-ab-cd.slice",
        "nottcontrollerab/../x.slice",
        "nottcontrollerabc.scope",
        "nott-gate-abc.slice",
    ],
)
def test_a_parent_that_is_not_a_controller_slice_is_refused(
    unit: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    with pytest.raises(ControllerRefusal, match="invalid controller aggregate unit"):
        custody.user_scope_prefix(parent_slice=unit)
    with pytest.raises(ControllerRefusal, match="invalid controller aggregate unit"):
        AggregateScope.recorded(unit)


def test_a_controller_slice_is_the_parent_of_its_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    unit = f"nottcontroller-{uuid.uuid4().hex}.slice"
    prefix, scope = custody.user_scope_prefix(parent_slice=unit)
    assert f"--slice={unit}" in prefix and prefix[-1] == "--"
    assert scope.parent.name == unit
    assert scope.parent.parent.name == "nottcontroller.slice"
    # One recorded before the shared parent is still found where it was made.
    legacy = f"nottcontroller{uuid.uuid4().hex}.slice"
    _, scope = custody.user_scope_prefix(parent_slice=legacy)
    assert scope.parent.name == legacy
    assert scope.parent.parent.name == f"user@{os.geteuid()}.service"


def test_no_user_manager_bus_is_a_custody_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A uid no host has a runtime directory for.
    monkeypatch.setattr(os, "geteuid", lambda: 2**31 - 2)
    assert not Path(f"/run/user/{2**31 - 2}").exists()
    with pytest.raises(ControllerRefusal, match="user manager"):
        custody.user_manager_environment()
    with pytest.raises(ControllerRefusal, match="user manager"):
        AggregateScope(**LIMITS)


def _unreachable() -> dict[str, str]:
    pytest.fail("the user manager was asked before the limits were judged")


# ---------------------------------------------------------------------------
# A real transient slice under the user manager.
# ---------------------------------------------------------------------------


@needs_manager
def test_native_scope_binds_before_exec_and_cleans_its_cgroup() -> None:
    with AggregateScope(**LIMITS) as scope:
        path = scope.path
        result = subprocess.run(
            scope.command(["/bin/cat", "/proc/self/cgroup"]),
            env=scope.environment({}),
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        assert result.returncode == 0
        assert f"/{scope.unit}/nott-gate-" in result.stdout
    assert path is not None and not path.exists()
    with pytest.raises(ControllerRefusal):
        scope.command(["/bin/true"])


@needs_manager
def test_a_recorded_slice_drains_and_closes_but_launches_nothing() -> None:
    scope = AggregateScope(**LIMITS)
    try:
        survivor = subprocess.Popen(
            scope.command(["/bin/sh", "-c", "echo ready; exec sleep 30"]),
            env=scope.environment({}),
            stdout=subprocess.PIPE,
        )
        assert survivor.stdout is not None
        assert survivor.stdout.readline() == b"ready\n"
        recorded = AggregateScope.recorded(scope.unit)
        assert recorded.path == scope.path
        with pytest.raises(ControllerRefusal):
            recorded.command(["/bin/true"])
        recorded.close()
        assert survivor.wait(timeout=5) != 0
        survivor.stdout.close()
        assert scope.path is not None and not scope.path.exists()
    except BaseException:
        scope.close()
        raise


@needs_manager
def test_memory_limit_is_shared_across_executor_scopes() -> None:
    with AggregateScope(**LIMITS) as scope:
        first = subprocess.Popen(
            scope.command(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    "import time; a=bytearray(40*1024**2); a[::4096]=bytes([1])*(len(a)//4096); print('ready',flush=True); time.sleep(3)",
                ]
            ),
            env=scope.environment({}),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert first.stdout is not None and first.stdout.readline() == b"ready\n"
            second = subprocess.run(
                scope.command(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        "import time; a=bytearray(40*1024**2); a[::4096]=bytes([1])*(len(a)//4096); time.sleep(.2)",
                    ]
                ),
                env=scope.environment({}),
                capture_output=True,
                timeout=4,
                check=False,
            )
            first.wait(timeout=4)
            assert first.returncode != 0 or second.returncode != 0
            assert scope.path is not None
            events = dict(
                line.split()
                for line in (scope.path / "memory.events").read_text().splitlines()
            )
            assert int(events["oom_kill"]) > 0
        finally:
            scope.close()
            first.wait(timeout=3)
            if first.stdout is not None:
                first.stdout.close()
            if first.stderr is not None:
                first.stderr.close()


@needs_manager
def test_tasks_limit_is_enforced_by_kernel_before_fork() -> None:
    with AggregateScope(memory_bytes=64 * 1024**2, tasks=1) as scope:
        code = """
import errno, os
try:
    pid = os.fork()
except OSError as error:
    assert error.errno == errno.EAGAIN
    print('kernel refused fork')
else:
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    raise SystemExit('fork escaped admitted task limit')
"""
        result = subprocess.run(
            scope.command([sys.executable, "-I", "-S", "-c", code]),
            env=scope.environment({}),
            capture_output=True,
            timeout=3,
            check=False,
        )
        assert result.returncode == 0
        assert result.stdout == b"kernel refused fork\n"
        assert scope.path is not None
        events = dict(
            line.split()
            for line in (scope.path / "pids.events").read_text().splitlines()
        )
        assert int(events["max"]) > 0


def test_every_slice_starts_under_one_weighted_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(custody, "user_manager_environment", dict)
    calls: list[list[str]] = []

    def observe(self: AggregateScope, argv: list[str]) -> str:
        calls.append(argv)
        if "StartTransientUnit" in argv:
            raise ControllerRefusal("observed")
        return ""

    monkeypatch.setattr(AggregateScope, "_call", observe)
    with pytest.raises(ControllerRefusal, match="observed"):
        AggregateScope(**LIMITS)
    assert len(calls) == 2, calls
    weigh, start = calls
    assert weigh[-8:] == [
        "SetUnitProperties",
        "sba(sv)",
        "nottcontroller.slice",
        "true",
        "1",
        "CPUWeight",
        "t",
        str(custody.CPU_WEIGHT),
    ]
    assert start[start.index("ssa(sv)a(sa(sv))") + 1].startswith("nottcontroller-")
    assert custody.CPU_WEIGHT < 100


@needs_manager
def test_concurrent_slices_share_one_weighted_parent() -> None:
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(AggregateScope, **LIMITS) for _ in range(2)]
    scopes = [future.result() for future in futures if future.exception() is None]
    try:
        assert len(scopes) == 2, [future.exception() for future in futures]
        (parent,) = {scope.path.parent for scope in scopes if scope.path is not None}
        assert parent.name == "nottcontroller.slice"
        if "cpu" not in (parent.parent / "cgroup.subtree_control").read_text().split():
            pytest.skip("the user manager does not delegate the cpu controller")
        assert (parent / "cpu.weight").read_text().strip() == str(custody.CPU_WEIGHT)
    finally:
        for scope in scopes:
            scope.close()


@needs_manager
def test_pending_native_job_materializes_before_scope_is_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = f"nottreadiness{uuid.uuid4().hex}.service"
    environment = custody.user_manager_environment()
    original = AggregateScope._call
    observed = []

    def ordered(self: AggregateScope, argv: list[str]) -> str:
        command = list(argv)
        if "StartTransientUnit" in command:
            count = command.index("fail") + 1
            command[count] = str(int(command[count]) + 1)
            command[-1:-1] = ["After", "as", "1", service]
        result = original(self, command)
        if "--property=ControlGroup" in command:
            observed.append(result.strip())
        return result

    monkeypatch.setattr(AggregateScope, "_call", ordered)
    try:
        subprocess.run(
            [
                "/usr/bin/systemd-run",
                "--user",
                "--quiet",
                "--no-block",
                f"--unit={service}",
                "--property=Type=oneshot",
                "--property=RemainAfterExit=yes",
                "--property=MemoryMax=16777216",
                "--property=MemorySwapMax=0",
                "--property=TasksMax=4",
                "--",
                "/bin/sleep",
                ".5",
            ],
            env=environment,
            capture_output=True,
            timeout=3,
            check=True,
        )
        with AggregateScope(**LIMITS) as scope:
            assert observed[0] == ""
            scope.verify()
            result = subprocess.run(
                scope.command(["/bin/cat", "/proc/self/cgroup"]),
                env=scope.environment({}),
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            assert result.returncode == 0
            assert f"/{scope.unit}/nott-gate-" in result.stdout
    finally:
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "stop", service],
            env=environment,
            capture_output=True,
            timeout=3,
            check=True,
        )


@needs_manager
@pytest.mark.parametrize("damage", ["inactive", "foreign-job", "automatic-stop"])
def test_scope_rejects_unready_or_unstable_unit_state(
    monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    original = AggregateScope._call

    def damaged(self: AggregateScope, argv: list[str]) -> str:
        result = original(self, argv)
        if STATE in argv:
            if damage == "inactive":
                return "LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=\nStopWhenUnneeded=no\n"
            if damage == "foreign-job":
                return "LoadState=loaded\nActiveState=active\nSubState=active\nJob=999999999\nStopWhenUnneeded=no\n"
            return "LoadState=loaded\nActiveState=active\nSubState=active\nJob=\nStopWhenUnneeded=yes\n"
        return result

    monkeypatch.setattr(AggregateScope, "_call", damaged)
    with pytest.raises(ControllerRefusal), AggregateScope(**LIMITS):
        pass


@needs_manager
def test_accepted_pending_job_has_bounded_readiness_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AggregateScope._call
    accepted = []

    def pending(self: AggregateScope, argv: list[str]) -> str:
        result = original(self, argv)
        if "StartTransientUnit" in argv:
            accepted.append(shlex.split(result)[1].rsplit("/", 1)[1])
        if STATE in argv:
            return f"LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob={accepted[0]}\nStopWhenUnneeded=no\n"
        return result

    monkeypatch.setattr(custody, "READINESS_SECONDS", 0.03)
    monkeypatch.setattr(AggregateScope, "_call", pending)
    started = time.monotonic()
    with (
        pytest.raises(ControllerRefusal, match="deadline"),
        AggregateScope(**LIMITS),
    ):
        pass
    assert time.monotonic() - started < 1
    assert len(accepted) == 1


@needs_manager
def test_nonempty_wrong_cgroup_is_never_readiness_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = AggregateScope._call
    observations = []

    def wrong(self: AggregateScope, argv: list[str]) -> str:
        result = original(self, argv)
        if "--property=ControlGroup" in argv:
            observations.append(result)
            return "/foreign.slice"
        return result

    monkeypatch.setattr(AggregateScope, "_call", wrong)
    with (
        pytest.raises(ControllerRefusal, match="unexpected"),
        AggregateScope(**LIMITS),
    ):
        pass
    assert len(observations) == 1


@needs_manager
@pytest.mark.parametrize("reply", ['o "unterminated', 'o "/not-a-job"'])
def test_malformed_accepted_job_reply_is_typed_refusal(
    monkeypatch: pytest.MonkeyPatch, reply: str
) -> None:
    original = AggregateScope._call

    def malformed(self: AggregateScope, argv: list[str]) -> str:
        result = original(self, argv)
        return reply if "StartTransientUnit" in argv else result

    monkeypatch.setattr(AggregateScope, "_call", malformed)
    with pytest.raises(ControllerRefusal), AggregateScope(**LIMITS):
        pass

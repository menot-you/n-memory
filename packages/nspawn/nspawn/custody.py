"""nSPAWN's custody primitives: a systemd-owned aggregate cgroup for a native
writer and every command launched into it, and the user manager that owns it."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from threading import Lock
from types import TracebackType
from typing import Protocol, Self

CGROUP_ROOT = Path("/sys/fs/cgroup")
READINESS_SECONDS = 3.0
#: The one parent of every nSPAWN custody unit (aggregate slices and drive
#: scopes), so all of nSPAWN together holds a single weight against app.slice.
CUSTODY_SLICE = "nottcontroller.slice"
#: The parent's weight: a default-weight (100) sibling wins 5:1 under contention;
#: work-conserving, so an idle host still lends every core (CPUQuota would cap
#: even then; idle can starve).
CPU_WEIGHT = 20
#: A runtime drop-in on the parent: systemd accepts it before the slice exists
#: and applies it whenever the slice is created implicitly under a child, and
#: every launch writes the same value, so concurrent launches agree.
WEIGH_CUSTODY_PARENT = (
    "/usr/bin/busctl",
    "--user",
    "call",
    "org.freedesktop.systemd1",
    "/org/freedesktop/systemd1",
    "org.freedesktop.systemd1.Manager",
    "SetUnitProperties",
    "sba(sv)",
    CUSTODY_SLICE,
    "true",
    "1",
    "CPUWeight",
    "t",
    str(CPU_WEIGHT),
)


class ControllerRefusal(Exception):
    """Custody cannot prove a required write or observation boundary.

    The name is a contract: a driver's refusal ack names the class, and the
    workflow reads it as a custody refusal.
    """


class ExecutorScope(Protocol):
    """The custody a native client and every command it launches run under."""

    def command(self, argv: list[str]) -> list[str]: ...
    def environment(self, target: dict[str, str] | None) -> dict[str, str]: ...
    def verify(self) -> None: ...
    def drain(self) -> None: ...
    def close(self) -> None: ...


def user_manager_environment() -> dict[str, str]:
    runtime = Path(f"/run/user/{os.geteuid()}")
    if not (runtime / "bus").is_socket():
        raise ControllerRefusal(
            "aggregate custody requires the controller's user manager"
        )
    return {
        "PATH": "/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
    }


def _systemd_call(environment: dict[str, str], argv: list[str]) -> str:
    try:
        result = subprocess.run(
            argv,
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ControllerRefusal("aggregate systemd operation unavailable") from exc
    if result.returncode:
        raise ControllerRefusal(
            f"aggregate systemd operation refused: {result.stderr.strip()[:512]}"
        )
    return result.stdout


def weigh_custody_parent(environment: dict[str, str]) -> None:
    """Give the custody parent its CPU weight before anything starts under it."""
    _systemd_call(environment, list(WEIGH_CUSTODY_PARENT))


def aggregate_group(unit: str) -> Path:
    """A controller aggregate's cgroup relative to the user manager.

    `nottcontroller-<id>.slice` nests under the custody parent; a slice recorded
    before the parent existed (`nottcontroller<id>.slice`) sits directly under
    the manager and is still found there to be drained and closed.
    """
    if unit.startswith("nottcontroller-"):
        name, parent = unit[15:-6], Path(CUSTODY_SLICE)
    else:
        name, parent = unit[14:-6], Path()
    if (
        not unit.startswith("nottcontroller")
        or not unit.endswith(".slice")
        or not name.isalnum()
    ):
        raise ControllerRefusal("invalid controller aggregate unit")
    return parent / unit


def user_scope_prefix(*, parent_slice: str | None = None) -> tuple[list[str], Path]:
    """Shared native scope launch contract; systemd enrolls before executor exec."""
    user_manager_environment()
    unit = f"nott-gate-{uuid.uuid4().hex}.scope"
    parent = (
        CGROUP_ROOT
        / "user.slice"
        / f"user-{os.geteuid()}.slice"
        / f"user@{os.geteuid()}.service"
    )
    prefix = [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
    ]
    if parent_slice is None:
        parent /= "app.slice"
    else:
        parent /= aggregate_group(parent_slice)
        prefix.append(f"--slice={parent_slice}")
    return [*prefix, "--"], parent / unit


class AggregateScope:
    """Trusted parent slice shared by the native client and every executor command."""

    def __init__(self, *, memory_bytes: int, tasks: int) -> None:
        if (
            type(memory_bytes) is not int
            or type(tasks) is not int
            or not 0 < memory_bytes <= 8 * 1024**3
            or not 0 < tasks <= 4096
        ):
            raise ControllerRefusal("invalid admitted aggregate resource limits")
        self.memory_bytes = memory_bytes
        self.tasks = tasks
        self.unit = f"nottcontroller-{uuid.uuid4().hex}.slice"
        self._environment = user_manager_environment()
        self._closed = False
        self._lock = Lock()
        self.path: Path | None = None
        self._call(list(WEIGH_CUSTODY_PARENT))
        reply = self._call(
            [
                "/usr/bin/busctl",
                "--user",
                "call",
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "StartTransientUnit",
                "ssa(sv)a(sa(sv))",
                self.unit,
                "fail",
                "4",
                "MemoryMax",
                "t",
                str(memory_bytes),
                "MemorySwapMax",
                "t",
                "0",
                "TasksMax",
                "t",
                str(tasks),
                "Description",
                "s",
                "NOTT nSPAWN native writer custody",
                "0",
            ]
        )
        initialized = False
        try:
            try:
                fields = shlex.split(reply)
            except ValueError as exc:
                raise ControllerRefusal(
                    "systemd returned a malformed aggregate job"
                ) from exc
            prefix = "/org/freedesktop/systemd1/job/"
            if (
                len(fields) != 2
                or fields[0] != "o"
                or not fields[1].startswith(prefix)
                or not fields[1][len(prefix) :].isdigit()
            ):
                raise ControllerRefusal(
                    "systemd did not return an accepted aggregate job"
                )
            self.path = self._await_group(fields[1][len(prefix) :])
            self.verify()
            initialized = True
        finally:
            if not initialized:
                self.close()

    @classmethod
    def recorded(cls, unit: str) -> Self:
        """The slice an earlier controller created and recorded, for `drain` and
        `close` only: its limits are unknown here, so `verify` refuses it and
        nothing can be launched into it."""
        _, scope = user_scope_prefix(parent_slice=unit)
        recorded = object.__new__(cls)
        recorded.memory_bytes = recorded.tasks = 0
        recorded.unit = unit
        recorded._environment = user_manager_environment()
        recorded._closed = False
        recorded._lock = Lock()
        recorded.path = scope.parent
        return recorded

    def _await_group(self, accepted_job: str) -> Path:
        expected = f"/user.slice/user-{os.geteuid()}.slice/user@{os.geteuid()}.service/{aggregate_group(self.unit)}"
        deadline = time.monotonic() + READINESS_SECONDS
        while True:
            control_group = self._call(
                [
                    "/usr/bin/systemctl",
                    "--user",
                    "show",
                    self.unit,
                    "--property=ControlGroup",
                    "--value",
                ]
            ).strip()
            state = dict(
                line.split("=", 1)
                for line in self._call(
                    [
                        "/usr/bin/systemctl",
                        "--user",
                        "show",
                        self.unit,
                        "--property=LoadState,ActiveState,SubState,Job,StopWhenUnneeded",
                    ]
                ).splitlines()
                if "=" in line
            )
            detail = f"unit={self.unit}, accepted_job={accepted_job}, state={state}, group={control_group!r}"
            if control_group and control_group != expected:
                raise ControllerRefusal(
                    f"systemd bound an unexpected aggregate cgroup: {detail}"
                )
            if (
                state.get("LoadState") != "loaded"
                or state.get("StopWhenUnneeded") != "no"
            ):
                raise ControllerRefusal(
                    f"aggregate unit is unavailable or unstable: {detail}"
                )
            job = state.get("Job")
            if job == "":
                if (
                    state.get("ActiveState") != "active"
                    or state.get("SubState") != "active"
                ):
                    raise ControllerRefusal(
                        f"aggregate activation ended without readiness: {detail}"
                    )
                # Activation may have completed between the two observations.
                if not control_group:
                    control_group = self._call(
                        [
                            "/usr/bin/systemctl",
                            "--user",
                            "show",
                            self.unit,
                            "--property=ControlGroup",
                            "--value",
                        ]
                    ).strip()
                if control_group != expected:
                    raise ControllerRefusal(
                        f"active aggregate has no exact cgroup: observed={control_group!r}, {detail}"
                    )
                return CGROUP_ROOT / control_group.lstrip("/")
            if job != accepted_job or state.get("ActiveState") not in {
                "inactive",
                "activating",
                "active",
            }:
                raise ControllerRefusal(
                    f"aggregate accepted job changed or failed: {detail}"
                )
            if time.monotonic() >= deadline:
                raise ControllerRefusal(
                    f"aggregate readiness deadline expired: {detail}"
                )
            time.sleep(0.01)

    def _call(self, argv: list[str]) -> str:
        return _systemd_call(self._environment, argv)

    def verify(self) -> None:
        if self._closed or self.path is None:
            raise ControllerRefusal("aggregate scope is closed or unbound")
        try:
            if (self.path / "memory.swap.max").read_text().strip() != "0":
                raise ControllerRefusal("aggregate executor swap is not disabled")
            if (self.path / "memory.max").read_text().strip() != str(
                self.memory_bytes
            ) or (self.path / "pids.max").read_text().strip() != str(self.tasks):
                raise ControllerRefusal(
                    "kernel aggregate limits differ from admitted limits"
                )
        except OSError as exc:
            raise ControllerRefusal("kernel aggregate limits unavailable") from exc

    def command(self, argv: list[str]) -> list[str]:
        self.verify()
        prefix, _ = user_scope_prefix(parent_slice=self.unit)
        # Manager access belongs to the launcher, never its executable target.
        return [
            *prefix,
            "/usr/bin/env",
            "-u",
            "DBUS_SESSION_BUS_ADDRESS",
            "-u",
            "XDG_RUNTIME_DIR",
            "--",
            *argv,
        ]

    def environment(self, target: dict[str, str] | None) -> dict[str, str]:
        environment = dict(os.environ if target is None else target)
        environment.update(
            {key: value for key, value in self._environment.items() if key != "PATH"}
        )
        return environment

    def drain(self) -> None:
        """Empty the slice and refuse if anything outlives the kill.

        A caller that reuses one slice across several commands needs this
        between them: the event counters are hierarchical, so a descendant
        that escaped its own process group — `setsid` puts it out of reach of
        a group kill — keeps allocating inside the slice and is charged to
        whichever command runs next. Emptying the slice is what makes each
        command's before/after delta its own.
        """
        if self.path is None or not self.path.exists():
            return
        try:
            (self.path / "cgroup.kill").write_text("1")
            deadline = time.monotonic() + 3
            while "populated 1" in (self.path / "cgroup.events").read_text():
                if time.monotonic() >= deadline:
                    raise ControllerRefusal("aggregate descendants survived cleanup")
                time.sleep(0.01)
        except OSError as exc:
            raise ControllerRefusal(
                "aggregate cleanup observation unavailable"
            ) from exc

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.drain()
            self._call(["/usr/bin/systemctl", "--user", "stop", self.unit])
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

"""U1 live tier: a dispatched run outlives its client and its stdio process.

Read-only runs only, through the real stdio binary and the real launcher. The
teardown reaps by what the test itself launched: every `nspawn-drive-*` scope
named by a log in its run root, every driver pid recorded there, and then any
process whose environment carries this test's `NOTT_NSPAWN_CONFIG` — before the
next test's `clean_database` drops the schema under it.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from live.live_support import McpStdio, store_rows

from nspawn.process_identity import ProcessIdentity, process_identity_matches

SYSTEMD_RUN = "/usr/bin/systemd-run"
SYSTEMCTL = "/usr/bin/systemctl"
LONG_PROMPT_PREFIX = "SLEEP_SECONDS="


def _bus() -> Path:
    return Path(f"/run/user/{os.geteuid()}/bus")


def _manager_environment() -> dict[str, str]:
    runtime = Path(f"/run/user/{os.geteuid()}")
    return {
        "XDG_RUNTIME_DIR": str(runtime),
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
    }


def _systemctl(*arguments: str) -> subprocess.CompletedProcess[bytes] | None:
    if not Path(SYSTEMCTL).exists():  # macOS: no scopes were made
        return None
    return subprocess.run(
        [SYSTEMCTL, "--user", *arguments],
        env={**os.environ, **_manager_environment()},
        capture_output=True,
        timeout=30,
        check=False,
    )


@dataclass
class Harvest:
    """What this test launched, so the teardown reaps exactly that."""

    run_root: Path
    config: str
    clients: list[McpStdio] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)

    @property
    def drivers(self) -> Path:
        return self.run_root / "drivers"

    def client(
        self, environment: dict[str, str], prefix: tuple[str, ...] = ()
    ) -> McpStdio:
        started = McpStdio(environment, prefix)
        self.clients.append(started)
        return started

    def driver_of(self, run_id: str) -> tuple[str, ProcessIdentity]:
        for ack in self.drivers.glob("*.ack"):
            if json.loads(ack.read_text()).get("run_id") == run_id:
                recorded = json.loads((self.drivers / f"{ack.stem}.pid").read_text())
                return ack.stem, ProcessIdentity(
                    pid=recorded["pid"],
                    boot_id=recorded["boot_id"],
                    start_ticks=recorded["start_ticks"],
                )
        pytest.fail(f"no driver acknowledged run {run_id}")


def _environ_of(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return b""


def _marked_pids(marker: str) -> list[int]:
    """This user's processes whose environment carries `marker` (NAME=value)."""
    if sys.platform == "darwin":
        # `ps -E` appends the environment of the caller's own processes.
        listing = subprocess.run(
            ["ps", "-E", "-ww", "-x", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return [
            int(line.split(None, 1)[0])
            for line in listing.splitlines()
            if f" {marker} " in f"{line} "
        ]
    return [
        int(entry.name)
        for entry in Path("/proc").iterdir()
        if entry.name.isdigit()
        and f"{marker}\0".encode() in _environ_of(int(entry.name))
    ]


def _stdio_paths(pid: int) -> list[str]:
    """What fds 0, 1 and 2 of `pid` name: `/proc` on Linux, `lsof` on macOS."""
    if sys.platform == "darwin":
        listing = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "0,1,2", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return [line[1:] for line in listing.splitlines() if line.startswith("n")]
    return [os.readlink(f"/proc/{pid}/fd/{n}") for n in (0, 1, 2)]


@pytest.fixture
def harvest(live_environment: dict[str, str]) -> Iterator[Harvest]:
    config = live_environment["NOTT_NSPAWN_CONFIG"]
    held = Harvest(Path(config).parent / "runs", config)
    try:
        yield held
    finally:
        for client in held.clients:
            with contextlib.suppress(OSError, ProcessLookupError):
                if client.process.poll() is None:
                    client.process.kill()
            client.close()
        for scope in held.scopes:
            _systemctl("stop", f"{scope}.scope")
        for log in held.drivers.glob("*.log"):
            _systemctl("stop", f"nspawn-drive-{log.stem}.scope")
        for pidfile in held.drivers.glob("*.pid"):
            recorded = json.loads(pidfile.read_text())
            identity = ProcessIdentity(
                pid=recorded["pid"],
                boot_id=recorded["boot_id"],
                start_ticks=recorded["start_ticks"],
            )
            if process_identity_matches(identity):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(identity.pid, signal.SIGKILL)
        for pid in _marked_pids(f"NOTT_NSPAWN_CONFIG={config}"):
            if pid != os.getpid():
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)


def _payload(answer: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in answer, answer
    decoded: dict[str, Any] = json.loads(answer["result"]["content"][0]["text"])
    return decoded


def _dispatch(client: McpStdio, work: Path, seconds: float) -> dict[str, Any]:
    client.handshake()
    return _payload(
        client.call_tool(
            "nspawn_run_start",
            {
                "prompt": f"{LONG_PROMPT_PREFIX}{seconds}",
                "cwd": str(work),
                "class": "mechanical_fanout",
                "read_only": True,
            },
        )
    )


def _phase(run_id: str) -> tuple[str, int] | None:
    rows = store_rows("SELECT phase, updated_at FROM runs WHERE run_id = ?", (run_id,))
    return None if not rows else (str(rows[0][0]), int(rows[0][1]))


def _completed(run_id: str, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _phase(run_id)
        if last is not None and last[0] in (
            "completed",
            "failed",
            "closed",
            "orphaned",
        ):
            break
        time.sleep(0.5)
    assert last is not None and last[0] == "completed", last
    return last[1]


def test_runs_survive_client_eof_sighup_and_sigkill_and_complete_later(
    live_environment: dict[str, str], harvest: Harvest, tmp_path: Path
) -> None:
    if not _bus().is_socket():
        live_environment = {**live_environment, "NOTT_NSPAWN_DRIVER_LAUNCH": "direct"}
    work = tmp_path / "work"
    work.mkdir()
    dispatched: dict[str, tuple[McpStdio, str, int]] = {}
    for how in ("eof", "sighup", "sigkill"):
        client = harvest.client(live_environment)
        dispatched[how] = (
            client,
            _dispatch(client, work, 35)["run_id"],
            int(time.time() * 1000),
        )
    for how, (client, run_id, _) in dispatched.items():
        launch_uuid, driver = harvest.driver_of(run_id)
        log = harvest.drivers / f"{launch_uuid}.log"
        assert _stdio_paths(driver.pid) == [os.path.realpath(log)] * 3
        if how == "eof":
            client.process.stdin.close()  # type: ignore[union-attr]
        else:
            client.process.send_signal(
                signal.SIGHUP if how == "sighup" else signal.SIGKILL
            )
        client.process.wait(timeout=5)
        assert process_identity_matches(driver), (
            f"{how}: the driver died with its stdio"
        )
    for how, (_, run_id, at) in dispatched.items():
        finished = _completed(run_id, 90)
        assert finished - at > 30_000, how


@pytest.mark.needs("systemd_user")
def test_the_driver_keeps_its_own_scope_when_the_stdio_scope_is_killed(
    live_environment: dict[str, str], harvest: Harvest, tmp_path: Path
) -> None:
    """The scope falsifier. Needs the user manager; any launch or scope
    error FAILS. `NOTT_NSPAWN_FALSIFY_DIRECT=1` runs the direct-launch mutant,
    which must turn this red."""
    environment = {**live_environment, **_manager_environment()}
    if os.environ.get("NOTT_NSPAWN_FALSIFY_DIRECT"):
        environment["NOTT_NSPAWN_DRIVER_LAUNCH"] = "direct"
    unit = f"nspawn-test-stdio-{uuid.uuid4().hex}"
    harvest.scopes.append(unit)
    client = harvest.client(
        environment,
        (
            SYSTEMD_RUN,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            f"--unit={unit}",
            "--",
        ),
    )
    client.handshake()
    # The launcher is decided at construction: a status and a quota that fill
    # the lazy supervisor first must not turn the dispatch in-process.
    assert "result" in client.call_tool("nspawn_run_list", {})
    client.call_tool("nspawn_quota_show", {})
    work = tmp_path / "work"
    work.mkdir()
    started = time.monotonic()
    answer = _payload(
        client.call_tool(
            "nspawn_run_start",
            {
                "prompt": f"{LONG_PROMPT_PREFIX}6",
                "cwd": str(work),
                "class": "mechanical_fanout",
                "read_only": True,
            },
        )
    )
    assert time.monotonic() - started < 15
    assert "ack_late" not in answer
    run_id = answer["run_id"]
    launch_uuid, driver = harvest.driver_of(run_id)
    cgroup = Path(f"/proc/{driver.pid}/cgroup").read_text()
    assert f"nspawn-drive-{launch_uuid}.scope" in cgroup, cgroup
    killed = _systemctl("kill", "--signal=SIGKILL", f"{unit}.scope")
    assert killed is not None and killed.returncode == 0, killed
    client.process.wait(timeout=10)
    _completed(run_id, 40)

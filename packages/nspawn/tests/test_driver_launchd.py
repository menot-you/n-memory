"""The macOS driver launcher: one launchd job per dispatch, its own coalition.

A job, not a detached child: launchd gives it a resource coalition no descendant
can leave (the darwin custody's unit), and a job outlives the ssh or terminal
session that dispatched it. The measured cases bootstrap real jobs in the
user's `gui/<uid>` domain.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from supervisor_support import registry

from nspawn import driver
from nspawn.registry import Registry
from nspawn.supervisor import Supervisor

__all__ = ["registry"]


def test_the_driver_job_is_run_once_never_kept_alive(tmp_path: Path) -> None:
    job = driver.driver_plist(
        "nspawn-drive-x",
        ["/usr/bin/python3", "-P"],
        {"HOME": "/Users/x"},
        stdin=tmp_path / "x.frame",
        log=tmp_path / "x.log",
    )
    assert job == {
        "Label": "nspawn-drive-x",
        "ProgramArguments": ["/usr/bin/python3", "-P"],
        "EnvironmentVariables": {"HOME": "/Users/x"},
        "StandardInPath": str(tmp_path / "x.frame"),
        "StandardOutPath": str(tmp_path / "x.log"),
        "StandardErrorPath": str(tmp_path / "x.log"),
        "RunAtLoad": True,
        "KeepAlive": False,
    }


def test_darwin_launches_launchd_jobs_and_linux_systemd_scopes() -> None:
    assert type(driver.default_launcher("darwin")) is driver.LaunchdLauncher
    assert type(driver.default_launcher("linux")) is driver.SystemdLauncher


def test_the_ack_travels_through_the_launch_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "x.ack.fifo"
    os.mkfifo(fifo, 0o600)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        write = driver.ack_writer({"ack_fifo": str(fifo)})
        write({"run_id": "r1"})
        assert os.read(reader, 4096) == b'{"run_id": "r1"}\n'
        # The writer closed its end: the stdio reads EOF, not a hang.
        assert os.read(reader, 4096) == b""
    finally:
        os.close(reader)


def test_an_ack_fifo_nobody_reads_is_a_logged_loss_not_a_hang(tmp_path: Path) -> None:
    fifo = tmp_path / "x.ack.fifo"
    os.mkfifo(fifo, 0o600)
    write = driver.ack_writer({"ack_fifo": str(fifo)})
    with pytest.raises(OSError):
        write({"run_id": "r1"})


STUB = """import json, os, stat, sys
frame = json.loads(sys.stdin.read())
with open({runs!r}, "a") as out:
    out.write(json.dumps({{
        "stdin_mode": stat.S_IMODE(os.fstat(0).st_mode),
        "label": os.environ.get("XPC_SERVICE_NAME"),
        "probe": os.environ.get("NOTT_PROBE"),
        "frame": frame,
    }}) + "\\n")
if {mode!r} == "ack":
    fd = os.open(frame["ack_fifo"], os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, json.dumps({{"run_id": "stub-run", "phase": "pending"}}).encode() + b"\\n")
    os.close(fd)
    sys.exit(0)
sys.exit(1)
"""


def _dispatch(
    registry: Registry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> tuple[dict[str, Any], Path, driver.LaunchdLauncher, str]:
    runs = tmp_path / f"runs-{mode}.jsonl"
    stub = tmp_path / f"stub-{mode}.py"
    stub.write_text(STUB.format(runs=str(runs), mode=mode))
    monkeypatch.setenv("NOTT_PROBE", "inherited")
    launcher = driver.LaunchdLauncher(argv=[sys.executable, str(stub)])
    stdio = Supervisor(registry, tmp_path / "runs", sidecar_kind="stdio")
    launch_uuid = str(uuid.uuid4())
    frame = {
        "tool": "nspawn_run_start",
        "arguments": {"prompt": "p"},
        "launch_uuid": launch_uuid,
        "drivers_dir": str(tmp_path / "runs" / "drivers"),
    }
    try:
        [answer] = launcher.dispatch([frame], stdio)
    finally:
        stdio.shutdown()
    return answer, runs, launcher, launch_uuid


def _loaded(label: str) -> bool:
    return (
        subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            timeout=10,
            check=False,
        ).returncode
        == 0
    )


def _await_booted_out(launcher: driver.LaunchdLauncher, label: str) -> None:
    for thread in launcher.threads:
        thread.join(timeout=30)
    assert not _loaded(label)


@pytest.mark.needs("launchd_coalition")
def test_a_driver_job_acks_through_its_fifo_and_is_booted_out_after_exit(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, runs, launcher, launch_uuid = _dispatch(
        registry, tmp_path, monkeypatch, "ack"
    )
    assert answer == {"run_id": "stub-run", "phase": "pending"}
    [seen] = [json.loads(line) for line in runs.read_text().splitlines()]
    label = f"nspawn-drive-{launch_uuid}"
    assert seen["label"] == label
    assert seen["probe"] == "inherited"
    assert seen["stdin_mode"] == 0o600
    assert seen["frame"]["launch_uuid"] == launch_uuid
    _await_booted_out(launcher, label)
    directory = tmp_path / "runs" / "drivers"
    assert stat.S_IMODE((directory / f"{launch_uuid}.log").stat().st_mode) == 0o600
    # Frame, plist and FIFO leave with the job; the log and the ack stay.
    assert sorted(path.name for path in directory.iterdir()) == [f"{launch_uuid}.log"]


@pytest.mark.needs("launchd_coalition")
def test_a_driver_that_exits_1_is_launch_failed_and_never_relaunched(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answer, runs, launcher, launch_uuid = _dispatch(
        registry, tmp_path, monkeypatch, "exit"
    )
    refused = answer["refused"]
    assert refused["type"] == "LaunchFailed"
    assert refused["rc"] == 1
    _await_booted_out(launcher, f"nspawn-drive-{launch_uuid}")
    time.sleep(2)
    assert len(runs.read_text().splitlines()) == 1


def test_a_domain_that_refuses_every_bootstrap_is_launch_failed(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refuse = tmp_path / "launchctl"
    refuse.write_text("#!/bin/sh\necho 'Bootstrap failed: 5' >&2\nexit 5\n")
    refuse.chmod(0o755)
    monkeypatch.setattr(driver, "LAUNCHCTL", str(refuse))
    answer, _, _, launch_uuid = _dispatch(registry, tmp_path, monkeypatch, "ack")
    refused = answer["refused"]
    assert refused["type"] == "LaunchFailed"
    assert "Bootstrap failed: 5" in refused["message"]
    directory = tmp_path / "runs" / "drivers"
    assert sorted(path.name for path in directory.iterdir()) == [f"{launch_uuid}.log"]


def test_the_plist_is_written_0600(tmp_path: Path) -> None:
    target = tmp_path / "x.plist"
    driver.write_private(target, plistlib.dumps({"Label": "x"}))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert plistlib.loads(target.read_bytes()) == {"Label": "x"}

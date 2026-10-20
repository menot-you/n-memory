"""U2 live tier: the stdio dies, the session lives, and the notice still lands.

A fake session (`tests/fake_session.py`) owns `<pid>.sock` and launches the
real stdio binary, so the stdio's parent is the socket owner exactly as under
Claude Code. Only the stdio is killed; the run completes in its driver, the
driver pushes to the session socket once, and a drain from a second session
takes nothing.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from live.live_support import MEMBER_ROOT, store_rows
from test_driver_survival import Harvest, _bus, _completed, _dispatch, harvest

__all__ = ["harvest"]

FAKE_SESSION = Path(__file__).resolve().parents[1] / "fake_session.py"


def _notice(run_id: str) -> tuple[int, str, object] | None:
    rows = store_rows(
        "SELECT caller_pid, caller_project, delivered_at FROM notices WHERE run_id = ?",
        (run_id,),
    )
    return None if not rows else (int(rows[0][0]), str(rows[0][1]), rows[0][2])


def _frames(record: Path) -> list[bytes]:
    if not record.exists():
        return []
    return [line for line in record.read_bytes().splitlines() if line.strip()]


def test_the_stdio_dies_the_session_lives_and_the_push_lands_exactly_once(
    live_environment: dict[str, str], harvest: Harvest, tmp_path: Path
) -> None:
    environment = dict(live_environment)
    if not _bus().is_socket():
        environment["NOTT_NSPAWN_DRIVER_LAUNCH"] = "direct"
    directory = Path(tempfile.mkdtemp(prefix="nsl"))
    record = directory / "record"
    try:
        client = harvest.client(
            environment,
            (sys.executable, str(FAKE_SESSION), str(directory), str(record)),
        )
        child = Path(f"{record}.child")
        deadline = time.monotonic() + 10
        while not child.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        stdio_pid = int(child.read_text())
        session_pid = client.process.pid
        work = tmp_path / "work"
        work.mkdir()
        run_id = _dispatch(client, work, 4)["run_id"]
        notice = _notice(run_id)
        assert notice is not None, "the dispatch wrote no notices row"
        caller_pid, project, _ = notice
        assert caller_pid == session_pid and caller_pid != stdio_pid
        os.kill(stdio_pid, signal.SIGKILL)
        _completed(run_id, 60)
        deadline = time.monotonic() + 10
        while not _frames(record) and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(1)
        frames = _frames(record)
        assert len(frames) == 1, frames
        assert f"{run_id} is completed".encode() in frames[0]
        delivered = _notice(run_id)
        assert delivered is not None and delivered[2] is not None
        second = subprocess.run(
            [sys.executable, "-m", "nspawn.cli", "notice", "drain"]
            + ["--project", project],
            cwd=MEMBER_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == "", (
            "a second session drained a delivered notice"
        )
        launch_uuid, _ = harvest.driver_of(run_id)
        log = (harvest.drivers / f"{launch_uuid}.log").read_text()
        assert "custody failed" not in log, log
    finally:
        shutil.rmtree(directory, ignore_errors=True)

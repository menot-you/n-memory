"""Process state read the same way on Linux and macOS, through `ps`.

`/proc` exists only on Linux; `ps -o stat=` answers on both, and a reaped
process simply has no row.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def process_state(pid: int) -> str | None:
    """The `ps` state of `pid` (`Z` for a zombie), or None when it is gone."""
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out or None


def live_processes_running(binary: Path) -> list[int]:
    """Pids whose argv carries `binary`; a zombie (no longer running) is excluded."""
    needle = os.fsdecode(binary)
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,stat=,args="],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    live = []
    for line in out.splitlines():
        pid, state, *argv = line.split()
        if not state.startswith("Z") and needle in argv:
            live.append(int(pid))
    return live

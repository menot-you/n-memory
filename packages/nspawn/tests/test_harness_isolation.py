"""A test run never reaches the Claude Code session that launched it.

MEASURED 2026-09-23: pytest inherits `CLAUDE_CODE_MESSAGING_SOCKET` from the
session that started it, and four notices from U2's RED runs landed in the
owner's live session ("... the registry refused the write; evidence is in
/var/tmp/pytest-of-nott/..."). The harness strips the variable for the whole
run (conftest `_no_caller_session`); this proves it from outside: a child pytest
whose parent environment NAMES a session socket drives an armed terminal seam
and the socket hears nothing.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from nspawn.child import CALLER_SOCKET_ENV

# Not named test_*: only the child pytest below collects it, by path.
INNER_MODULE = Path(__file__).with_name("harness_isolation_inner.py")
MEMBER_ROOT = Path(__file__).resolve().parents[1]


def test_a_test_run_never_reaches_the_session_socket_its_parent_names() -> None:
    directory = Path(tempfile.mkdtemp(prefix="nsh"))
    path = directory / "session.sock"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as session:
            session.bind(str(path))
            session.listen(8)
            session.settimeout(0.5)
            environment = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("PYTEST_")
            }
            environment[CALLER_SOCKET_ENV] = str(path)
            inner = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    str(INNER_MODULE),
                ],
                cwd=MEMBER_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            received: list[bytes] = []
            while True:
                try:
                    connection, _ = session.accept()
                except TimeoutError:
                    break
                with connection:
                    received.append(connection.recv(65536))
        assert received == [], f"a test run wrote into its parent's session: {received}"
        assert inner.returncode == 0, inner.stdout + inner.stderr
    finally:
        shutil.rmtree(directory, ignore_errors=True)

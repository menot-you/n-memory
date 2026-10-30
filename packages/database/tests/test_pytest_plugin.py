"""Disposable stores must also be visible to host user units."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import nott_database as db
from nott_database.pytest_plugin import private_directory


def test_private_directory_is_shared_with_host_units() -> None:
    with private_directory() as folder:
        assert folder.parent.parent == Path("/var/tmp")
        assert folder.parent.stat().st_mode & 0o777 == 0o700
        assert not folder.exists()
    assert not folder.parent.exists()


def test_host_user_unit_reads_the_test_store() -> None:
    bus = Path(f"/run/user/{os.geteuid()}/bus")
    if shutil.which("systemd-run") is None or not bus.is_socket():
        pytest.skip("a host user manager is required")
    with private_directory() as folder:
        path = folder / "shared.sqlite3"
        db.create(path, "CREATE TABLE proof(value); INSERT INTO proof VALUES (42);")
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--quiet",
                "--wait",
                "--pipe",
                "/usr/bin/env",
                "-i",
                "PATH=/usr/bin:/bin",
                f"NOTT_DATABASE_DIR={folder}",
                "OTEL_SDK_DISABLED=true",
                sys.executable,
                "-c",
                (
                    "from contextlib import closing; from pathlib import Path; "
                    "import sys, nott_database as db\n"
                    "with closing(db.connect(Path(sys.argv[1]))) as conn: "
                    "print(db.query(conn, 'SELECT value FROM proof'))"
                ),
                str(path),
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}",
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "[(42,)]"

"""The shared pytest fence protects children from the owner's System1 file."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_system1_fence_has_a_falsifier(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".nott").mkdir(parents=True)
    path = home / ".nott/.env"
    path.write_text("NOTT_SYSTEM1_ENDPOINT=http://127.0.0.1:9/ask\n")
    path.chmod(0o600)
    fence = os.environ.get("NOTT_TEST_SYSTEM1_FILE")
    assert fence is not None
    assert not Path(fence).exists()
    child_env = {"HOME": str(home), "NOTT_TEST_SYSTEM1_FILE": fence}
    source = "from nott_system1 import route; print(route().endpoint)"
    fenced = subprocess.run(
        [sys.executable, "-c", source],
        env=child_env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert fenced.stdout.strip() == "None"
    del child_env["NOTT_TEST_SYSTEM1_FILE"]
    unfenced = subprocess.run(
        [sys.executable, "-c", source],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert unfenced.returncode == 0, unfenced.stderr
    assert unfenced.stdout.strip() == "http://127.0.0.1:9/ask"
    assert fenced.stderr == unfenced.stderr == ""


def test_fence_precedes_collection_and_wide_scope_fixtures(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".nott").mkdir(parents=True)
    path = home / ".nott/.env"
    path.write_text("NOTT_SYSTEM1_ENDPOINT=http://127.0.0.1:9/private\n")
    path.chmod(0o600)
    probe = tmp_path / "test_fence_probe.py"
    probe.write_text(
        "import os\n"
        "import pytest\n"
        "from nott_system1 import route\n"
        "fence = os.environ.get('NOTT_TEST_SYSTEM1_FILE')\n"
        "assert fence is not None\n"
        "collected = route().endpoint\n"
        "@pytest.fixture(scope='session')\n"
        "def session_route():\n"
        "    return route().endpoint\n"
        "@pytest.fixture(scope='module')\n"
        "def module_route():\n"
        "    return route().endpoint\n"
        "def test_private_fence(session_route, module_route):\n"
        "    assert (collected, session_route, module_route) == (None, None, None)\n"
    )
    config = tmp_path / "pytest.ini"
    config.write_text("[pytest]\n")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(probe),
            "-q",
            "--rootdir",
            str(tmp_path),
            "--confcutdir",
            str(tmp_path),
            "-c",
            str(config),
            "-p",
            "no:cacheprovider",
        ],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "TMPDIR": "/var/tmp"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

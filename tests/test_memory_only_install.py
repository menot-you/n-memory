"""A lexical client does not import the optional executor package."""

import os
import subprocess
import sys
from pathlib import Path


def test_cli_and_stdio_import_without_nspawn() -> None:
    script = """
import importlib.abc, sys
class RefuseSpawn(importlib.abc.MetaPathFinder):
 def find_spec(self, fullname, path=None, target=None):
  if fullname == "nspawn" or fullname.startswith("nspawn."):
   raise AssertionError("optional nspawn was imported")
sys.meta_path.insert(0, RefuseSpawn())
from nmemory.cli import main
from nmemory.server import MemoryServer
from nmemory.curator import CuratorClient
assert main(["--version"]) == 0
assert not CuratorClient().is_configured()
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_startup_guidance_has_no_personal_grant(tmp_path: Path) -> None:
    from workspace_root import workspace_path

    binary = tmp_path / "memory"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    result = subprocess.run(
        ["bash", str(workspace_path("plugin/hooks/nmemory-session.sh"))],
        input="{}",
        env={**os.environ, "NOTT_NMEMORY_BIN": str(binary)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Standing grant" not in result.stdout
    assert "plugin:nott:nmemory" not in result.stdout
    assert "nmemory_capsule_ingest" in result.stdout

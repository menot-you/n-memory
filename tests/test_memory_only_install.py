"""A lexical client does not import the optional executor package."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


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
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "NOTT_NMEMORY_CURATOR": "0"},
    )
    assert result.returncode == 0, result.stderr


# The note names the tools, not a server: the server name differs by client and install.
SERVER_LINE = (
    "Memory for this session is the nMEMORY MCP server, under the name that this "
    "install registers (tools nmemory_context_show, nmemory_capsule_ingest, "
    "nmemory_outcome_record, nmemory_session_start, nmemory_session_finish). If they "
    "arrive deferred, load them with ToolSearch by those names; do not guess other "
    "names."
)
FORBIDDEN = ("Standing grant", "2026-09-14", "plugin:nott:", "NOTT plugin")


@pytest.mark.parametrize("plugin_root", [False, True], ids=["no-root", "other-root"])
def test_startup_guidance_has_no_personal_grant(
    tmp_path: Path, plugin_root: bool
) -> None:
    from workspace_root import workspace_path

    binary = tmp_path / "memory"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    # An explicit environment: an inherited NOTT_NSPAWN_RUN_ID, NOTT_MEMORY_POLICY,
    # NOTT_SESSION_DIGEST or CLAUDE_PLUGIN_ROOT cannot decide the result.
    environment = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "PATH": os.environ["PATH"],
        "TMPDIR": str(tmp_path),
        "NOTT_NMEMORY_BIN": str(binary),
    }
    if plugin_root:
        other = tmp_path / "other-plugin"
        (other / ".claude-plugin").mkdir(parents=True)
        (other / ".claude-plugin/plugin.json").write_text('{"name": "other"}\n')
        environment["CLAUDE_PLUGIN_ROOT"] = str(other)
    result = subprocess.run(
        ["bash", str(workspace_path("plugin/hooks/nmemory-session.sh"))],
        input='{"session_id":"absent","source":"startup"}',
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for text in FORBIDDEN:
        assert text not in result.stdout, f"the note prints {text!r}"
    assert result.stdout.count(SERVER_LINE) == 1, result.stdout

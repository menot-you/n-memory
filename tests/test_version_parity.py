"""Tests for CLI surface: --version binding (FUGU F5) and the removed doors (D-DOOR-1)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import pytest
from child_env import child_env

from nmemory import __version__
from nmemory.database import SCHEMA_VERSION as CONTRACT_VERSION


def test_cli_version_matches_package_metadata() -> None:
    """python -m nmemory --version outputs package __version__ and CONTRACT_VERSION."""
    proc = subprocess.run(
        [sys.executable, "-m", "nmemory", "--version"],
        capture_output=True,
        text=True,
        env=child_env(),
        check=False,
    )
    assert proc.returncode == 0
    expected = f"nmemory {__version__} (schema v{CONTRACT_VERSION})"
    assert proc.stdout.strip() == expected


def test_daemon_module_is_gone() -> None:
    """D-DOOR-1 (owner, 2026-08-31): the daemon path was removed; nothing may restore it.

    The unix socket WORKED when it was removed — bind 0600 via UnixDoor with
    peer-cred, 24 tools, end-to-end ingest and readback, measured on 2026-08-31.
    It was removed due to non-use, not a defect, which is why the only guard against
    its return is this one.
    """
    assert importlib.util.find_spec("nmemory.daemon") is None


@pytest.mark.parametrize("verb", ["serve", "connect"])
def test_removed_door_verb_is_refused(verb: str) -> None:
    """`serve` and `connect` do not exist in the command table.

    THE ASSERTION THAT CARRIES THIS TEST IS the named refusal, never the exit code.
    `nmemory serve` exits 2 ON BOTH SIDES of the removal: before, via the guard
    "at least one door is required"; after, as an unknown command. A test that only
    looked at the returncode would pass against the unmodified tree and would
    not be a falsifier at all.

    Do not assert the absence of "serve" in --help: the parser description is
    "nMEMORY MCP server and CLI", and "serve" is a substring of "server" forever.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "nmemory", verb],
        capture_output=True,
        text=True,
        env=child_env(),
        check=False,
    )
    assert proc.returncode == 2
    assert f"unknown command: nmemory {verb}" in proc.stderr

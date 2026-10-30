"""pytest11 plugin: every test gets its own `NOTT_DATABASE_DIR`.

The directory is named, not created: `create()` makes it 0700 on first use, so a test
that never opens a store leaves nothing in it. Subprocesses inherit the variable.
The plugin also sets `OTEL_SDK_DISABLED=true` before collection; subprocesses
inherit this setting, and telemetry tests opt into their own collectors.
Each test and its children use an absent System1 file through NOTT_TEST_SYSTEM1_FILE.

It lives in a private parent with a short path, not under pytest's basetemp: a test may
bind a unix socket in it, and AF_UNIX refuses a path past 104 bytes (108 on Linux) — a
deep basetemp made the bind fail with the directory itself correct.

The parent is in `/var/tmp`, shared by native writers and the host user units
that their tests start. A writer's `/dev/shm` is private to each Bash call: a
host unit cannot open a store there. Keep the short, private parent even on
hosts where `/dev/shm` is writable.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from nott_database import DIRECTORY_VARIABLE


def pytest_configure(config: pytest.Config) -> None:
    """Disable OTLP before collection; tests may enable an owned collector."""
    resources = ExitStack()
    config.add_cleanup(resources.close)
    folder = resources.enter_context(private_directory())
    patch = resources.enter_context(pytest.MonkeyPatch.context())
    patch.setenv("OTEL_SDK_DISABLED", "true")
    patch.setenv("NOTT_TEST_SYSTEM1_FILE", str(folder.parent / "system1.env"))


@contextmanager
def private_directory() -> Iterator[Path]:
    """A named, not yet created directory in a fresh 0700 parent, removed on exit."""
    parent = Path(tempfile.mkdtemp(prefix="nd-", dir="/var/tmp"))
    try:
        yield parent / "d"
    finally:
        shutil.rmtree(parent)


@pytest.fixture(autouse=True)
def _nott_database_dir() -> Iterator[None]:
    # Its own MonkeyPatch, not the `monkeypatch` fixture: that one is torn down after
    # this, so a test that patched `os.lstat` would still have it patched under rmtree.
    with private_directory() as folder, pytest.MonkeyPatch.context() as patch:
        patch.setenv(DIRECTORY_VARIABLE, str(folder))
        patch.setenv("NOTT_TEST_SYSTEM1_FILE", str(folder.parent / "system1.env"))
        yield

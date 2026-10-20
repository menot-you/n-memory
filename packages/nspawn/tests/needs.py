"""`@pytest.mark.needs(*capabilities)` names the host a test runs on.

Registered by `tests/conftest.py`. Each capability is probed once per session. A
missing one skips the test with its name; `NOTT_TEST_REQUIRE=all` turns that skip
into a failure for every capability the host's platform owes, which is how a host
keeps a skipped measurement from reading as a clean one. A capability another
platform owes (a cgroup on a Mac) still skips, naming that. An unknown capability,
or an unknown `NOTT_TEST_REQUIRE` value, is a usage error: a typo must not become a
silent skip.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from nspawn.custody import ControllerRefusal, user_manager_environment

REQUIRE_VARIABLE = "NOTT_TEST_REQUIRE"
#: The platform whose capabilities `NOTT_TEST_REQUIRE=all` holds this host to.
PLATFORM = sys.platform


def _systemd_user() -> bool:
    try:
        user_manager_environment()
    except ControllerRefusal:
        return False
    return all(
        shutil.which(tool)
        for tool in ("systemctl", "busctl", "systemd-run", "loginctl")
    )


#: The closed set. Each name is what a test needs from the host, not a platform.
PROBES: dict[str, Callable[[], bool]] = {
    # the user manager: systemctl, busctl, systemd-run, loginctl over its bus
    "systemd_user": _systemd_user,
    # cgroup v2, read and written under /sys/fs/cgroup
    "cgroup": lambda: Path("/sys/fs/cgroup/cgroup.controllers").is_file(),
    # the executor fence: the absolute paths a native run is confined by
    "bwrap": lambda: all(
        os.access(tool, os.X_OK) for tool in ("/usr/bin/bwrap", "/usr/bin/prlimit")
    ),
    # a per-run launchd job, whose resource coalition holds the native client
    "launchd_coalition": lambda: os.access("/bin/launchctl", os.X_OK),
}

#: Which platforms owe each capability: only those make it a failure under `all`.
OWED: dict[str, frozenset[str]] = {
    "systemd_user": frozenset({"linux"}),
    "cgroup": frozenset({"linux"}),
    "bwrap": frozenset({"linux"}),
    "launchd_coalition": frozenset({"darwin"}),
}

_PRESENT = pytest.StashKey[dict[str, bool]]()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "needs(*capabilities): host capabilities the test runs on, from "
        f"{sorted(PROBES)}; missing ones skip, or fail under {REQUIRE_VARIABLE}=all",
    )
    require = os.environ.get(REQUIRE_VARIABLE, "")
    if require not in ("", "all"):
        raise pytest.UsageError(f"{REQUIRE_VARIABLE}={require!r}: only 'all' is known")
    config.stash[_PRESENT] = {}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        for marker in item.iter_markers("needs"):
            for name in marker.args:
                if name not in PROBES:
                    raise pytest.UsageError(
                        f"{item.nodeid}: unknown host capability {name!r}; "
                        f"known: {sorted(PROBES)}"
                    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    present = item.config.stash[_PRESENT]
    for marker in item.iter_markers("needs"):
        for name in marker.args:
            if name not in present:
                present[name] = PROBES[name]()
            if present[name]:
                continue
            reason = f"host capability missing: {name}"
            if PLATFORM not in OWED[name]:
                pytest.skip(f"{reason} (not owed on {PLATFORM})")
            if os.environ.get(REQUIRE_VARIABLE) == "all":
                pytest.fail(f"{reason} ({REQUIRE_VARIABLE}=all)", pytrace=False)
            pytest.skip(reason)

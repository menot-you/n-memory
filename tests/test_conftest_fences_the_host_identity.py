"""The suite, and every child it spawns, runs without the host's production identity.

A box that serves nMEMORY exports the live store's permission, keys, vault, scope and
session in its login shell, under the old spellings and the new ones. A test run from
that shell inherited all of them, and so did each `nmemory` child a test spawned: the
old spellings made the child refuse, and the new ones handed a disposable run the live
store's permission and keys. `conftest` removes both for the whole session.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

from child_env import child_env
from nott_system1 import SCOPES, route_variables
from nott_system1.renamed import RENAMED as SYSTEM1_RENAMED
from nott_system1.renamed import RETIRED as SYSTEM1_RETIRED

from nmemory.renamed import RENAMED

IDENTITY = (
    "NOTT_NMEMORY_HMAC_KEY",
    "NOTT_NMEMORY_HMAC_KEY_FILE",
    "NOTT_NMEMORY_PROJECT",
    "NOTT_NMEMORY_HARNESS",
    "NOTT_NMEMORY_MODEL",
    "NOTT_NMEMORY_SESSION_ID",
    *(name for scope in (None, *sorted(SCOPES)) for name in route_variables(scope)),
)
OLD = (
    RENAMED["NOTT_NMEMORY_HMAC_KEY"],
    RENAMED["NOTT_NMEMORY_SYSTEM1"],
    SYSTEM1_RENAMED["NOTT_SYSTEM1_API_KEY"],
    *SYSTEM1_RETIRED,
)


INNER = "test_the_suite_and_its_children_carry_no_host_identity"


def test_the_suite_and_its_children_carry_no_host_identity() -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, os; print(json.dumps(sorted(os.environ)))",
        ],
        capture_output=True,
        check=True,
        text=True,
    )
    for names in (set(os.environ), set(json.loads(child.stdout))):
        assert not names & {*IDENTITY, *OLD}, sorted(names & {*IDENTITY, *OLD})


def test_the_fence_removes_an_identity_that_is_in_the_environment() -> None:
    """Bites in any environment: the suite runs in a subprocess with every name set."""
    env = child_env(
        {
            **os.environ,
            **{name: "host-identity-sentinel" for name in (*IDENTITY, *OLD)},
        }
    )
    # From the member root, where mutmut's copy keeps its configuration: a mutated
    # module loads it from the working directory, and `tests/` holds none.
    member = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:randomly",
            "-p",
            "no:cacheprovider",
            f"tests/{pathlib.Path(__file__).name}::{INNER}",
        ],
        cwd=member,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, (
        "the conftest fence let the host's identity through:\n"
        f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    )

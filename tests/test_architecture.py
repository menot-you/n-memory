"""The member's architecture, measured on the unmutated tree.

import-linter reads `[tool.importlinter]` from this member's `pyproject.toml`: the
modules of `nmemory` form no import cycle, and the domain never reaches
persistence or a subprocess. It puts its working directory first on `sys.path`,
so it reads the package in that directory. `--no-cache` keeps it from writing
its cache into the member root.

vulture reads `[tool.vulture]` from this member's `pyproject.toml`: `nmemory` and
the product code of every consumer that imports it, without tests, so a name only
tests use is reported dead. The consumers are usage sources only; their own
findings belong to their members. vulture prints member paths relative to its
working directory and consumer paths absolute, so a finding under `nmemory/` is
one of ours.

The working directory is the tree found from the workspace root, never this
file's own: under mutmut this file runs from `mutants/`, and both tools would then
read the mutated copy; vulture would report mutmut's own trampolines.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from workspace_root import workspace_path

MEMBER = workspace_path("mcps/nmemory")
# The venv's own console script, next to the interpreter running the suite.
LINT_IMPORTS = Path(sys.executable).parent / "lint-imports"

# vulture's exit codes: 0 no dead code, 1 invalid input (a configured path that
# does not exist, a file it could not parse), 2 invalid arguments, 3 dead code found.
_VULTURE_COMPLETED = {0, 3}
_VULTURE_FAILED = {
    1: "invalid input: a configured path is missing or a file could not be read",
    2: "invalid arguments: the [tool.vulture] configuration was refused",
}


def test_vulture_reports_no_dead_code_in_the_member() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "vulture"],
        cwd=MEMBER,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode in _VULTURE_COMPLETED, (
        f"vulture exited {proc.returncode} "
        f"({_VULTURE_FAILED.get(proc.returncode, 'unknown exit code')}): "
        f"{proc.stderr or proc.stdout}"
    )
    # vulture names on stderr each file it could not parse, and still exits 3 when
    # it also reports findings: an unread file would hide its own dead code.
    assert proc.stderr == "", proc.stderr
    findings = [
        line for line in proc.stdout.splitlines() if line.startswith("nmemory/")
    ]
    assert findings == [], "\n".join(findings)


def test_import_linter_contracts_are_kept() -> None:
    proc = subprocess.run(
        [str(LINT_IMPORTS), "--no-cache"],
        cwd=MEMBER,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    # lint-imports exits 0 when every contract is kept and 1 when one is broken or
    # the configuration could not be read; its report names the contract and the
    # imports that break it.
    assert proc.returncode == 0, f"{proc.stdout}{proc.stderr}"

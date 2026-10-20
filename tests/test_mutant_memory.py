"""A mutmut worker for this member runs in a bounded address space.

mutmut bounds a mutant's CPU time and nothing else. A mutant that turns a bounded loop
into an unbounded allocation grows until something outside stops it: on SERVER, workers
of #832's full run reached 12 GiB each and the run was killed for the whole host's memory.
The bound lives in this member's conftest, so every runner reaches it, and the one that
remains is a `mutmut run` started by hand (owner D5).
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
from pathlib import Path

import pytest

MEMBER = Path(__file__).resolve().parents[1]
GIB = 2**30
BOUND = 6 * GIB
INFINITY = resource.RLIM_INFINITY

# mutmut's own sequence, in one fresh interpreter: its parent runs pytest.main first (the
# clean run, MUTANT_UNDER_TEST empty) and so imports this conftest; then it forks, the
# worker names its mutant and runs pytest.main again. Each prints its limits after.
_MUTMUT = """
import json, os, resource, sys
import pytest

soft, hard, name = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
resource.setrlimit(resource.RLIMIT_AS, (soft, hard))
args = ["--collect-only", "-q", "-p", "no:cacheprovider", "tests/test_mutant_memory.py"]

def report(where, rc):
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    print("LIMITS " + json.dumps({"where": where, "rc": int(rc), "soft": soft, "hard": hard}), flush=True)

report("start", 0)
os.environ["MUTANT_UNDER_TEST"] = ""
report("parent", pytest.main(args))
pid = os.fork()
if pid == 0:
    os.environ["MUTANT_UNDER_TEST"] = name
    report("worker", pytest.main(args))
    os._exit(0)
_, status = os.waitpid(pid, 0)
sys.exit(os.waitstatus_to_exitcode(status))
"""


def _run(start: tuple[int, int], name: str) -> dict[str, dict[str, int]]:
    done = subprocess.run(
        [sys.executable, "-c", _MUTMUT, str(start[0]), str(start[1]), name],
        cwd=MEMBER,
        env={k: v for k, v in os.environ.items() if k != "MUTANT_UNDER_TEST"},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    seen = [
        json.loads(line[7:])
        for line in done.stdout.splitlines()
        if line.startswith("LIMITS ")
    ]
    by = {row["where"]: row for row in seen}
    assert set(by) == {"start", "parent", "worker"}, done.stdout + done.stderr
    assert by["parent"]["rc"] == 0 and by["worker"]["rc"] == 0, (
        done.stdout + done.stderr
    )
    assert (by["start"]["soft"], by["start"]["hard"]) == start, (start, by)
    return by


def _starts() -> tuple[tuple[int, int], ...]:
    # Every starting (soft, hard) pair is set by the child itself, never inherited. An
    # unlimited or 8 GiB soft limit must come down to the bound, 6 GiB and 4 GiB must stay,
    # and a finite hard limit must survive untouched: a process may lower its own hard limit
    # but never raise it, so a guard that asked for more would fail every worker.
    if resource.getrlimit(resource.RLIMIT_AS)[1] != INFINITY:
        pytest.skip("a hard RLIMIT_AS is inherited: the unlimited start cannot be set")
    return (
        (INFINITY, INFINITY),
        (8 * GIB, INFINITY),
        (BOUND + 1, INFINITY),
        (BOUND, INFINITY),
        (4 * GIB, INFINITY),
        (8 * GIB, 8 * GIB),
        (4 * GIB, 4 * GIB),
    )


def _bounded(soft: int) -> int:
    return BOUND if soft == INFINITY or soft > BOUND else soft


def test_every_mutant_worker_is_bounded_and_keeps_its_hard_limit() -> None:
    for start in _starts():
        by = _run(start, "nmemory.store.x_append__mutmut_1")
        assert by["parent"]["soft"] == start[0], (start, by)
        assert by["worker"]["soft"] == _bounded(start[0]), (start, by)
        assert by["worker"]["hard"] == start[1], (start, by)
    # Any mutant's name, not the one above: mutmut names every mutant `<function>__mutmut_<n>`.
    other = _run((INFINITY, INFINITY), "nmemory.capsule.x_headline_of__mutmut_42")
    assert other["worker"]["soft"] == BOUND, other


def test_mutmut_s_own_phases_leave_the_limit_as_it_was() -> None:
    # The clean run, the stats run and the forced failure run the whole suite in mutmut's
    # parent, which every worker is forked from: bounding them would bound the parent. From
    # an unlimited start any bound applied shows.
    _starts()
    for phase in ("", "stats", "fail", "list_all_tests", "mutant_generation"):
        by = _run((INFINITY, INFINITY), phase)
        assert (by["worker"]["soft"], by["worker"]["hard"]) == (INFINITY, INFINITY), (
            phase,
            by,
        )

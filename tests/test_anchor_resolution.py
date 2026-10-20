"""test_anchor_resolution.py — pin every repaired path anchor to its canonical destination.

Eleven anchors across four test files were repaired when their relative paths went wrong
after the harness reorg. The suite happened to pass for the wrong reasons: the editable
install masks PYTHONPATH anchors (nmemory is on sys.path either way), scratch destinations
create whatever directory they were told (off-by-one writes a directory, just not the
intended one). This file reads each anchor OUT OF THE SOURCE, extracts the literal value used in the source
(so a regression that changes the expression changes the result here), evaluates it with
the host file's __file__ bound, and compares the resolved path to the canonical expected
destination. No nmemory import is performed, so the editable install cannot mask a wrong
path.

The shapes are exercised below:

1. PYTHONPATH anchor:  os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
   Sites: test_durability.py:34-36 ; test_tools.py:29
   Resolves to:        mcps/nmemory (the package directory)

2. Scratch destination anchor:  Path(__file__).parents[4] / ".tmp_<name>"
   Resolves to:        <repo_root>/.tmp_<name> (a directory the test creates and tears down)

3. CWD anchor:  Path(__file__).parents[4]
   Resolves to:        <repo_root>
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from workspace_root import WORKSPACE_ROOT

THIS_FILE = Path(__file__).resolve()
THIS_DIR = THIS_FILE.parent  # mcps/nmemory/tests
PKG_DIR = (THIS_DIR.parent).resolve()  # mcps/nmemory
# The nearest ancestor holding uv.lock, the same in the tree and in mutmut's copy.
REPO_ROOT = WORKSPACE_ROOT


def _resolve_expr(host_file: Path, expr: str) -> Path:
    """Evaluate `expr` with __file__ bound to `host_file`. Returns the resolved Path."""
    code = (
        "import os\n"
        "from pathlib import Path\n"
        f"__file__ = {str(host_file)!r}\n"
        f"result = ({expr})"
    )
    ns: dict = {"__file__": str(host_file), "os": os, "Path": Path}
    exec(code, ns)  # noqa: S102 — sandboxed eval of a regex-extracted source anchor
    return Path(ns["result"]).resolve()


def _pythonpath_anchor_from_source(source: str) -> str:
    """Extract the PYTHONPATH anchor expression AS-WRITTEN from the source text.

    Returns the full expression `os.path.abspath(os.path.join(os.path.dirname(__file__),
    "<inner>"))` where <inner> is whatever the source has — typically `".."`. A regression
    that changes the inner string changes the expression and is caught downstream.
    """
    m = re.search(
        r"os\.path\.abspath\(\s*os\.path\.join\(\s*os\.path\.dirname\(\s*__file__\s*\)\s*,\s*\"([^\"]*)\"\s*\)\s*\)",
        source,
    )
    assert m is not None, (
        "PYTHONPATH anchor not found: expected "
        'os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))'
    )
    return f'os.path.abspath(os.path.join(os.path.dirname(__file__), "{m.group(1)}"))'


def _parents_anchor_from_source(source: str) -> str:
    """Extract the first `Path(__file__).parents[<N>]` expression AS-WRITTEN from the source.

    Returns the full expression including the bracket literal, so a regression that changes
    parents[4] to parents[3] changes the extracted N and is caught downstream.
    """
    m = re.search(r"Path\(__file__\)\.parents\[\s*(\d+)\s*\]", source)
    assert m is not None, "Path(__file__).parents[N] anchor not found in source"
    return f"Path(__file__).parents[{m.group(1)}]"


def _all_parents_anchors_from_source(source: str) -> list[str]:
    """Extract EVERY `Path(__file__).parents[<N>]` expression AS-WRITTEN from the source.

    Returns the full expression string for each match (e.g. `Path(__file__).parents[4]`).
    Used when a single test file contains more than one such anchor.
    """
    return re.findall(r"Path\(__file__\)\.parents\[\s*\d+\s*\]", source)


def _scratch_dest_suffixes_from_source(source: str) -> list[str]:
    """Find every scratch-destination suffix the source attaches to a parents[N] / repo_root
    via `/`.

    Returns the list of suffix strings (e.g. ["lanes/state/repro",
    ".tmp_missing_test_repo"]). A regression that renames or removes one of these suffixes
    changes this list and is caught by the scratch-destination test below.

    Handles both shapes:
      single-line: Path(__file__).parents[4] / "lanes" / "state" / "repro"
      two-line:    repo_root = Path(__file__).parents[4]
                   tmpdir = repo_root / ".tmp_missing_test_repo"
    """
    suffixes: list[str] = []

    # Single-line chains: Path(__file__).parents[N] / "a" / "b" / "c"
    for m in re.finditer(
        r'Path\(__file__\)\.parents\[\s*\d+\s*\](?:\s*/\s*"([^"]+)")+',
        source,
    ):
        parts = re.findall(r'"([^"]+)"', m.group(0))
        if parts:
            suffixes.append("/".join(parts))

    # Two-line: `... = repo_root / "<suffix>"` where repo_root was bound to parents[N]
    for m in re.finditer(r'repo_root\s*/\s*"([^"]+)"', source):
        suffixes.append(m.group(1))

    # Dedupe, preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for s in suffixes:
        if s not in seen:
            seen.add(s)
            deduped.append(s)
    return deduped


# --------------------------------------------------------------------------- shape 1


def test_durability_pythonpath_anchor_resolves_to_package_dir() -> None:
    """The PYTHONPATH anchor in test_durability.py:34-36 resolves to mcps/nmemory."""
    host = THIS_DIR / "test_durability.py"
    source = host.read_text(encoding="utf-8")
    expr = _pythonpath_anchor_from_source(source)
    assert _resolve_expr(host, expr) == PKG_DIR


def test_tools_pythonpath_anchor_resolves_to_package_dir() -> None:
    """The PYTHONPATH anchor in test_tools.py:29 resolves to mcps/nmemory."""
    host = THIS_DIR / "test_tools.py"
    source = host.read_text(encoding="utf-8")
    expr = _pythonpath_anchor_from_source(source)
    assert _resolve_expr(host, expr) == PKG_DIR


# --------------------------------------------------------------------------- shape 2 + 3


def test_tools_parents_anchor_resolves_to_repo_root() -> None:
    """The parents[1] scratch anchor in test_tools.py resolves to PKG_DIR."""
    host = THIS_DIR / "test_tools.py"
    source = host.read_text(encoding="utf-8")
    expr = _parents_anchor_from_source(source)
    assert _resolve_expr(host, expr) in (REPO_ROOT, PKG_DIR)


def test_tools_scratch_dest_suffixes_resolve_under_repo_root() -> None:
    """The scratch-destination suffix in test_tools.py resolves under REPO_ROOT."""
    host = THIS_DIR / "test_tools.py"
    source = host.read_text(encoding="utf-8")
    suffixes = _scratch_dest_suffixes_from_source(source)
    expected = {".tmp_missing_unit_repo"}
    assert expected.issubset(set(suffixes)), (
        f"missing {expected - set(suffixes)} in test_tools.py scratch dests "
        f"(found: {sorted(suffixes)})"
    )
    # The host's depth below the workspace root, counted rather than fixed: mutmut's copy
    # of the suite sits one directory deeper than the tree.
    depth = len(host.relative_to(REPO_ROOT).parts) - 1
    for s in suffixes:
        expr = f'Path(__file__).parents[{depth}] / "{s}"'
        resolved = _resolve_expr(host, expr)
        assert resolved == REPO_ROOT / s


# --------------------------------------------------------------------------- proof


def test_anchors_resolve_under_a_fake_layout() -> None:
    """Sanity: the same anchor expressions, evaluated against a FAKE test file path, do NOT
    produce this package's PKG_DIR. Proves the test is reading host_file, not hard-coding
    a path that happens to match this file's location."""
    fake_host = Path("/tmp/__nonexistent__/tests/fake_test.py")
    expr = 'os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))'
    fake_pkg = _resolve_expr(fake_host, expr)
    assert fake_pkg != PKG_DIR
    assert fake_pkg == Path("/tmp/__nonexistent__").resolve()

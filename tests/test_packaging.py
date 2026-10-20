"""The release wheel carries every package of `nmemory`, and nothing beside it.

A release is installed with `uv pip install --no-editable` (tools/install-mcp.sh),
so it holds only what the build backend packages. Under setuptools,
`packages = ["nmemory"]` named the top package alone and the release shipped
without `nmemory/commands`: its `import nmemory.cli` failed. The member now builds
with `uv_build`, which ships the module named after the distribution with every
file inside it. These tests build the real wheel with that backend and read it:
every package directory of the source tree and the tool catalogue are in it, and
a `mutants/` tree left next to `nmemory/` (mutmut's output, ignored by git) is not.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from workspace_root import workspace_path

MEMBER = workspace_path("mcps/nmemory")


def _build_wheel(source: Path, out: Path) -> list[str]:
    """Build the wheel of `source` offline into `out` and list its entries."""
    uv = shutil.which("uv")
    assert uv is not None, "uv is the workspace's build front end and is not on PATH"
    proc = subprocess.run(
        [uv, "build", "--wheel", "--offline", "--no-sources", "--out-dir", str(out)],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    [wheel] = sorted(out.glob("nmemory-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def _source_packages() -> list[str]:
    root = MEMBER / "nmemory"
    return sorted(
        init.parent.relative_to(MEMBER).as_posix()
        for init in root.rglob("__init__.py")
        if "__pycache__" not in init.parts
    )


def _top_level(entries: list[str]) -> set[str]:
    tops = {entry.split("/", 1)[0] for entry in entries}
    return {top for top in tops if not top.endswith(".dist-info")}


def test_every_nmemory_package_is_in_the_release(tmp_path: Path) -> None:
    entries = _build_wheel(MEMBER, tmp_path)
    packages = _source_packages()
    assert "nmemory" in packages and "nmemory/commands" in packages
    missing = [name for name in packages if f"{name}/__init__.py" not in entries]
    assert missing == [], f"packages the wheel leaves out: {missing}"
    assert "nmemory/tools_schema.json" in entries, (
        "without the catalogue `tools/list` answers zero tools outside the tree"
    )
    assert _top_level(entries) == {"nmemory"}, entries
    assert not [entry for entry in entries if "__pycache__" in entry], entries


def test_importing_nmemory_leaves_sys_path_alone(tmp_path: Path) -> None:
    """An installed release imports `nmemory` from its own environment.

    The package once walked up to a checkout's `.venv` and put its site-packages
    first on `sys.path`: an import from the source tree then ran against whatever
    that checkout had installed, and an installed release carried code that only
    means something inside the repository. The package is copied four levels
    below a disposable root and every ancestor up to that root carries a
    `.venv/lib/python3/site-packages`, so a walk up any of those levels finds one.
    The interpreter runs isolated and without `site`, so nothing but the copy's
    parent is importable, and importing the package must add no entry of its own.
    """
    parent = tmp_path / "one" / "two" / "three"
    shutil.copytree(
        MEMBER / "nmemory",
        parent / "nmemory",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for level in (tmp_path, parent.parent.parent, parent.parent, parent):
        (level / ".venv" / "lib" / "python3" / "site-packages").mkdir(parents=True)
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(parent)!r})\n"
        "before = list(sys.path)\n"
        "import nmemory\n"
        "print(json.dumps([p for p in sys.path if p not in before]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [], proc.stdout


def test_the_release_never_ships_the_mutation_tree(tmp_path: Path) -> None:
    source = tmp_path / "member"
    source.mkdir()
    shutil.copytree(
        MEMBER / "nmemory",
        source / "nmemory",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        if (MEMBER / name).is_file():
            shutil.copy2(MEMBER / name, source / name)
    mutants = source / "mutants" / "nmemory"
    mutants.mkdir(parents=True)
    (mutants / "__init__.py").write_text("")
    entries = _build_wheel(source, tmp_path / "out")
    assert "nmemory/__init__.py" in entries, entries
    assert _top_level(entries) == {"nmemory"}, entries
    assert not [entry for entry in entries if "mutants" in entry], entries

"""The workspace root, found the same way from the tree and from mutmut's copy.

mutmut runs a member's suite from `<member>/mutants/`, one directory deeper than the
tree, so a fixed `parents[N]` names the wrong directory there. The workspace lockfile
marks the root: members carry none, and mutmut copies only the member's own.
"""

from pathlib import Path


def workspace_root(start: Path) -> Path:
    """The nearest ancestor of `start` holding `uv.lock`; `FileNotFoundError` if none."""
    here = start.resolve()
    for directory in here.parents:
        if (directory / "uv.lock").is_file():
            return directory
    raise FileNotFoundError(f"no ancestor of {here} carries the workspace uv.lock")


WORKSPACE_ROOT = workspace_root(Path(__file__))


def workspace_path(relative: str) -> Path:
    """Resolve a source path in the private or public test workspace."""
    import json

    manifest = WORKSPACE_ROOT / "source-manifest.json"
    if not manifest.is_file():
        return WORKSPACE_ROOT / relative
    component = json.loads(manifest.read_text())["component"]
    contract = json.loads((WORKSPACE_ROOT / "ops/mirror/projects.json").read_text())
    entry = contract["components"][component]
    mapping = {entry["primary"]: ".", **entry["packages"]}
    if component == "nmemory":
        mapping["plugin/hooks"] = "nmemory/hooks"
    for origin in sorted(mapping, key=len, reverse=True):
        if relative == origin or relative.startswith(origin + "/"):
            return WORKSPACE_ROOT / mapping[origin] / Path(relative).relative_to(origin)
    return WORKSPACE_ROOT / relative

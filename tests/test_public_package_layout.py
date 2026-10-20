"""Check tool paths in producer metadata and public package metadata."""

import json
import tomllib
from pathlib import Path

import pytest

from nmemory.commands import layout


@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("missing", [False, True])
def test_metadata_keeps_only_selected_tool_paths(
    tmp_path: Path, public: bool, missing: bool
) -> None:
    root, stage = tmp_path / "root", tmp_path / "stage"
    root.mkdir()
    (root / "LICENSE").write_text("Test license.\n")
    if public:
        (root / "source-manifest.json").write_text('{"component": "plugin"}')
    mapping = {"mcps/nmemory": "packages/nmemory", "evals/nmemory": "evals"}
    for module, target in (("nmemory", "packages/nmemory"), ("nmemory_evals", "evals")):
        (stage / target / module).mkdir(parents=True)
        (stage / target / "pyproject.toml").write_text(
            f'[project]\nname = "{module.replace("_", "-")}"\nversion = "1.0.0"\n'
        )
    path = stage / "packages/nmemory/pyproject.toml"
    value = (
        "../../evals/nmemory_evals" if public else "../../evals/nmemory/nmemory_evals"
    )
    if missing:
        value = "../../outside"
    with path.open("a") as stream:
        stream.write("\n[tool.vulture]\npaths = " + json.dumps([value]) + "\n")
    if missing:
        with pytest.raises(ValueError, match="unselected source"):
            layout.metadata(root, stage, mapping)
    else:
        layout.metadata(root, stage, mapping)
        assert tomllib.loads(path.read_text())["tool"]["vulture"]["paths"] == [
            "../../evals/nmemory_evals"
        ]

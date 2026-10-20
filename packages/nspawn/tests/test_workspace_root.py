"""Resolve test resources from producer and public workspaces."""

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

_spec = importlib.util.spec_from_file_location(
    "nspawn_test_workspace_root", Path(__file__).with_name("workspace_root.py")
)
assert _spec is not None and _spec.loader is not None
location = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(location)


def test_resolver_import_is_member_local(monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = ModuleType("workspace_root")
    foreign.__file__ = "/synthetic/other/tests/workspace_root.py"
    monkeypatch.setitem(sys.modules, "workspace_root", foreign)
    namespace = runpy.run_path(__file__)
    assert Path(namespace["location"].__file__) == Path(__file__).with_name(
        "workspace_root.py"
    )


@pytest.mark.parametrize("component", ["nspawn", "nmemory", "plugin"])
def test_public_resource_location(tmp_path: Path, monkeypatch, component: str) -> None:
    contract = json.loads(
        location.workspace_path("ops/mirror/projects.json").read_text()
    )
    root = tmp_path / component
    root.mkdir()
    (root / "source-manifest.json").write_text(
        json.dumps({"schema": 1, "component": component})
    )
    (root / "ops/mirror").mkdir(parents=True)
    (root / "ops/mirror/projects.json").write_text(json.dumps(contract))
    member = "." if component == "nspawn" else "packages/nspawn"
    emitted = root / member / "tests/resources/templates/nott/spawn.toml"
    emitted.parent.mkdir(parents=True)
    emitted.write_text("# Synthetic resource\n")
    monkeypatch.setattr(location, "WORKSPACE_ROOT", root)
    assert location.workspace_path("templates/nott/spawn.toml") == emitted
    assert location.workspace_path("templates/nott/spawn.toml").is_file()
    assert (
        location.workspace_path("mcps/nspawn/nspawn/schema.py")
        == root / member / "nspawn/schema.py"
    )
    assert (
        location.workspace_path("libs/database/nott_database/__init__.py")
        == root / "packages/database/nott_database/__init__.py"
    )


def test_producer_resource_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert location.workspace_path("templates/nott/spawn.toml").is_file()
    emitted = tmp_path / "templates/nott/spawn.toml"
    emitted.parent.mkdir(parents=True)
    emitted.write_text("# Synthetic resource\n")
    monkeypatch.setattr(location, "WORKSPACE_ROOT", tmp_path)
    path = location.workspace_path("templates/nott/spawn.toml")
    assert path == emitted
    assert path.is_file()


@pytest.mark.parametrize(
    ("marker", "error"),
    [
        ("{", json.JSONDecodeError),
        ("{}", KeyError),
        ('{"component": "unknown"}', KeyError),
    ],
)
def test_invalid_public_marker_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    marker: str,
    error: type[Exception],
) -> None:
    contract = location.workspace_path("ops/mirror/projects.json").read_text()
    (tmp_path / "source-manifest.json").write_text(marker)
    (tmp_path / "ops/mirror").mkdir(parents=True)
    (tmp_path / "ops/mirror/projects.json").write_text(contract)
    monkeypatch.setattr(location, "WORKSPACE_ROOT", tmp_path)
    with pytest.raises(error):
        location.workspace_path("templates/nott/spawn.toml")


def test_roster_is_a_test_resource() -> None:
    """The versioned roster remains outside the runtime package."""
    path = location.workspace_path("templates/nott/spawn.toml")
    assert "nspawn/nspawn" not in path.as_posix()

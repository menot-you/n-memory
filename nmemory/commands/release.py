"""Export a standalone workspace from an explicit source directory."""

from __future__ import annotations

import importlib.util
import json
import re
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

from .. import NMemoryError
from .common import parse_command

EVAL_TESTS = (
    "conftest.py",
    "workspace_root.py",
    "test_determinism.py",
    "test_metrics.py",
    "test_driver.py",
    "test_abstention.py",
)
EXCLUDE = {
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".hypothesis",
    ".git",
    ".nott",
}
# This file tests the private native evaluation runner, outside this distribution.
NONPORTABLE = {"test_request_recall_hook.py"}


class ExportError(NMemoryError):
    """The source or destination does not meet the export contract."""


def _source(root: Path, relative: Path) -> Path:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        raise ExportError(f"release export: missing source: {relative}") from None
    if not resolved.is_relative_to(root):
        raise ExportError(f"release export: source link escapes the root: {relative}")
    if any(part.is_symlink() for part in (path, *path.parents) if part != root):
        raise ExportError(f"release export: source links are unsupported: {relative}")
    return path


def _copy(root: Path, relative: Path, stage: Path, target: Path | None = None) -> None:
    path = _source(root, relative)
    destination = stage / (relative if target is None else target)
    metadata = path.stat()
    if stat.S_ISDIR(metadata.st_mode):
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted(path.iterdir()):
            if child.name in EXCLUDE or child.suffix == ".pyc":
                continue
            _copy(
                root,
                relative / child.name,
                stage,
                destination.relative_to(stage) / child.name,
            )
    elif stat.S_ISREG(metadata.st_mode):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        destination.chmod(0o755 if metadata.st_mode & 0o111 else 0o644)
    else:
        raise ExportError(f"release export: unsupported source file: {relative}")


def _metadata(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text())
    except ValueError, UnicodeError:
        raise ExportError("release export: invalid package metadata") from None


def _layout(root: Path) -> ModuleType:
    path = _source(
        root,
        Path("mcps/nmemory/nmemory/commands/layout.py")
        if not (root / "source-manifest.json").is_file()
        else Path("nmemory/commands/layout.py"),
    )
    spec = importlib.util.spec_from_file_location("public_layout", path)
    if spec is None or spec.loader is None:
        raise ExportError("release export: layout helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _docs(root: Path, stage: Path, component: str = "nmemory") -> None:
    layout = _layout(root)
    try:
        layout.documents(root, stage, layout.read(root), component, _copy)
    except ValueError as error:
        raise ExportError(str(error)) from None


def _links(stage: Path, selected: ModuleType | None = None) -> None:
    from . import layout

    policy = layout if selected is None else selected
    missing = policy.local_links(stage)
    if missing:
        raise ExportError(
            "release export: unresolved local link in " + json.dumps(missing)
        )


def _projection(root: Path) -> ModuleType:
    path = _source(
        root,
        Path("nmemory/commands/projection.py")
        if (root / "source-manifest.json").is_file()
        else Path("mcps/nmemory/nmemory/commands/projection.py"),
    )
    spec = importlib.util.spec_from_file_location("memory_public_projection", path)
    if spec is None or spec.loader is None:
        raise ExportError("release export: public projection policy is unavailable")
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    return policy


def _members(root: Path, stage: Path, component: str = "nmemory") -> dict[str, str]:
    layout = _layout(root)
    try:
        mapping: dict[str, str] = layout.paths(layout.read(root), component)
    except (KeyError, ValueError) as error:
        raise ExportError("release export: invalid component layout") from error
    public = (root / "source-manifest.json").is_file()
    for member, target in mapping.items():
        base = Path(target if public else member)
        metadata = _metadata(_source(root, base / "pyproject.toml"))
        module = metadata["project"]["name"].replace("-", "_")
        for name in ("pyproject.toml", "README.md"):
            _copy(root, base / name, stage, Path(target) / name)
        if module == "nmemory_evals":
            package = _source(root, base / module)
            for path in sorted(package.rglob("*")):
                if path.suffix in {".py", ".typed"} and not any(
                    part in EXCLUDE for part in path.relative_to(package).parts
                ):
                    _copy(
                        root,
                        path.relative_to(root),
                        stage,
                        Path(target) / path.relative_to(root / base),
                    )
            for name in EVAL_TESTS:
                _copy(root, base / "tests" / name, stage, Path(target) / "tests" / name)
        else:
            _copy(root, base / module, stage, Path(target) / module)
            tests = _source(root, base / "tests")
            for child in sorted(tests.iterdir()):
                if child.name not in EXCLUDE | NONPORTABLE:
                    _copy(
                        root,
                        child.relative_to(root),
                        stage,
                        Path(target) / "tests" / child.name,
                    )
        if member == "libs/netdoor":
            _copy(
                root,
                base / "http-door-auth.tsv",
                stage,
                Path(target) / "http-door-auth.tsv",
            )
        if member == "mcps/nbrowser":
            for resource in ("npm", "python"):
                _copy(root, base / resource, stage, Path(target) / resource)
    if component == "nmemory":
        layout.hooks(
            root,
            stage,
            Path("nmemory/hooks") if public else Path("plugin/hooks"),
            Path("nmemory/hooks"),
            _copy,
        )
    try:
        layout.resources(root, stage, layout.read(root), mapping, _copy)
    except ValueError as error:
        raise ExportError(str(error)) from None
    _copy(root, Path("ops/mirror/projects.json"), stage)
    return mapping


def export(
    source_root: Path, output: Path, component: str = "nmemory"
) -> dict[str, Any]:
    """Create one standalone source tree. Refuse an existing destination."""
    try:
        root = source_root.resolve(strict=True)
    except FileNotFoundError:
        raise ExportError("release export: missing source root") from None
    if not root.is_dir():
        raise ExportError("release export: source root must be a directory")
    if output.exists() or output.is_symlink():
        raise ExportError("release export: output already exists")
    output = output.resolve()
    if (
        output == root
        or root.is_relative_to(output)
        or (
            output.is_relative_to(root) and output.relative_to(root).parts[0] != ".nott"
        )
    ):
        raise ExportError("release export: output overlaps selected source paths")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".nmemory-export-", dir=output.parent
    ) as temporary:
        stage = Path(temporary) / "source"
        stage.mkdir()
        _copy(root, Path("LICENSE"), stage)
        _copy(root, Path(".python-version"), stage)
        policy = _projection(root)
        mapping = _members(root, stage, component)
        if (root / "uv.lock").exists():
            _copy(root, Path("uv.lock"), stage)
        _docs(root, stage, component)
        layout = _layout(root)
        try:
            policy.public_contributing(
                stage,
                "uv run --group dev pytest tests -q",
            )
            policy.public_runtime(stage, mapping)
            policy.public_wire_fixtures(stage, mapping)
            policy.commentary(stage)
            packages = layout.metadata(root, stage, mapping)
            policy.format_source(stage)
            _links(stage, layout)
        except (policy.Refusal, policy.Finding, ValueError) as error:
            raise ExportError(str(error)) from None
        findings = policy.scan(stage)
        if findings:
            raise ExportError(
                "release export: public source scan refused: " + json.dumps(findings)
            )
        try:
            dev = _metadata(_source(root, Path("pyproject.toml")))["dependency-groups"][
                "dev"
            ]
        except KeyError:
            raise ExportError("release export: incomplete workspace metadata") from None
        metadata_path = stage / "pyproject.toml"
        text = metadata_path.read_text()
        if "[tool.uv.workspace]" not in text:
            text += (
                "\n[tool.uv.workspace]\nmembers = "
                + json.dumps([value for value in mapping.values() if value != "."])
                + "\n"
            )
        if "[dependency-groups]" in text:
            # Keep the member's tools and add the root's verification tools.
            member_dev = (
                _metadata(metadata_path).get("dependency-groups", {}).get("dev", [])
            )
            text = re.sub(
                r"(?ms)(\[dependency-groups\]\s*dev\s*=\s*)\[.*?\]",
                lambda match: (
                    match[1] + json.dumps(list(dict.fromkeys([*member_dev, *dev])))
                ),
                text,
                count=1,
            )
        else:
            text += "\n[dependency-groups]\ndev = " + json.dumps(dev) + "\n"
        metadata_path.write_text(text)
        result = subprocess.run(
            ["uv", "lock", "--offline", "--directory", str(stage)],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        if result.returncode:
            # Keep the complete preparation and diagnostics for the integrator.
            stage.rename(output)
            (output / "lock-diagnostic.txt").write_text(
                layout.diagnostic(result.stdout + result.stderr)
            )
            raise ExportError(
                "release export: offline lock generation failed. Supply the uv resolution cache. The output directory contains the preparation and diagnostics."
            )
        manifest = {
            "schema": 1,
            "target": layout.read(root)["repositories"][component],
            "component": component,
            "primary_module": component,
            "license": layout.LICENSE,
            "members": [value for value in mapping.values() if value != "."],
            "packages": packages,
            "excluded_tests": {
                name: "Requires the private native evaluation runner."
                for name in sorted(NONPORTABLE)
            },
            "files": layout.inventory(stage),
        }
        (stage / "source-manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
        findings = policy.scan(stage)
        if findings:
            raise ExportError(
                "release export: public source scan refused: " + json.dumps(findings)
            )
        # Keep publication outside this command. The owning publisher approves it.
        stage.rename(output)
    return manifest


def cmd_release_export(argv: list[str]) -> None:
    args = parse_command("release", "export", argv)
    print(
        json.dumps(
            export(args.source_root, args.output, component=args.component),
            sort_keys=True,
        )
    )
